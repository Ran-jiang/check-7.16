"""核查判定及面向前端的结果模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from .citation import SourceLocation

from .evidence import (
    ArticleEvidence,
    ArticleExcerpt,
    CaseEvidence,
    CaseLookupStatus,
    CaseSourceTrace,
    LookupStatus,
    SourceTier,
    SourceTrace,
)


class ComparisonVerdict(str, Enum):
    PASS = "pass"
    ISSUE = "issue"
    INSUFFICIENT_INPUT = "insufficient_input"


class SemanticExecutionStatus(str, Enum):
    COMPLETED = "completed"
    LLM_ERROR = "llm_error"
    SKIPPED = "skipped"


class RiskLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"


class SemanticErrorType(str, Enum):
    SOURCE_NOT_FOUND = "法律渊源不存在"
    LOCATION_ERROR = "条款编号或引用定位错误"
    OUTDATED_SOURCE = "法源已废止或失效"
    MEANING_DISTORTED = "曲解权威文本原意"
    NO_SUBSTANTIVE_MATCH = "引用内容与权威文本无实质对应"


class SemanticIssue(BaseModel):
    error_type: SemanticErrorType
    risk_level: RiskLevel
    diff_summary: str = Field(max_length=300)
    suggestion: str
    auto_fixable: Literal[False] = False


_RETRYABLE_ERROR_CODES = {
    "transport_error",
    "timeout",
    "rate_limited",
    "upstream_error",
}


class SemanticCheckResult(BaseModel):
    execution_status: SemanticExecutionStatus = SemanticExecutionStatus.COMPLETED
    verdict: ComparisonVerdict | None = None
    issues: list[SemanticIssue] = Field(default_factory=list)
    notes: str = ""
    error_code: str | None = None
    retryable: bool = False
    skipped_reason: str | None = None
    semantic_job_id: str | None = None

    @model_validator(mode="after")
    def validate_verdict_shape(self) -> "SemanticCheckResult":
        self.retryable = self.error_code in _RETRYABLE_ERROR_CODES
        if self.execution_status == SemanticExecutionStatus.COMPLETED:
            if self.verdict is None:
                raise ValueError("completed semantic check requires verdict")
        elif self.verdict is not None:
            raise ValueError("llm_error/skipped semantic check cannot have verdict")
        if self.verdict == ComparisonVerdict.PASS:
            self.issues = []
            if "打捞轮恢复" not in self.notes:
                self.notes = ""
        elif self.verdict == ComparisonVerdict.ISSUE:
            if not self.issues:
                raise ValueError("issue requires issues")
        elif self.verdict == ComparisonVerdict.INSUFFICIENT_INPUT and not self.notes:
            raise ValueError("insufficient_input requires notes")
        if self.execution_status != SemanticExecutionStatus.COMPLETED and self.issues:
            raise ValueError("non-completed semantic check cannot have issues")
        return self


# 兼容一个小版本：字段名不变，对外角色已由 0.3 状态模型取代。
SemanticComparison = SemanticCheckResult


class CitationReferenceCheck(BaseModel):
    check_id: str
    cited_text: str
    law_title: str
    article_no: Optional[str] = None
    paragraphs: list[str] = Field(default_factory=list)
    items: list[str] = Field(default_factory=list)
    reference_role: Literal["direct", "nested", "inherited"] = "direct"
    mention_span: tuple[int, int] | None = None
    citation_span: tuple[int, int] | None = None
    quote_span: tuple[int, int] | None = None
    lookup_status: LookupStatus
    evidence: Optional[ArticleEvidence] = None
    rule_findings: list[SemanticIssue] = Field(default_factory=list)
    semantic_comparison: Optional[SemanticComparison] = None
    verification_scope: Literal["full", "existence_only"] = "full"
    source_attempts: list[SourceTrace] = Field(default_factory=list)


class CitationCard(BaseModel):
    card_id: str
    claim_id: str
    claim_text: str
    anchor_ids: list[str] = Field(default_factory=list)
    source_locations: list[SourceLocation] = Field(default_factory=list)
    references: list[CitationReferenceCheck] = Field(min_length=1)


class CaseCheck(BaseModel):
    check_id: str
    claim_id: str
    claim_text: str
    anchor_ids: list[str] = Field(default_factory=list)
    cited_case_number: Optional[str] = None
    cited_case_name: Optional[str] = None
    lookup_status: CaseLookupStatus
    evidence: Optional[CaseEvidence] = None
    message: str = ""
    source_attempts: list[CaseSourceTrace] = Field(default_factory=list)
    source_locations: list[SourceLocation] = Field(default_factory=list)


class FrontendVerificationDocument(BaseModel):
    schema_version: str = "0.4"
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source_claim_doc_id: str
    citation_cards: list[CitationCard] = Field(default_factory=list)
    case_checks: list[CaseCheck] = Field(default_factory=list)
