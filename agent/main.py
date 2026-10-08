"""矿权日报 Agent 主入口。

流程：
    用户输入
      → intent.parse_request()      解析实体与工具路由决策
      → workflow.MCPToolbox         启动三个 MCP server
      → 并发调用所需工具            三类数据源互不依赖，并发取数
      → 错误隔离                    任何 server 失败都降级，不中断
      → briefing.render_briefing()  渲染 Markdown 简报（含引用溯源）

与流水线的区别（面试高频问题）：
    流水线固定执行全部工具并按序拼接；本 Agent 先根据输入判断该调哪些。
    例如「今天锂价多少」只调用价格工具，不会去拉新闻和储量报告。
    路由决策来自 intent.py，且可通过 --explain 查看决策过程。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 确保项目根在 sys.path 中，使agent / servers / models 可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.briefing import render_briefing
from agent.intent import MiningRequest, parse_request
from agent.workflow import MCPToolbox, ServerUnavailable
from utils import get_logger

logger = get_logger("main")

# 矿种 → 可用的样本报告 key（PDF server 的输入）
_RESOURCE_KEYS = {
    "lithium": "pilbara_greenbushes",
    "copper": "pilbara_greenbushes",
    "nickel": "barrick_nickel",
    "zinc": "newmont_kamoa",
    "iron_ore": "newmont_kamoa",
}


async def gather(req: MiningRequest, explain: bool = False) -> tuple[dict, set[str]]:
    """按意图执行工具调用，返回 (各段数据, 不可用 server 集合)。"""
    collected: dict[str, dict] = {}
    unavailable: set[str] = set()

    commodity = req.commodity.value if req.commodity else "lithium"
    region = req.region or ""

    # 组装要并发的调用任务：(数据段标签, server 名, 工具名, 工具入参)
    # 这里是同步构造任务描述，不涉及IO；真正的并发在下面的 run_one 里。
    commodity = req.commodity.value if req.commodity else "lithium"
    region = req.region or ""

    tasks: list[tuple[str, str, str, dict]] = []

    if req.want_news:
        query = " ".join(filter(None, [req.region, req.commodity.value if req.commodity else ""]))
        tasks.append(("news", "news", "search", {"query": query or "mining", "days": req.news_days}))

    if req.want_resources:
        key = _RESOURCE_KEYS.get(commodity, "pilbara_greenbushes")
        tasks.append(("resources", "pdf", "extract_resources", {"pdf_url": key}))

    if req.want_price:
        tasks.append(
            ("price", "price", "get_price",
             {"commodity": commodity, "date": _latest_trading_day()})
        )

    if req.want_trend:
        tasks.append(
            ("price_trend", "price", "get_trend",
             {"commodity": commodity, "days": max(req.days, 30)})
        )

    if not tasks:
        return collected, unavailable

    async with MCPToolbox() as toolbox:
        unavailable |= toolbox.unavailable_servers

        async def run_one(label: str, server: str, tool: str, args: dict):
            try:
                result = await toolbox.call_tool(server, tool, args)
                return label, result, None
            except ServerUnavailable as exc:
                return label, None, exc

        # 并发执行：三类数据源互不依赖，总耗时取最慢而非相加
        results = await asyncio.gather(*(run_one(*t) for t in tasks))

        for label, result, err in results:
            if err:
                logger.warning("%s 调用失败: %s", label, err)
                # 标记对应 server 不可用，让渲染层如实标注降级
                server_name = next((s for l, s, _, _ in tasks if l == label), "")
                unavailable.add(server_name or "unknown")
                continue

            if label == "news":
                collected["news"] = result
            elif label == "resources":
                collected["resources"] = result
            elif label == "price":
                collected.setdefault("price", {})
                collected["price"]["point"] = result
            elif label == "price_trend":
                collected.setdefault("price", {})
                collected["price"].update(result)

        if explain:
            print("\n[Agent] 路由决策:", file=sys.stderr)
            print(req.explain(), file=sys.stderr)

    return collected, unavailable


def _latest_trading_day() -> str:
    """取最近的工作日作为查询日期（周末无数据）。"""
    from datetime import date, timedelta

    d = date.today()
    while d.weekday() >= 5:  # 5=周六6=周日
        d -= timedelta(days=1)
    return d.isoformat()


async def run_briefing(user_query: str, explain: bool = False) -> str:
    """端到端生成简报。"""
    req = parse_request(user_query)
    data, unavailable = await gather(req, explain=explain)
    return render_briefing(req, data, unavailable)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="矿权日报 Agent —— 基于 MCP 协议的多源矿权信息汇总"
    )
    parser.add_argument(
        "query",
        nargs="?",
        default="给我生成一份关于 Pilbara 锂矿的今日简报",
        help="自然语言查询，例如「给我生成一份关于 Pilbara 锂矿的今日简报」",
    )
    parser.add_argument("--explain", action="store_true", help="打印 Agent 路由决策过程")
    parser.add_argument("-o", "--output", help="输出到文件而非 stdout")
    args = parser.parse_args()

    markdown = asyncio.run(run_briefing(args.query, explain=args.explain))

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown, encoding="utf-8")
        print(f"[OK] 简报已保存: {out}")
    else:
        print(markdown)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())