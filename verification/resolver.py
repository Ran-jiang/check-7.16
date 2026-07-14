"""Resolve extracted legal claims to source evidence."""

from __future__ import annotations

import difflib
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from claims.schema import ClaimDocument, ClaimType
from laws.sqlite_store import (
    connect,
    list_current_articles,
    list_law_titles,
    normalize_title,
    strip_version_annotation,
)

from .article_retrieval import retrieve_relevant_articles

from .pkulaw_mcp import (
    PkulawCaseRecord,
    PkulawMcpError,
    PkulawNotConfiguredError,
    PkulawNotFoundError,
)
from .query_builder import build_case_keyword_query, build_case_semantic_query
from .schema import (
    CaseCheck,
    CaseEvidence,
    CaseLookupStatus,
    CaseSourceTrace,
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

# 法条查询与千问语义核查都是 IO 密集调用，用线程池并发压缩总耗时
_LOOKUP_WORKERS = 6
_SEMANTIC_WORKERS = 4


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
    include_statutes: bool = True,
    include_cases: bool = True,
) -> FrontendVerificationDocument:
    source_chain = list(sources) if sources is not None else build_default_sources(db_path)
    recognizer = case_recognizer if case_recognizer is not None else PkulawCaseSource()
    known_titles = _load_known_titles(db_path)
    locations = _claim_locations(claim_doc)

    items = _collect_check_items(claim_doc) if include_statutes else []

    # ---- 阶段一：法条查询。同一（法名, 条号）只查一次，唯一查询并发执行 ----
    lookup_results = _run_lookups(source_chain, items)

    # ---- 阶段二：确定性规则判定 + 语义核查。已有确定结论的检查跳过千问 ----
    semantic_results = _run_semantics(
        semantic_checker, items, lookup_results, known_titles, db_path
    )

    checks: list[LegalCheck] = []
    for index, item in enumerate(items):
        check_id = f"vc_{index + 1:05d}"
        if item.not_verifiable is not None:
            checks.append(
                _not_verifiable_check(
                    check_id,
                    item.claim,
                    item.law_title,
                    item.article_no,
                    item.not_verifiable,
                    locations[item.claim.claim_id],
                )
            )
            continue
        result, attempts = lookup_results[item.lookup_key]
        rule_findings, semantic_comparison = semantic_results[index]
        checks.append(
            LegalCheck(
                check_id=check_id,
                claim_id=item.claim.claim_id,
                claim_text=item.claim.text,
                anchor_ids=list(item.claim.anchor_ids),
                location_text=locations[item.claim.claim_id][0],
                location_occurrence=locations[item.claim.claim_id][1],
                law_title=item.law_title,
                article_no=item.article_no,
                lookup_status=result.status,
                evidence=result.evidence,
                rule_findings=rule_findings,
                semantic_comparison=semantic_comparison,
                source_attempts=attempts,
            )
        )

    return FrontendVerificationDocument(
        source_claim_doc_id=claim_doc.claim_meta.claim_doc_id,
        legal_checks=checks,
        case_checks=(
            verify_case_claims(claim_doc, recognizer, locations)
            if include_cases
            else []
        ),
    )


@dataclass
class _CheckItem:
    """一条待核查的（claim × 法规 × 条号）组合。"""

    claim: object
    law_title: str
    source_type: str
    article: object
    article_no: Optional[str]
    not_verifiable: Optional[str]

    @property
    def lookup_key(self) -> tuple:
        # 有条号时结果与上下文无关，可跨 claim 复用；
        # 无条号时按 claim 上下文做召回，不能复用
        if self.article_no:
            return (self.law_title, self.source_type, self.article_no)
        return (
            self.law_title,
            self.source_type,
            None,
            self.claim.context_text or self.claim.text,
        )


def _collect_check_items(claim_doc: ClaimDocument) -> list[_CheckItem]:
    items: list[_CheckItem] = []
    for claim in claim_doc.claims:
        if claim.claim_type != ClaimType.LEGAL_SOURCE_CLAIM:
            continue
        for legal_source in getattr(claim.entities, "legal_sources", []):
            not_verifiable = _classify_not_verifiable(legal_source.title)
            for article in legal_source.articles or [None]:
                items.append(
                    _CheckItem(
                        claim=claim,
                        law_title=legal_source.title,
                        source_type=legal_source.source_type.value,
                        article=article,
                        article_no=article.article if article is not None else None,
                        not_verifiable=not_verifiable,
                    )
                )
    return items


def _run_lookups(
    source_chain: list[StatuteSource], items: list[_CheckItem]
) -> dict[tuple, tuple[LookupResult, list]]:
    """对唯一的 lookup_key 并发执行查询链。"""
    requests: dict[tuple, LookupRequest] = {}
    for item in items:
        if item.not_verifiable is not None:
            continue
        requests.setdefault(
            item.lookup_key,
            LookupRequest(
                law_title=item.law_title,
                source_type=item.source_type,
                article_no=item.article_no,
                context_text=item.claim.context_text or item.claim.text,
            ),
        )
    if not requests:
        return {}
    keys = list(requests)
    with ThreadPoolExecutor(max_workers=_LOOKUP_WORKERS) as pool:
        outcomes = list(
            pool.map(lambda key: _lookup_with_chain(source_chain, requests[key]), keys)
        )
    return dict(zip(keys, outcomes))


def _run_semantics(
    semantic_checker: SemanticChecker | None,
    items: list[_CheckItem],
    lookup_results: dict[tuple, tuple[LookupResult, list]],
    known_titles: list[str],
    db_path: str | Path,
) -> dict[int, tuple[list[SemanticIssue], SemanticComparison | None]]:
    """先算确定性规则；规则已有结论的跳过千问，其余需要 LLM 的并发执行。"""
    results: dict[int, tuple[list[SemanticIssue], SemanticComparison | None]] = {}
    llm_jobs: list[tuple[int, _CheckItem, LookupResult]] = []

    for index, item in enumerate(items):
        if item.not_verifiable is not None:
            results[index] = ([], None)
            continue
        result, attempts = lookup_results[item.lookup_key]
        rule_findings = _build_rule_findings(
            item.law_title, item.article_no, result, attempts, known_titles
        )
        if rule_findings:
            # 确定性核查层已给出结论，无需再消耗千问调用
            results[index] = (rule_findings, None)
            continue
        placeholder = _semantic_without_llm(semantic_checker, item, result)
        if placeholder is not _NEEDS_LLM:
            results[index] = (rule_findings, placeholder)
            continue
        results[index] = (rule_findings, None)
        llm_jobs.append((index, item, result))

    if llm_jobs and semantic_checker is not None:
        with ThreadPoolExecutor(max_workers=_SEMANTIC_WORKERS) as pool:
            outcomes = list(
                pool.map(
                    lambda job: _compare_with_llm(semantic_checker, job[1], job[2]),
                    llm_jobs,
                )
            )
        for (index, item, _), comparison in zip(llm_jobs, outcomes):
            rule_findings, _ = results[index]
            # 引用不支持时，尝试给出"疑似应改引第X条"的建议
            _append_article_suggestion(db_path, semantic_checker, item, comparison)
            results[index] = (rule_findings, comparison)

    return results


# 引错了地方（而非说错了内容）时才有"改引哪条"可言
_SUGGEST_TRIGGER_TYPES = {
    SemanticErrorType.NO_SUBSTANTIVE_MATCH,
    SemanticErrorType.LOCATION_ERROR,
}


def _append_article_suggestion(
    db_path: str | Path,
    semantic_checker: SemanticChecker | None,
    item: _CheckItem,
    comparison: SemanticComparison | None,
) -> None:
    """语义核查判"无实质对应/定位错误"且本法在本地有全文时，
    召回候选条款请 LLM 判断哪一条真正支持文书表述，追加改引建议。
    改引建议是增强信息：任何一步不满足条件都静默跳过，不影响主结论。"""
    if comparison is None or comparison.verdict != ComparisonVerdict.ISSUE:
        return
    suggest = getattr(semantic_checker, "suggest_article", None)
    if suggest is None:
        return
    target_issues = [
        issue for issue in comparison.issues if issue.error_type in _SUGGEST_TRIGGER_TYPES
    ]
    if not target_issues:
        return
    path = Path(db_path)
    if not path.exists():
        return
    with connect(path) as conn:
        articles = list_current_articles(conn, item.law_title)
    if not articles:
        return
    excerpts = retrieve_relevant_articles(item.claim.text, articles)
    if not excerpts:
        return
    candidates = [
        {"article_no": excerpt.article_no, "article_text": excerpt.article_text}
        for excerpt in excerpts
    ]
    article_no = suggest(_document_quote(item.claim), candidates)
    if not article_no or article_no == item.article_no:
        return
    for issue in target_issues:
        issue.suggestion = (
            issue.suggestion.rstrip("。")
            + f"。经本法全文召回比对，疑似应改引{article_no}。"
        )


_NEEDS_LLM = object()


def _semantic_without_llm(
    semantic_checker: SemanticChecker | None,
    item: _CheckItem,
    lookup_result: LookupResult,
):
    """不需要调 LLM 就能决定语义结论的情形；返回 _NEEDS_LLM 表示需要调用。"""
    if semantic_checker is None:
        return None
    if lookup_result.status == LookupStatus.LAW_NOT_FOUND:
        cited_source = _cited_source(item.law_title, item.article)
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
    if lookup_result.status not in (
        LookupStatus.ARTICLE_FOUND,
        LookupStatus.RELEVANT_ARTICLES_FOUND,
    ):
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
    return _NEEDS_LLM


def _compare_with_llm(
    semantic_checker: SemanticChecker,
    item: _CheckItem,
    lookup_result: LookupResult,
) -> SemanticComparison:
    try:
        return semantic_checker.compare(
            _document_quote(item.claim),
            item.claim.context_text or item.claim.text,
            _cited_source(item.law_title, item.article),
            lookup_result.evidence,
        )
    except SemanticCheckError as exc:
        return SemanticComparison(
            verdict=ComparisonVerdict.BUG,
            notes=str(exc),
        )


def verify_case_claims(
    claim_doc: ClaimDocument,
    recognizer: CaseNumberRecognizer,
    locations: dict[str, tuple[str, int]] | None = None,
) -> list[CaseCheck]:
    locations = locations or _claim_locations(claim_doc)
    refs = [
        (claim, ref)
        for claim in claim_doc.claims
        for ref in getattr(claim.entities, "case_refs", [])
    ]
    if not refs:
        return []

    # 全篇合并为一次案号识别调用（法宝接口本身支持批量），节省额度与往返
    claims_with_numbers = [
        claim for claim, ref in refs if ref.case_number
    ]
    if claims_with_numbers:
        joined_text = "\n".join(dict.fromkeys(claim.text for claim in claims_with_numbers))
        recognized, error_status, message = _recognize_case_numbers(recognizer, joined_text)
    else:
        recognized, error_status, message = [], None, ""

    search_jobs = {
        _case_search_key(claim, ref): (claim, ref)
        for claim, ref in refs
        if not ref.case_number
    }
    if search_jobs:
        keys = list(search_jobs)
        with ThreadPoolExecutor(max_workers=_LOOKUP_WORKERS) as pool:
            outcomes = list(
                pool.map(
                    lambda key: _search_case_reference(
                        recognizer, *search_jobs[key]
                    ),
                    keys,
                )
            )
        search_results = dict(zip(keys, outcomes))
    else:
        search_results = {}

    checks: list[CaseCheck] = []
    for next_id, (claim, ref) in enumerate(refs, start=1):
        location_text, occurrence = locations[claim.claim_id]
        if ref.case_number:
            evidence = (
                _match_case_number(ref.case_number, recognized)
                if recognized is not None
                else None
            )
            status, note = _case_status(recognized, evidence, error_status, message)
            trace = CaseSourceTrace(
                source_name="北大法宝 MCP：案号识别",
                source_url=evidence.url if evidence else None,
                status=status,
                message=note,
            )
        else:
            status, evidence, note, traces = search_results[
                _case_search_key(claim, ref)
            ]
            trace = traces[-1] if traces else CaseSourceTrace(
                source_name="CCitecheck 案例检索路由",
                status=status,
                message=note,
            )
        checks.append(
            CaseCheck(
                check_id=f"cc_{next_id:05d}",
                claim_id=claim.claim_id,
                claim_text=claim.text,
                anchor_ids=list(claim.anchor_ids),
                location_text=location_text,
                location_occurrence=occurrence,
                cited_case_number=ref.case_number,
                cited_case_name=ref.case_name,
                lookup_status=status,
                evidence=evidence,
                message=note,
                source_attempts=(
                    [trace] if ref.case_number else traces or [trace]
                ),
            )
        )
    return checks


def _recognize_case_numbers(recognizer: CaseNumberRecognizer, text: str):
    try:
        return recognizer.recognize(text), None, ""
    except PkulawMcpError as exc:
        status = _case_error_status(exc)
        return None, status, str(exc)


def _case_search_key(claim, ref) -> tuple[str, str, str]:
    return (
        ref.case_name or "",
        ref.court or "",
        claim.context_text or claim.text,
    )


def _search_case_reference(recognizer, claim, ref):
    search_keyword = getattr(recognizer, "search_keyword", None)
    search_semantic = getattr(recognizer, "search_semantic", None)
    if not callable(search_keyword) and not callable(search_semantic):
        note = "该案例线索未附案号，当前案例源不支持案名或语义检索，请人工核验"
        return CaseLookupStatus.MANUAL_REVIEW, None, note, [
            CaseSourceTrace(
                source_name="CCitecheck 案例检索路由",
                status=CaseLookupStatus.MANUAL_REVIEW,
                message=note,
            )
        ]

    context = claim.context_text or claim.text
    title, fulltext = build_case_keyword_query(ref.case_name, context, ref.court)
    routes = []
    if callable(search_keyword) and (title or fulltext):
        routes.append(
            (
                "北大法宝 MCP：检索司法案例-关键词",
                "关键词检索",
                lambda: search_keyword(title, fulltext),
                {"title_query": title, "fulltext_query": fulltext},
            )
        )
    if callable(search_semantic):
        semantic_query = build_case_semantic_query(
            ref.case_name, context, ref.court
        )
        routes.append(
            (
                "北大法宝 MCP：检索司法案例-语义",
                "语义检索",
                lambda: search_semantic(semantic_query),
                {"semantic_query": semantic_query},
            )
        )

    traces: list[CaseSourceTrace] = []
    candidates: list[PkulawCaseRecord] = []
    completed = False
    error_statuses: list[CaseLookupStatus] = []
    for source_name, route_label, operation, metadata in routes:
        records, match, trace, route_completed, error_status = _execute_case_route(
            source_name, operation, metadata, ref
        )
        traces.append(trace)
        completed = completed or route_completed
        if error_status is not None:
            error_statuses.append(error_status)
        if match:
            return (
                CaseLookupStatus.VERIFIED,
                _case_record_evidence(match, ref.case_name),
                f"案名已通过北大法宝{route_label}核验",
                traces,
            )
        candidates.extend(records)

    if candidates:
        top = candidates[0]
        note = "北大法宝已返回相关候选，但无法确定唯一对应案例，请人工确认"
        return (
            CaseLookupStatus.MANUAL_REVIEW,
            _case_record_evidence(top, ref.case_name),
            note,
            traces,
        )
    if error_statuses:
        status = (
            CaseLookupStatus.SOURCE_NOT_CONFIGURED
            if all(item == CaseLookupStatus.SOURCE_NOT_CONFIGURED for item in error_statuses)
            else CaseLookupStatus.SOURCE_ERROR
        )
        return status, None, "北大法宝案例检索未完成，无法判断", traces
    if completed and ref.case_name:
        return (
            CaseLookupStatus.NOT_FOUND,
            None,
            "北大法宝关键词与语义检索均未命中该案例线索，疑似名称有误或不存在",
            traces,
        )
    note = "案例线索不足，无法构造可验证的检索条件，请人工核验"
    return CaseLookupStatus.MANUAL_REVIEW, None, note, traces


def _execute_case_route(source_name, operation, metadata, ref):
    try:
        records = operation()
    except PkulawNotFoundError:
        return (
            [],
            None,
            CaseSourceTrace(
                source_name=source_name,
                status=CaseLookupStatus.NOT_FOUND,
                message="检索完成，未命中案例",
                metadata=metadata,
            ),
            True,
            None,
        )
    except PkulawMcpError as exc:
        error_status = _case_error_status(exc)
        return (
            [],
            None,
            CaseSourceTrace(
                source_name=source_name,
                status=error_status,
                message=str(exc),
                metadata=metadata,
            ),
            False,
            error_status,
        )

    match = _match_case_record(ref.case_name, ref.court, records)
    status = (
        CaseLookupStatus.VERIFIED
        if match
        else CaseLookupStatus.MANUAL_REVIEW
        if records
        else CaseLookupStatus.NOT_FOUND
    )
    message = (
        "案名已在检索候选中精确匹配"
        if match
        else "检索完成，返回相关候选但未确定唯一同名案例"
        if records
        else "检索完成，未命中案例"
    )
    return (
        records,
        match,
        CaseSourceTrace(
            source_name=source_name,
            source_url=match.url if match else None,
            status=status,
            message=message,
            metadata={**metadata, "candidate_count": len(records)},
        ),
        True,
        None,
    )


def _case_error_status(exc: PkulawMcpError) -> CaseLookupStatus:
    if isinstance(exc, PkulawNotConfiguredError):
        return CaseLookupStatus.SOURCE_NOT_CONFIGURED
    return CaseLookupStatus.SOURCE_ERROR


def _match_case_record(
    case_name: str | None,
    court: str | None,
    records: list[PkulawCaseRecord],
) -> PkulawCaseRecord | None:
    target = _normalize_case_name(case_name or "")
    if not target:
        return None
    normalized_court = _normalize_case_name(court or "")
    for record in records:
        candidate = _normalize_case_name(record.title)
        title_matches = target == candidate or (
            min(len(target), len(candidate)) >= 6
            and (target in candidate or candidate in target)
        )
        court_matches = not normalized_court or normalized_court in _normalize_case_name(
            record.court
        )
        if title_matches and court_matches:
            return record
    return None


def _normalize_case_name(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", value)
    return normalized.replace("指导性案例", "指导案例")


def _case_record_evidence(
    record: PkulawCaseRecord, cited_name: str | None
) -> CaseEvidence:
    return CaseEvidence(
        matched_text=cited_name or record.title,
        case_number=record.case_number,
        gid=record.gid,
        court=record.court,
        title=record.title,
        last_instance_date=record.last_instance_date,
        url=record.url,
    )


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
    if "专项通知" in law_title:
        return "专项通知不按法条编号核验，请以发布机关原文件及适用期限为准"
    return None


def _not_verifiable_check(
    check_id: str,
    claim,
    law_title: str,
    article_no,
    reason: str,
    location: tuple[str, int] = ("", 0),
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
        location_text=location[0],
        location_occurrence=location[1],
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
    return claim.text


def _claim_locations(claim_doc: ClaimDocument) -> dict[str, tuple[str, int]]:
    """为重复句生成稳定的搜索序号；同一锚点上的法规/案例共用序号。"""
    counters: dict[str, int] = {}
    identities: dict[tuple[str, ...], tuple[str, int]] = {}
    result: dict[str, tuple[str, int]] = {}
    for claim in claim_doc.claims:
        identity = tuple(claim.anchor_ids)
        location = identities.get(identity)
        if location is None:
            location_text = claim.text
            occurrence = counters.get(location_text, 0)
            counters[location_text] = occurrence + 1
            location = (location_text, occurrence)
            identities[identity] = location
        result[claim.claim_id] = location
    return result


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
