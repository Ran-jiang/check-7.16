"""法规证据的语义核查门控与执行。"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.evidence import ArticleEvidence, LookupStatus
from ..domain.checks import ExecutionStatus
from ..domain.statute_results import StatuteErrorCode, StatuteFinding, StatuteMeaningCheck
from ..tracing.sources import LookupResult
from .semantic import SemanticCheckError, SemanticChecker

@dataclass(frozen=True)
class SemanticGate:
    proceed: bool
    reason: str | None = None


def decide_semantic_gate(
    lookup_result: LookupResult,
    findings: list[StatuteFinding],
    *,
    reference_role: str = "direct",
    span_status: str = "located",
) -> SemanticGate:
    if span_status == "error":
        return SemanticGate(False, "citation_alignment_error")
    blocking = {StatuteErrorCode.CITATION_LOCATION_ERROR, StatuteErrorCode.SOURCE_NOT_FOUND}
    if any(f.code in blocking for f in findings):
        return SemanticGate(False, "blocked_by_rule_finding")
    if lookup_result.status in {LookupStatus.SOURCE_ERROR, LookupStatus.LAW_NOT_FOUND}:
        return SemanticGate(False, "retrieval_incomplete")
    if reference_role == "nested":
        if lookup_result.status in {LookupStatus.ARTICLE_FOUND, LookupStatus.RELEVANT_ARTICLES_FOUND}:
            return SemanticGate(False, "nested_reference")
        return SemanticGate(False, "retrieval_incomplete")
    if lookup_result.evidence is None or not lookup_result.evidence.article_text:
        return SemanticGate(False, "retrieval_incomplete")
    return SemanticGate(True)


def skipped_semantic_result(reason: str) -> StatuteMeaningCheck:
    code = "citation_alignment_error" if reason == "citation_alignment_error" else None
    return StatuteMeaningCheck(
        execution_status=ExecutionStatus.SKIPPED,
        error_code=code,
        skipped_reason=reason,
    )


def compare_with_llm(
    semantic_checker: SemanticChecker,
    document_quote: str,
    context_text: str,
    cited_source: str,
    evidence: ArticleEvidence,
) -> StatuteMeaningCheck:
    try:
        return semantic_checker.compare(document_quote, context_text, cited_source, evidence)
    except SemanticCheckError as exc:
        error_code = getattr(exc, "error_code", "semantic_error")
        return StatuteMeaningCheck(
            execution_status=ExecutionStatus.LLM_ERROR,
            notes=str(exc),
            error_code=error_code,
            retryable=error_code in {"transport_error", "timeout", "rate_limited", "upstream_error"},
        )


__all__ = ["SemanticGate", "compare_with_llm", "decide_semantic_gate", "skipped_semantic_result"]
