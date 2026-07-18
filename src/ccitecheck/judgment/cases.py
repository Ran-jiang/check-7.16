"""司法案例的两级检索、身份确认与裁判观点核查。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from ..domain.citation import ClaimDocument, ClaimType
from ..domain.evidence import CaseEvidence, CaseLookupStatus, CaseSourceTrace
from ..domain.case_results import (
    CaseErrorCode,
    CaseFinding,
    CaseHoldingCheck,
    CaseVerificationResult,
)
from ..domain.checks import ExecutionStatus
from ..tracing.queries import build_case_keyword_query, build_case_semantic_query
from ..tracing.sources.pkulaw.cases import CaseSearcher
from ..tracing.sources.pkulaw.client import (
    PkulawCaseRecord,
    PkulawMcpError,
    PkulawNotConfiguredError,
    PkulawNotFoundError,
)
from .case_identity import (
    compare_case_identity,
    guiding_case_id as _guiding_case_id,
    normalize_case_name as _normalize_case_name,
    normalize_case_number as _normalize_case_number,
    normalize_court as _normalize_court,
    same_court as _same_court,
)

_CASE_LOOKUP_WORKERS = 6
@dataclass(frozen=True)
class _RouteOutcome:
    records: list[PkulawCaseRecord]
    trace: CaseSourceTrace
    completed: bool


def verify_case_claims(
    claim_doc: ClaimDocument,
    searcher: CaseSearcher,
    semantic_checker=None,
) -> list[CaseVerificationResult]:
    """按精准检索、语义补查的顺序核查案例引用。"""
    refs = _unique_case_refs(claim_doc)
    if not refs:
        return []
    with ThreadPoolExecutor(max_workers=_CASE_LOOKUP_WORKERS) as pool:
        outcomes = list(
            pool.map(
                lambda pair: _verify_case_reference(searcher, pair[0], pair[1]),
                refs,
            )
        )
    checks: list[CaseVerificationResult] = []
    for next_id, ((claim, ref), outcome) in enumerate(zip(refs, outcomes), start=1):
        status, evidence, message, traces = outcome
        holding_check = (
            _check_holding(claim, evidence, semantic_checker)
            if status == CaseLookupStatus.VERIFIED
            else None
        )
        findings = list(holding_check.findings) if holding_check else []
        if evidence is not None:
            identity = compare_case_identity(
                cited_case_number=ref.case_number,
                cited_case_name=ref.case_name,
                cited_court=ref.court,
                evidence=evidence,
            )
            if identity is not None:
                findings.insert(0, identity)
        elif status == CaseLookupStatus.NOT_FOUND:
            findings.append(CaseFinding(
                code=CaseErrorCode.CASE_NOT_FOUND,
                risk_level="HIGH",
                summary=message,
                suggestion="请核对案名、案号、法院或裁判日期。",
            ))
        checks.append(CaseVerificationResult(
            check_id=f"cc_{next_id:05d}",
            claim_id=claim.claim_id,
            claim_text=claim.text,
            jurisdiction=ref.jurisdiction,
            anchor_ids=list(claim.anchor_ids),
            source_locations=list(claim.source_locations),
            cited_case_number=ref.case_number,
            cited_case_name=ref.case_name,
            cited_court=ref.court,
            lookup_status=status,
            evidence=evidence,
            message=message,
            source_attempts=traces,
            findings=findings,
            outcome=("issue" if findings or status == CaseLookupStatus.NOT_FOUND else "pass" if status == CaseLookupStatus.VERIFIED else "bug"),
            holding_check=holding_check,
        ))
    return checks


def _unique_case_refs(claim_doc: ClaimDocument):
    result = []
    seen: set[tuple[str, str]] = set()
    for claim in claim_doc.claims:
        for ref in getattr(claim.entities, "case_refs", []):
            identity = _normalize_case_number(ref.case_number or "") or _normalize_case_name(ref.case_name or "")
            key = (claim.claim_id, identity)
            if key not in seen:
                seen.add(key)
                result.append((claim, ref))
    return result


def _verify_case_reference(searcher: CaseSearcher, claim, ref):
    if ref.jurisdiction != "CN":
        message = "该案例属于当前不支持的外国法域，请人工核验。"
        trace = CaseSourceTrace(
            source_name="CCiteCheck 案例法域分类",
            status=CaseLookupStatus.OUT_OF_SCOPE,
            message=message,
        )
        return CaseLookupStatus.OUT_OF_SCOPE, None, message, [trace]
    title, fulltext = _exact_query(claim, ref)
    if not title and not fulltext:
        message = "现有引用信息不足以构造案例检索条件。"
        trace = CaseSourceTrace(
            source_name="CCiteheck 案例检索路由",
            status=CaseLookupStatus.MANUAL_REVIEW,
            message=message,
        )
        return CaseLookupStatus.MANUAL_REVIEW, None, message, [trace]

    exact = _run_route(
        "北大法宝 MCP：get_case_list",
        lambda: searcher.search_keyword(title, fulltext),
        {"route": "get_case_list", "title_query": title, "fulltext_query": fulltext},
    )
    traces = [exact.trace]
    if not exact.completed:
        return exact.trace.status, None, exact.trace.message, traces

    match, basis = _match_case_record(ref.case_number, ref.case_name, ref.court, exact.records)
    if match is not None:
        if claim.claim_type == ClaimType.CASE_HOLDING_PARAPHRASE and not match.holding:
            supplemented = _semantic_route(searcher, claim, ref)
            traces.append(supplemented.trace)
            if not supplemented.completed:
                return supplemented.trace.status, None, supplemented.trace.message, traces
            same = _same_case(match, supplemented.records)
            if same is not None and same.holding:
                match = same
        return _verified(match, ref.case_name, basis, traces)

    semantic = _semantic_route(searcher, claim, ref)
    traces.append(semantic.trace)
    if not semantic.completed:
        return semantic.trace.status, None, semantic.trace.message, traces

    match, basis = _match_case_record(ref.case_number, ref.case_name, ref.court, semantic.records)
    if match is None:
        match = _cross_route_match(exact.records, semantic.records)
        basis = "cross_route_case_number" if match is not None else None
    if match is not None:
        return _verified(match, ref.case_name, basis, traces)

    candidates = _unique_records([*exact.records, *semantic.records])
    if candidates:
        metadata = [_candidate_metadata(record) for record in candidates[:10]]
        traces[-1].metadata["candidates"] = metadata
        message = "北大法宝返回了案例，但现有引用信息不足以证明是哪一份裁判文书。可参考案例如下。"
        return (
            CaseLookupStatus.MANUAL_REVIEW,
            None,
            message,
            traces,
        )
    message = "北大法宝精准检索和语义检索均未找到文书引用的案例，请核对案名、案号、法院或裁判日期。"
    return CaseLookupStatus.NOT_FOUND, None, message, traces


def _exact_query(claim, ref) -> tuple[str, str]:
    title = (ref.case_name or "").strip()
    if ref.case_number:
        return title, ref.case_number.strip()
    _, fulltext = build_case_keyword_query(ref.case_name, claim.context_text or claim.text, ref.court)
    return title, fulltext


def _semantic_route(searcher, claim, ref) -> _RouteOutcome:
    query = build_case_semantic_query(ref.case_name, claim.context_text or claim.text, ref.court)
    return _run_route(
        "北大法宝 MCP：search_case",
        lambda: searcher.search_semantic(query),
        {"route": "search_case", "semantic_query": query},
    )


def _run_route(source_name, operation, metadata) -> _RouteOutcome:
    try:
        records = operation()
    except PkulawNotFoundError:
        records = []
    except PkulawMcpError as exc:
        status = _case_error_status(exc)
        return _RouteOutcome([], CaseSourceTrace(
            source_name=source_name,
            status=status,
            message=("数据源未配置" if status == CaseLookupStatus.SOURCE_NOT_CONFIGURED else "数据源调用失败"),
            metadata={**metadata, "error": str(exc)},
        ), False)
    status = CaseLookupStatus.MANUAL_REVIEW if records else CaseLookupStatus.NOT_FOUND
    return _RouteOutcome(records, CaseSourceTrace(
        source_name=source_name,
        status=status,
        message=("检索完成，返回案例候选" if records else "检索完成，未命中案例"),
        metadata={**metadata, "candidate_count": len(records)},
    ), True)


def _match_case_record(case_number, case_name, court, records):
    if case_number:
        target = _normalize_case_number(case_number)
        matches = [record for record in records if _normalize_case_number(record.case_number) == target]
        if len(matches) == 1:
            return matches[0], "exact_case_number"
    target_title = _normalize_case_name(case_name or "")
    if target_title:
        matches = [record for record in records if _normalize_case_name(record.title) == target_title]
        if len(matches) > 1 and court:
            matches = [record for record in matches if _same_court(court, record.court)]
            if len(matches) == 1:
                return matches[0], "exact_title_and_court"
        if len(matches) == 1:
            return matches[0], "exact_case_title"
        guiding_id = _guiding_case_id(target_title)
        if guiding_id:
            matches = [record for record in records if _guiding_case_id(_normalize_case_name(record.title)) == guiding_id]
            if len(matches) == 1:
                return matches[0], "exact_case_title"
    return None, None


def _verified(record, cited_name, basis, traces):
    traces[-1].status = CaseLookupStatus.VERIFIED
    traces[-1].source_url = record.url
    traces[-1].message = "案例身份匹配通过"
    traces[-1].metadata["match_basis"] = basis
    return (
        CaseLookupStatus.VERIFIED,
        _case_record_evidence(record, cited_name),
        "",
        traces,
    )


def _same_case(reference: PkulawCaseRecord, records: list[PkulawCaseRecord]):
    if reference.case_number:
        number = _normalize_case_number(reference.case_number)
        matches = [record for record in records if _normalize_case_number(record.case_number) == number]
    else:
        title = _normalize_case_name(reference.title)
        matches = [record for record in records if _normalize_case_name(record.title) == title]
    return matches[0] if len(matches) == 1 else None


def _cross_route_match(left, right):
    left_by_number = {_normalize_case_number(record.case_number): record for record in left if record.case_number}
    overlaps = {
        number: record for number, record in left_by_number.items()
        if any(_normalize_case_number(item.case_number) == number for item in right)
    }
    return next(iter(overlaps.values())) if len(overlaps) == 1 else None


def _unique_records(records):
    result = []
    seen = set()
    for record in records:
        key = _normalize_case_number(record.case_number) or f"{_normalize_case_name(record.title)}|{_normalize_court(record.court)}"
        if key not in seen:
            seen.add(key)
            result.append(record)
    return result


def _candidate_metadata(record):
    return {
        "title": record.title,
        "case_number": record.case_number,
        "court": record.court,
        "last_instance_date": record.last_instance_date,
        "url": record.url,
    }


def _case_record_evidence(record, cited_name):
    return CaseEvidence(
        matched_text=cited_name or record.title,
        case_number=record.case_number,
        gid=record.gid,
        court=record.court,
        title=record.title,
        last_instance_date=record.last_instance_date,
        url=record.url,
        holding=record.holding,
    )


def _check_holding(claim, evidence, semantic_checker):
    if claim.claim_type != ClaimType.CASE_HOLDING_PARAPHRASE:
        return None
    if evidence is None or not evidence.holding:
        return CaseHoldingCheck(
            execution_status=ExecutionStatus.SKIPPED,
            skipped_reason="holding_unavailable",
            notes="案例身份已经确认，但北大法宝 MCP 中未收录裁判观点。",
        )
    compare = getattr(semantic_checker, "compare_holding", None)
    if not callable(compare):
        return CaseHoldingCheck(
            execution_status=ExecutionStatus.SKIPPED,
            skipped_reason="semantic_disabled",
        )
    paraphrase = getattr(claim.entities, "holding_text", "") or claim.text
    try:
        return compare(paraphrase, evidence.holding, evidence.title)
    except Exception as exc:
        from .semantic import SemanticCheckError
        if not isinstance(exc, SemanticCheckError):
            raise
        return CaseHoldingCheck(
            execution_status=ExecutionStatus.LLM_ERROR,
            notes=str(exc),
            error_code=getattr(exc, "error_code", "semantic_error"),
        )


def _case_error_status(exc: PkulawMcpError) -> CaseLookupStatus:
    return CaseLookupStatus.SOURCE_NOT_CONFIGURED if isinstance(exc, PkulawNotConfiguredError) else CaseLookupStatus.SOURCE_ERROR


__all__ = ["verify_case_claims"]
