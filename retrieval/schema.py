"""
法条检索层数据结构。

ProvisionQuery 由 v0.2 ClaimDocument 展开而来（每个 法源×条 一条查询），
ProvisionResult 是路由器的最终产出，供下一阶段比对模块消费。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class RetrievalStatus(str, Enum):
    """
    检索结论。

    FOUND: 拿到目标条文原文
    LAW_FOUND_NO_ARTICLE: 引注只到法规级（无条款号），法规已确认存在
    ARTICLE_NOT_FOUND: 法规在本地库内但条号不存在——本地库权威且完整，
        这是"引注条号错误"的强信号，路由器不再下探外部源
    NOT_FOUND: 所有数据源均未命中
    """
    FOUND = "found"
    LAW_FOUND_NO_ARTICLE = "law_found_no_article"
    ARTICLE_NOT_FOUND = "article_not_found"
    NOT_FOUND = "not_found"


class ProvisionQuery(BaseModel):
    """一条法条检索请求。"""
    law_title: str = Field(description="引注中的法规名（不含书名号）")
    source_type: Optional[str] = Field(
        default=None,
        description="v0.2 推断的规范类型（law/judicial_interpretation/...）",
    )
    article_label: Optional[str] = Field(
        default=None, description="条号标签，如'第一百八十四条之一'；法规级引注为 None"
    )
    paragraph_labels: list[str] = Field(
        default_factory=list, description="款号标签，如['第二款']"
    )
    item_labels: list[str] = Field(
        default_factory=list, description="项号标签，如['第（三）项']"
    )
    quote_text: Optional[str] = Field(
        default=None,
        description="引注附带的转述/引用文本（v0.2 paraphrase_text），用于全文反查兜底",
    )
    claim_id: Optional[str] = Field(default=None, description="来源 claim，溯源用")


class ProvisionEvidence(BaseModel):
    """一份条文证据（某一层数据源的返回）。"""
    provider: str = Field(description="local / gov_search / pkulaw")
    law_title: str
    article_label: Optional[str] = None
    text: str = Field(default="", description="条文原文（全条）")
    clause_texts: list[str] = Field(
        default_factory=list,
        description="命中的款/项原文（引注到款项且本地库可细取时填写）",
    )
    section_path: Optional[str] = None
    source_url: Optional[str] = None
    law_status: Optional[str] = Field(
        default=None, description="法规时效性（本地库为 laws.status）"
    )
    retrieved_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    note: Optional[str] = Field(
        default=None, description="附加说明，如'经全文反查定位'"
    )


class SuggestedArticle(BaseModel):
    """条号未命中时的候选建议（本地库 FTS 反查产物）。"""
    law_title: str
    article_label: str
    text: str
    score: float = Field(description="bm25 得分，越小越相关")


class ProvisionResult(BaseModel):
    """路由器最终产出。"""
    query: ProvisionQuery
    status: RetrievalStatus
    evidence: Optional[ProvisionEvidence] = None
    suggestions: list[SuggestedArticle] = Field(
        default_factory=list,
        description="ARTICLE_NOT_FOUND 时按 quote_text 反查出的候选条文",
    )
    providers_tried: list[str] = Field(default_factory=list)
    errors: list[str] = Field(
        default_factory=list,
        description="各数据源的失败记录（不中断路由，仅供诊断）",
    )
