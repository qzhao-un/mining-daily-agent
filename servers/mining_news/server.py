"""mining-news-mcp：矿业新闻聚合 MCP Server。

题目要求的两个工具：
    - search(query, days)   按关键词检索近 N 天新闻
    - fetch_article(url)    取单篇文章详情

设计要点：
    1. 业务逻辑不写在 tool 函数里，而是下沉到 NewsProvider。
       这样换数据源（本地样本 / 远端 RSS）不需要改 MCP 契约。
    2. 所有异常都在 tool 层转为结构化返回值，而不是抛出去让 server 崩。
       原因：server 崩溃会导致 Agent 的降级逻辑失效。
    3. 日志一律走 stderr —— stdio 传输下 stdout 被 JSON-RPC 占用。
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from models import DataOrigin, NewsArticle
from utils import get_logger

logger = get_logger("mining-news-mcp")

# 项目内共享的数据目录（server.py 位于 servers/<name>/server.py，需上溯三层到项目根）
DATA_DIR = Path(__file__).resolve().parents[2] / "data"

mcp = FastMCP("mining-news-mcp")


# ==========================================================================
# Provider 层：数据来源抽象
# ==========================================================================

class NewsProvider(ABC):
    """新闻数据源接口。

    存在的意义：真实生产里新闻可能来自 RSS、付费 API 或内部爬虫，
    这些都会变。MCP 工具的契约不应该跟着数据源变。
    """

    @abstractmethod
    def search(self, query: str, days: int) -> list[NewsArticle]:
        """返回匹配关键词且在时间窗内的文章。"""

    @abstractmethod
    def fetch_article(self, url: str) -> Optional[NewsArticle]:
        """按 URL 返回单篇文章详情。"""


class SampleNewsProvider(NewsProvider):
    """本地样本数据源。

    保证在无网络、无凭证的环境下工具依然可用且可验证，
    这是让「5 分钟跑起来」成立的前提。
    """

    def __init__(self, data_file: Path | None = None) -> None:
        path = data_file or (DATA_DIR / "news_samples.json")
        self._file = path
        self._articles: list[NewsArticle] = self._load()

    def _load(self) -> list[NewsArticle]:
        if not self._file.exists():
            logger.warning("新闻样本文件不存在: %s", self._file)
            return []
        raw = json.loads(self._file.read_text(encoding="utf-8"))
        out: list[NewsArticle] = []
        for item in raw.get("articles", []):
            try:
                out.append(NewsArticle(**item, origin=DataOrigin.SAMPLE))
            except Exception as exc:  # 单条脏数据不应拖垮整个文件
                logger.warning("跳过无法解析的新闻条目: %s", exc)
        return out

    @property
    def articles(self) -> list[NewsArticle]:
        return self._articles

    def search(self, query: str, days: int) -> list[NewsArticle]:
        keywords = [k for k in re.split(r"[,\s]+", query.lower()) if k]
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        matched: list[NewsArticle] = []
        for art in self._articles:
            try:
                published = datetime.fromisoformat(art.published_at).replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                published = cutoff  # 日期不可解析时不做时间过滤，只做关键词匹配
            if published < cutoff:
                continue

            haystack = f"{art.title} {art.summary}".lower()
            hit = [k for k in keywords if k in haystack]
            # 所有关键词都没命中才跳过；部分命中即收录（AND 太严会让召回为空）
            if keywords and not hit:
                continue
            matched.append(art.model_copy(update={"matched_keywords": hit}))

        # 越新的排前面
        return sorted(matched, key=lambda a: a.published_at, reverse=True)

    def fetch_article(self, url: str) -> Optional[NewsArticle]:
        return next((a for a in self._articles if a.url == url), None)


class RemoteRSSNewsProvider(NewsProvider):
    """远端 RSS 数据源（可选启用）。

    这里刻意保持「失败即抛异常，由上层决定是否降级」，
    而不是静默返回空 —— 静默失败会让人误以为真的没有新闻。
    """

    DEFAULT_FEEDS = [
        "https://www.mining.com/feed/",
        "https://www.spglobal.com/commodityinsights/en/rss/latest-news",
    ]

    def __init__(self, feeds: list[str] | None = None, timeout: int = 8) -> None:
        self._feeds = feeds or self.DEFAULT_FEEDS
        self._timeout = timeout
        self._fallback = SampleNewsProvider()

    def _fetch_feed(self, url: str) -> list[NewsArticle]:
        req = urllib.request.Request(url, headers={"User-Agent": "mining-rights-agent/1.0"})
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")

        articles: list[NewsArticle] = []
        for block in re.findall(r"<item>(.*?)</item>", body, re.S):
            def _tag(name: str) -> str:
                m = re.search(rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", block, re.S)
                return m.group(1).strip() if m else ""

            link = _tag("link")
            if not link:
                continue
            try:
                articles.append(
                    NewsArticle(
                        title=_tag("title"),
                        url=link,
                        source=urllib.parse.urlparse(link).netloc or "unknown",
                        published_at=_tag("pubDate") or datetime.now(timezone.utc).isoformat(),
                        summary=re.sub(r"<[^>]+>", "", _tag("description"))[:400],
                        origin=DataOrigin.REMOTE,
                    )
                )
            except Exception as exc:
                logger.warning("RSS 条目解析失败: %s", exc)
        return articles

    def search(self, query: str, days: int) -> list[NewsArticle]:
        try:
            pool: list[NewsArticle] = []
            for feed in self._feeds:
                pool.extend(self._fetch_feed(feed))
        except Exception as exc:
            logger.warning("远端 RSS 不可用，回落到样本: %s", exc)
            return self._fallback.search(query, days)

        keywords = [k for k in re.split(r"[,\s]+", query.lower()) if k]
        out = []
        for art in pool:
            haystack = f"{art.title} {art.summary}".lower()
            hit = [k for k in keywords if k in haystack]
            if keywords and not hit:
                continue
            out.append(art.model_copy(update={"matched_keywords": hit}))
        return out

    def fetch_article(self, url: str) -> Optional[NewsArticle]:
        for feed in self._feeds:
            try:
                for art in self._fetch_feed(feed):
                    if art.url == url:
                        return art
            except Exception:
                continue
        return self._fallback.fetch_article(url)


# ==========================================================================
# Provider 选择
# ==========================================================================

def get_provider() -> NewsProvider:
    """根据环境变量选择数据源。

    默认使用样本库 —— 离线可跑是交付要求之一（5 分钟跑起来）。
    设置 MINING_NEWS_PROVIDER=remote 可切到实时 RSS。
    """
    import os

    choice = os.getenv("MINING_NEWS_PROVIDER", "sample").lower()
    if choice == "remote":
        logger.info("使用远端 RSS 数据源")
        return RemoteRSSNewsProvider()
    return SampleNewsProvider()


# ==========================================================================
# MCP Tools
# ==========================================================================

@mcp.tool()
def search(query: str, days: int = 7) -> dict:
    """按关键词检索矿业新闻。

    Args:
        query: 检索关键词，空格或逗号分隔。例如 "Pilbara lithium"。
        days: 回溯天数窗口，默认 7 天。

    Returns:
        dict: 包含 articles 列表（字段 title / url / source / published_at / summary）
        与 meta（命中的关键词、数据来源 origin、条数）。
        无结果时 articles 为空数组，但 meta 仍会说明来源，不静默失败。
    """
    try:
        provider = get_provider()
        articles = provider.search(query, days)
        origin = articles[0].origin if articles else DataOrigin.SAMPLE
        return {
            "articles": [a.model_dump() for a in articles],
            "meta": {
                "query": query,
                "days": days,
                "count": len(articles),
                "origin": origin.value,
                "source_note": "实时抓取" if origin == DataOrigin.REMOTE else "本地样本数据（非实时）",
            },
        }
    except Exception as exc:
        # 不抛异常：让 Agent 有机会降级，而不是整个 server 挂掉
        logger.error("search 失败: %s", exc)
        return {"articles": [], "meta": {"query": query, "error": str(exc), "origin": "degraded"}}


@mcp.tool()
def fetch_article(url: str) -> dict:
    """按 URL 获取单篇新闻详情。

    Args:
        url: 新闻原文链接，必须以 http:// 或 https:// 开头。

    Returns:
        dict: 包含 article 字段；找不到时返回 found=False 与说明，
        而不是抛异常 —— 「没找到」是正常业务结果，不是错误。
    """
    try:
        provider = get_provider()
        article = provider.fetch_article(url)
        if article is None:
            return {
                "found": False,
                "meta": {
                    "url": url,
                    "reason": "该 URL 不在当前数据源中。若数据源为本地样本，请改用 search 返回的 url。",
                },
            }
        return {"found": True, "article": article.model_dump()}
    except Exception as exc:
        logger.error("fetch_article 失败: %s", exc)
        return {"found": False, "meta": {"url": url, "error": str(exc), "origin": "degraded"}}


if __name__ == "__main__":
    # 注意：不能有 print() 写入 stdout，会污染 JSON-RPC 协议通道
    mcp.run(transport="stdio")