"""公共数据模型。

所有 MCP server 与 Agent 编排层共用这里的结构，保证跨模块的数据契约一致。
使用 pydantic v2 做运行时校验，避免脏数据流入下游。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_serializer, field_validator


class DataOrigin(str, Enum):
    """数据来源标记。

    这个字段存在的意义是「诚实」：任何一条数据都能自报家门，
    便于在简报中区分实时数据与本地样本，避免误导下游使用者。
    """

    REMOTE = "remote"      # 实时抓取/远端接口
    SAMPLE = "sample"      # 本地样本库（离线可用）
    DEGRADED = "degraded"  # 降级：远端不可用，回落到样本或空值


# --------------------------------------------------------------------------
# News
# --------------------------------------------------------------------------

class NewsArticle(BaseModel):
    """单篇新闻。"""

    title: str = Field(..., description="新闻标题")
    url: str = Field(..., description="原文链接，用于引用溯源")
    source: str = Field(..., description="来源媒体，如 mining.com")
    published_at: str = Field(..., description="发布日期，ISO8601 格式")
    summary: str = Field(default="", description="摘要正文")
    matched_keywords: list[str] = Field(default_factory=list, description="命中的检索关键词")
    origin: DataOrigin = DataOrigin.SAMPLE

    @field_serializer("origin")
    def _serialize_origin(self, v: DataOrigin) -> str:
        """枚举转字符串。

        MCP 工具的返回值会被序列化成 JSON 传给客户端，
        若不显式转换，StrEnum 成员会变成 "DataOrigin.SAMPLE"
        这种 Python 内部表示，下游解析会出错。
        """
        return v.value

    @field_validator("url")
    @classmethod
    def url_must_be_http(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("url 必须以 http:// 或 https:// 开头")
        return v


# --------------------------------------------------------------------------
# Mineral Resources (NI 43-101)
# --------------------------------------------------------------------------

class ResourceCategory(str, Enum):
    """NI 43-101 储量分类。"""

    MEASURED = "Measured"
    INDICATED = "Indicated"
    INFERRED = "Inferred"


class MineralResource(BaseModel):
    """一条储量记录。

    对应 NI 43-101 技术报告中储量表的一行。
    """

    category: ResourceCategory = Field(..., description="储量分类")
    ore_tonnage: Optional[float] = Field(None, description="矿石量，单位 Mt")
    grade: Optional[float] = Field(None, description="品位，g/t Au 或 % Cu")
    grade_unit: Optional[str] = Field(None, description="品位单位")
    contained_metal: Optional[float] = Field(None, description="金属量，单位 t 或 oz")
    metal_unit: Optional[str] = Field(None, description="金属量单位")
    commodity: Optional[str] = Field(None, description="矿种，如 Lithium / Copper")
    page: Optional[int] = Field(None, description="所在页码")
    source_url: str = Field(..., description="PDF 来源链接，用于引用溯源")

    # 置信度：抽取失败时诚实标记，而不是瞎填数字。
    # 「系统知道自己不知道」比「假装知道」更重要。
    confidence: str = Field(
        default="high",
        description="抽取置信度 high / medium / low",
    )
    origin: DataOrigin = DataOrigin.SAMPLE

    @field_serializer("origin")
    def _serialize_origin(self, v: DataOrigin) -> str:
        return v.value


# --------------------------------------------------------------------------
# Price
# --------------------------------------------------------------------------

class PricePoint(BaseModel):
    """某个交易日的价格点。"""

    commodity: str
    date: str = Field(..., description="交易日，YYYY-MM-DD")
    price: float = Field(..., description="价格")
    unit: str = Field(..., description="价格单位，如 USD/t")
    origin: DataOrigin = DataOrigin.SAMPLE
    source_url: Optional[str] = None

    @field_serializer("origin")
    def _serialize_origin(self, v: DataOrigin) -> str:
        return v.value


class PriceTrend(BaseModel):
    """价格趋势汇总。"""

    commodity: str
    unit: str
    days: int
    start_price: Optional[float] = None
    end_price: Optional[float] = None
    change_pct: Optional[float] = Field(None, description="区间涨跌幅，百分比")
    series: list[PricePoint] = Field(default_factory=list)
    origin: DataOrigin = DataOrigin.SAMPLE
    warning: Optional[str] = Field(
        default=None,
        description="数据不完整或降级时的说明，供简报如实告知用户",
    )

    @field_serializer("origin")
    def _serialize_origin(self, v: DataOrigin) -> str:
        return v.value