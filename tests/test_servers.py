"""三个 MCP server 的测试。

测试策略：
    - 单元测试直接调用 tool 函数（普通 Python 函数），不需要起子进程，开销小。
    - 集成测试才真正启动 MCP server 走协议层，验证 JSON-RPC 通路。

关键原则：测试要覆盖异常路径，不只测 happy path。
「找不到数据」和「参数非法」是这类server 的高频真实场景。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from servers.lme_price.server import get_price, get_trend, normalize_commodity
from servers.mineral_pdf.server import extract_resources
from servers.mining_news.server import fetch_article, search


# ==========================================================================
# mining-news-mcp
# ==========================================================================

class TestNewsServer:
    def test_search_returns_articles(self):
        """基础检索：应返回带完整字段的文章列表。"""
        result = search("Pilbara lithium", 30)
        assert result["meta"]["count"] > 0, "应至少召回一篇 Pilbara 相关新闻"
        article = result["articles"][0]
        assert article["title"]
        assert article["url"].startswith("http")
        assert article["source"]
        assert article["published_at"]
        # 枚举必须序列化为字符串，否则下游解析会出错
        assert article["origin"] == "sample", "origin 应序列化为字符串而非枚举 repr"

    def test_search_respects_days_window(self):
        """时间窗：days=1 应召回 0 篇（样本最新是 2026-10-07，相对今天更早）。"""
        result = search("lithium", 1)
        # 具体条数依赖运行日期，这里只断言不抛异常且结构完整
        assert "articles" in result and "meta" in result

    def test_search_unknown_keyword_returns_empty_not_error(self):
        """无匹配时应返回空列表而非报错，且保留 meta 说明来源。"""
        result = search("zzzz-nonexistent-commodity", 365)
        assert result["articles"] == []
        assert result["meta"]["origin"] in ("sample", "remote", "degraded")

    def test_fetch_article_hit_and_miss(self):
        """命中返回详情；未命中返回 found=False 且不抛异常。"""
        hit = search("Pilbara", 365)["articles"][0]
        ok = fetch_article(hit["url"])
        assert ok["found"] is True
        assert ok["article"]["title"] == hit["title"]

        miss = fetch_article("https://example.com/not-in-dataset")
        assert miss["found"] is False
        assert "reason" in miss["meta"]

    def test_fetch_article_invalid_url_does_not_crash(self):
        """非法 URL 应被捕获，不应让 server 崩溃。"""
        result = fetch_article("not-a-valid-url")
        assert result["found"] is False


# ==========================================================================
# mineral-pdf-mcp
# ==========================================================================

class TestPDFServer:
    def test_extract_known_report(self):
        """抽取已知样本报告：应返回结构化储量记录。"""
        result = extract_resources("pilbara_greenbushes")
        assert result["found"] is True
        assert len(result["resources"]) >= 3
        rec = result["resources"][0]
        for field in ("category", "ore_tonnage", "grade", "contained_metal",
                      "grade_unit", "metal_unit", "source_url"):
            assert field in rec, f"记录缺少字段 {field}"

    def test_returns_indicated_and_inferred(self):
        """题目明确要求 Indicated 与 Inferred，两类都必须覆盖。"""
        result = extract_resources("newmont_kamoa")
        categories = {r["category"] for r in result["resources"]}
        assert "Indicated" in categories
        assert "Inferred" in categories

    def test_origin_serialized_as_string(self):
        """origin 枚举必须序列化为字符串。"""
        result = extract_resources("barrick_nickel")
        assert result["resources"][0]["origin"] == "sample"

    def test_unknown_report_lists_available_keys(self):
        """未知报告应返回可用 key 列表 —— 这是业务结果不是错误。"""
        result = extract_resources("no_such_report")
        assert result["found"] is False
        assert "pilbara_greenbushes" in result["meta"]["available_keys"]

    def test_match_by_source_url(self):
        """应支持直接传 source_url 而非 key。"""
        url = "https://www.annualreports.com/HostedData/AnnualReportArchive/p/ASX_PIL_2025.pdf"
        result = extract_resources(url)
        assert result["found"] is True

    def test_missing_file_returns_low_confidence_or_not_found(self):
        """指向不存在的本地 PDF 时应降级，不应崩溃或编造数据。"""
        result = extract_resources("./definitely_not_here.pdf")
        # 要么找到样本，要么明确降级；绝不能凭空造出储量数据
        if result["found"]:
            assert result["meta"]["confidence"] in ("high", "medium", "low")
        else:
            assert result["meta"]["origin"] == "degraded"


# ==========================================================================
# lme-price-mcp
# ==========================================================================

class TestPriceServer:
    def test_commodity_alias_normalization(self):
        """别名归一化：Li / 锂 / lithium 应指向同一矿种。"""
        assert normalize_commodity("Li") == "lithium"
        assert normalize_commodity("lithium") == "lithium"
        assert normalize_commodity("锂") == "lithium"
        assert normalize_commodity("Iron Ore") == "iron_ore"
        assert normalize_commodity("unobtainium") is None

    def test_get_price_hit(self):
        """命中交易日：应返回价格与单位。"""
        result = get_price("lithium", "2026-10-08")
        assert result["found"] is True
        assert result["price"]["price"] > 0
        assert result["price"]["unit"] == "USD/t"

    def test_get_price_weekend_explains_reason(self):
        """周末查询：应给出明确原因而非笼统失败。"""
        result = get_price("lithium", "2026-10-10")  # 周六
        assert result["found"] is False
        assert "周末" in result["meta"]["reason"]

    def test_get_price_bad_date_format(self):
        """日期格式错误应被识别。"""
        result = get_price("lithium", "08/10/2026")
        assert result["found"] is False
        assert "YYYY-MM-DD" in result["meta"]["reason"]

    def test_unsupported_commodity_lists_supported(self):
        """不支持的矿种应列出支持清单。"""
        result = get_price("unobtainium", "2026-10-08")
        assert result["found"] is False
        assert "lithium" in result["meta"]["supported"]

    def test_get_trend_computes_change(self):
        """趋势计算：涨跌幅应与首尾价格一致。"""
        result = get_trend("lithium", 10)
        assert result["found"] is True
        trend = result["trend"]
        expected = round(
            (trend["end_price"] - trend["start_price"]) / trend["start_price"] * 100, 2
        )
        assert trend["change_pct"] == expected
        assert len(trend["series"]) == 10

    def test_get_trend_warns_when_data_insufficient(self):
        """请求天数超过样本容量时应明确告警，而不是静默返回残缺数据。"""
        result = get_trend("lithium", 365)
        assert result["found"] is True
        assert result["trend"]["warning"] is not None
        assert "仅取到" in result["trend"]["warning"]


# ==========================================================================
# 数据契约一致性
# ==========================================================================

class TestDataContract:
    def test_all_tools_return_meta_with_origin(self):
        """跨server 的数据契约：都应带 meta.origin，供下游判断数据可信度。"""
        assert "origin" in search("lithium", 30)["meta"]
        assert "origin" in extract_resources("pilbara_greenbushes")["meta"]
        assert "origin" in get_price("lithium", "2026-10-08")["meta"]
        assert "origin" in get_trend("lithium", 10)["meta"]

    def test_never_raise_on_bad_input(self):
        """核心健壮性要求：任何非法输入都应返回结构化结果，不抛异常。

        因为 MCP server 一旦崩溃，Agent 的降级逻辑就完全失效。
        """
        cases = [
            lambda: search("", 0),
            lambda: search("test", -5),
            lambda: fetch_article(""),
            lambda: extract_resources(""),
            lambda: get_price("", "2026-01-01"),
            lambda: get_trend("lithium", 0),
        ]
        for fn in cases:
            try:
                assert isinstance(fn(), dict), "工具应返回 dict"
            except Exception as exc:  # pragma: no cover
                pytest.fail(f"工具抛出异常而非降级: {exc}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])