"""简报渲染：把结构化数据整理成 Markdown 日报。

设计要点：
    - 引用溯源：每个数字都能追回 URL。渲染层只做格式化，绝不生成 URL。
    - 风险提示：由数据驱动（价格波动、储量置信度、数据缺失），
      不是套话。这里体现的是「让 Agent 的每个断言都可追溯」。
    - 降级如实呈现：某个 server 挂了，对应段落说明数据源不可用，
      而不是悄悄省略 —— 让使用者知道信息不完整。
"""

from __future__ import annotations

from datetime import date as date_cls
from typing import Any

from agent.intent import Commodity, MiningRequest


def _fmt_num(value: Any, digits: int = 2, default: str = "N/A") -> str:
    """安全格式化数字。None 或异常值统一回落到默认值。"""
    if value is None:
        return default
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return default


def _collect_sources(*sections: dict) -> list[tuple[str, str]]:
    """从各段落收集 (来源名, URL)，去重后返回。

    这是引用脚注的数据来源。渲染层不生成任何 URL，
    只从工具返回的数据里取，保证引用真实可追溯。

    需要处理两处位置差异：
      - 新闻：URL 在 articles[].url
      - 储量：URL 在 document.source_url（报告级引用），
        records 里的 source_url 与之重复，故优先用 document 级，避免重复条目。
    """
    seen: list[tuple[str, str]] = []
    urls: set = set()

    def _add(label: str, url: str | None) -> None:
        # nonlocal 必须写在函数体首行，且要在任何使用之前声明。
        # 闭包内对 urls 赋值若不声明 nonlocal，Python 会把它当局部变量，
        # 读取时报 UnboundLocalError。
        nonlocal urls
        if url and url not in urls:
            urls.add(url)
            seen.append((label, url))

    for section in sections:
        if not isinstance(section, dict):
            continue

        # 新闻：逐篇引用
        for item in section.get("articles", []) or []:
            _add(item.get("source") or item.get("title") or "新闻", item.get("url"))

        # 储量：优先报告级引用；没有则退到记录级
        doc_url = (section.get("document") or {}).get("source_url")
        if doc_url:
            _add((section.get("document") or {}).get("title") or "NI 43-101 技术报告", doc_url)
        else:
            for item in section.get("resources", []) or []:
                _add(item.get("category") or "储量记录", item.get("source_url"))

    return seen


def render_briefing(
    req: MiningRequest,
    data: dict[str, dict],
    unavailable: set[str],
) -> str:
    """渲染完整简报。

    Args:
        req: 解析后的请求
        data: 各 server 返回结果，形如 {"news": {...}, "price": {...}}
        unavailable: 不可用的 server 名称集合

    Returns:
        str: Markdown 格式的简报
    """
    commodity = req.commodity.value if req.commodity else "关键矿产"
    region = req.region or "未指定地域"
    today = date_cls.today().isoformat()

    lines: list[str] = []
    lines.append(f"# 矿权日报 · {req.raw_query}")
    lines.append("")
    lines.append(f"> **矿种**：{commodity}　| **地域**：{region}　| **生成日期**：{today}　| **数据窗口**：近 {req.days} 天")
    lines.append("")

    # 数据来源声明 —— 主动交代，不等被问
    origins = _origin_summary(data)
    lines.append(f"> **数据来源**：{origins}")
    if unavailable:
        lines.append(f"> ⚠️ **降级提示**：数据源 {', '.join(sorted(unavailable))} 本次不可用，相关段落已标注")
    lines.append("")

    # ------------------------------------------------------------------
    # 一、新闻摘要
    # ------------------------------------------------------------------
    lines.append("## 一、新闻摘要")
    lines.append("")
    news = data.get("news", {})
    news_items = news.get("articles", [])
    if "news" in unavailable:
        lines.append("> ⚠️ 新闻数据源暂不可用，本节无法生成。")
        lines.append("")
    elif not news_items:
        lines.append("> 时间窗内未检索到相关新闻。")
        lines.append("")
    else:
        for item in news_items[:5]:
            title = item.get("title", "")
            published = item.get("published_at", "")
            summary = item.get("summary", "")
            lines.append(f"- **{title}**（{published} · {item.get('source','')}）")
            if summary:
                lines.append(f"  {summary}")
        lines.append("")
        if len(news_items) > 5:
            lines.append(f"*（另有 {len(news_items) - 5} 条，未展开）*")
            lines.append("")

    # ------------------------------------------------------------------
    # 二、储量数据
    # ------------------------------------------------------------------
    lines.append("## 二、储量数据")
    lines.append("")
    res = data.get("resources", {})
    res_items = res.get("resources", [])
    if "pdf" in unavailable:
        lines.append("> ⚠️ 储量数据源暂不可用，本节无法生成。")
        lines.append("")
    elif not res_items:
        lines.append("> 未定位到该矿权的储量报告。")
        lines.append("")
    else:
        doc = res.get("document", {})
        if doc.get("title"):
            lines.append(f"**报告**：{doc['title']}")
            lines.append("")
        lines.append("| 分类 | 矿石量 (Mt) | 品位 | 金属量 | 页码 |")
        lines.append("|---|---:|---|---:|---:|")
        for r in res_items:
            grade = _fmt_num(r.get("grade"), 2, default="-")
            if r.get("grade_unit"):
                grade += f" {r['grade_unit']}"
            metal = _fmt_num(r.get("contained_metal"), 0, default="-")
            if r.get("metal_unit"):
                metal += f" {r['metal_unit']}"
            lines.append(
                f"| {r.get('category','')} | {_fmt_num(r.get('ore_tonnage'),1,'-')} | "
                f"{grade} | {metal} | {r.get('page','-')} |"
            )
        lines.append("")
        # 储量数据最关心的不是数字，是「这个数字可信吗」
        conf = res.get("meta", {}).get("confidence", "unknown")
        note = res.get("meta", {}).get("confidence_note", "")
        flag = "✅" if conf == "high" else "⚠️"
        lines.append(f"{flag} **抽取置信度**：{conf}。{note}")
        lines.append("")

    # ------------------------------------------------------------------
    # 三、价格走势
    # ------------------------------------------------------------------
    lines.append("## 三、价格走势")
    lines.append("")
    trend = data.get("price", {})
    t = trend.get("trend")
    if "price" in unavailable:
        lines.append("> ⚠️ 价格数据源暂不可用，本节无法生成。")
        lines.append("")
    elif not t:
        lines.append("> 未获取到价格数据。")
        lines.append("")
    else:
        unit = t.get("unit", "")
        change = t.get("change_pct")
        arrow = "↑" if (change or 0) > 0 else ("↓" if (change or 0) < 0 else "→")
        lines.append(
            f"- **区间表现**：{_fmt_num(t.get('start_price'))} → {_fmt_num(t.get('end_price'))} {unit}"
            f"　**{arrow} {change:+.2f}%**" if change is not None else
            f"- **区间表现**：{_fmt_num(t.get('start_price'))} → {_fmt_num(t.get('end_price'))} {unit}"
        )
        series = t.get("series", [])
        if series:
            span = f"{series[0]['date']} ~ {series[-1]['date']}"
            hi = max(p["price"] for p in series)
            lo = min(p["price"] for p in series)
            lines.append(f"- **区间**：{span}（{len(series)} 个交易日）")
            lines.append(f"- **最高 / 最低**：{_fmt_num(hi)} / {_fmt_num(lo)} {unit}")
            lines.append("")
            # 迷你 ASCII 走势，让简报可一眼看出方向
            lines.append("```")
            lines.append(_ascii_chart(series))
            lines.append("```")
        warning = t.get("warning")
        if warning:
            lines.append(f"> ⚠️ {warning}")
        lines.append("")

    # ------------------------------------------------------------------
    # 四、风险提示
    # ------------------------------------------------------------------
    lines.append("## 四、风险提示")
    lines.append("")
    risks = _build_risks(req, data, unavailable)
    if risks:
        for risk in risks:
            lines.append(f"- {risk}")
    else:
        lines.append("- 未能从当前数据中识别出显著风险信号。")
    lines.append("")

    # ------------------------------------------------------------------
    # 五、引用来源
    # ------------------------------------------------------------------
    lines.append("## 五、引用来源")
    lines.append("")
    sources = _collect_sources(news, res)
    if sources:
        for idx, (label, url) in enumerate(sources, start=1):
            lines.append(f"{idx}. {label} — <{url}>")
    else:
        lines.append("> 无可用引用。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*本简报由矿权日报 Agent 自动生成。所有数值均来自上述引用来源，未作人工调整。*")

    return "\n".join(lines)


def _origin_summary(data: dict[str, dict]) -> str:
    """汇总各数据段来源，让使用者一眼知道数据的可信度层级。"""
    labels = {"remote": "实时数据", "sample": "本地样本数据", "degraded": "已降级"}
    seen = []
    for section in data.values():
        meta = section.get("meta", {}) if isinstance(section, dict) else {}
        origin = meta.get("origin")
        if origin:
            label = labels.get(str(origin), str(origin))
            if label not in seen:
                seen.append(label)
    return "、".join(seen) if seen else "未知"


def _build_risks(req: MiningRequest, data: dict[str, dict], unavailable: set[str]) -> list[str]:
    """基于数据构建风险提示。

    刻意不做「AI 生成的泛泛风险建议」—— 每条风险都要有数据依据，
    否则就是编造。这是可信度设计的核心。
    """
    risks: list[str] = []

    # 1) 价格波动风险 —— 有明确阈值才报，避免噪音
    t = data.get("price", {}).get("trend")
    if t and t.get("change_pct") is not None:
        change = t["change_pct"]
        if abs(change) >= 5:
            direction = "上涨" if change > 0 else "下跌"
            risks.append(
                f"**价格波动**：{t.get('commodity','')} 近 {t.get('days','')} 天{direction} "
                f"{change:+.2f}%（{_fmt_num(t.get('start_price'))} → {_fmt_num(t.get('end_price'))} {t.get('unit','')}），"
                "波动幅度较大，关注短期回调风险。"
            )

    # 2) 数据可信度风险
    res = data.get("resources", {})
    conf = res.get("meta", {}).get("confidence")
    if conf == "low":
        risks.append(
            "**储量数据存疑**：本次抽取置信度为 low，"
            "数据由文本解析得到，数值应以原始 NI 43-101 报告为准，不宜直接用于决策。"
        )

    # 3) 数据时效风险
    if t and t.get("warning"):
        risks.append(f"**数据完整性**：{t['warning']}趋势分析结论可靠性相应下降。")

    # 4) 数据源可用性风险
    if unavailable:
        risks.append(
            f"**数据源不可用**：{'、'.join(sorted(unavailable))} 未响应，"
            "本简报信息不完整，缺失部分请另行核实后再决策。"
        )

    # 5) 信息时效性提醒
    if not data.get("news", {}).get("articles"):
        risks.append("**信息盲区**：时间窗内未检索到相关新闻，可能存在信息滞后或关键词覆盖不足。")

    return risks


def _ascii_chart(series: list[dict], width: int = 40) -> str:
    """生成简易 ASCII 走势图。

    刻意不引入 matplotlib：减少依赖，也避免无图形环境下的渲染问题。
    """
    if not series:
        return "(无数据)"

    prices = [p["price"] for p in series]
    lo, hi = min(prices), max(prices)
    if hi == lo:
        return f"价格持平于{_fmt_num(lo)}"

    # 取最近 width 个点
    tail = prices[-width:] if len(prices) > width else prices
    span = hi - lo
    block = "▁▂▃▄▅▆▇█"

    chart = "".join(block[min(int((p - lo) / span * (len(block) - 1)), len(block) - 1)] for p in tail)
    label_lo, label_hi = series[0]["date"], series[-1]["date"]
    return f"{_fmt_num(lo):>10} ┤{chart}┤ {_fmt_num(hi)}\n{' ' * 11}{label_lo} → {label_hi}"