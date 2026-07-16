"""把核查过程数据组装为统一前端结果模型。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.citation import Claim
from ..domain.evidence import ArticleEvidence, LookupStatus, SourceTier, SourceTrace
from ..domain.result import (
    CaseCheck,
    FrontendVerificationDocument,
    LegalCheck,
    SemanticComparison,
    SemanticIssue,
)


@dataclass
class LegalCheckData:
    """组装一条法规核查结果所需的数据。"""

    claim: Claim
    law_title: str
    article_no: str | None
    not_verifiable: str | None = None
    lookup_status: LookupStatus | None = None
    evidence: ArticleEvidence | None = None
    source_attempts: list[SourceTrace] = field(default_factory=list)
    rule_findings: list[SemanticIssue] = field(default_factory=list)
    semantic_comparison: SemanticComparison | None = None


def build_verification_document(
    source_claim_doc_id: str,
    legal_data: list[LegalCheckData],
    case_checks: list[CaseCheck],
) -> FrontendVerificationDocument:
    """生成供 Word、飞书和报告共用的核查结果。"""
    legal_checks = [
        _build_legal_check(index, data)
        for index, data in enumerate(legal_data, 1)
    ]
    return FrontendVerificationDocument(
        source_claim_doc_id=source_claim_doc_id,
        legal_checks=legal_checks,
        case_checks=case_checks,
    )


def _build_legal_check(index: int, data: LegalCheckData) -> LegalCheck:
    check_id = f"vc_{index:05d}"
    if data.not_verifiable is not None:
        attempts = [
            SourceTrace(
                tier=SourceTier.LOCAL_SQLITE,
                source_name="CCiteheck 文件类型分类",
                status=LookupStatus.NOT_VERIFIABLE,
                message=data.not_verifiable,
            )
        ]
        lookup_status = LookupStatus.NOT_VERIFIABLE
        evidence = None
    else:
        if data.lookup_status is None:
            raise ValueError("可核查法规缺少溯源结果")
        attempts = data.source_attempts
        lookup_status = data.lookup_status
        evidence = data.evidence

    return LegalCheck(
        check_id=check_id,
        claim_id=data.claim.claim_id,
        claim_text=data.claim.text,
        anchor_ids=list(data.claim.anchor_ids),
        source_locations=list(data.claim.source_locations),
        law_title=data.law_title,
        article_no=data.article_no,
        lookup_status=lookup_status,
        evidence=evidence,
        rule_findings=data.rule_findings,
        semantic_comparison=data.semantic_comparison,
        source_attempts=attempts,
    )


__all__ = ["LegalCheckData", "build_verification_document"]
