"""法规核验专用结果模型。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from .citation import SourceLocation
from .evidence import ArticleEvidence, LookupStatus, SourceTrace
from .checks import CheckVerdict, ExecutionStatus
from .revisions import RevisionProposal


class StatuteErrorCode(str, Enum):
    SOURCE_NOT_FOUND = "source_not_found"
    CITATION_LOCATION_ERROR = "citation_location_error"
    SOURCE_REPEALED = "source_repealed"
    SOURCE_AMENDED = "source_amended"
    MEANING_DISTORTED = "meaning_distorted"


class StatuteLocator(BaseModel):
    article_no: str | None = None
    paragraph_no: str | None = None
    item_no: str | None = None


class StatuteVersion(BaseModel):
    version_key: str
    version_label: str | None = None
    version_status: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    article_no: str
    article_text: str


class StructuredItem(BaseModel):
    item_no: str
    text: str


class StructuredParagraph(BaseModel):
    paragraph_no: str
    text: str
    items: list[StructuredItem] = Field(default_factory=list)


class StructuredArticle(BaseModel):
    article_no: str
    raw_text: str
    paragraph_boundaries_reliable: bool
    paragraphs: list[StructuredParagraph] = Field(min_length=1)


class StatuteLocationCandidate(BaseModel):
    locator: StatuteLocator
    text: str
    source_url: str | None = None


class StatuteLocationResolution(BaseModel):
    status: Literal["resolved", "candidates_pending", "not_found"]
    candidates: list[StatuteLocationCandidate] = Field(default_factory=list)
    source_trace: SourceTrace | None = None


class StatuteFinding(BaseModel):
    code: StatuteErrorCode
    risk_level: Literal["HIGH", "MEDIUM"]
    summary: str = Field(max_length=300)
    suggestion: str
    cited_locator: StatuteLocator | None = None
    resolved_locator: StatuteLocator | None = None
    historical_version: StatuteVersion | None = None
    location_recheck_required: bool = False
    revision: RevisionProposal | None = None


class StatuteMeaningCheck(BaseModel):
    execution_status: ExecutionStatus = ExecutionStatus.COMPLETED
    verdict: CheckVerdict | None = None
    findings: list[StatuteFinding] = Field(default_factory=list)
    notes: str = ""
    error_code: str | None = None
    retryable: bool = False
    skipped_reason: str | None = None
    job_id: str | None = None


class StatuteVerificationResult(BaseModel):
    check_id: str
    card_id: str
    claim_id: str
    claim_text: str
    law_title: str
    jurisdiction: str = "CN"
    document_quote: str = ""
    cited_locators: list[StatuteLocator] = Field(default_factory=list)
    lookup_status: LookupStatus
    evidence: ArticleEvidence | None = None
    findings: list[StatuteFinding] = Field(default_factory=list)
    outcome: Literal["pass", "issue", "bug"]
    message: str = ""
    meaning_check: StatuteMeaningCheck | None = None
    reference_role: Literal["direct", "nested", "inherited"] = "direct"
    source_locations: list[SourceLocation] = Field(default_factory=list)
    source_attempts: list[SourceTrace] = Field(default_factory=list)


__all__ = [
    "StatuteErrorCode",
    "StatuteFinding",
    "StatuteLocator",
    "StatuteLocationCandidate",
    "StatuteLocationResolution",
    "StatuteMeaningCheck",
    "StatuteVersion",
    "StructuredArticle",
    "StructuredItem",
    "StructuredParagraph",
    "StatuteVerificationResult",
]
