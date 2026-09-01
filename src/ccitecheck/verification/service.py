"""基于既有证据执行语义比较。"""

from __future__ import annotations

from ..domain.evidence import ArticleEvidence
from ..domain.checks import ExecutionStatus
from ..domain.statute_results import (
    LegalApplicationCheck,
    StatuteMeaningCheck,
)
from .legal_application import ApplicationAuthority
from .semantic import SemanticCheckError, SemanticChecker

def skipped_semantic_result(reason: str) -> StatuteMeaningCheck:
    code = "citation_alignment_error" if reason == "citation_alignment_error" else None
    return StatuteMeaningCheck(
        execution_status=ExecutionStatus.SKIPPED,
        error_code=code,
        skipped_reason=reason,
    )


def compare_with_llm(
    semantic_checker: SemanticChecker,
    claim_text: str,
    cited_source: str,
    evidence: ArticleEvidence,
) -> StatuteMeaningCheck:
    try:
        return semantic_checker.compare(claim_text, cited_source, evidence)
    except SemanticCheckError as exc:
        error_code = getattr(exc, "error_code", "semantic_error")
        return StatuteMeaningCheck(
            execution_status=ExecutionStatus.LLM_ERROR,
            notes=str(exc),
            error_code=error_code,
            retryable=error_code in {"transport_error", "timeout", "rate_limited", "upstream_error"},
        )


def compare_application_with_llm(
    semantic_checker: SemanticChecker,
    original_text: str,
    authorities: list[ApplicationAuthority],
) -> LegalApplicationCheck:
    try:
        return semantic_checker.compare_application(original_text, authorities)
    except SemanticCheckError as exc:
        error_code = getattr(exc, "error_code", "semantic_error")
        return LegalApplicationCheck(
            execution_status=ExecutionStatus.LLM_ERROR,
            notes=str(exc),
            error_code=error_code,
            retryable=error_code in {
                "transport_error", "timeout", "rate_limited", "upstream_error",
            },
        )


__all__ = [
    "compare_application_with_llm",
    "compare_with_llm",
    "skipped_semantic_result",
]
