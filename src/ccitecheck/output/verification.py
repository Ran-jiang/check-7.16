"""把核查过程数据组装为统一前端结果模型。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.citation import Claim
from ..domain.evidence import ArticleEvidence, LookupStatus, SourceTier, SourceTrace
from ..domain.result import (
    CaseCheck,
    CitationCard,
    CitationReferenceCheck,
    FrontendVerificationDocument,
    SemanticComparison,
    SemanticIssue,
)


@dataclass
class CitationReferenceData:
    """组装一条法规核查结果所需的数据。"""

    claim: Claim
    law_title: str
    article_no: str | None
    paragraphs: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    cited_text: str = ""
    reference_role: str = "direct"
    mention_span: tuple[int, int] | None = None
    citation_span: tuple[int, int] | None = None
    quote_span: tuple[int, int] | None = None
    not_verifiable: str | None = None
    out_of_scope: str | None = None
    lookup_status: LookupStatus | None = None
    evidence: ArticleEvidence | None = None
    source_attempts: list[SourceTrace] = field(default_factory=list)
    rule_findings: list[SemanticIssue] = field(default_factory=list)
    semantic_comparison: SemanticComparison | None = None
    verification_scope: str = "full"
    jurisdiction: str = "CN"


def build_verification_document(
    source_claim_doc_id: str,
    reference_data: list[CitationReferenceData],
    case_checks: list[CaseCheck],
) -> FrontendVerificationDocument:
    """生成供 Word、飞书和报告共用的核查结果。"""
    cards: list[CitationCard] = []
    cards_by_claim: dict[str, CitationCard] = {}
    for index, data in enumerate(reference_data, 1):
        reference = _build_reference_check(index, data)
        card = cards_by_claim.get(data.claim.claim_id)
        if card is None:
            card = CitationCard(
                card_id=f"card_{len(cards) + 1:05d}",
                claim_id=data.claim.claim_id,
                claim_text=data.claim.text,
                anchor_ids=list(data.claim.anchor_ids),
                source_locations=list(data.claim.source_locations),
                references=[reference],
            )
            cards_by_claim[data.claim.claim_id] = card
            cards.append(card)
        else:
            card.references.append(reference)
    return FrontendVerificationDocument(
        source_claim_doc_id=source_claim_doc_id,
        citation_cards=cards,
        case_checks=case_checks,
    )


def _build_reference_check(index: int, data: CitationReferenceData) -> CitationReferenceCheck:
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
    elif data.out_of_scope is not None:
        attempts = [
            SourceTrace(
                tier=SourceTier.LOCAL_SQLITE,
                source_name="CCiteheck 法域分类",
                status=LookupStatus.OUT_OF_SCOPE,
                message=data.out_of_scope,
            )
        ]
        lookup_status = LookupStatus.OUT_OF_SCOPE
        evidence = None
    else:
        if data.lookup_status is None:
            raise ValueError("可核查法规缺少溯源结果")
        attempts = data.source_attempts
        lookup_status = data.lookup_status
        evidence = data.evidence

    return CitationReferenceCheck(
        check_id=check_id,
        cited_text=data.cited_text,
        law_title=data.law_title,
        article_no=data.article_no,
        paragraphs=data.paragraphs,
        items=data.items,
        reference_role=data.reference_role,
        mention_span=data.mention_span,
        citation_span=data.citation_span,
        quote_span=data.quote_span,
        lookup_status=lookup_status,
        evidence=evidence,
        rule_findings=data.rule_findings,
        semantic_comparison=data.semantic_comparison,
        verification_scope=data.verification_scope,
        jurisdiction=data.jurisdiction,
        source_attempts=attempts,
    )


__all__ = ["CitationReferenceData", "build_verification_document"]
