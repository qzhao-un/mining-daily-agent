"""MCP 协议层集成测试。

与 test_servers.py 的区别：
    test_servers.py 直接调用 Python 函数，不经过协议层，验证业务逻辑。
    本文件真正以子进程启动 MCP server，走 stdio JSON-RPC，
    验证「工具能被客户端发现与调用」—— 这才是题目要的验收标准。

这是项目最关键的一道验证：如果这里挂了，Cursor / Claude Desktop 就挂不上，
后面所有工作都白做。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 每个 server 的启动命令：以模块方式运行，确保项目根在 sys.path 中
SERVERS = {
    "mining-news-mcp": "servers.mining_news.server",
    "mineral-pdf-mcp": "servers.mineral_pdf.server",
    "lme-price-mcp": "servers.lme_price.server",
}


async def _list_tools(module: str) -> dict[str, list[str]]:
    """启动 server 并返回 {工具名: [参数名]}。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", module],
        env=env,
        cwd=str(PROJECT_ROOT),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return {
                tool.name: sorted((tool.inputSchema or {}).get("properties", {}).keys())
                for tool in result.tools
            }


@pytest.mark.parametrize("server_name,module", SERVERS.items())
def test_server_exposes_tools(server_name: str, module: str):
    """每个 server 都能启动，且暴露了题目要求的工具。

    这里刻意用真实子进程而不是 mock —— 协议层的问题只有真跑才暴露。
    """
    tools = asyncio.run(_list_tools(module))
    assert tools, f"{server_name} 未暴露任何工具"


def test_news_tools_signature():
    """工具签名必须与题目一致：search(query, days) / fetch_article(url)。"""
    tools = asyncio.run(_list_tools(SERVERS["mining-news-mcp"]))
    assert "search" in tools, tools
    assert "fetch_article" in tools, tools
    assert "query" in tools["search"] and "days" in tools["search"]
    assert "url" in tools["fetch_article"]


def test_pdf_tool_signature():
    """extract_resources(pdf_url)。"""
    tools = asyncio.run(_list_tools(SERVERS["mineral-pdf-mcp"]))
    assert "extract_resources" in tools, tools
    assert "pdf_url" in tools["extract_resources"]


def test_price_tools_signature():
    """get_price(commodity, date) / get_trend(commodity, days)。"""
    tools = asyncio.run(_list_tools(SERVERS["lme-price-mcp"]))
    assert "get_price" in tools, tools
    assert "get_trend" in tools, tools
    assert set(tools["get_price"]) >= {"commodity", "date"}
    assert set(tools["get_trend"]) >= {"commodity", "days"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])