"""意图解析：把自然语言请求转成结构化的执行计划。

这是本项目区别于「固定流水线」的核心。
流水线是「所有工具都跑一遍，按顺序拼结果」；
Agent 是「先判断用户要什么，再决定调哪些工具」。

例如：
    「今天锂价多少」           → 只需要价格工具，不必拉新闻和储量
    「Pilbara 锂矿有什么风险」 → 三类都要，且必须带趋势数据
    「昨天铜价」               → 单点查询，不需要趋势
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Commodity(str, Enum):
    """支持的矿种。"""

    LITHIUM = "lithium"
    COPPER = "copper"
    NICKEL = "nickel"
    ZINC = "zinc"
    IRON_ORE = "iron_ore"


class TimeWindow(str, Enum):
    """时间窗。"""

    TODAY = "today"
    DAYS_7 = "days_7"
    DAYS_30 = "days_30"
    CUSTOM = "custom"


@dataclass
class MiningRequest:
    """解析后的请求。"""

    raw_query: str

    # --- 实体识别 ---
    commodity: Commodity | None = None
    region: str | None = None
    time_window: TimeWindow = TimeWindow.DAYS_7
    days: int = 7
    news_days: int = 7  # 新闻检索窗口，与价格时序窗口解耦

    # --- 工具路由决策 ---
    want_news: bool = False
    want_resources: bool = False
    want_price: bool = False
    want_trend: bool = False

    # --- 输出形式 ---
    want_briefing: bool = True
    risk_focus: bool = False

    matched_keywords: list[str] = field(default_factory=list)

    def tool_plan(self) -> list[str]:
        """返回计划调用的工具列表（有序）。"""
        plan: list[str] = []
        if self.want_news:
            plan.append("search")
        if self.want_resources:
            plan.append("extract_resources")
        if self.want_price:
            plan.append("get_price")
        if self.want_trend:
            plan.append("get_trend")
        return plan

    def explain(self) -> str:
        """人类可读的决策说明。

        刻意提供这个能力：面试时能直接展示「Agent 如何判断调什么」，
        而不是让人只能看到黑盒结果。
        """
        commodity = self.commodity.value if self.commodity else "未指定"
        region = self.region or "未指定"
        return (
            f"矿种={commodity} | 地域={region} | 价格窗口={self.time_window.value}({self.days}天)\n"
            f"新闻窗口={self.news_days}天\n"
            f"计划调用: {' → '.join(self.tool_plan()) or '无'}"
        )


# --------------------------------------------------------------------------
# 词表
# --------------------------------------------------------------------------

_COMMODITY_PATTERNS: list[tuple[Commodity, str]] = [
    (Commodity.LITHIUM, r"lithium|\bLi\b|\bLi2O\b|锂"),
    (Commodity.COPPER, r"copper|\bCu\b|铜"),
    (Commodity.NICKEL, r"nickel|\bNi\b|镍"),
    (Commodity.ZINC, r"zinc|\bZn\b|铅锌"),
    (Commodity.IRON_ORE, r"iron\s*ore|铁矿石|\bFe\b"),
]

_REGION_PATTERNS = [
    ("Pilbara", r"pilbara"),
    ("Greenbushes", r"greenbushes"),
    ("Kamoa", r"kamoa"),
    ("Kalgoorlie", r"kalgoorlie"),
    ("Pilbara_NW", r"pilbara\s+northwest|\bPilbara\s+NW\b"),
]

_TIME_PATTERNS: list[tuple[TimeWindow, int, str]] = [
    (TimeWindow.TODAY, 1, r"today|今日|今天|当天"),
    (TimeWindow.DAYS_7, 7, r"近?\s*7\s*天|过去一周|last\s*week|past\s*week"),
    (TimeWindow.DAYS_30, 30, r"近?\s*(?:30|一个月|1个月|month)\s*天?|过去一个月|last\s*month|past\s*month"),
]

# 新闻检索窗口下限。
#
# 为什么需要下限：用户说「今日简报」时，字面意思是「今天这个主题的简报」，
# 而不是「只看今天 24 小时内发布的新闻」。矿产类新闻本身是低频事件，
# 若严格按 1 天过滤，召回率几乎为零，简报会长期空着新闻段。
# 因此这里区分两个概念：
#     - time_window.days  -> 价格等时序数据的取数窗口
#     - news_min_days      -> 新闻检索的最小回溯窗口
# 两者解耦，各自服务于「简报有内容」和「价格分析有意义」的不同需求。
NEWS_MIN_DAYS = 14

# 意图关键词 → 触发哪些工具
_NEWS_HINTS = re.compile(
    r"新闻|资讯|消息|报道|动态|news|update|报道|announcement|政策|policy|regulation", re.I
)
_PRICE_HINTS = re.compile(
    # 中文口语里「锂价」「铜价」是「锂 + 价」的省略结构，单独一个「价」字
    # 就代表价格意图，所以必须纳入；否则「昨天镍价」这类问句会漏判。
    r"价格|报价|行情|price|quote|多少钱|什么价|涨到|跌到|值多少|价位|how\s*much|当前价|最新价|价",
    re.I,
)
_TREND_HINTS = re.compile(r"趋势|走势|变化|涨跌|trend|movement|走势如何|变化情况", re.I)
_RESOURCE_HINTS = re.compile(r"储量|资源量|矿量|品位|resource|reserve|储量数据|报告|report", re.I)
_RISK_HINTS = re.compile(r"风险|risk|隐患|威胁|警示|不确定", re.I)
_BRIEFING_HINTS = re.compile(r"简报|日报|周报|briefing|report\s*card|汇总|综述", re.I)


def parse_request(text: str) -> MiningRequest:
    """解析自然语言请求为结构化执行计划。

    这是一个规则式的轻量实现（无 LLM 依赖）。
    理由：24h 交付里引入 LLM 会带来 key、网络、超时三重不确定性；
    规则方案行为可预测、易测试、可在面试中清楚解释。

    若后续要接入 LLM，只需替换本函数返回的 MiningRequest 即可，
    上层 orchestrator 无需改动 —— 这也是一层解耦。
    """
    req = MiningRequest(raw_query=text, want_news=False, want_resources=False,
                        want_price=False, want_trend=False)

    lowered = text.lower()

    # --- 1. 矿种 ---
    for commodity, pattern in _COMMODITY_PATTERNS:
        if re.search(pattern, lowered, re.I):
            req.commodity = commodity
            req.matched_keywords.append(commodity.value)
            break

    # --- 2. 地域 ---
    for region, pattern in _REGION_PATTERNS:
        if re.search(pattern, lowered, re.I):
            req.region = region
            req.matched_keywords.append(region)
            break

    # --- 3. 时间窗 ---
    for window, days, pattern in _TIME_PATTERNS:
        if re.search(pattern, lowered, re.I):
            req.time_window = window
            req.days = days
            break

    # --- 4. 工具路由 ---
    has_news = bool(_NEWS_HINTS.search(text))
    has_price = bool(_PRICE_HINTS.search(text))
    has_trend = bool(_TREND_HINTS.search(text))
    has_resource = bool(_RESOURCE_HINTS.search(text))
    want_briefing = bool(_BRIEFING_HINTS.search(text))
    risk_focus = bool(_RISK_HINTS.search(text))

    # 明确提到风险 → 必须有趋势数据，否则风险判断没有依据
    if risk_focus and not (has_price or has_trend):
        has_trend = True

    # 要简报 → 通常三类信息都要，除非用户明确限定
    if want_briefing and not (has_news or has_price or has_resource or has_trend):
        has_news = has_price = has_resource = has_trend = True

    req.want_news = has_news
    req.want_resources = has_resource
    req.want_price = has_price
    req.want_trend = has_trend or (has_price and not has_trend and want_briefing)
    req.want_briefing = want_briefing or (has_news or has_resource or has_price or has_trend)
    req.risk_focus = risk_focus

    # 价格时序分析至少需要 30 天，否则「趋势」无从谈起
    req.days = max(req.days, 30) if (has_trend or risk_focus) else req.days

    # 新闻检索窗口取 max(用户时间窗, 下限)：
    # 矿产新闻低频，窗口过窄会导致召回为空，简报缺段。
    req.news_days = max(req.days, NEWS_MIN_DAYS)

    # 「风险」但没指定矿种 → 默认给锂（示例题域），并在 explain 中体现
    if req.commodity is None and (want_briefing or risk_focus):
        req.commodity = Commodity.LITHIUM

    return req