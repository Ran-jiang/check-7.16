"""法律溯源产生的证据与数据源轨迹模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class SourceTier(str, Enum):
    LOCAL_SQLITE = "local_sqlite"
    PKULAW_FALLBACK = "pkulaw_fallback"
    EURLEX = "eurlex"


class LookupStatus(str, Enum):
    ARTICLE_FOUND = "article_found"
    RELEVANT_ARTICLES_FOUND = "relevant_articles_found"
    LAW_FOUND_ARTICLE_MISSING = "law_found_article_missing"
    LAW_FOUND_TEXT_UNAVAILABLE = "law_found_text_unavailable"
    LAW_NOT_FOUND = "law_not_found"
    SOURCE_NOT_CONFIGURED = "source_not_configured"
    SOURCE_ERROR = "source_error"
    NOT_VERIFIABLE = "not_verifiable"
    OUT_OF_SCOPE = "out_of_scope"


class CaseLookupStatus(str, Enum):
    VERIFIED = "verified"
    NOT_FOUND = "not_found"
    MANUAL_REVIEW = "manual_review"
    SOURCE_NOT_CONFIGURED = "source_not_configured"
    SOURCE_ERROR = "source_error"
    OUT_OF_SCOPE = "out_of_scope"


class SourceTrace(BaseModel):
    tier: SourceTier
    source_name: str
    source_url: Optional[str] = None
    fetched_at: Optional[str] = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    status: LookupStatus
    message: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArticleExcerpt(BaseModel):
    article_no: str
    article_text: str
    relevance_score: float = Field(ge=0)


class ArticleEvidence(BaseModel):
    law_title: str
    source_type: str
    article_no: Optional[str] = None
    article_text: Optional[str] = None
    version_label: Optional[str] = None
    version_status: Optional[str] = None
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    related_articles: list[ArticleExcerpt] = Field(default_factory=list)
    structure_path: Optional[str] = None
    data_source: SourceTrace


class CaseEvidence(BaseModel):
    matched_text: str
    case_number: str
    gid: str
    court: str = ""
    title: str = ""
    last_instance_date: Optional[str] = None
    url: Optional[str] = None


class CaseSourceTrace(BaseModel):
    source_name: str
    source_url: Optional[str] = None
    fetched_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    status: CaseLookupStatus
    message: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "ArticleEvidence",
    "ArticleExcerpt",
    "CaseEvidence",
    "CaseLookupStatus",
    "CaseSourceTrace",
    "LookupStatus",
    "SourceTier",
    "SourceTrace",
]
