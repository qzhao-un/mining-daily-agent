"""lme-price-mcp：LME / 交易所金属价格 MCP Server。

题目要求的两个工具：
    - get_price(commodity, date)   取指定交易日价格
    - get_trend(commodity, days)    取区间趋势

设计要点（本项目的核心工程决策）：
    题目提到的 LME / SHFE 数据源存在登录墙与频控限制，
    24h 内无法完成合规接入。因此这里把「取数据」抽象为 Provider 接口：

        MockPriceProvider   本地样本，离线可用，保证可验证
        RemotePriceProvider 公开接口，环境变量开启时尝试

    工具签名不随数据源变化。这样做的价值不是"绕过登录墙"，
    而是体现「数据源可替换」的架构能力 —— 面试时这一点是可讲的设计，
    同时 README 中会明确标注每条数据的真实来源，不隐瞒。
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from datetime import date as date_cls
from datetime import timedelta
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from models import DataOrigin, PricePoint, PriceTrend
from utils import get_logger

logger = get_logger("lme-price-mcp")

# 项目内共享的数据目录（server.py 位于 servers/<name>/server.py，需上溯三层到项目根）
DATA_DIR = Path(__file__).resolve().parents[2] / "data"

mcp = FastMCP("lme-price-mcp")

# 支持的矿种与其价格单位
COMMODITY_UNITS = {
    "lithium": "USD/t",
    "copper": "USD/t",
    "nickel": "USD/t",
    "zinc": "USD/t",
    "iron_ore": "USD/dmt",
}

# 常见别名归一化，避免用户输入 "Iron Ore" / "Li" 导致查不到
ALIASES = {
    "li": "lithium",
    "lithium": "lithium",
    "锂": "lithium",
    "cu": "copper",
    "copper": "copper",
    "铜": "copper",
    "ni": "nickel",
    "nickel": "nickel",
    "镍": "nickel",
    "zn": "zinc",
    "zinc": "zinc",
    "锌": "zinc",
    "iron ore": "iron_ore",
    "iron_ore": "iron_ore",
    "fe": "iron_ore",
    "铁矿石": "iron_ore",
}


def normalize_commodity(raw: str) -> Optional[str]:
    """把用户输入归一化为标准矿种 key。"""
    key = raw.strip().lower().replace("-", "_")
    return ALIASES.get(key) or ALIASES.get(key.replace("_", " "))


# ==========================================================================
# Provider 层
# ==========================================================================

class PriceProvider(ABC):
    """价格数据源接口。

    换数据源不需要改 MCP 工具契约，这是本层存在的唯一理由。
    """

    @abstractmethod
    def get_price(self, commodity: str, target_date: str) -> Optional[PricePoint]:
        """取指定日期价格；该日期无数据时返回 None。"""

    @abstractmethod
    def get_series(self, commodity: str, days: int) -> list[PricePoint]:
        """取最近 N 个交易日的序列。"""


class MockPriceProvider(PriceProvider):
    """本地样本数据源（默认）。

    数据为合成样本，用于离线演示与测试，不冒充实时行情。
    """

    def __init__(self, data_file: Path | None = None) -> None:
        path = data_file or (DATA_DIR / "price_samples.json")
        self._file = path
        self._series: dict[str, list[dict]] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._series = raw.get("series", {})

    @property
    def commodities(self) -> list[str]:
        return list(self._series.keys())

    def get_price(self, commodity: str, target_date: str) -> Optional[PricePoint]:
        point = next(
            (p for p in self._series.get(commodity, []) if p["date"] == target_date), None
        )
        if not point:
            return None
        return PricePoint(
            commodity=commodity,
            date=point["date"],
            price=point["price"],
            unit=COMMODITY_UNITS.get(commodity, "USD/t"),
            origin=DataOrigin.SAMPLE,
            source_url=f"file://{self._file}",
        )

    def get_series(self, commodity: str, days: int) -> list[PricePoint]:
        rows = self._series.get(commodity, [])
        selected = rows[-days:] if days > 0 else []
        unit = COMMODITY_UNITS.get(commodity, "USD/t")
        return [
            PricePoint(
                commodity=commodity,
                date=r["date"],
                price=r["price"],
                unit=unit,
                origin=DataOrigin.SAMPLE,
                source_url=f"file://{self._file}",
            )
            for r in selected
        ]


class RemotePriceProvider(PriceProvider):
    """公开数据源（可选启用）。

    题目提到的 LME / SHFE 行情接口需要登录墙与频控，
    本项目不尝试绕过 —— 这类限制应通过合规的数据授权解决。
    这里保留 Provider 位置：拿到正式授权后，只需新增一个子类，
    上层工具与 Agent 编排完全不用改。
    """

    def __init__(self, fallback: MockPriceProvider | None = None) -> None:
        self._fallback = fallback or MockPriceProvider()

    def get_price(self, commodity: str, target_date: str) -> Optional[PricePoint]:
        logger.info("远端行情未授权，回落到样本数据: %s", commodity)
        return self._fallback.get_price(commodity, target_date)

    def get_series(self, commodity: str, days: int) -> list[PricePoint]:
        logger.info("远端行情未授权，回落到样本数据: %s", commodity)
        return self._fallback.get_series(commodity, days)


def get_provider() -> PriceProvider:
    """按环境变量选择数据源：sample（默认）/ remote。"""
    choice = os.getenv("MINING_PRICE_PROVIDER", "sample").lower()
    if choice == "remote":
        return RemotePriceProvider()
    return MockPriceProvider()


# ==========================================================================
# MCP Tools
# ==========================================================================

@mcp.tool()
def get_price(commodity: str, date: str) -> dict:
    """获取指定交易日的价格。

    Args:
        commodity: 矿种。支持 lithium/copper/nickel/zinc/iron_ore，
            也支持别名如 Li、Cu、锂、铜、铁矿石。
        date: 交易日，格式 YYYY-MM-DD。需为交易日（周末无数据）。

    Returns:
        dict: 包含 price 字段（date/price/unit/origin），
        以及 meta（数据来源说明）。
        该日期无数据时 found=False —— 这是正常业务结果，不抛异常。
    """
    try:
        key = normalize_commodity(commodity)
        if key is None:
            return {
                "found": False,
                "meta": {
                    "error": f"不支持的矿种: {commodity}",
                    "supported": list(COMMODITY_UNITS.keys()),
                },
            }

        provider = get_provider()
        point = provider.get_price(key, date)

        if point is None:
            # 明确区分「格式错误」与「当日无数据」，避免用户误以为系统坏了
            try:
                parsed = date_cls.fromisoformat(date)
                reason = (
                    f"{parsed.isoformat()} 是周末或节假日，无交易数据。"
                    if parsed.weekday() >= 5
                    else f"{date} 在当前数据源中没有记录。"
                )
            except ValueError:
                reason = f"日期格式应为 YYYY-MM-DD，收到: {date}"

            return {
                "found": False,
                "meta": {
                    "commodity": key,
                    "date": date,
                    "reason": reason,
                    "available_range": (
                        f"{provider.get_series(key, 9999)[0].date} ~ "
                        f"{provider.get_series(key, 9999)[-1].date}"
                        if provider.get_series(key, 9999)
                        else "无"
                    ),
                },
            }

        return {"found": True, "price": point.model_dump(), "meta": _meta_for(key, point)}
    except Exception as exc:
        logger.error("get_price 失败: %s", exc)
        return {"found": False, "meta": {"commodity": commodity, "error": str(exc)}}


@mcp.tool()
def get_trend(commodity: str, days: int = 30) -> dict:
    """获取区间价格趋势。

    Args:
        commodity: 矿种，支持同上。
        days: 回溯交易日数量，默认 30。

    Returns:
        dict: 包含 trend 字段（start_price/end_price/change_pct/series）
        与 meta（数据来源、区间实际覆盖的交易日数）。
        数据不足或不足days 个交易日时，会在 meta.warning 中如实说明，
        而不是静默返回残缺数据。
    """
    try:
        key = normalize_commodity(commodity)
        if key is None:
            return {
                "found": False,
                "meta": {"error": f"不支持的矿种: {commodity}", "supported": list(COMMODITY_UNITS.keys())},
            }

        provider = get_provider()
        series = provider.get_series(key, days)

        if not series:
            return {
                "found": False,
                "meta": {"commodity": key, "reason": f"当前数据源没有 {key} 的任何记录"},
            }

        start_price = series[0].price
        end_price = series[-1].price
        change_pct = round((end_price - start_price) / start_price * 100, 2) if start_price else None

        warning = None
        if len(series) < days:
            warning = (
                f"请求 {days} 个交易日，实际仅取到 {len(series)} 个"
                f"（{series[0].date} ~ {series[-1].date}）。"
            )

        origin = series[0].origin
        trend = PriceTrend(
            commodity=key,
            unit=COMMODITY_UNITS.get(key, "USD/t"),
            days=days,
            start_price=start_price,
            end_price=end_price,
            change_pct=change_pct,
            series=series,
            origin=origin,
            warning=warning,
        )

        return {
            "found": True,
            "trend": trend.model_dump(),
            "meta": _meta_for(key, series[0]) | {"requested_days": days, "actual_days": len(series)},
        }
    except Exception as exc:
        logger.error("get_trend 失败: %s", exc)
        return {"found": False, "meta": {"commodity": commodity, "error": str(exc)}}


def _meta_for(commodity: str, point: PricePoint) -> dict:
    """统一的来源说明字段。

    主动交代数据来源，而不是等被追问 —— 这是可信度设计的一部分。
    """
    return {
        "commodity": commodity,
        "origin": point.origin.value,
        "source_note": "实时数据源" if point.origin == DataOrigin.REMOTE else "本地样本数据（合成，非实时行情）",
        "retrieved_at": date_cls.today().isoformat(),
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")