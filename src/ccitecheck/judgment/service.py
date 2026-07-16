"""法规证据的确定性与语义判定。

本模块只解释已经取得的证据，不访问数据库、不调度数据源，也不控制并发。
"""

from __future__ import annotations

from ..domain.evidence import ArticleEvidence, LookupStatus
from ..domain.result import (
    ComparisonVerdict,
    RiskLevel,
    SemanticComparison,
    SemanticErrorType,
    SemanticIssue,
)
from ..tracing.sources import LookupResult
from .semantic import SemanticCheckError, SemanticChecker

_SUGGEST_TRIGGER_TYPES = {
    SemanticErrorType.NO_SUBSTANTIVE_MATCH,
    SemanticErrorType.LOCATION_ERROR,
}


def semantic_without_llm(
    semantic_checker: SemanticChecker | None,
    cited_source: str,
    lookup_result: LookupResult,
) -> SemanticComparison | None | object:
    """处理无需调用大模型即可确定的语义核查状态。"""
    if semantic_checker is None:
        return None
    if lookup_result.status == LookupStatus.LAW_NOT_FOUND:
        return SemanticComparison(
            verdict=ComparisonVerdict.ISSUE,
            issues=[
                SemanticIssue(
                    error_type=SemanticErrorType.SOURCE_NOT_FOUND,
                    risk_level=RiskLevel.HIGH,
                    diff_summary=f"权威来源未检索到{cited_source}"[:80],
                    suggestion="请核实法规名称、发布机关、发文字号及条款号。",
                )
            ],
            notes="",
        )
    if lookup_result.status not in {
        LookupStatus.ARTICLE_FOUND,
        LookupStatus.RELEVANT_ARTICLES_FOUND,
    }:
        return SemanticComparison(
            verdict=ComparisonVerdict.BUG,
            notes=(
                "未取得可供语义核查的法条原文："
                f"{lookup_result.trace.message or lookup_result.status.value}"
            ),
        )
    if lookup_result.evidence is None:
        return SemanticComparison(
            verdict=ComparisonVerdict.BUG,
            notes="检索状态为已命中，但缺少法条证据。",
        )
    return NEEDS_LLM


def compare_with_llm(
    semantic_checker: SemanticChecker,
    document_quote: str,
    context_text: str,
    cited_source: str,
    evidence: ArticleEvidence,
) -> SemanticComparison:
    """调用语义检查器比较文书引用与权威证据。"""
    try:
        return semantic_checker.compare(
            document_quote,
            context_text,
            cited_source,
            evidence,
        )
    except SemanticCheckError as exc:
        return SemanticComparison(verdict=ComparisonVerdict.BUG, notes=str(exc))


def add_article_suggestion(
    semantic_checker: SemanticChecker | None,
    document_quote: str,
    current_article_no: str | None,
    candidates: list[dict[str, str]],
    comparison: SemanticComparison | None,
) -> None:
    """为定位类问题追加候选条款建议，不改变原判定结论。"""
    if comparison is None or comparison.verdict != ComparisonVerdict.ISSUE:
        return
    suggest = getattr(semantic_checker, "suggest_article", None)
    if suggest is None or not candidates:
        return
    target_issues = [
        issue
        for issue in comparison.issues
        if issue.error_type in _SUGGEST_TRIGGER_TYPES
    ]
    if not target_issues:
        return
    article_no = suggest(document_quote, candidates)
    if not article_no or article_no == current_article_no:
        return
    for issue in target_issues:
        issue.suggestion = (
            issue.suggestion.rstrip("。")
            + f"。经本法全文召回比对，疑似应改引{article_no}。"
        )


NEEDS_LLM = object()

__all__ = [
    "NEEDS_LLM",
    "add_article_suggestion",
    "compare_with_llm",
    "semantic_without_llm",
]
