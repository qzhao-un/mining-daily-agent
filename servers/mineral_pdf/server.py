"""mineral-pdf-mcp：NI 43-101 储量报告抽取 MCP Server。

题目要求的工具：
    - extract_resources(pdf_url)  抽取 Indicated / Inferred 储量

设计要点：
    1. 抽取链路刻意显式分层，便于面试时讲清楚：
       PDF → 文本/表格 → 定位储量小节 → 逐行解析 → 结构化 MineralResource
    2. 关键原则：抽不准就不抽，不硬填数字。
       每条记录带 confidence 字段，抽取不确定时标 low，
       让下游 Agent 有机会如实告知用户「该数据可信度低」，
       而不是给出一个看起来精确实则臆造的数值。
    3. 所有异常转为结构化返回值，保证 server 不崩。
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from models import DataOrigin, MineralResource, ResourceCategory
from utils import get_logger

logger = get_logger("mineral-pdf-mcp")

# 项目内共享的数据目录（server.py 位于 servers/<name>/server.py，需上溯三层到项目根）
DATA_DIR = Path(__file__).resolve().parents[2] / "data"

mcp = FastMCP("mineral-pdf-mcp")


# ==========================================================================
# Provider 层
# ==========================================================================

class ResourceProvider(ABC):
    """储量数据源接口。

    「URL → 资源记录」这一步被抽象出来，是为了让
    PDF 远程抓取、本地文件、样本库三种来源可以互换。
    """

    @abstractmethod
    def fetch_document(self, pdf_url: str) -> Optional[tuple[str, str]]:
        """返回 (文档引用, 来源标记)。文档引用供 extractor 使用。"""

    @abstractmethod
    def locate_source(self, pdf_url: str) -> Optional[tuple[str, str, int]]:
        """按 URL 定位报告，返回 (文档标题, 真实来源URL, 总页数)。

        用于样本库：URL 直接映射到已知的报告。
        """

    @abstractmethod
    def extract(self, pdf_url: str) -> tuple[list[MineralResource], str, str]:
        """抽取储量记录，返回 (记录列表, 真实来源URL, 数据来源标记)。"""


class SampleResourceProvider(ResourceProvider):
    """本地样本库：URL → 报告 → 储量记录。

    保证离线可验证，是「5 分钟跑起来」的前提。
    """

    def __init__(self, data_file: Path | None = None) -> None:
        path = data_file or (DATA_DIR / "resources_samples.json")
        self._file = path
        self._reports: dict[str, dict] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._reports = raw.get("reports", {})

    @property
    def report_keys(self) -> list[str]:
        return list(self._reports.keys())

    def _match(self, pdf_url: str) -> Optional[dict]:
        """按 URL 匹配报告。

        匹配策略：先精确匹配 source_url，再退化为按 key 包含关系匹配，
        最后按文档标题关键词匹配。这样用户传原始 URL 或传 report key 都能工作。
        """
        if pdf_url in self._reports:
            return self._reports[pdf_url]

        for key, report in self._reports.items():
            if report.get("source_url") == pdf_url:
                return report

        lowered = pdf_url.lower()
        for key, report in self._reports.items():
            # 支持 "pilbara_greenbushes" 或 "greenbushes" 这类简写
            if key.lower() in lowered or lowered in key.lower():
                return report

        for key, report in self._reports.items():
            title = report.get("document_title", "").lower()
            if title and lowered in title:
                return report

        return None

    def fetch_document(self, pdf_url: str) -> Optional[tuple[str, str]]:
        report = self._match(pdf_url)
        return (pdf_url, DataOrigin.SAMPLE.value) if report else None

    def locate_source(self, pdf_url: str) -> Optional[tuple[str, str, int]]:
        report = self._match(pdf_url)
        if not report:
            return None
        return (
            report.get("document_title", pdf_url),
            report.get("source_url", pdf_url),
            int(report.get("page_count", 0)),
        )

    def extract(self, pdf_url: str) -> tuple[list[MineralResource], str, str]:
        report = self._match(pdf_url)
        if not report:
            return [], pdf_url, DataOrigin.DEGRADED.value

        out: list[MineralResource] = []
        for item in report.get("resources", []):
            try:
                out.append(
                    MineralResource(
                        category=ResourceCategory(item["category"]),
                        ore_tonnage=item.get("ore_tonnage"),
                        grade=item.get("grade"),
                        grade_unit=item.get("grade_unit"),
                        contained_metal=item.get("contained_metal"),
                        metal_unit=item.get("metal_unit"),
                        commodity=item.get("commodity"),
                        page=item.get("page"),
                        source_url=report.get("source_url", pdf_url),
                        confidence="high",
                        origin=DataOrigin.SAMPLE,
                    )
                )
            except Exception as exc:
                # 单条记录解析失败不应丢弃整份报告
                logger.warning("跳过无法解析的储量记录: %s", exc)
        return out, report.get("source_url", pdf_url), DataOrigin.SAMPLE.value


class PDFTextResourceProvider(ResourceProvider):
    """真实 PDF 解析：从 PDF 文本中正则抽取储量表。

    采用「先定位小节，再抽表」的策略，而不是全文盲抽：
    NI 43-101 报告里储量表通常集中在「Mineral Resources」章节，
    先定位能大幅降低误抽其他数字表格的概率。

    注意准确率有限 —— 这是刻意的设计选择：
    抽不准时 confidence 标 low 并如实返回，而不是编造数字。
    """

    SECTION_PATTERN = re.compile(
        r"(?:indicated|inferred)\s+(?:mineral\s+)?resources?",
        re.IGNORECASE,
    )
    CATEGORY_PATTERN = re.compile(r"\b(measured|indicated|inferred)\b", re.IGNORECASE)
    NUMBER_PATTERN = re.compile(r"\d[\d,]*\.?\d*")

    def __init__(self, sample_fallback: SampleResourceProvider | None = None) -> None:
        self._fallback = sample_fallback or SampleResourceProvider()

    def fetch_document(self, pdf_url: str) -> Optional[tuple[str, str]]:
        if not pdf_url.startswith(("http://", "https://")):
            return (pdf_url, DataOrigin.SAMPLE.value)
        return (pdf_url, DataOrigin.REMOTE.value)

    def locate_source(self, pdf_url: str) -> Optional[tuple[str, str, int]]:
        return (pdf_url, pdf_url, 0)

    def _extract_text(self, pdf_path: Path) -> list[tuple[int, str]]:
        """返回 [(页码, 该页文本)]。"""
        try:
            import pdfplumber
        except ImportError:
            logger.warning("pdfplumber 未安装，无法解析真实 PDF")
            return []
        pages: list[tuple[int, str]] = []
        with pdfplumber.open(pdf_path) as pdf:
            for idx, page in enumerate(pdf.pages, start=1):
                pages.append((idx, page.extract_text() or ""))
        return pages

    def extract(self, pdf_url: str) -> tuple[list[MineralResource], str, str]:
        local = Path(pdf_url)
        if not local.exists():
            # 远端 URL 暂不支持自动下载（避免 24h 内陷入反爬与凭证问题）
            logger.warning("PDF 不可本地访问: %s", pdf_url)
            return [], pdf_url, DataOrigin.DEGRADED.value

        pages = self._extract_text(local)
        if not pages:
            return [], pdf_url, DataOrigin.DEGRADED.value

        out: list[MineralResource] = []
        for page_no, text in pages:
            if not self.SECTION_PATTERN.search(text):
                continue
            current_category: Optional[ResourceCategory] = None
            for line in text.splitlines():
                cat = self.CATEGORY_PATTERN.search(line)
                if cat:
                    try:
                        current_category = ResourceCategory(cat.group(1).capitalize())
                    except ValueError:
                        current_category = None

                nums = [float(n.replace(",", "")) for n in self.NUMBER_PATTERN.findall(line)]
                if current_category is None or len(nums) < 2:
                    continue

                # 只有当一行同时出现品位单位与金属量单位时才认定为储量数据行，
                # 否则很可能是页眉/页码噪声。
                has_grade_unit = bool(re.search(r"%\s*(Li2O|Cu|Ni|Au|Fe|Zn|Pb)|g/t", line, re.I))
                has_metal_unit = bool(re.search(r"\b(t|oz|Mt)\b", line, re.I))
                if not (has_grade_unit and has_metal_unit):
                    continue

                out.append(
                    MineralResource(
                        category=current_category,
                        ore_tonnage=nums[0],
                        grade=nums[1] if len(nums) > 1 else None,
                        grade_unit="unknown",
                        contained_metal=nums[-1],
                        metal_unit="unknown",
                        commodity=self._guess_commodity(text),
                        page=page_no,
                        source_url=pdf_url,
                        # 关键：正则抽取存在歧义，如实标注低置信度
                        confidence="low",
                        origin=DataOrigin.REMOTE,
                    )
                )

        if not out:
            return [], pdf_url, DataOrigin.DEGRADED.value
        return out, pdf_url, DataOrigin.REMOTE.value

    @staticmethod
    def _guess_commodity(text: str) -> Optional[str]:
        for name, pattern in {
            "Lithium": r"lithium|Li2O",
            "Copper": r"copper|\bCu\b",
            "Nickel": r"nickel|\bNi\b",
            "Gold": r"gold|\bAu\b",
            "Zinc": r"zinc|\bZn\b",
        }.items():
            if re.search(pattern, text, re.IGNORECASE):
                return name
        return None


def get_provider() -> ResourceProvider:
    """按环境变量选择：default / sample / pdf。"""
    choice = os.getenv("MINING_PDF_PROVIDER", "default").lower()
    if choice == "pdf":
        logger.info("使用真实 PDF 解析模式")
        return PDFTextResourceProvider()
    return SampleResourceProvider()


# ==========================================================================
# MCP Tool
# ==========================================================================

@mcp.tool()
def extract_resources(pdf_url: str) -> dict:
    """从NI 43-101 矿权报告中抽取储量数据。

    抽取 Indicated 与 Inferred 储量（同时返回 Measured 供对比），
    每条记录包含 ore_tonnage（矿石量 Mt）、grade（品位）、
    contained_metal（金属量）及各自单位、commodity、page。

    Args:
        pdf_url: 报告 URL 或本地样本 key。
            样本库支持的 key：pilbara_greenbushes / newmont_kamoa / barrick_nickel，
            也可直接传 source_url。

    Returns:
        dict: 包含 resources 列表、document 元信息、以及 meta。
        meta.confidence_note 说明整体抽取可信度。
        找不到报告时 found=False 并列出可用 key —— 这是正常业务结果，
        不是错误，不抛异常。
    """
    try:
        provider = get_provider()
        records, real_url, origin = provider.extract(pdf_url)
        title_info = provider.locate_source(pdf_url)

        if not records:
            sample_provider = SampleResourceProvider()
            return {
                "found": False,
                "resources": [],
                "meta": {
                    "pdf_url": pdf_url,
                    "origin": origin,
                    "available_keys": sample_provider.report_keys,
                    "reason": (
                        "未能从该来源定位储量表。样本库仅用于演示抽取链路，"
                        "真实 PDF 需设置 MINING_PDF_PROVIDER=pdf 并提供本地文件路径。"
                    ),
                },
            }

        confidences = {r.confidence for r in records}
        overall = "high" if confidences == {"high"} else "low"

        return {
            "found": True,
            "document": {
                "title": title_info[0] if title_info else pdf_url,
                "source_url": real_url,
                "page_count": title_info[2] if title_info else None,
            },
            "resources": [r.model_dump() for r in records],
            "meta": {
                "pdf_url": pdf_url,
                "origin": origin,
                "record_count": len(records),
                "confidence": overall,
                "confidence_note": (
                    "数据来自本地样本库，字段已结构化校对，可信。"
                    if overall == "high"
                    else "由正则从 PDF 文本抽取，存在歧义，请以原始报告为准。"
                ),
                "source_note": "本地样本数据（非实时）" if origin == DataOrigin.SAMPLE.value else "PDF 解析结果",
            },
        }
    except Exception as exc:
        logger.error("extract_resources 失败: %s", exc)
        return {
            "found": False,
            "resources": [],
            "meta": {"pdf_url": pdf_url, "error": str(exc), "origin": "degraded"},
        }


if __name__ == "__main__":
    mcp.run(transport="stdio")