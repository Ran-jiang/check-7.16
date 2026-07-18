"""法规证据的语义核查门控与执行。"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.evidence import ArticleEvidence, LookupStatus
from ..domain.result import (
    ComparisonVerdict,
    SemanticCheckResult,
    SemanticErrorType,
    SemanticExecutionStatus,
    SemanticIssue,
)
from ..tracing.sources import LookupResult
from .semantic import SemanticCheckError, SemanticChecker

_SUGGEST_TRIGGER_TYPES = {
    SemanticErrorType.NO_SUBSTANTIVE_MATCH,
    SemanticErrorType.LOCATION_ERROR,
}


@dataclass(frozen=True)
class SemanticGate:
    proceed: bool
    reason: str | None = None


def decide_semantic_gate(
    lookup_result: LookupResult,
    findings: list[SemanticIssue],
    *,
    reference_role: str = "direct",
    span_status: str = "located",
) -> SemanticGate:
    if span_status == "error":
        return SemanticGate(False, "citation_alignment_error")
    blocking = {SemanticErrorType.LOCATION_ERROR, SemanticErrorType.SOURCE_NOT_FOUND}
    if any(f.error_type in blocking for f in findings):
        return SemanticGate(False, "blocked_by_rule_finding")
    if lookup_result.status in {
        LookupStatus.SOURCE_ERROR,
        LookupStatus.LAW_NOT_FOUND,
        LookupStatus.OUT_OF_SCOPE,
    }:
        return SemanticGate(False, "retrieval_incomplete")
    if reference_role == "nested":
        if lookup_result.status in {LookupStatus.ARTICLE_FOUND, LookupStatus.RELEVANT_ARTICLES_FOUND}:
            return SemanticGate(False, "nested_reference")
        return SemanticGate(False, "retrieval_incomplete")
    if lookup_result.evidence is None or not lookup_result.evidence.article_text:
        return SemanticGate(False, "retrieval_incomplete")
    return SemanticGate(True)


def skipped_semantic_result(reason: str) -> SemanticCheckResult:
    code = "citation_alignment_error" if reason == "citation_alignment_error" else None
    return SemanticCheckResult(
        execution_status=SemanticExecutionStatus.SKIPPED,
        error_code=code,
        skipped_reason=reason,
    )


def compare_with_llm(
    semantic_checker: SemanticChecker,
    document_quote: str,
    context_text: str,
    cited_source: str,
    evidence: ArticleEvidence,
    paragraphs: list[str] | None = None,
) -> SemanticCheckResult:
    try:
        return semantic_checker.compare(
            document_quote, context_text, cited_source, evidence, paragraphs=paragraphs
        )
    except SemanticCheckError as exc:
        error_code = getattr(exc, "error_code", "semantic_error")
        return SemanticCheckResult(
            execution_status=SemanticExecutionStatus.LLM_ERROR,
            notes=str(exc),
            error_code=error_code,
        )


def add_article_suggestion(
    semantic_checker: SemanticChecker | None,
    document_quote: str,
    current_article_no: str | None,
    candidates: list[dict[str, str]],
    comparison: SemanticCheckResult | None,
) -> None:
    if comparison is None or comparison.verdict != ComparisonVerdict.ISSUE:
        return
    suggest = getattr(semantic_checker, "suggest_article", None)
    if suggest is None or not candidates:
        return
    target_issues = [i for i in comparison.issues if i.error_type in _SUGGEST_TRIGGER_TYPES]
    if not target_issues:
        return
    article_no = suggest(document_quote, candidates)
    if not article_no or article_no == current_article_no:
        return
    for issue in target_issues:
        issue.suggestion = issue.suggestion.rstrip("。") + f"。经本法全文召回比对，疑似应改引{article_no}。"


__all__ = ["SemanticGate", "add_article_suggestion", "compare_with_llm", "decide_semantic_gate", "skipped_semantic_result"]
