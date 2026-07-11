"""Frontend-facing verification schema."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class SourceTier(str, Enum):
    LOCAL_SQLITE = "local_sqlite"
    OFFICIAL_SOURCE = "official_source"
    PKULAW_FALLBACK = "pkulaw_fallback"


class LookupStatus(str, Enum):
    ARTICLE_FOUND = "article_found"
    RELEVANT_ARTICLES_FOUND = "relevant_articles_found"
    LAW_FOUND_ARTICLE_MISSING = "law_found_article_missing"
    LAW_FOUND_TEXT_UNAVAILABLE = "law_found_text_unavailable"
    LAW_NOT_FOUND = "law_not_found"
    SOURCE_NOT_CONFIGURED = "source_not_configured"
    SOURCE_ERROR = "source_error"
    NOT_VERIFIABLE = "not_verifiable"


class CaseLookupStatus(str, Enum):
    VERIFIED = "verified"
    NOT_FOUND = "not_found"
    SOURCE_NOT_CONFIGURED = "source_not_configured"
    SOURCE_ERROR = "source_error"


class SemanticStatus(str, Enum):
    SUPPORTED = "supported"
    NOT_SUPPORTED = "not_supported"
    CONDITIONAL_OR_INCOMPLETE = "conditional_or_incomplete"
    INSUFFICIENT_CONTEXT = "insufficient_context"
    ERROR = "error"


class ComparisonVerdict(str, Enum):
    PASS = "pass"
    ISSUE = "issue"
    BUG = "bug"


class ComparisonConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RiskLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"


class SemanticErrorType(str, Enum):
    SOURCE_NOT_FOUND = "法律渊源不存在"
    LOCATION_ERROR = "条款编号或引用定位错误"
    OUTDATED_SOURCE = "旧法旧规误用"
    MEANING_DISTORTED = "曲解权威文本原意"
    NO_SUBSTANTIVE_MATCH = "引用内容与权威文本无实质对应"
    CONCLUSION_NOT_NECESSARILY_SUPPORTED = "法条不足以必然支持文书结论"


class SemanticIssue(BaseModel):
    error_type: SemanticErrorType
    risk_level: RiskLevel
    diff_summary: str = Field(max_length=80)
    suggestion: str
    auto_fixable: Literal[False] = False


class SemanticComparison(BaseModel):
    verdict: ComparisonVerdict
    issues: list[SemanticIssue] = Field(default_factory=list)
    confidence: Optional[ComparisonConfidence] = None
    notes: Optional[str] = None

    @model_validator(mode="after")
    def validate_verdict_shape(self) -> "SemanticComparison":
        if self.verdict == ComparisonVerdict.PASS:
            if self.issues or self.confidence is not None or self.notes is not None:
                raise ValueError("pass must contain only verdict")
        elif self.verdict == ComparisonVerdict.ISSUE:
            if not self.issues or self.confidence is None:
                raise ValueError("issue requires issues and confidence")
        elif self.issues or self.confidence != ComparisonConfidence.LOW or not self.notes:
            raise ValueError("bug requires empty issues, low confidence, and notes")
        return self


class SourceTrace(BaseModel):
    tier: SourceTier
    source_name: str
    source_url: Optional[str] = None
    fetched_at: Optional[str] = None
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
    data_source: SourceTrace


class SemanticAssessment(BaseModel):
    status: SemanticStatus
    conclusion: str
    required_elements: list[str] = Field(default_factory=list)
    satisfied_elements: list[str] = Field(default_factory=list)
    missing_or_uncertain_elements: list[str] = Field(default_factory=list)
    reasoning_summary: str = ""
    confidence: float = Field(ge=0, le=1)


class LegalCheck(BaseModel):
    check_id: str
    claim_id: str
    claim_text: str
    anchor_ids: list[str] = Field(default_factory=list)
    law_title: str
    article_no: Optional[str] = None
    lookup_status: LookupStatus
    evidence: Optional[ArticleEvidence] = None
    rule_findings: list[SemanticIssue] = Field(default_factory=list)
    semantic_comparison: Optional[SemanticComparison] = None
    semantic_assessment: Optional[SemanticAssessment] = None
    source_attempts: list[SourceTrace] = Field(default_factory=list)


class CaseEvidence(BaseModel):
    matched_text: str
    case_number: str
    gid: str
    court: str = ""
    title: str = ""
    last_instance_date: Optional[str] = None
    url: Optional[str] = None


class CaseCheck(BaseModel):
    check_id: str
    claim_id: str
    claim_text: str
    anchor_ids: list[str] = Field(default_factory=list)
    cited_case_number: str
    lookup_status: CaseLookupStatus
    evidence: Optional[CaseEvidence] = None
    message: str = ""


class FrontendVerificationDocument(BaseModel):
    schema_version: str = "0.2"
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source_claim_doc_id: str
    legal_checks: list[LegalCheck] = Field(default_factory=list)
    case_checks: list[CaseCheck] = Field(default_factory=list)
