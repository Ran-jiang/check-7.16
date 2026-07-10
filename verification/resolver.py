"""Resolve extracted legal claims to source evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from claims.schema import ClaimDocument, ClaimType

from .exact_comparison import compare_exact_text
from .pkulaw_mcp import PkulawMcpError
from .schema import (
    CaseCheck,
    CaseEvidence,
    CaseLookupStatus,
    ComparisonConfidence,
    ComparisonVerdict,
    FrontendVerificationDocument,
    ExactTextComparison,
    LegalCheck,
    LookupStatus,
    SemanticComparison,
    RiskLevel,
    SemanticErrorType,
    SemanticIssue,
)
from .semantic import SemanticCheckError, SemanticChecker
from .sources import (
    CaseNumberRecognizer,
    LocalSQLiteSource,
    LookupRequest,
    LookupResult,
    PkulawCaseSource,
    PkulawFallbackSource,
    StatuteSource,
)


def build_default_sources(db_path: str | Path) -> list[StatuteSource]:
    return [
        LocalSQLiteSource(db_path),
        PkulawFallbackSource(),
    ]


def verify_claim_document_for_frontend(
    claim_doc: ClaimDocument,
    db_path: str | Path,
    sources: Iterable[StatuteSource] | None = None,
    semantic_checker: SemanticChecker | None = None,
    case_recognizer: CaseNumberRecognizer | None = None,
) -> FrontendVerificationDocument:
    source_chain = list(sources) if sources is not None else build_default_sources(db_path)
    recognizer = case_recognizer if case_recognizer is not None else PkulawCaseSource()
    checks: list[LegalCheck] = []
    next_id = 1

    for claim in claim_doc.claims:
        if claim.claim_type not in (
            ClaimType.LEGAL_SOURCE_CLAIM,
            ClaimType.LEGAL_SOURCE_PARAPHRASE,
        ):
            continue
        legal_sources = getattr(claim.entities, "legal_sources", [])
        for legal_source in legal_sources:
            articles = legal_source.articles or [None]
            for article in articles:
                article_no = article.article if article is not None else None
                result, attempts = _lookup_with_chain(
                    source_chain,
                    LookupRequest(
                        law_title=legal_source.title,
                        source_type=legal_source.source_type.value,
                        article_no=article_no,
                        context_text=claim.text,
                    ),
                )
                doc_quote = _document_quote(claim)
                exact_comparison = None
                if (
                    article is not None
                    and result.evidence is not None
                    and result.evidence.article_text is not None
                ):
                    exact_comparison = compare_exact_text(
                        doc_quote,
                        result.evidence.article_text,
                    )
                semantic_comparison = _compare_semantics(
                    semantic_checker,
                    doc_quote,
                    claim.text,
                    _cited_source(legal_source.title, article),
                    result,
                    exact_comparison,
                )
                checks.append(
                    LegalCheck(
                        check_id=f"vc_{next_id:05d}",
                        claim_id=claim.claim_id,
                        claim_text=claim.text,
                        anchor_ids=list(claim.anchor_ids),
                        law_title=legal_source.title,
                        article_no=article_no,
                        lookup_status=result.status,
                        evidence=result.evidence,
                        exact_comparison=exact_comparison,
                        semantic_comparison=semantic_comparison,
                        source_attempts=attempts,
                    )
                )
                next_id += 1

    return FrontendVerificationDocument(
        source_claim_doc_id=claim_doc.claim_meta.claim_doc_id,
        legal_checks=checks,
        case_checks=verify_case_claims(claim_doc, recognizer),
    )


def verify_case_claims(
    claim_doc: ClaimDocument,
    recognizer: CaseNumberRecognizer,
) -> list[CaseCheck]:
    checks: list[CaseCheck] = []
    next_id = 1
    for claim in claim_doc.claims:
        cited_numbers = _cited_case_numbers(claim)
        if not cited_numbers:
            continue
        recognized, error_status, message = _recognize_case_numbers(recognizer, claim.text)
        for cited in cited_numbers:
            evidence = _match_case_number(cited, recognized) if recognized is not None else None
            status, note = _case_status(recognized, evidence, error_status, message)
            checks.append(
                CaseCheck(
                    check_id=f"cc_{next_id:05d}",
                    claim_id=claim.claim_id,
                    claim_text=claim.text,
                    anchor_ids=list(claim.anchor_ids),
                    cited_case_number=cited,
                    lookup_status=status,
                    evidence=evidence,
                    message=note,
                )
            )
            next_id += 1
    return checks


def _cited_case_numbers(claim) -> list[str]:
    case_refs = getattr(claim.entities, "case_refs", [])
    return [ref.case_number for ref in case_refs if ref.case_number]


def _recognize_case_numbers(recognizer: CaseNumberRecognizer, text: str):
    try:
        return recognizer.recognize(text), None, ""
    except PkulawMcpError as exc:
        status = (
            CaseLookupStatus.SOURCE_NOT_CONFIGURED
            if "PKULAW_ACCESS_TOKEN" in str(exc)
            else CaseLookupStatus.SOURCE_ERROR
        )
        return None, status, str(exc)


def _match_case_number(cited: str, recognized) -> CaseEvidence | None:
    target = _normalize_case_number(cited)
    for item in recognized:
        if target in (_normalize_case_number(item.text), _normalize_case_number(item.case_flag)):
            return CaseEvidence(
                matched_text=item.text,
                case_number=item.case_flag or item.text,
                gid=item.gid,
                court=item.court,
                title=item.title,
                last_instance_date=item.last_instance_date,
                url=item.url,
            )
    return None


def _case_status(recognized, evidence, error_status, message):
    if recognized is None:
        return error_status, message
    if evidence is not None:
        return CaseLookupStatus.VERIFIED, ""
    return CaseLookupStatus.NOT_FOUND, "文书案号未在北大法宝案例库命中，疑似有误或不存在"


def _normalize_case_number(value: str) -> str:
    table = str.maketrans({"（": "(", "）": ")", "〔": "(", "〕": ")", "　": "", " ": ""})
    return value.translate(table)


def _compare_semantics(
    semantic_checker: SemanticChecker | None,
    doc_quote: str,
    quote_context: str,
    cited_source: str,
    lookup_result: LookupResult,
    exact_comparison: ExactTextComparison | None,
) -> SemanticComparison | None:
    if semantic_checker is None:
        return None
    if lookup_result.status == LookupStatus.LAW_NOT_FOUND:
        return SemanticComparison(
            verdict=ComparisonVerdict.ISSUE,
            issues=[
                SemanticIssue(
                    error_type=SemanticErrorType.SOURCE_NOT_FOUND,
                    risk_level=RiskLevel.HIGH,
                    diff_summary=f"权威来源未检索到{cited_source}",
                    suggestion="请核实法规名称、发布机关、发文字号及条款号。",
                )
            ],
            confidence=ComparisonConfidence.HIGH,
            notes="",
        )
    if lookup_result.status not in (
        LookupStatus.ARTICLE_FOUND,
        LookupStatus.RELEVANT_ARTICLES_FOUND,
    ):
        return SemanticComparison(
            verdict=ComparisonVerdict.BUG,
            confidence=ComparisonConfidence.LOW,
            notes=(
                "未取得可供语义核查的法条原文："
                f"{lookup_result.trace.message or lookup_result.status.value}"
            ),
        )
    if lookup_result.evidence is None:
        return SemanticComparison(
            verdict=ComparisonVerdict.BUG,
            confidence=ComparisonConfidence.LOW,
            notes="检索状态为已命中，但缺少法条证据。",
        )
    try:
        return semantic_checker.compare(
            doc_quote,
            quote_context,
            cited_source,
            lookup_result.evidence,
            exact_comparison,
        )
    except SemanticCheckError as exc:
        return SemanticComparison(
            verdict=ComparisonVerdict.BUG,
            confidence=ComparisonConfidence.LOW,
            notes=str(exc),
        )


def _document_quote(claim) -> str:
    paraphrase_text = getattr(claim.entities, "paraphrase_text", "")
    return paraphrase_text or claim.text


def _cited_source(law_title: str, article) -> str:
    if article is None:
        return f"《{law_title}》"
    locations = [article.article, *article.paragraphs, *article.items]
    return f"《{law_title}》" + "".join(locations)


def _lookup_with_chain(
    sources: list[StatuteSource],
    request: LookupRequest,
) -> tuple[LookupResult, list]:
    attempts = []
    last_result: LookupResult | None = None
    best_partial: LookupResult | None = None
    for source in sources:
        result = source.lookup(request)
        attempts.append(result.trace)
        last_result = result
        if result.status in (
            LookupStatus.ARTICLE_FOUND,
            LookupStatus.RELEVANT_ARTICLES_FOUND,
        ):
            return result, attempts
        if result.status in (
            LookupStatus.LAW_FOUND_ARTICLE_MISSING,
            LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE,
        ):
            best_partial = result
    if last_result is None:
        raise ValueError("No statute sources configured")
    if best_partial is not None:
        return best_partial, attempts
    return last_result, attempts
