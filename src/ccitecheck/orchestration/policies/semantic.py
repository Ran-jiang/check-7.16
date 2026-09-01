"""Scheduler 的语义比较门控。"""

from dataclasses import dataclass

from ...domain.evidence import LookupStatus
from ...domain.statute_results import StatuteErrorCode, StatuteFinding
from ...retrieval.sources import LookupResult


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
    blocking = {
        StatuteErrorCode.SOURCE_NOT_FOUND,
        StatuteErrorCode.LAW_NAME_ERROR,
        StatuteErrorCode.ARTICLE_NOT_FOUND,
        StatuteErrorCode.ARTICLE_NUMBER_ERROR,
        StatuteErrorCode.CITATION_HIERARCHY_ERROR,
    }
    if any(item.code in blocking for item in findings):
        return SemanticGate(False, "blocked_by_rule_finding")
    if lookup_result.status in {LookupStatus.SOURCE_ERROR, LookupStatus.LAW_NOT_FOUND}:
        return SemanticGate(False, "retrieval_incomplete")
    if reference_role == "nested":
        reason = (
            "nested_reference"
            if lookup_result.status in {
                LookupStatus.ARTICLE_FOUND,
                LookupStatus.RELEVANT_ARTICLES_FOUND,
            }
            else "retrieval_incomplete"
        )
        return SemanticGate(False, reason)
    if lookup_result.evidence is None or not lookup_result.evidence.article_text:
        return SemanticGate(False, "retrieval_incomplete")
    return SemanticGate(True)


__all__ = ["SemanticGate", "decide_semantic_gate"]
