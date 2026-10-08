"""日志与通用错误类型。

关键约束：MCP server 走 stdio 传输，而 stdio 的 stdout 被 JSON-RPC 协议占用。
任何 print() / stdout 写入都会污染协议通道，导致客户端解析失败。
因此本项目所有日志一律走 stderr。
"""

from __future__ import annotations

import logging
import sys


def get_logger(name: str) -> logging.Logger:
    """返回配置好 stderr 输出的 logger。

    强制 stream=sys.stderr，绝不写 stdout —— 这是 MCP stdio server 的硬性要求。
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        # MCP server 上下日志量大，默认 WARNING，避免噪音干扰协议层。
        logger.setLevel(logging.WARNING)
    return logger


class MiningAgentError(Exception):
    """项目内通用错误基类。"""


class DataSourceError(MiningAgentError):
    """数据源不可用（网络、凭证、格式等）。

    注意：本异常应被server 层捕获并转为非致命返回值，
    不应让整个 MCP server 崩溃 —— 那会让 Agent 的降级逻辑失效。
    """


class ResourceExtractionError(MiningAgentError):
    """PDF 储量抽取失败。

    抛这个异常时必须携带部分结果，而不是全部丢弃，
    便于 Agent 判断哪些段落可信、哪些需要标注。
    """

    def __init__(self, message: str, partial: list | None = None):
        super().__init__(message)
        self.partial = partial or []


class InvalidCommodityError(MiningAgentError):
    """不支持的矿种。"""