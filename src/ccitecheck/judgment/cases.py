"""司法案例引用的检索与判定。

本模块负责案号识别、无案号案例检索、候选匹配以及案例核查结果组装。
它不负责法规查询、法规语义比较或整份文档的总流程编排。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from ..domain.citation import ClaimDocument
from ..domain.evidence import (
    CaseEvidence,
    CaseLookupStatus,
    CaseSourceTrace,
)
from ..domain.result import CaseCheck
from ..tracing.queries import build_case_keyword_query, build_case_semantic_query
from ..tracing.sources.pkulaw.cases import CaseNumberRecognizer
from ..tracing.sources.pkulaw.client import (
    PkulawCaseRecord,
    PkulawMcpError,
    PkulawNotConfiguredError,
    PkulawNotFoundError,
)

# 案例关键词检索和语义检索均为 IO 密集调用，使用独立线程池。
_CASE_LOOKUP_WORKERS = 6

_OUT_OF_SCOPE_NOTE = "外国判例，超出本产品核查边界，请人工核验"


def _is_foreign(ref) -> bool:
    return getattr(ref, "jurisdiction", "CN") == "FOREIGN"


def verify_case_claims(
    claim_doc: ClaimDocument,
    recognizer: CaseNumberRecognizer,
) -> list[CaseCheck]:
    """核查案例引用，并保留共享块坐标供输出端定位。"""
    raw_refs = [
        (claim, ref)
        for claim in claim_doc.claims
        for ref in getattr(claim.entities, "case_refs", [])
    ]
    refs = []
    seen_refs: set[tuple[str, str]] = set()
    for claim, ref in raw_refs:
        identity = (
            _normalize_case_number(ref.case_number)
            if ref.case_number
            else _normalize_case_name(ref.case_name or "")
        )
        key = (claim.claim_id, identity)
        if key in seen_refs:
            continue
        seen_refs.add(key)
        refs.append((claim, ref))
    if not refs:
        return []

    # 全篇合并为一次案号识别调用（法宝接口本身支持批量），节省额度与往返
    claims_with_numbers = [
        claim for claim, ref in refs if ref.case_number and not _is_foreign(ref)
    ]
    if claims_with_numbers:
        joined_text = "\n".join(
            dict.fromkeys(claim.text for claim in claims_with_numbers)
        )
        recognized, error_status, message = _recognize_case_numbers(
            recognizer, joined_text
        )
    else:
        recognized, error_status, message = [], None, ""

    search_jobs = {
        _case_search_key(claim, ref): (claim, ref)
        for claim, ref in refs
        if not ref.case_number and not _is_foreign(ref)
    }
    if search_jobs:
        keys = list(search_jobs)
        with ThreadPoolExecutor(max_workers=_CASE_LOOKUP_WORKERS) as pool:
            outcomes = list(
                pool.map(
                    lambda key: _search_case_reference(recognizer, *search_jobs[key]),
                    keys,
                )
            )
        search_results = dict(zip(keys, outcomes))
    else:
        search_results = {}

    checks: list[CaseCheck] = []
    for next_id, (claim, ref) in enumerate(refs, start=1):
        if _is_foreign(ref):
            status = CaseLookupStatus.OUT_OF_SCOPE
            evidence = None
            note = _OUT_OF_SCOPE_NOTE
            traces = []
            trace = CaseSourceTrace(
                source_name="CCiteheck 法域分类",
                status=status,
                message=note,
            )
        elif ref.case_number:
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
            trace = (
                traces[-1]
                if traces
                else CaseSourceTrace(
                    source_name="CCiteheck 案例检索路由",
                    status=status,
                    message=note,
                )
            )
        checks.append(
            CaseCheck(
                check_id=f"cc_{next_id:05d}",
                claim_id=claim.claim_id,
                claim_text=claim.text,
                anchor_ids=list(claim.anchor_ids),
                source_locations=list(claim.source_locations),
                cited_case_number=ref.case_number,
                cited_case_name=ref.case_name,
                lookup_status=status,
                evidence=evidence,
                message=note,
                source_attempts=([trace] if ref.case_number else traces or [trace]),
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
        return (
            CaseLookupStatus.MANUAL_REVIEW,
            None,
            note,
            [
                CaseSourceTrace(
                    source_name="CCiteheck 案例检索路由",
                    status=CaseLookupStatus.MANUAL_REVIEW,
                    message=note,
                )
            ],
        )

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
        semantic_query = build_case_semantic_query(ref.case_name, context, ref.court)
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
            if all(
                item == CaseLookupStatus.SOURCE_NOT_CONFIGURED
                for item in error_statuses
            )
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
        target_guiding_id = _guiding_case_id(target)
        candidate_guiding_id = _guiding_case_id(candidate)
        title_matches = (
            target == candidate
            or (
                target_guiding_id is not None
                and target_guiding_id == candidate_guiding_id
            )
            or (
                min(len(target), len(candidate)) >= 6
                and (target in candidate or candidate in target)
            )
        )
        court_matches = (
            not normalized_court
            or normalized_court in _normalize_case_name(record.court)
        )
        if title_matches and court_matches:
            return record
    return None


def _normalize_case_name(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", value)
    return normalized.replace("指导性案例", "指导案例")


def _guiding_case_id(value: str) -> str | None:
    match = re.match(r"^指导案例(\d+)号", value)
    return match.group(1) if match else None


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
        if target in (
            _normalize_case_number(item.text),
            _normalize_case_number(item.case_flag),
        ):
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
    return (
        CaseLookupStatus.NOT_FOUND,
        "文书案号未在北大法宝案例库命中，疑似有误或不存在",
    )


def _normalize_case_number(value: str) -> str:
    table = str.maketrans(
        {"（": "(", "）": ")", "〔": "(", "〕": ")", "　": "", " ": ""}
    )
    return value.translate(table)


__all__ = ["verify_case_claims"]
