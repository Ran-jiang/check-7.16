"""引用溯源、判定和结果输出的应用层编排。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import os
import re
from pathlib import Path
from typing import Iterable

from ..domain.citation import ArticleRef, Claim, ClaimDocument, ClaimType, StructureRef
from ..domain.evidence import ArticleEvidence, ArticleExcerpt, LookupStatus, SourceTier, SourceTrace
from ..domain.checks import CheckVerdict, ExecutionStatus
from ..domain.legal_numbers import chinese_number_to_int
from ..domain.result import FrontendVerificationDocument
from ..domain.statute_results import (
    StatuteErrorCode,
    StatuteFinding,
    StatuteLocationResolution,
    StatuteLocator,
    StatuteMeaningCheck,
    StatuteVerificationResult,
    StatuteVersion,
)
from ..domain.revisions import RevisionProposal
from ..infrastructure.database import (
    connect,
    find_law,
    list_articles_in_structure,
    list_historical_article_versions,
    list_law_titles,
    resolve_structure_path,
)
from ..judgment.cases import verify_case_claims
from ..judgment.semantic import SemanticChecker, SemanticCheckError
from ..judgment.statutes import (
    LocationAssessment,
    LocationStatus,
    assess_location,
    assess_statute,
    classify_not_verifiable,
    parse_article_structure,
    resolve_location_candidates,
)
from ..judgment.service import (
    compare_with_llm,
    decide_semantic_gate,
    skipped_semantic_result,
)
from ..recognition.spans import locate_claim_article_spans
from ..tracing.service import build_default_sources, build_eu_sources, run_lookup_batch
from ..tracing.sources import (
    CaseSearcher,
    LookupRequest,
    LookupResult,
    PkulawCaseSource,
    StatuteSource,
)
from ..tracing.sources.eurlex import article_number_from_citation, fetch_article_excerpt

_EU_CN_ARTICLE_PATTERN = re.compile(r"第([一二三四五六七八九十百千零两0-9]+)条")
_EU_EN_ARTICLE_PATTERN = re.compile(r"Article\s+(\d+)", re.IGNORECASE)


def _semantic_workers() -> int:
    return max(1, int(os.getenv("QWEN_SEMANTIC_WORKERS", "4")))


def _salvage_max() -> int:
    return max(0, int(os.getenv("QWEN_SALVAGE_MAX", "8")))


def verify_claim_document(
    claim_document: ClaimDocument,
    database_path: str | Path,
    sources: Iterable[StatuteSource] | None = None,
    semantic_checker: SemanticChecker | None = None,
    case_searcher: CaseSearcher | None = None,
    include_statutes: bool = True,
    include_cases: bool = True,
) -> FrontendVerificationDocument:
    """编排一份引用文档的溯源、判定和结果输出。"""
    source_chain = (
        list(sources) if sources is not None else build_default_sources(database_path)
    )
    searcher = case_searcher or PkulawCaseSource()
    items = _collect_check_items(claim_document) if include_statutes else []
    lookup_results = _run_lookups(source_chain, items, database_path)
    historical_versions = _load_historical_versions(
        database_path, items, lookup_results
    )
    location_repairs = _run_location_repairs(
        source_chain, items, lookup_results, historical_versions
    )
    judgments = _run_judgments(
        semantic_checker,
        items,
        lookup_results,
        _load_known_titles(database_path),
        historical_versions,
        location_repairs,
    )
    _resolve_repealed_successors(source_chain, items, judgments)
    _verify_semantic_locator_candidates(
        source_chain, items, judgments, lookup_results, semantic_checker
    )
    statute_results = _aggregate_duplicate_statute_results(
        _build_statute_results(items, lookup_results, judgments)
    )
    case_results = (
        verify_case_claims(claim_document, searcher, semantic_checker)
        if include_cases
        else []
    )
    return FrontendVerificationDocument(
        source_claim_doc_id=claim_document.claim_meta.claim_doc_id,
        statute_results=statute_results,
        case_results=case_results,
    )


@dataclass
class _CheckItem:
    """一条待核查的引用、法规与条款组合。"""

    claim: Claim
    law_title: str
    source_type: str
    article: ArticleRef | None
    article_no: str | None
    not_verifiable: str | None
    jurisdiction: str = "CN"
    out_of_scope: str | None = None
    structure: StructureRef | None = None

    @property
    def skip_lookup(self) -> bool:
        return self.not_verifiable is not None or self.out_of_scope is not None

    @property
    def lookup_key(self) -> tuple:
        if self.article_no:
            return (self.law_title, self.source_type, self.article_no)
        return (
            self.law_title,
            self.source_type,
            None,
            self.claim.context_text or self.claim.text,
        )

    @property
    def document_quote(self) -> str:
        if self.article and self.article.span_status == "located" and self.article.quote_span:
            start, end = self.article.quote_span
            if 0 <= start < end <= len(self.claim.text):
                return self.claim.text[start:end]
        return self.claim.text

    @property
    def reference_role(self) -> str:
        return self.article.reference_role if self.article else "direct"

    @property
    def span_status(self) -> str:
        return self.article.span_status if self.article else "fallback"


def _collect_check_items(claim_document: ClaimDocument) -> list[_CheckItem]:
    items: list[_CheckItem] = []
    for claim in claim_document.claims:
        if claim.claim_type != ClaimType.LEGAL_SOURCE_CLAIM:
            continue
        if any(
            article.span_status == "fallback"
            for source in getattr(claim.entities, "legal_sources", [])
            for article in source.articles
        ):
            locate_claim_article_spans(claim)
        for legal_source in getattr(claim.entities, "legal_sources", []):
            not_verifiable = classify_not_verifiable(legal_source.title)
            jurisdiction = legal_source.jurisdiction
            out_of_scope = _out_of_scope_message(jurisdiction)
            if not legal_source.articles and legal_source.structures:
                for structure in legal_source.structures:
                    items.append(_CheckItem(
                        claim=claim,
                        law_title=legal_source.title,
                        source_type=legal_source.source_type.value,
                        article=None,
                        article_no=structure.label,
                        not_verifiable=not_verifiable,
                        jurisdiction=jurisdiction,
                        out_of_scope=out_of_scope,
                        structure=structure,
                    ))
                continue
            for article in legal_source.articles or [None]:
                items.append(
                    _CheckItem(
                        claim=claim,
                        law_title=legal_source.title,
                        source_type=legal_source.source_type.value,
                        article=article,
                        article_no=article.article if article is not None else None,
                        not_verifiable=not_verifiable,
                        jurisdiction=jurisdiction,
                        out_of_scope=out_of_scope,
                    )
                )
    return items


def _out_of_scope_message(jurisdiction: str) -> str | None:
    if jurisdiction == "FOREIGN":
        return "该法规属于当前不支持的外国法域，超出本产品核查边界，请人工核验。"
    return None


def _run_lookups(
    source_chain: list[StatuteSource],
    items: list[_CheckItem],
    database_path: str | Path,
) -> dict[tuple, tuple[LookupResult, list[SourceTrace]]]:
    requests: dict[tuple, LookupRequest] = {}
    eu_requests: dict[tuple, LookupRequest] = {}
    for item in items:
        if item.skip_lookup or item.structure is not None:
            continue
        bucket = eu_requests if item.jurisdiction == "EU" else requests
        bucket.setdefault(
            item.lookup_key,
            LookupRequest(
                law_title=item.law_title,
                source_type=item.source_type,
                article_no=item.article_no,
                context_text=item.claim.context_text or item.claim.text,
            ),
        )
    results = run_lookup_batch(source_chain, requests)
    if eu_requests:
        results.update(run_lookup_batch(build_eu_sources(), eu_requests))
    results.update(_run_structure_lookups(items, database_path))
    return results


def _run_structure_lookups(
    items: list[_CheckItem],
    database_path: str | Path,
) -> dict[tuple, tuple[LookupResult, list[SourceTrace]]]:
    results: dict[tuple, tuple[LookupResult, list[SourceTrace]]] = {}
    for item in items:
        if item.structure is None or item.skip_lookup or item.lookup_key in results:
            continue
        results[item.lookup_key] = _lookup_structure(item, database_path)
    return results


def _lookup_structure(
    item: _CheckItem, database_path: str | Path
) -> tuple[LookupResult, list[SourceTrace]]:
    trace = SourceTrace(
        tier=SourceTier.LOCAL_SQLITE,
        source_name="CCiteCheck 本地章节结构",
        status=LookupStatus.LAW_NOT_FOUND,
    )
    with connect(database_path) as connection:
        law = find_law(connection, item.law_title)
        if law is None:
            trace.message = "本地法规库未收录该法规，章节引用无法核验"
            return LookupResult(trace.status, None, trace), [trace]
        tokens = [(unit.unit, unit.number) for unit in item.structure.units]
        candidates = resolve_structure_path(connection, int(law["id"]), tokens)
        if not candidates:
            trace.status = LookupStatus.LAW_FOUND_ARTICLE_MISSING
            trace.message = f"现行章节结构中不存在{item.structure.label}"
            evidence = ArticleEvidence(
                law_title=law["title"], source_type=law["source_type"],
                article_no=item.structure.label, data_source=trace,
            )
            return LookupResult(trace.status, evidence, trace), [trace]
        trace.status = LookupStatus.RELEVANT_ARTICLES_FOUND
        trace.metadata["candidate_count"] = len(candidates)
        if len(candidates) == 1:
            node = candidates[0]
            members = list_articles_in_structure(connection, int(node["id"]))
            trace.message = f"已定位章节：{node['path_label']}"
            evidence = ArticleEvidence(
                law_title=law["title"], source_type=law["source_type"],
                article_no=item.structure.label, version_status=law["status"],
                structure_path=node["path_label"], data_source=trace,
                related_articles=[ArticleExcerpt(
                    article_no=row["article_no"], article_text=row["text"][:200],
                    relevance_score=1.0,
                ) for row in members[:3]],
            )
            return LookupResult(trace.status, evidence, trace), [trace]
        paths = [row["path_label"] for row in candidates[:5]]
        trace.message = f"存在 {len(candidates)} 个候选章节，请补充上级编号"
        evidence = ArticleEvidence(
            law_title=law["title"], source_type=law["source_type"],
            article_no=item.structure.label, version_status=law["status"],
            structure_path="候选：" + "；".join(paths), data_source=trace,
        )
        return LookupResult(trace.status, evidence, trace), [trace]


def _run_location_repairs(
    source_chain: list[StatuteSource],
    items: list[_CheckItem],
    lookup_results: dict[tuple, tuple[LookupResult, list[SourceTrace]]],
    historical_versions: dict[tuple, list[StatuteVersion]],
) -> dict[int, StatuteLocationResolution]:
    locator_source = next((
        source
        for source in source_chain
        if callable(getattr(source, "locate_candidates", None))
    ), None)
    if locator_source is None:
        return {}
    repairs: dict[int, StatuteLocationResolution] = {}
    for index, item in enumerate(items):
        if item.lookup_key not in lookup_results or not _has_subarticle_locator(item):
            continue
        lookup_result, _ = lookup_results[item.lookup_key]
        if _assess_item_location(item, lookup_result).status != LocationStatus.INVALID:
            continue
        if _matching_historical_location(
            item, historical_versions.get(item.lookup_key, [])
        ) is not None:
            continue
        # The exact article has already been retrieved and parsed.  Reuse it
        # before another network lookup: an invalid paragraph/item number can
        # often be repaired deterministically from the quoted text itself.
        if lookup_result.evidence is not None:
            local_resolution = resolve_location_candidates(
                item.document_quote, [lookup_result.evidence]
            )
            if local_resolution.status != "not_found":
                local_resolution.source_trace = lookup_result.trace
                repairs[index] = local_resolution
                continue
        candidate_result = locator_source.locate_candidates(LookupRequest(
            law_title=item.law_title,
            source_type=item.source_type,
            article_no=item.article_no,
            context_text=item.document_quote,
        ))
        resolution = resolve_location_candidates(
            item.document_quote, candidate_result.candidates
        )
        resolution.source_trace = candidate_result.trace
        candidate_result.trace.metadata["location_candidates"] = [
            candidate.model_dump(mode="json")
            for candidate in resolution.candidates
        ]
        repairs[index] = resolution
    return repairs


def _location_suggestion(
    resolution: StatuteLocationResolution | None,
) -> str:
    if resolution is None or resolution.status == "not_found":
        return "请核实所引款、项编号。"
    if resolution.status == "candidates_pending":
        return "北大法宝返回了多个可能对应的位置，请结合上下文人工确认。"
    locator = resolution.candidates[0].locator
    target = "".join(filter(None, (
        locator.article_no,
        locator.paragraph_no,
        locator.item_no,
    )))
    return f"经北大法宝候选原文核对，所述内容对应{target}，请更正引用位置。"


def _location_user_message(
    problem: str,
    resolution: StatuteLocationResolution | None,
) -> str:
    suggestion = _location_suggestion(resolution)
    if resolution is not None and resolution.status == "resolved":
        return suggestion
    return f"{problem}，{suggestion}"


def _has_subarticle_locator(item: _CheckItem) -> bool:
    return bool(item.article and (item.article.paragraphs or item.article.items))


def _run_judgments(
    semantic_checker: SemanticChecker | None,
    items: list[_CheckItem],
    lookup_results: dict[tuple, tuple[LookupResult, list[SourceTrace]]],
    known_titles: list[str],
    historical_versions: dict[tuple, list[StatuteVersion]],
    location_repairs: dict[int, StatuteLocationResolution],
) -> dict[int, tuple[list[StatuteFinding], StatuteMeaningCheck | None]]:
    results: dict[int, tuple[list[StatuteFinding], StatuteMeaningCheck | None]] = {}
    semantic_jobs: list[tuple[int, _CheckItem, LookupResult]] = []

    for index, item in enumerate(items):
        if item.skip_lookup:
            results[index] = ([], None)
            continue
        lookup_result, attempts = lookup_results[item.lookup_key]
        if item.structure is not None:
            if lookup_result.status == LookupStatus.LAW_FOUND_ARTICLE_MISSING:
                results[index] = ([StatuteFinding(
                    code=StatuteErrorCode.CITATION_LOCATION_ERROR,
                    risk_level="HIGH",
                    summary=lookup_result.trace.message,
                    suggestion=f"{lookup_result.trace.message}，请核实章节编号及其上级结构。",
                )], None)
            elif int(lookup_result.trace.metadata.get("candidate_count", 1)) > 1:
                results[index] = (
                    [], skipped_semantic_result("structure_ambiguous")
                )
            else:
                results[index] = ([], None)
            continue
        findings = assess_statute(
            item.law_title,
            item.article_no,
            lookup_result,
            attempts,
            known_titles,
            historical_versions.get(item.lookup_key),
        )
        location = _assess_item_location(item, lookup_result)
        if location.status == LocationStatus.INVALID:
            historical = _matching_historical_location(
                item, historical_versions.get(item.lookup_key, [])
            )
            if historical is not None:
                findings.append(StatuteFinding(
                    code=StatuteErrorCode.SOURCE_AMENDED,
                    risk_level="HIGH",
                    summary=f"现行版本中{location.message}，但历史版本存在所引位置",
                    suggestion=f"现行版本中{location.message}，但历史版本存在该位置；请核实适用时间并改引现行规定。",
                    cited_locator=_item_locators(item)[0],
                    historical_version=historical,
                ))
            else:
                repair = location_repairs.get(index)
                finding = StatuteFinding(
                    code=StatuteErrorCode.CITATION_LOCATION_ERROR,
                    risk_level="HIGH",
                    summary=location.message,
                    suggestion=_location_user_message(location.message, repair),
                )
                if repair is not None and repair.status == "resolved":
                    finding.resolved_locator = repair.candidates[0].locator
                    finding.revision = _locator_revision(item, repair.candidates[0].locator)
                findings.append(finding)
        elif location.status == LocationStatus.STRUCTURE_UNAVAILABLE:
            results[index] = (
                findings,
                skipped_semantic_result("citation_structure_unavailable"),
            )
            continue
        if (
            lookup_result.status == LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE
            and _is_existence_only_reference(item)
        ):
            results[index] = (findings, None)
            continue
        gate = decide_semantic_gate(
            lookup_result,
            findings,
            reference_role=item.reference_role,
            span_status=item.span_status,
        )
        if not gate.proceed:
            results[index] = (findings, skipped_semantic_result(gate.reason or "retrieval_incomplete"))
            continue
        results[index] = (findings, None)
        semantic_jobs.append((index, item, lookup_result))

    if semantic_jobs and semantic_checker is not None:
        unique_jobs: dict[str, tuple[_CheckItem, LookupResult]] = {}
        job_ids: dict[int, str] = {}
        for index, item, lookup_result in semantic_jobs:
            job_id = _semantic_job_id(item, lookup_result)
            job_ids[index] = job_id
            unique_jobs.setdefault(job_id, (item, lookup_result))
        with ThreadPoolExecutor(max_workers=_semantic_workers()) as pool:
            comparisons = dict(zip(
                unique_jobs,
                pool.map(lambda job: _compare_job(semantic_checker, job[0], job[1]), unique_jobs.values()),
            ))
        salvage_ids = [
            job_id
            for job_id, comparison in comparisons.items()
            if comparison.execution_status == "llm_error" and comparison.retryable
        ][:_salvage_max()]
        for job_id in salvage_ids:
            item, lookup_result = unique_jobs[job_id]
            recovered = _compare_job(semantic_checker, item, lookup_result)
            if recovered.execution_status == "completed":
                recovered.notes = (
                    f"{recovered.notes}（打捞轮恢复）"
                    if recovered.notes
                    else "（打捞轮恢复）"
                )
                comparisons[job_id] = recovered
            else:
                comparisons[job_id] = recovered
        for index, item, _ in semantic_jobs:
            comparison = comparisons[job_ids[index]].model_copy(deep=True)
            comparison.job_id = job_ids[index]
            findings, _ = results[index]
            results[index] = (findings, comparison)
            if item.jurisdiction == "EU":
                _append_verified_eu_candidate(item, lookup_results[item.lookup_key][0], comparison)
    return results


def _is_existence_only_reference(item: _CheckItem) -> bool:
    """同一句并列列举多个无条号法源时，只核验存在性与效力。"""
    if item.article_no or item.article is not None:
        return False
    sources = list(getattr(item.claim.entities, "legal_sources", []))
    return len(sources) > 1 and all(not source.articles for source in sources)


def _locator_revision(
    item: _CheckItem, resolved: StatuteLocator
) -> RevisionProposal | None:
    cited = _item_locators(item)[0]
    original_locator = "".join(filter(None, (
        cited.article_no, cited.paragraph_no, cited.item_no,
    )))
    revised_locator = "".join(filter(None, (
        resolved.article_no, resolved.paragraph_no, resolved.item_no,
    )))
    if not original_locator or not revised_locator or original_locator == revised_locator:
        return None
    if item.claim.text.count(original_locator) != 1:
        sub_original = "".join(filter(None, (cited.paragraph_no, cited.item_no)))
        sub_revised = "".join(filter(None, (resolved.paragraph_no, resolved.item_no)))
        if not sub_original or not sub_revised or item.claim.text.count(sub_original) != 1:
            return None
        original_locator, revised_locator = sub_original, sub_revised
    revised_text = item.claim.text.replace(original_locator, revised_locator, 1)
    return RevisionProposal(
        strategy="replace_exact_text",
        original_text=item.claim.text,
        revised_text=revised_text,
        rationale=f"将错误引用位置更正为{revised_locator}",
        machine_applicable=True,
        preconditions=[
            "original_text_unique",
            "document_unchanged",
            "candidate_deterministically_verified",
        ],
    )


_MAX_LOCATOR_PROPOSALS = 3


def _verify_semantic_locator_candidates(
    source_chain: list[StatuteSource],
    items: list[_CheckItem],
    judgments: dict[int, tuple[list[StatuteFinding], StatuteMeaningCheck | None]],
    lookup_results: dict[tuple, tuple[LookupResult, list[SourceTrace]]],
    semantic_checker: SemanticChecker | None = None,
) -> None:
    """迭代验证 LLM 提名的候选条号：取回原文→确定性包含或语义比对确认→
    不匹配则携带验证反馈索取下一个候选，最多三轮，穷尽后明示已复查条号。"""
    locator_source = next((
        source for source in source_chain
        if callable(getattr(source, "locate_candidates", None))
    ), None)
    if locator_source is None:
        return
    for index, item in enumerate(items):
        _, meaning = judgments[index]
        if item.jurisdiction != "CN" or meaning is None:
            continue
        for finding in meaning.findings:
            if not finding.location_recheck_required:
                continue
            candidate = finding.candidate_article_no or _regex_candidate(finding, item)
            if candidate is None:
                _resolve_by_semantic_recall(locator_source, item, finding)
                continue
            tried = _run_locator_proposal_loop(
                locator_source, semantic_checker, item, finding, candidate,
                _cited_article_text(lookup_results, item),
            )
            if finding.resolved_locator is None and tried:
                tried_list = "、".join(entry["article_no"] for entry in tried)
                finding.suggestion = (
                    f"{finding.suggestion} 已复查{tried_list}，"
                    "均与引文内容不符，请人工确认实际引用条款。"
                )


def _run_locator_proposal_loop(
    locator_source,
    semantic_checker: SemanticChecker | None,
    item: _CheckItem,
    finding: StatuteFinding,
    candidate: str,
    cited_article_text: str,
) -> list[dict[str, str]]:
    tried: list[dict[str, str]] = []
    for round_no in range(_MAX_LOCATOR_PROPOSALS):
        if (
            candidate is None
            or candidate == item.article_no
            or any(entry["article_no"] == candidate for entry in tried)
        ):
            break
        lookup = locator_source.lookup(LookupRequest(
            law_title=item.law_title,
            source_type=item.source_type,
            article_no=candidate,
            context_text=item.document_quote,
        ))
        if lookup.status == LookupStatus.ARTICLE_FOUND and lookup.evidence:
            if _approve_candidate(semantic_checker, item, finding, lookup.evidence, candidate):
                return tried
            tried.append({
                "article_no": candidate,
                "article_text": lookup.evidence.article_text or "",
                "mismatch": "条文原文与文书内容不构成对应",
            })
        else:
            tried.append({
                "article_no": candidate,
                "article_text": "",
                "mismatch": "北大法宝未取到该条原文",
            })
        if round_no + 1 >= _MAX_LOCATOR_PROPOSALS:
            break
        propose = getattr(semantic_checker, "propose_locator_candidate", None)
        if not callable(propose):
            break
        try:
            candidate = propose(
                law_title=item.law_title,
                document_quote=item.document_quote,
                cited_article_no=item.article_no or "",
                cited_article_text=cited_article_text,
                tried=tried,
            )
        except SemanticCheckError:
            break
    return tried


def _approve_candidate(
    semantic_checker: SemanticChecker | None,
    item: _CheckItem,
    finding: StatuteFinding,
    evidence: ArticleEvidence,
    candidate: str,
) -> bool:
    resolution = resolve_location_candidates(item.document_quote, [evidence])
    if resolution.status == "resolved":
        resolved = resolution.candidates[0].locator
        finding.resolved_locator = resolved
        finding.revision = _locator_revision(item, resolved)
        if finding.revision:
            finding.suggestion = f"经北大法宝再次核对，所述内容对应{resolved.article_no}，建议更正引用位置。"
        return True
    if semantic_checker is None:
        return False
    try:
        confirmation = semantic_checker.compare(
            item.document_quote,
            item.claim.text,
            f"《{item.law_title}》{candidate}",
            evidence,
        )
    except SemanticCheckError:
        return False
    # 定位确认问的是"引文说的是不是这条"，不是"转述是否零瑕疵"：
    # 判曲解但可比（recheck=False）说明引文确指该条，条号照改，瑕疵随卡片提示。
    if confirmation.verdict == CheckVerdict.PASS:
        residual = None
    elif confirmation.verdict == CheckVerdict.ISSUE and confirmation.findings and all(
        not issue.location_recheck_required for issue in confirmation.findings
    ):
        residual = confirmation.findings[0].suggestion
    else:
        return False
    resolved = StatuteLocator(article_no=candidate)
    finding.resolved_locator = resolved
    finding.revision = _locator_revision(item, resolved)
    if finding.revision:
        finding.suggestion = f"经北大法宝复查并比对原文，所述内容对应{candidate}，建议更正引用条号。"
        if residual:
            finding.suggestion += f"更正后请注意：{residual}"
        return True
    return False


def _regex_candidate(finding: StatuteFinding, item: _CheckItem) -> str | None:
    """结构化字段缺失时从结论文本回捞唯一候选条号（兼容旧模型输出）。"""
    candidates: dict[int, str] = {}
    for match in _EU_CN_ARTICLE_PATTERN.finditer(f"{finding.summary} {finding.suggestion}"):
        number = chinese_number_to_int(match.group(1))
        if number:
            candidates[number] = match.group(1)
    candidates.pop(article_number_from_citation(item.article_no), None)
    if len(candidates) != 1:
        return None
    return f"第{next(iter(candidates.values()))}条"


def _resolve_by_semantic_recall(locator_source, item: _CheckItem, finding: StatuteFinding) -> None:
    """无任何候选线索时按语义召回，仅确定性包含成立才批准修订。"""
    recalled = locator_source.locate_candidates(LookupRequest(
        law_title=item.law_title,
        source_type=item.source_type,
        article_no=item.article_no,
        context_text=item.document_quote,
    ))
    resolution = resolve_location_candidates(item.document_quote, recalled.candidates)
    if resolution.status != "resolved":
        return
    resolved = resolution.candidates[0].locator
    finding.resolved_locator = resolved
    finding.revision = _locator_revision(item, resolved)
    if finding.revision:
        finding.suggestion = f"经北大法宝再次核对，所述内容对应{resolved.article_no}，建议更正引用位置。"


def _cited_article_text(
    lookup_results: dict[tuple, tuple[LookupResult, list[SourceTrace]]],
    item: _CheckItem,
) -> str:
    entry = lookup_results.get(item.lookup_key)
    if entry is None or entry[0].evidence is None:
        return ""
    return entry[0].evidence.article_text or ""


_CITATION_TEXT = re.compile(r"《[^》]+》第[^，。；：]{1,30}条(?:第[^，。；：]{1,12}[款项])?")


def _resolve_repealed_successors(
    source_chain: list[StatuteSource],
    items: list[_CheckItem],
    judgments: dict[int, tuple[list[StatuteFinding], StatuteMeaningCheck | None]],
) -> None:
    """仅批准能由候选权威正文直接支持的唯一跨法源替换。"""
    source = next((
        candidate for candidate in source_chain
        if callable(getattr(candidate, "locate_successor_candidates", None))
    ), None)
    if source is None:
        return
    for index, item in enumerate(items):
        findings, _ = judgments[index]
        repealed = next((f for f in findings if f.code == StatuteErrorCode.SOURCE_REPEALED), None)
        if repealed is None or item.jurisdiction != "CN":
            continue
        result = source.locate_successor_candidates(LookupRequest(
            law_title=item.law_title,
            source_type=item.source_type,
            context_text=item.document_quote,
        ))
        supported = [
            candidate for candidate in result.candidates
            if _candidate_supports_quote(item.document_quote, candidate.article_text or "")
        ]
        unique = {
            (candidate.law_title, candidate.article_no): candidate
            for candidate in supported
            if candidate.article_no
        }
        if len(unique) != 1:
            if result.candidates:
                repealed.suggestion = "北大法宝返回了多个现行继受法候选，请人工确认后再修改引用。"
            continue
        candidate = next(iter(unique.values()))
        revision = _successor_revision(item, candidate)
        if revision is None:
            continue
        repealed.resolved_locator = StatuteLocator(article_no=candidate.article_no)
        repealed.suggestion = (
            f"该法源已经废止；经北大法宝核对，现行对应规则为"
            f"《{candidate.law_title}》{candidate.article_no}，建议更新法源和条号。"
        )
        repealed.revision = revision


def _candidate_supports_quote(document_quote: str, article_text: str) -> bool:
    normalized_article = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", article_text)
    without_citation = _CITATION_TEXT.sub("", document_quote)
    clauses = [
        re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", clause)
        for clause in re.split(r"[，。；：]", without_citation)
    ]
    return any(len(clause) >= 8 and clause in normalized_article for clause in clauses)


def _successor_revision(item: _CheckItem, evidence: ArticleEvidence) -> RevisionProposal | None:
    if not evidence.article_no:
        return None
    cited = _item_locators(item)[0]
    old_citation = f"《{item.law_title}》{cited.article_no or ''}"
    new_citation = f"《{evidence.law_title}》{evidence.article_no}"
    if item.claim.text.count(old_citation) != 1:
        return None
    return RevisionProposal(
        strategy="replace_exact_text",
        original_text=item.claim.text,
        revised_text=item.claim.text.replace(old_citation, new_citation, 1),
        rationale="将已废止法源更新为经权威数据源核验的现行继受条文",
        machine_applicable=True,
        preconditions=[
            "original_text_unique",
            "document_unchanged",
            "candidate_deterministically_verified",
        ],
    )


def _matching_historical_location(
    item: _CheckItem, versions: list[StatuteVersion]
) -> StatuteVersion | None:
    for version in versions:
        structure = parse_article_structure(version.article_no, version.article_text)
        if assess_location(structure, _item_locators(item)).status == LocationStatus.VALID:
            return version
    return None


def _append_verified_eu_candidate(
    item: _CheckItem,
    lookup_result: LookupResult,
    comparison: StatuteMeaningCheck,
) -> None:
    evidence = lookup_result.evidence
    if evidence is None or not comparison.findings:
        return
    celex = evidence.source_metadata.get("celex")
    if not celex:
        return
    cited = article_number_from_citation(item.article_no)
    numbers: set[int] = set()
    for finding in comparison.findings:
        text = f"{finding.summary} {finding.suggestion}"
        for match in _EU_CN_ARTICLE_PATTERN.finditer(text):
            number = chinese_number_to_int(match.group(1))
            if number:
                numbers.add(number)
        numbers.update(int(match.group(1)) for match in _EU_EN_ARTICLE_PATTERN.finditer(text))
    numbers.discard(cited)
    if len(numbers) != 1:
        return
    number = numbers.pop()
    excerpt = fetch_article_excerpt(celex, number)
    if excerpt is not None:
        evidence.related_articles.append(excerpt)


def _compare_job(
    semantic_checker: SemanticChecker,
    item: _CheckItem,
    lookup_result: LookupResult,
) -> StatuteMeaningCheck:
    if lookup_result.evidence is None:
        raise ValueError("语义核查任务缺少法条证据")
    evidence = lookup_result.evidence
    location = _assess_item_location(item, lookup_result)
    if location.status != LocationStatus.VALID:
        raise ValueError("语义核查任务缺少已验证的条款项定位")
    if location.authoritative_text:
        evidence = evidence.model_copy(update={"article_text": location.authoritative_text})
    return compare_with_llm(
        semantic_checker,
        item.document_quote,
        item.claim.text,
        _cited_source(item),
        evidence,
    )


def _assess_item_location(item: _CheckItem, lookup_result: LookupResult):
    if item.article and len(item.article.paragraphs) > 1 and item.article.items:
        return LocationAssessment(
            LocationStatus.STRUCTURE_UNAVAILABLE,
            "抽取结果未记录各项分别属于哪一款",
        )
    evidence = lookup_result.evidence
    structure = (
        parse_article_structure(evidence.article_no or item.article_no or "", evidence.article_text)
        if evidence is not None and evidence.article_text
        else None
    )
    return assess_location(structure, _item_locators(item))


def _item_locators(item: _CheckItem) -> list[StatuteLocator]:
    if item.article is None:
        return [StatuteLocator(article_no=item.article_no)] if item.article_no else []
    paragraphs = item.article.paragraphs or [None]
    items = item.article.items or [None]
    return [
        StatuteLocator(
            article_no=item.article_no,
            paragraph_no=paragraph,
            item_no=subitem,
        )
        for paragraph in paragraphs
        for subitem in items
    ]


def _build_statute_results(
    items: list[_CheckItem],
    lookup_results: dict[tuple, tuple[LookupResult, list[SourceTrace]]],
    judgments: dict[int, tuple[list[StatuteFinding], StatuteMeaningCheck | None]],
) -> list[StatuteVerificationResult]:
    results: list[StatuteVerificationResult] = []
    card_ids: dict[str, str] = {}
    for index, item in enumerate(items):
        lookup_result = attempts = None
        if not item.skip_lookup:
            lookup_result, attempts = lookup_results[item.lookup_key]
        elif item.out_of_scope:
            attempts = [SourceTrace(
                tier=SourceTier.LOCAL_SQLITE,
                source_name="CCiteCheck 法域分类",
                status=LookupStatus.OUT_OF_SCOPE,
                message=item.out_of_scope,
            )]
        findings, meaning_check = judgments[index]
        all_findings = [*findings, *(meaning_check.findings if meaning_check else [])]
        card_id = card_ids.setdefault(
            item.claim.claim_id, f"card_{len(card_ids) + 1:05d}"
        )
        results.append(
            StatuteVerificationResult(
                check_id=f"vc_{index + 1:05d}",
                card_id=card_id,
                claim_id=item.claim.claim_id,
                claim_text=item.claim.text,
                law_title=item.law_title,
                jurisdiction=item.jurisdiction,
                document_quote=item.document_quote,
                cited_locators=_item_locators(item),
                lookup_status=(
                    lookup_result.status if lookup_result
                    else LookupStatus.OUT_OF_SCOPE if item.out_of_scope
                    else LookupStatus.NOT_VERIFIABLE
                ),
                evidence=(lookup_result.evidence if lookup_result else None),
                findings=all_findings,
                outcome=(
                    "bug" if item.out_of_scope
                    else _statute_outcome(all_findings, meaning_check, item.reference_role)
                ),
                message=(
                    meaning_check.notes if meaning_check
                    else item.out_of_scope or item.not_verifiable or ""
                ),
                meaning_check=meaning_check,
                reference_role=item.reference_role,
                source_locations=item.claim.source_locations,
                source_attempts=attempts or [],
            )
        )
    return results


def _aggregate_duplicate_statute_results(
    results: list[StatuteVerificationResult],
) -> list[StatuteVerificationResult]:
    """同一法条的同一错误因简称/承前引用在文书多处出现时，合并为一张
    卡片并保留全部出现位置，避免同一问题重复刷屏。"""
    aggregated: list[StatuteVerificationResult] = []
    groups: dict[tuple, StatuteVerificationResult] = {}
    counts: dict[tuple, int] = {}
    for result in results:
        if result.outcome != "issue" or not result.findings:
            aggregated.append(result)
            continue
        key = (
            result.law_title,
            tuple(
                (locator.article_no, locator.paragraph_no, locator.item_no)
                for locator in result.cited_locators
            ),
            tuple(
                (finding.code, finding.risk_level, finding.suggestion)
                for finding in result.findings
            ),
        )
        first = groups.get(key)
        if first is None:
            groups[key] = result
            counts[key] = 1
            aggregated.append(result)
            continue
        counts[key] += 1
        seen = {
            (location.platform, location.block_id, location.char_start, location.char_end)
            for location in first.source_locations
        }
        merged = list(first.source_locations)
        for location in result.source_locations:
            identity = (location.platform, location.block_id, location.char_start, location.char_end)
            if identity not in seen:
                seen.add(identity)
                merged.append(location)
        first.source_locations = merged
    for key, first in groups.items():
        if counts[key] > 1:
            note = f"该问题在文书中共出现 {counts[key]} 处，已合并为一条，可逐处定位。"
            first.message = f"{first.message} {note}".strip()
    return aggregated


def _statute_outcome(
    findings: list[StatuteFinding],
    meaning_check: StatuteMeaningCheck | None,
    reference_role: str,
) -> str:
    if findings:
        return "issue"
    if reference_role == "nested":
        return "pass"
    if meaning_check is None:
        return "pass"
    if meaning_check.execution_status != ExecutionStatus.COMPLETED:
        return "bug"
    return "pass" if meaning_check.verdict == CheckVerdict.PASS else "bug"


def _semantic_job_id(item: _CheckItem, lookup_result: LookupResult) -> str:
    evidence = lookup_result.evidence
    evidence_material = "\0".join([
        evidence.law_title if evidence else "",
        evidence.article_no or "" if evidence else "",
        evidence.article_text or "" if evidence else "",
    ])
    evidence_digest = hashlib.sha256(evidence_material.encode("utf-8")).hexdigest()[:16]
    material = "\0".join([
        item.law_title.strip(),
        item.article_no or "",
        _cited_source(item),
        " ".join(item.document_quote.split()),
        evidence_digest,
    ])
    return "sj_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _load_known_titles(database_path: str | Path) -> list[str]:
    path = Path(database_path)
    if not path.exists():
        return []
    with connect(path) as connection:
        return list_law_titles(connection)


def _load_historical_versions(
    database_path: str | Path,
    items: list[_CheckItem],
    lookup_results: dict[tuple, tuple[LookupResult, list[SourceTrace]]],
) -> dict[tuple, list[StatuteVersion]]:
    path = Path(database_path)
    if not path.exists():
        return {}
    targets = {
        item.lookup_key: item
        for item in items
        if item.article_no
        and item.lookup_key in lookup_results
        and (
            lookup_results[item.lookup_key][0].status
            == LookupStatus.LAW_FOUND_ARTICLE_MISSING
            or _assess_item_location(
                item, lookup_results[item.lookup_key][0]
            ).status == LocationStatus.INVALID
        )
    }
    if not targets:
        return {}
    result: dict[tuple, list[StatuteVersion]] = {}
    with connect(path) as connection:
        for key, item in targets.items():
            rows = list_historical_article_versions(
                connection, item.law_title, item.article_no or ""
            )
            if rows:
                result[key] = [
                    StatuteVersion(
                        version_key=row["version_key"],
                        version_label=row["version_label"],
                        version_status=row["version_status"],
                        effective_from=row["effective_from"],
                        effective_to=row["effective_to"],
                        article_no=row["article_no"],
                        article_text=row["text"],
                    )
                    for row in rows
                ]
    return result


def _cited_source(item: _CheckItem) -> str:
    if item.article is None:
        return f"《{item.law_title}》"
    locations = [
        item.article.article,
        *item.article.paragraphs,
        *item.article.items,
    ]
    return f"《{item.law_title}》" + "".join(locations)


__all__ = ["verify_claim_document"]
