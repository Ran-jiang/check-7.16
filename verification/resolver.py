"""Resolve extracted legal claims to source evidence."""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Iterable, Optional

from claims.schema import ClaimDocument, ClaimType
from laws.sqlite_store import (
    connect,
    list_law_titles,
    normalize_title,
    strip_version_annotation,
)

from .pkulaw_mcp import PkulawMcpError
from .schema import (
    CaseCheck,
    CaseEvidence,
    CaseLookupStatus,
    ComparisonConfidence,
    ComparisonVerdict,
    FrontendVerificationDocument,
    LegalCheck,
    LookupStatus,
    SemanticComparison,
    RiskLevel,
    SemanticErrorType,
    SemanticIssue,
    SourceTier,
    SourceTrace,
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
    include_cases: bool = True,
) -> FrontendVerificationDocument:
    source_chain = list(sources) if sources is not None else build_default_sources(db_path)
    recognizer = case_recognizer if case_recognizer is not None else PkulawCaseSource()
    known_titles = _load_known_titles(db_path)
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
            not_verifiable = _classify_not_verifiable(legal_source.title)
            for article in articles:
                article_no = article.article if article is not None else None
                if not_verifiable is not None:
                    checks.append(
                        _not_verifiable_check(
                            f"vc_{next_id:05d}",
                            claim,
                            legal_source.title,
                            article_no,
                            not_verifiable,
                        )
                    )
                    next_id += 1
                    continue
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
                rule_findings = _build_rule_findings(
                    legal_source.title,
                    article_no,
                    result,
                    attempts,
                    known_titles,
                )
                semantic_comparison = _compare_semantics(
                    semantic_checker,
                    doc_quote,
                    claim.text,
                    _cited_source(legal_source.title, article),
                    result,
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
                        rule_findings=rule_findings,
                        semantic_comparison=semantic_comparison,
                        source_attempts=attempts,
                    )
                )
                next_id += 1

    return FrontendVerificationDocument(
        source_claim_doc_id=claim_doc.claim_meta.claim_doc_id,
        legal_checks=checks,
        case_checks=verify_case_claims(claim_doc, recognizer) if include_cases else [],
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
        )
    except SemanticCheckError as exc:
        return SemanticComparison(
            verdict=ComparisonVerdict.BUG,
            confidence=ComparisonConfidence.LOW,
            notes=str(exc),
        )


# ============================================================
# 确定性规则判定（不依赖 LLM）
# ============================================================

_GB_STANDARD_PATTERN = re.compile(r"GB\s*/?\s*[TZ]?\s*\d{3,6}")
_REPEALED_PATTERN = re.compile(r"废止|失效")


def _classify_not_verifiable(law_title: str) -> Optional[str]:
    """识别不属于法条库核验范围的文件，返回原因说明。"""
    if "征求意见稿" in law_title:
        return "征求意见稿尚未生效，不属于可核验的现行法源，请人工确认引用意图"
    if _GB_STANDARD_PATTERN.search(law_title):
        return "国家/行业标准不在法规库核验范围内，请以标准全文出版物为准"
    return None


def _not_verifiable_check(
    check_id: str, claim, law_title: str, article_no, reason: str
) -> LegalCheck:
    trace = SourceTrace(
        tier=SourceTier.LOCAL_SQLITE,
        source_name="CCitecheck 文件类型分类",
        status=LookupStatus.NOT_VERIFIABLE,
        message=reason,
    )
    return LegalCheck(
        check_id=check_id,
        claim_id=claim.claim_id,
        claim_text=claim.text,
        anchor_ids=list(claim.anchor_ids),
        law_title=law_title,
        article_no=article_no,
        lookup_status=LookupStatus.NOT_VERIFIABLE,
        source_attempts=[trace],
    )


def _load_known_titles(db_path: str | Path) -> list[str]:
    path = Path(db_path)
    if not path.exists():
        return []
    with connect(path) as conn:
        return list_law_titles(conn)


def _build_rule_findings(
    law_title: str,
    article_no: Optional[str],
    result: LookupResult,
    attempts: list,
    known_titles: list[str],
) -> list[SemanticIssue]:
    findings: list[SemanticIssue] = []

    # A2 旧法旧规：任何证据层面的时效字段显示废止/失效
    evidence = result.evidence
    repealed = False
    if evidence is not None:
        timeliness_values = [
            evidence.version_status or "",
            evidence.version_label or "",
            str(evidence.source_metadata.get("timeliness", "")),
        ]
        repealed = any(_REPEALED_PATTERN.search(value) for value in timeliness_values)
        if repealed:
            findings.append(
                SemanticIssue(
                    error_type=SemanticErrorType.OUTDATED_SOURCE,
                    risk_level=RiskLevel.HIGH,
                    diff_summary=(
                        f"《{strip_version_annotation(law_title)}》时效状态为"
                        "『废止或失效』，不应作为现行依据引用"
                    )[:80],
                    suggestion="请改引现行有效的替代法规，并核对对应条文内容。",
                )
            )

    # A1 条文不存在：本地库有该法完整全文但条号未命中，且后备源也没找到该条
    if result.status == LookupStatus.LAW_FOUND_ARTICLE_MISSING and article_no:
        local_count = 0
        for trace in attempts:
            if trace.tier == SourceTier.LOCAL_SQLITE:
                local_count = trace.metadata.get("local_article_count", 0)
        if local_count > 0:
            findings.append(
                SemanticIssue(
                    error_type=SemanticErrorType.LOCATION_ERROR,
                    risk_level=RiskLevel.HIGH,
                    diff_summary=(
                        f"《{strip_version_annotation(law_title)}》现行全文共"
                        f"{local_count}条，其中不存在{article_no}"
                    )[:80],
                    suggestion="请核实条文编号；该条在现行有效版本中不存在。",
                )
            )
        elif result.evidence is not None and not repealed:
            # 已废止法规查不到条文属预期，不再叠加噪音
            findings.append(
                SemanticIssue(
                    error_type=SemanticErrorType.LOCATION_ERROR,
                    risk_level=RiskLevel.MEDIUM,
                    diff_summary=f"北大法宝已收录该法规，但未检索到{article_no}"[:80],
                    suggestion="请人工核实该条文编号是否存在。",
                )
            )

    # A3 法源不存在 / 法名疑似有误
    if result.status == LookupStatus.LAW_NOT_FOUND:
        search_completed = any(
            trace.metadata.get("search_completed") for trace in attempts
        )
        candidate_titles = [
            title
            for trace in attempts
            for title in trace.metadata.get("candidate_titles", [])
        ]
        suggestion_title = _suggest_similar_title(
            law_title, known_titles + candidate_titles
        )
        suggestion_text = (
            f"疑似应为《{suggestion_title}》，请核实法规名称。"
            if suggestion_title
            else "请核实法规名称、发布机关及发文字号。"
        )
        if search_completed:
            findings.append(
                SemanticIssue(
                    error_type=SemanticErrorType.SOURCE_NOT_FOUND,
                    risk_level=RiskLevel.HIGH,
                    diff_summary=(
                        f"本地法规库与北大法宝均未检索到"
                        f"《{strip_version_annotation(law_title)}》"
                    )[:80],
                    suggestion=suggestion_text,
                )
            )
        elif suggestion_title:
            findings.append(
                SemanticIssue(
                    error_type=SemanticErrorType.SOURCE_NOT_FOUND,
                    risk_level=RiskLevel.MEDIUM,
                    diff_summary=(
                        f"未检索到《{strip_version_annotation(law_title)}》，"
                        f"名称与《{suggestion_title}》高度相似"
                    )[:80],
                    suggestion=suggestion_text,
                )
            )

    return findings


def _suggest_similar_title(law_title: str, known_titles: list[str]) -> Optional[str]:
    if not known_titles:
        return None
    target = strip_version_annotation(normalize_title(law_title))
    matches = difflib.get_close_matches(target, known_titles, n=1, cutoff=0.8)
    if matches:
        return matches[0]
    # 剥掉"中华人民共和国"前缀再试一次
    short = target.replace("中华人民共和国", "", 1)
    shorts = [t.replace("中华人民共和国", "", 1) for t in known_titles]
    matches = difflib.get_close_matches(short, shorts, n=1, cutoff=0.8)
    if matches:
        index = shorts.index(matches[0])
        return known_titles[index]
    return None


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
