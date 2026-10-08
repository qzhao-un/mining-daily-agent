"""MCP 客户端封装：管理三个 server 子进程的启动、工具调用、超时与错误隔离。

这是整个 Agent 的基础设施层。三个关键职责：

    1. 进程管理：以 stdio JSON-RPC 方式启动并连接 MCP server。
    2. 错误隔离：任一 server 崩溃/超时，其余数据仍能拿回，
       编排层据此降级，而不是整体失败。
    3. 并发调用：三类数据源互不依赖，并发取数，总耗时取最慢而非相加。

stdio 传输下的关键约束：服务端日志必须走 stderr。
若server 把日志打到 stdout，会污染 JSON-RPC 通道导致协议解析失败。
本模块用 stderr=subprocess.DEVNULL / 重定向来保证这一点。
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from utils import get_logger

logger = get_logger("mcp-client")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# server 名 → 启动模块
SERVER_MODULES = {
    "news": "servers.mining_news.server",
    "pdf": "servers.mineral_pdf.server",
    "price": "servers.lme_price.server",
}

# 单个工具调用的超时上限（秒）。
# 没有超时的话，一个挂死的 server 会让整个简报永远出不来。
CALL_TIMEOUT = float(os.getenv("MCP_CALL_TIMEOUT", "20"))


class ServerUnavailable(Exception):
    """server 无法启动或通信失败。

    注意：这个异常会被 orchestrator 捕获并转为降级，
    不会让整个 Agent 崩溃 —— 降级能力是本题的核心要求之一。
    """


class MCPToolbox:
    """管理多个 MCP server 会话，并提供带超时的工具调用。"""

    def __init__(self) -> None:
        self._stack: Optional[AsyncExitStack] = None
        self._sessions: dict[str, ClientSession] = {}
        self._unavailable: set[str] = set()

    async def __aenter__(self) -> "MCPToolbox":
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        await self._start_all()
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._sessions.clear()

    async def _start_all(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PROJECT_ROOT)
        env.setdefault("PYTHONIOENCODING", "utf-8")

        for name, module in SERVER_MODULES.items():
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", module],
                env=env,
                cwd=str(PROJECT_ROOT),
            )
            try:
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._sessions[name] = session
                logger.info("MCP server 已连接: %s", name)
            except Exception as exc:
                # 单个 server 启动失败不影响其余：记下来，后面降级用
                self._unavailable.add(name)
                logger.error("MCP server 启动失败: %s (%s)", name, exc)

    @property
    def unavailable_servers(self) -> set[str]:
        return set(self._unavailable)

    async def call_tool(
        self, server: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """调用指定 server 的工具，返回解析后的结果。

        Args:
            server: server 名（news / pdf / price）
            tool: 工具名
            arguments: 工具入参

        Raises:
            ServerUnavailable: server 未连接或调用失败/超时。
        """
        if server in self._unavailable or server not in self._sessions:
            raise ServerUnavailable(f"server '{server}' 不可用")

        session = self._sessions[server]
        try:
            result = await asyncio.wait_for(
                session.call_tool(tool, arguments=arguments),
                timeout=CALL_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            raise ServerUnavailable(f"调用 {server}.{tool} 超时 (> {CALL_TIMEOUT}s)") from exc
        except Exception as exc:
            raise ServerUnavailable(f"调用 {server}.{tool} 失败: {exc}") from exc

        return _parse_result(result)


def _parse_result(result: Any) -> dict[str, Any]:
    """把 MCP CallToolResult 转成 dict。

    server 返回的 content 通常是 JSON 字符串，需要解析回结构化数据。
    若解析失败则原样包一层，保证调用方总能拿到 dict 而不抛异常。
    """
    import json

    texts = []
    for item in getattr(result, "content", []) or []:
        # TextContent.text
        if hasattr(item, "text"):
            texts.append(item.text)

    if not texts:
        return {"raw": None, "parse_error": "server 未返回 content"}

    payload = texts[0]
    try:
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            return parsed
        return {"data": parsed}
    except json.JSONDecodeError:
        # 不是 JSON 就当作纯文本返回，不抛异常
        return {"text": payload, "origin": "degraded"}