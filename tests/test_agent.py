"""Agent 编排层测试。

重点验证两件事：
    1. 意图解析与工具路由 —— Agent 区别于固定流水线的核心能力
    2. 简报渲染 —— 四段结构与引用溯源必须齐备
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.briefing import _build_risks, _collect_sources, render_briefing
from agent.intent import MiningRequest, TimeWindow, parse_request


class TestIntentParsing:
    def test_extracts_commodity(self):
        """矿种识别，支持中文与英文。"""
        assert parse_request("锂矿简报").commodity.value == "lithium"
        assert parse_request("copper price").commodity.value == "copper"
        assert parse_request("镍价走势").commodity.value == "nickel"

    def test_extracts_region(self):
        """地域识别。"""
        assert parse_request("Pilbara 锂矿简报").region == "Pilbara"
        assert parse_request("Greenbushes 储量").region == "Greenbushes"

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("给我一份关于 Pilbara 锂矿的今日简报",
             ["search", "extract_resources", "get_price", "get_trend"]),
            ("今天锂价多少", ["get_price"]),
            ("昨天镍价", ["get_price"]),
            ("铁矿石近30天走势", ["get_trend"]),
            ("给我一份关于 Greenbushes 的储量报告", ["extract_resources"]),
            ("今天有什么新闻", ["search"]),
        ],
    )
    def test_tool_routing(self, query: str, expected: list[str]):
        """工具路由：根据输入决定调什么，而非全量执行。"""
        assert parse_request(query).tool_plan() == expected

    def test_single_price_query_does_not_call_all_tools(self):
        """核心区分点：单点问价不应拉新闻和储量。"""
        plan = parse_request("今天锂价多少").tool_plan()
        assert len(plan) == 1, "单点问价只应调用 get_price，这正是 Agent 与流水线的区别"

    def test_news_window_has_floor(self):
        """新闻检索窗口应设下限。

        矿产新闻低频，若严格按「今日」过滤 1 天，召回必然为空。
        """
        req = parse_request("今日锂矿简报")
        assert req.news_days >= 14, "新闻窗口应有下限，避免召回为空"

    def test_trend_query_extends_window(self):
        """趋势分析需要足够长的价格窗口。"""
        assert parse_request("铜价趋势").days >= 30

    def test_unsupported_query_returns_empty_plan(self):
        """无关输入不应强行调用工具。"""
        req = parse_request("hello world")
        assert req.tool_plan() == []


class TestBriefingRendering:
    @pytest.fixture
    def sample_data(self) -> dict:
        return {
            "news": {
                "articles": [
                    {
                        "title": "Pilbara lithium expansion approved",
                        "url": "https://www.mining.com/pilbara-1",
                        "source": "mining.com",
                        "published_at": "2026-10-07",
                        "summary": "JV confirmed drilling budget.",
                    }
                ],
                "meta": {"origin": "sample"},
            },
            "resources": {
                "found": True,
                "document": {
                    "title": "Pilbara Annual Report 2025",
                    "source_url": "https://example.com/report.pdf",
                },
                "resources": [
                    {
                        "category": "Indicated",
                        "ore_tonnage": 78.4,
                        "grade": 1.21,
                        "grade_unit": "% Li2O",
                        "contained_metal": 2380000,
                        "metal_unit": "t Li",
                        "page": 87,
                        "source_url": "https://example.com/report.pdf",
                    }
                ],
                "meta": {"origin": "sample", "confidence": "high", "confidence_note": "已校对"},
            },
            "price": {
                "trend": {
                    "commodity": "lithium",
                    "unit": "USD/t",
                    "days": 30,
                    "start_price": 8420.0,
                    "end_price": 8452.0,
                    "change_pct": 0.38,
                    "series": [
                        {"date": "2026-09-08", "price": 8420.0},
                        {"date": "2026-10-08", "price": 8452.0},
                    ],
                    "warning": None,
                },
                "meta": {"origin": "sample"},
            },
        }

    def test_renders_all_four_sections(self, sample_data):
        """题目要求四段：新闻摘要 / 储量数据 / 价格走势 / 风险提示。"""
        req = parse_request("给我生成一份关于 Pilbara 锂矿的今日简报")
        md = render_briefing(req, sample_data, set())
        for section in ["新闻摘要", "储量数据", "价格走势", "风险提示", "引用来源"]:
            assert section in md, f"简报缺少「{section}」段落"

    def test_includes_source_links(self, sample_data):
        """题目明确要求引用源链接。"""
        req = parse_request("给我生成一份关于 Pilbara 锂矿的今日简报")
        md = render_briefing(req, sample_data, set())
        assert "https://www.mining.com/pilbara-1" in md
        assert "https://example.com/report.pdf" in md

    def test_degradation_is_disclosed(self, sample_data):
        """数据源不可用时必须如实标注，而不是静默省略。"""
        req = parse_request("给我生成一份关于 Pilbara 锂矿的今日简报")
        md = render_briefing(req, sample_data, unavailable={"news"})
        assert "降级" in md or "不可用" in md
        assert "news" in md

    def test_origin_disclosed(self, sample_data):
        """数据来源必须主动声明，不等被追问。"""
        req = parse_request("给我生成一份关于 Pilbara 锂矿的今日简报")
        md = render_briefing(req, sample_data, set())
        assert "本地样本数据" in md

    def test_no_url_fabrication(self, sample_data):
        """渲染层不得生成 URL：引用必须来自输入数据。"""
        req = parse_request("给我生成一份关于 Pilbara 锂矿的今日简报")
        md = render_briefing(req, sample_data, set())
        import re

        urls = set(re.findall(r"https?://[^\s>)]+", md))
        allowed = {"https://www.mining.com/pilbara-1", "https://example.com/report.pdf"}
        assert urls <= allowed, f"出现了数据中不存在的 URL: {urls - allowed}"


class TestRiskLogic:
    def test_high_volatility_triggers_risk(self):
        """价格波动超阈值应提示风险。"""
        req = parse_request("锂价风险")
        data = {"price": {"trend": {"commodity": "lithium", "days": 30,
                                    "start_price": 7000.0, "end_price": 8200.0,
                                    "change_pct": 17.1, "unit": "USD/t"}}}
        risks = _build_risks(req, data, set())
        assert any("价格波动" in r for r in risks)

    def test_low_volatility_no_risk(self):
        """小幅波动不报风险，避免噪音。"""
        req = parse_request("锂价风险")
        data = {"price": {"trend": {"commodity": "lithium", "days": 30,
                                    "start_price": 8400.0, "end_price": 8452.0,
                                    "change_pct": 0.6, "unit": "USD/t"}}}
        risks = _build_risks(req, data, set())
        assert not any("价格波动" in r for r in risks)

    def test_low_confidence_flagged(self):
        """储量抽取置信度低时必须提示。"""
        req = parse_request("锂矿简报")
        data = {"resources": {"meta": {"confidence": "low"}}}
        risks = _build_risks(req, data, set())
        assert any("储量数据存疑" in r for r in risks)

    def test_unavailable_source_flagged(self):
        """数据源不可用必须进风险提示。"""
        req = parse_request("锂矿简报")
        risks = _build_risks(req, {}, {"news"})
        assert any("数据源不可用" in r for r in risks)


class TestSourceCollection:
    def test_dedupes_same_url(self):
        """同一 URL 只列一次。"""
        sources = _collect_sources(
            {"articles": [{"url": "https://a.com", "source": "X"},
                           {"url": "https://a.com", "source": "X"}]}
        )
        assert len(sources) == 1

    def test_prefers_document_level_url(self):
        """储量优先用报告级引用，避免逐条重复。"""
        sources = _collect_sources(
            {"document": {"title": "Report", "source_url": "https://r.pdf"},
             "resources": [{"source_url": "https://r.pdf"}]}
        )
        assert len(sources) == 1
        assert sources[0][0] == "Report"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])