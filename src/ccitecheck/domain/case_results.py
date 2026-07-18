"""司法案例核验专用结果模型。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from .citation import SourceLocation
from .evidence import CaseEvidence, CaseLookupStatus, CaseSourceTrace
from .revisions import RevisionProposal
from .checks import CheckVerdict, ExecutionStatus


class CaseErrorCode(str, Enum):
    CASE_NOT_FOUND = "case_not_found"
    CASE_IDENTITY_ERROR = "case_identity_error"
    HOLDING_NOT_IN_CASE = "holding_not_in_case"


class CaseFinding(BaseModel):
    code: CaseErrorCode
    risk_level: Literal["HIGH", "MEDIUM"]
    summary: str = Field(max_length=300)
    suggestion: str
    revision: RevisionProposal | None = None


class CaseHoldingCheck(BaseModel):
    execution_status: ExecutionStatus = ExecutionStatus.COMPLETED
    verdict: CheckVerdict | None = None
    findings: list[CaseFinding] = Field(default_factory=list)
    notes: str = ""
    error_code: str | None = None
    retryable: bool = False
    skipped_reason: str | None = None


class CaseCandidate(BaseModel):
    title: str
    case_number: str | None = None
    court: str | None = None
    last_instance_date: str | None = None
    url: str | None = None


class CaseVerificationResult(BaseModel):
    check_id: str
    claim_id: str
    claim_text: str
    jurisdiction: str = "CN"
    cited_case_number: str | None = None
    cited_case_name: str | None = None
    cited_court: str | None = None
    lookup_status: CaseLookupStatus
    evidence: CaseEvidence | None = None
    findings: list[CaseFinding] = Field(default_factory=list)
    outcome: Literal["pass", "issue", "bug"]
    message: str = ""
    holding_check: CaseHoldingCheck | None = None
    source_locations: list[SourceLocation] = Field(default_factory=list)
    source_attempts: list[CaseSourceTrace] = Field(default_factory=list)
    candidate_cases: list[CaseCandidate] = Field(default_factory=list)


__all__ = [
    "CaseErrorCode",
    "CaseCandidate",
    "CaseFinding",
    "CaseHoldingCheck",
    "CaseVerificationResult",
]
