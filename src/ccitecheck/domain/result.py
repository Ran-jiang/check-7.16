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
    BUG = "bug"


class RiskLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"


class SemanticErrorType(str, Enum):
    SOURCE_NOT_FOUND = "法律渊源不存在"
    LOCATION_ERROR = "条款编号或引用定位错误"
    OUTDATED_SOURCE = "旧法旧规误用"
    MEANING_DISTORTED = "曲解权威文本原意"
    NO_SUBSTANTIVE_MATCH = "引用内容与权威文本无实质对应"


class SemanticIssue(BaseModel):
    error_type: SemanticErrorType
    risk_level: RiskLevel
    diff_summary: str = Field(max_length=80)
    suggestion: str
    auto_fixable: Literal[False] = False


class SemanticComparison(BaseModel):
    verdict: ComparisonVerdict
    issues: list[SemanticIssue] = Field(default_factory=list)
    notes: Optional[str] = None

    @model_validator(mode="after")
    def validate_verdict_shape(self) -> "SemanticComparison":
        if self.verdict == ComparisonVerdict.PASS:
            if self.issues or self.notes is not None:
                raise ValueError("pass must contain only verdict")
        elif self.verdict == ComparisonVerdict.ISSUE:
            if not self.issues:
                raise ValueError("issue requires issues")
        elif self.issues or not self.notes:
            raise ValueError("bug requires empty issues and notes")
        return self


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
    source_attempts: list[SourceTrace] = Field(default_factory=list)
    source_locations: list[SourceLocation] = Field(default_factory=list)


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
    schema_version: str = "0.2"
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source_claim_doc_id: str
    legal_checks: list[LegalCheck] = Field(default_factory=list)
    case_checks: list[CaseCheck] = Field(default_factory=list)
