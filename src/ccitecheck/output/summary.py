"""把核查结果汇总为各前端共用的数量摘要。

本模块只统计已有判定结果，不重新解释证据，也不改变任何核查状态。
Word、飞书和后续其他输出端应共用这里的统计口径。
"""

from pydantic import BaseModel

from ..domain.evidence import CaseLookupStatus, LookupStatus
from ..domain.result import ComparisonVerdict, FrontendVerificationDocument, SemanticExecutionStatus


class VerificationSummary(BaseModel):
    """一次文档核查的数量汇总。"""

    total: int
    card_total: int
    reference_total: int
    passed: int
    issues: int
    bugs: int
    cases_verified: int
    cases_not_found: int


def summarize_verification(
    verification: FrontendVerificationDocument,
) -> VerificationSummary:
    """按统一口径统计通过、问题和无法判断的数量。"""
    passed = issues = bugs = 0
    references = [
        reference
        for card in verification.citation_cards
        for reference in card.references
    ]
    for check in references:
        comparison = check.semantic_comparison
        if check.rule_findings:
            issues += 1
            continue
        if comparison is None:
            if check.lookup_status in {
                LookupStatus.ARTICLE_FOUND,
                LookupStatus.RELEVANT_ARTICLES_FOUND,
            }:
                passed += 1
            else:
                bugs += 1
            continue
        if comparison.execution_status != SemanticExecutionStatus.COMPLETED:
            if comparison.skipped_reason == "nested_reference":
                passed += 1
            else:
                bugs += 1
            continue
        if comparison.verdict == ComparisonVerdict.PASS:
            passed += 1
        elif comparison.verdict == ComparisonVerdict.ISSUE:
            issues += 1
        else:
            bugs += 1

    cases_verified = sum(
        check.lookup_status == CaseLookupStatus.VERIFIED
        for check in verification.case_checks
    )
    cases_not_found = sum(
        check.lookup_status == CaseLookupStatus.NOT_FOUND
        for check in verification.case_checks
    )
    passed += cases_verified
    issues += cases_not_found
    bugs += sum(
        check.lookup_status in {
            CaseLookupStatus.MANUAL_REVIEW,
            CaseLookupStatus.SOURCE_NOT_CONFIGURED,
            CaseLookupStatus.SOURCE_ERROR,
        }
        for check in verification.case_checks
    )
    claim_ids = {card.claim_id for card in verification.citation_cards}
    claim_ids.update(check.claim_id for check in verification.case_checks)
    return VerificationSummary(
        total=len(references) + len(verification.case_checks),
        card_total=len(claim_ids),
        reference_total=len(references) + len(verification.case_checks),
        passed=passed,
        issues=issues,
        bugs=bugs,
        cases_verified=cases_verified,
        cases_not_found=cases_not_found,
    )


__all__ = ["VerificationSummary", "summarize_verification"]
