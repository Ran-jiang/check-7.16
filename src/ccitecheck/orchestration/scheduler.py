"""CCitecheck 核查控制平面与 Word 兼容流水线。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import os
import re
from pathlib import Path
from typing import Iterable

from ..domain.citation import (
    ArticleRef,
    Claim,
    ClaimDocument,
    ClaimType,
    StructureRef,
)
from ..domain.citation_integrity import (
    ensure_claim_citation_integrity,
    legal_source_alias_index,
)
from ..domain.evidence import ArticleEvidence, ArticleExcerpt, LookupStatus, SourceTier, SourceTrace
from ..domain.display_groups import display_group_id
from ..domain.checks import CheckVerdict, ExecutionStatus
from ..domain.legal_numbers import chinese_number_to_int
from ..domain.result import FrontendVerificationDocument, VERIFICATION_SCHEMA_VERSION
from ..domain.statute_results import (
    LegalApplicationCheck,
    StatuteErrorCode,
    StatuteFinding,
    StatuteLocationResolution,
    StatuteLocator,
    StatuteMeaningCheck,
    StatuteVerificationResult,
    StatuteVersion,
)
from ..domain.revisions import RevisionProposal
from ..domain.claims import RawClaim
from ..domain.evidence import TechnicalStatus
from ..domain.queries import HypothesisFeedback
from ..domain.runs import RunState, SourceAttempt, VerificationRun
from ..domain.verification import VerificationStatus
from ..query_construction import (
    QueryResources,
    build_initial_hypothesis,
    rebuild_hypothesis,
    resolve_identity_candidates,
)
from ..query_construction.jurisdiction import detect_jurisdiction
from ..retrieval import RetrievalTask, SourceRegistry, execute as execute_retrieval
from ..verification import VerificationContext, verify as verify_evidence
from ..infrastructure.database import (
    connect,
    find_law,
    list_articles_in_structure,
    list_historical_article_versions,
    list_law_titles,
    normalize_title,
    resolve_structure_path,
)
from ..verification.statutes.nested import resolve_nested_relations
from .cases import verify_case_claims
from ..verification.semantic import SemanticChecker, SemanticCheckError
from ..verification.legal_application import (
    ApplicationAuthority,
    ApplicationCandidate,
    build_application_jobs,
)
from ..verification.statutes import (
    LocationAssessment,
    LocationStatus,
    assess_location,
    assess_statute,
    classify_not_verifiable,
    parse_article_structure,
    resolve_location_candidates,
)
from ..verification.service import (
    compare_application_with_llm,
    compare_with_llm,
    skipped_semantic_result,
)
from ..recognition.spans import locate_claim_article_spans
from ..retrieval.service import build_default_sources, build_eu_sources
from ..retrieval.sources import (
    CaseSearcher,
    LookupRequest,
    LookupResult,
    PkulawCaseSource,
    StatuteSource,
)
from ..retrieval.sources.eurlex import article_number_from_citation, fetch_article_excerpt
from ..retrieval.sources.pkulaw.client import PkulawMcpClient
from .policies.retrieval import run_lookup_batch
from .policies.semantic import decide_semantic_gate
from .policies.nested import finalize_nested_dependencies

_EU_CN_ARTICLE_PATTERN = re.compile(r"第([一二三四五六七八九十百千零两0-9]+)条")
_EU_EN_ARTICLE_PATTERN = re.compile(r"Article\s+(\d+)", re.IGNORECASE)


@dataclass
class SchedulerContext:
    law_db: str | Path
    source_registry: SourceRegistry | None = None
    verification_context: VerificationContext | None = None
    semantic_checker: SemanticChecker | None = None
    case_searcher: CaseSearcher | None = None


class VerificationScheduler:
    """只控制下一步动作，不修改 RawClaim 或作法律结论。"""

    def __init__(self, context: SchedulerContext):
        self.context = context

    def verify_claim(self, claim: RawClaim) -> VerificationRun:
        run = VerificationRun(claim_id=claim.claim_id)
        resources = QueryResources(law_db=self.context.law_db)
        hypothesis = build_initial_hypothesis(
            claim, resources
        )
        run.hypothesis_history.append(hypothesis.hypothesis_id)
        registry = self.context.source_registry or SourceRegistry.default(
            self.context.law_db
        )
        attempted_titles: set[str] = set()

        while True:
            run.state = RunState.HYPOTHESIS_READY
            candidate_titles: list[str] = []
            for identity in sorted(hypothesis.identity_candidates, key=lambda item: item.priority):
                identity_key = normalize_title(identity.title)
                if identity_key in attempted_titles:
                    continue
                attempted_titles.add(identity_key)
                for planned in sorted(hypothesis.source_plan, key=lambda item: item.priority):
                    while True:
                        run.state = RunState.RETRIEVING
                        evidence = execute_retrieval(
                            planned.source_id,
                            hypothesis,
                            RetrievalTask(
                                kind=("case" if claim.case_mentions else "statute"),
                                context_text=claim.context_text or claim.raw_text,
                                identity_title=identity.title,
                                case_number=(
                                    claim.case_mentions[0].raw_case_number
                                    if claim.case_mentions else None
                                ),
                                case_name=(
                                    claim.case_mentions[0].raw_case_name
                                    if claim.case_mentions else None
                                ),
                                court=(
                                    claim.case_mentions[0].raw_court
                                    if claim.case_mentions else None
                                ),
                            ),
                            registry=registry,
                        )
                        run.evidence_history.append(evidence.evidence_id)
                        run.source_attempts.append(SourceAttempt(
                            source_id=planned.source_id,
                            hypothesis_id=hypothesis.hypothesis_id,
                            evidence_id=evidence.evidence_id,
                            technical_status=evidence.technical_status.value,
                        ))
                        if (
                            evidence.technical_status == TechnicalStatus.SOURCE_ERROR
                            and run.technical_retry_count < run.max_technical_retries
                        ):
                            run.technical_retry_count += 1
                            continue
                        break

                    candidate_titles.extend(
                        str(title)
                        for title in evidence.provider_metadata.get("candidate_titles", [])
                        if title
                    )
                    if evidence.technical_status != TechnicalStatus.COMPLETED:
                        continue
                    run.state = RunState.EVIDENCE_READY
                    run.state = RunState.VERIFYING
                    result = verify_evidence(
                        claim, evidence, self.context.verification_context
                    )
                    run.verification_history.append(result.verification_id)
                    run.state = RunState.VERIFIED
                    if result.overall_status != VerificationStatus.INSUFFICIENT_EVIDENCE:
                        run.state = RunState.OUTPUT
                        run.terminal_reason = "verification_complete"
                        return run

            next_title = next((
                title for title in candidate_titles
                if normalize_title(title) not in attempted_titles
            ), None)
            if next_title and run.hypothesis_retry_count < run.max_hypothesis_retries:
                hypothesis = rebuild_hypothesis(
                    claim,
                    hypothesis,
                    HypothesisFeedback(
                        trigger_reason="ambiguous_identity",
                        identity_title=next_title,
                        basis="retrieval_candidate",
                    ),
                    resources,
                )
                run.hypothesis_retry_count += 1
                run.hypothesis_history.append(hypothesis.hypothesis_id)
                continue

            run.state = RunState.STOPPED
            run.terminal_reason = (
                "hypothesis_limit_reached" if next_title else "all_sources_exhausted"
            )
            return run

    def verify_document(
        self,
        claim_document: ClaimDocument,
        *,
        include_statutes: bool = True,
        include_cases: bool = True,
    ) -> FrontendVerificationDocument:
        """第一阶段 Word 兼容入口；复用现有 DTO 聚合。"""
        return verify_claim_document(
            claim_document,
            self.context.law_db,
            semantic_checker=self.context.semantic_checker,
            case_searcher=self.context.case_searcher,
            include_statutes=include_statutes,
            include_cases=include_cases,
        )


def verify_claim(claim: RawClaim, context: SchedulerContext) -> VerificationRun:
    """执行单条 RawClaim 的可追溯核验流程。"""
    return VerificationScheduler(context).verify_claim(claim)


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
    shared_pkulaw = (
        PkulawMcpClient()
        if sources is None or case_searcher is None
        else None
    )
    source_chain = (
        list(sources)
        if sources is not None
        else build_default_sources(database_path, shared_pkulaw)
    )
    searcher = case_searcher or PkulawCaseSource()
    if case_searcher is None and shared_pkulaw is not None:
        searcher.client = shared_pkulaw
    items = (
        _collect_check_items(claim_document, database_path)
        if include_statutes else []
    )
    lookup_results = _run_lookups(source_chain, items, database_path)
    resolve_nested_relations(items, lookup_results, semantic_checker)
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
    finalize_nested_dependencies(items, judgments)
    application_checks = _run_application_checks(
        semantic_checker,
        items,
        lookup_results,
        judgments,
        claim_document.document_text,
    )
    statute_results = _aggregate_duplicate_statute_results(
        _build_statute_results(
            items, lookup_results, judgments, application_checks
        )
    )
    case_results = (
        verify_case_claims(claim_document, searcher, semantic_checker)
        if include_cases
        else []
    )
    return FrontendVerificationDocument(
        schema_version=VERIFICATION_SCHEMA_VERSION,
        source_claim_doc_id=claim_document.claim_meta.claim_doc_id,
        statute_results=statute_results,
        case_results=case_results,
    )


@dataclass
class _CheckItem:
    """一条待核查的引用、法规与条款组合。"""

    claim: Claim
    law_title: str
    display_title: str
    article: ArticleRef | None
    article_no: str | None
    not_verifiable: str | None
    jurisdiction: str = "CN"
    out_of_scope: str | None = None
    structure: StructureRef | None = None
    recognition_form: str = "explicit"
    law_identity_resolved: bool = True
    law_identity_resolver: str | None = None
    raw_title_candidate: str | None = None
    citation_span: tuple[int, int] | None = None
    reference_role: str = "direct"
    span_status: str = "fallback"
    parent_index: int | None = None
    relation_status: str | None = None
    relation_message: str = ""
    relation_candidate_article_no: str | None = None
    relation_parent_authoritative_text: str = ""

    @property
    def skip_lookup(self) -> bool:
        return (
            self.not_verifiable is not None
            or self.out_of_scope is not None
            or not self.law_identity_resolved
        )

    @property
    def lookup_key(self) -> tuple:
        if self.article_no:
            return (self.law_title, self.article_no)
        return (
            self.law_title,
            None,
            self.claim.context_text or self.claim.text,
        )

def _collect_check_items(
    claim_document: ClaimDocument, database_path: str | Path | None = None
) -> list[_CheckItem]:
    items: list[_CheckItem] = []
    resources = QueryResources(law_db=database_path)
    declarations = [
        declaration
        for claim in claim_document.claims
        for declaration in getattr(claim.entities, "alias_declarations", [])
    ]
    for claim in claim_document.claims:
        if claim.claim_type != ClaimType.LEGAL_SOURCE_CLAIM:
            continue
        if not getattr(claim.entities, "citations", []):
            locate_claim_article_spans(claim)
        ensure_claim_citation_integrity(claim)
        citations = getattr(claim.entities, "citations", [])
        if citations:
            source_by_title = legal_source_alias_index(claim.entities.legal_sources)
            for citation in citations:
                source = source_by_title[citation.law_title]
                identities = resolve_identity_candidates(
                    source.title,
                    resources,
                    declarations=declarations,
                    raw_title_candidate=source.raw_title_candidate,
                )
                query_title = source.canonical_title or (
                    identities[-1].title if identities else source.title
                )
                jurisdiction = source.jurisdiction or detect_jurisdiction(
                    source.title, claim.context_text or claim.text
                )
                article = ArticleRef(
                    article=citation.locator.article,
                    paragraphs=(
                        [citation.locator.paragraph]
                        if citation.locator.paragraph else []
                    ),
                    items=[citation.locator.item] if citation.locator.item else [],
                )
                display_title = source.title or citation.law_title
                items.append(_CheckItem(
                    claim=claim,
                    law_title=query_title,
                    display_title=display_title,
                    article=article,
                    article_no=article.article,
                    not_verifiable=classify_not_verifiable(display_title),
                    jurisdiction=jurisdiction,
                    out_of_scope=_out_of_scope_message(jurisdiction),
                    recognition_form=source.recognition.form,
                    law_identity_resolver=source.recognition.resolver,
                    citation_span=citation.citation_span,
                    reference_role=citation.role,
                    span_status=citation.span_status,
                ))
            # citations 已覆盖所有已确认法源的条款引用；无条款/章节仍走旧路径。
            if all(source.articles for source in claim.entities.legal_sources):
                legal_sources = []
            else:
                legal_sources = [
                    source for source in claim.entities.legal_sources
                    if not source.articles
                ]
        else:
            legal_sources = claim.entities.legal_sources
        for legal_source in legal_sources:
            identities = resolve_identity_candidates(
                legal_source.title,
                resources,
                declarations=declarations,
                raw_title_candidate=legal_source.raw_title_candidate,
            )
            query_title = legal_source.canonical_title or (
                identities[-1].title if identities else legal_source.title
            )
            display_title = legal_source.title
            not_verifiable = classify_not_verifiable(display_title)
            jurisdiction = legal_source.jurisdiction or detect_jurisdiction(
                legal_source.title, claim.context_text or claim.text
            )
            out_of_scope = _out_of_scope_message(jurisdiction)
            if not legal_source.articles and legal_source.structures:
                for structure in legal_source.structures:
                    items.append(_CheckItem(
                        claim=claim,
                        law_title=query_title,
                        display_title=display_title,
                        article=None,
                        article_no=structure.label,
                        not_verifiable=not_verifiable,
                        jurisdiction=jurisdiction,
                        out_of_scope=out_of_scope,
                        structure=structure,
                        recognition_form=legal_source.recognition.form,
                        law_identity_resolver=legal_source.recognition.resolver,
                    ))
                continue
            for article in legal_source.articles or [None]:
                items.append(
                    _CheckItem(
                        claim=claim,
                        law_title=query_title,
                        display_title=display_title,
                        article=article,
                        article_no=article.article if article is not None else None,
                        not_verifiable=not_verifiable,
                        jurisdiction=jurisdiction,
                        out_of_scope=out_of_scope,
                        recognition_form=legal_source.recognition.form,
                        law_identity_resolver=legal_source.recognition.resolver,
                    )
                )
        for mention in getattr(
            claim.entities, "unresolved_legal_mentions", []
        ):
            matched = resources.law_lexicon().longest_suffix_match(
                mention.raw_text
            )
            for article in mention.articles or [None]:
                items.append(_CheckItem(
                    claim=claim,
                    law_title=matched.canonical_title if matched else "",
                    display_title=matched.surface_title if matched else mention.raw_text,
                    article=article,
                    article_no=article.article if article is not None else None,
                    not_verifiable=(
                        None if matched
                        else "法规名称尚未确认，请人工核对或补充正式法名。"
                    ),
                    jurisdiction=detect_jurisdiction(
                        matched.canonical_title if matched else mention.raw_text,
                        claim.context_text or claim.text,
                    ),
                    recognition_form="bare",
                    law_identity_resolved=matched is not None,
                    law_identity_resolver="lexicon" if matched else None,
                    raw_title_candidate=mention.raw_text,
                ))
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
                law_title=law["title"],
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
                law_title=law["title"],
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
            law_title=law["title"],
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
        if item.lookup_key not in lookup_results:
            continue
        lookup_result, _ = lookup_results[item.lookup_key]
        article_missing = _article_is_missing(item, lookup_result)
        hierarchy_invalid = (
            _has_subarticle_locator(item)
            and not article_missing
            and _assess_item_location(item, lookup_result).status
            == LocationStatus.INVALID
        )
        if not article_missing and not hierarchy_invalid:
            continue
        if _matching_historical_location(
            item, historical_versions.get(item.lookup_key, [])
        ) is not None:
            continue
        # The exact article has already been retrieved and parsed.  Reuse it
        # before another network lookup: an invalid paragraph/item number can
        # often be repaired deterministically from the quoted text itself.
        if article_missing and lookup_result.evidence is not None:
            related = [
                ArticleEvidence(
                    law_title=lookup_result.evidence.law_title,
                    source_type=lookup_result.evidence.source_type,
                    article_no=excerpt.article_no,
                    article_text=excerpt.article_text,
                    data_source=lookup_result.trace,
                )
                for excerpt in lookup_result.evidence.related_articles
            ]
            local_resolution = resolve_location_candidates(item.claim.text, related)
            if local_resolution.status != "not_found":
                local_resolution.source_trace = lookup_result.trace
                repairs[index] = local_resolution
                continue
        elif lookup_result.evidence is not None:
            local_resolution = resolve_location_candidates(
                item.claim.text, [lookup_result.evidence]
            )
            if local_resolution.status != "not_found":
                local_resolution.source_trace = lookup_result.trace
                repairs[index] = local_resolution
                continue
        candidate_result = locator_source.locate_candidates(LookupRequest(
            law_title=item.law_title,
            article_no=item.article_no,
            context_text=item.claim.text,
        ))
        resolution = resolve_location_candidates(
            item.claim.text, candidate_result.candidates
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


def _article_is_missing(item: _CheckItem, result: LookupResult) -> bool:
    """所引条号未取得精确条文；相关条款召回不能视为该条已存在。"""
    return bool(
        item.article_no
        and result.status in {
            LookupStatus.LAW_FOUND_ARTICLE_MISSING,
            LookupStatus.RELEVANT_ARTICLES_FOUND,
        }
        and (
            result.evidence is None
            or not result.evidence.article_text
            or result.evidence.article_no != item.article_no
        )
    )


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
        if not item.law_identity_resolved:
            candidate = item.raw_title_candidate or "该裸引用"
            results[index] = ([StatuteFinding(
                code=StatuteErrorCode.SOURCE_NAME_AMBIGUOUS,
                risk_level="MEDIUM",
                summary=f"原文未加书名号，无法确定{item.article_no or '所引条款'}所属法规",
                suggestion=f"暂无法从“{candidate}”确定完整法规名称，请补写完整法名后再核验。",
            )], None)
            continue
        if item.skip_lookup:
            results[index] = ([], None)
            continue
        if item.relation_status in {"parent_unavailable", "insufficient"}:
            results[index] = (
                [], skipped_semantic_result("nested_relation_unverifiable")
            )
            continue
        if item.relation_status == "locator_mismatch":
            results[index] = ([], StatuteMeaningCheck(
                verdict=CheckVerdict.ISSUE,
                findings=[StatuteFinding(
                    code=StatuteErrorCode.ARTICLE_NUMBER_ERROR,
                    risk_level="HIGH",
                    summary="内部转引与主法条实际援引的位置不对应",
                    suggestion=(
                        item.relation_message
                        or "主法条原文援引了同一规则，但当前条号不对应，请核实引用位置。"
                    ),
                    location_recheck_required=True,
                    candidate_article_no=item.relation_candidate_article_no,
                )],
            ))
            continue
        lookup_result, attempts = lookup_results[item.lookup_key]
        if item.structure is not None:
            if lookup_result.status == LookupStatus.LAW_FOUND_ARTICLE_MISSING:
                results[index] = ([StatuteFinding(
                    code=StatuteErrorCode.CITATION_HIERARCHY_ERROR,
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
        article_missing = _article_is_missing(item, lookup_result)
        repair = location_repairs.get(index)
        missing_finding = next((
            finding for finding in findings
            if finding.code == StatuteErrorCode.ARTICLE_NOT_FOUND
        ), None)
        if missing_finding is not None and repair is not None and repair.status == "resolved":
            resolved = repair.candidates[0].locator
            if (
                article_number_from_citation(resolved.article_no)
                != article_number_from_citation(item.article_no)
            ):
                missing_finding.code = StatuteErrorCode.ARTICLE_NUMBER_ERROR
                missing_finding.summary = (
                    f"所引{item.article_no}不存在，文中内容对应{resolved.article_no}"
                )
                missing_finding.suggestion = _location_suggestion(repair)
                missing_finding.resolved_locator = resolved
                missing_finding.revision = _locator_revision(item, resolved)

        location = (
            LocationAssessment(LocationStatus.VALID)
            if article_missing else _assess_item_location(item, lookup_result)
        )
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
                finding = StatuteFinding(
                    code=StatuteErrorCode.CITATION_HIERARCHY_ERROR,
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
        # 只写法规名、未写条号的引用一律只核验存在性与效力，不再召回相关
        # 条款做语义核查（本地库有全文也不做）。法源不存在/已废止由上面的
        # findings 反映；法源存在且有效即通过。
        if _is_existence_only_reference(item) and lookup_result.status in (
            LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE,
            LookupStatus.RELEVANT_ARTICLES_FOUND,
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

    # 新链路按完整原文一次性比较全部权威条文。仅当调用方还是旧式测试替身或
    # 旧实现时，才保留逐条语义比对作为兼容兜底。
    legacy_checker = (
        semantic_checker
        if semantic_checker is not None
        and not callable(getattr(semantic_checker, "compare_application", None))
        else None
    )
    if semantic_jobs and legacy_checker is not None:
        unique_jobs: dict[str, tuple[_CheckItem, LookupResult]] = {}
        job_ids: dict[int, str] = {}
        for index, item, lookup_result in semantic_jobs:
            job_id = _semantic_job_id(item, lookup_result)
            job_ids[index] = job_id
            unique_jobs.setdefault(job_id, (item, lookup_result))
        with ThreadPoolExecutor(max_workers=_semantic_workers()) as pool:
            comparisons = dict(zip(
                unique_jobs,
                pool.map(lambda job: _compare_job(legacy_checker, job[0], job[1]), unique_jobs.values()),
            ))
        salvage_ids = [
            job_id
            for job_id, comparison in comparisons.items()
            if comparison.execution_status == "llm_error" and comparison.retryable
        ][:_salvage_max()]
        for job_id in salvage_ids:
            item, lookup_result = unique_jobs[job_id]
            recovered = _compare_job(legacy_checker, item, lookup_result)
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
    """只写法规名、未写条号的引用（含单部法规），仅核验存在性与效力。

    调用点已限定在 LAW_FOUND_TEXT_UNAVAILABLE：法规存在但法宝取不回条文
    全文、又无条号可定位时，存在性已确认即视为通过，不再标待核实。
    本地库有全文的无条号引用走相关条款召回（relevant_articles_found），
    不经此路径。"""
    return not item.article_no and item.article is None


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
                if (
                    finding.resolved_locator is not None
                    and item.relation_status == "locator_mismatch"
                ):
                    item.relation_status = "resolved"
                    item.relation_message = finding.suggestion
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
            if (
                finding.resolved_locator is not None
                and item.relation_status == "locator_mismatch"
            ):
                item.relation_status = "resolved"
                item.relation_message = finding.suggestion


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
            article_no=candidate,
            context_text=item.claim.text,
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
                claim_text=item.claim.text,
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
    resolution = resolve_location_candidates(item.claim.text, [evidence])
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
        article_no=item.article_no,
        context_text=item.claim.text,
    ))
    resolution = resolve_location_candidates(item.claim.text, recalled.candidates)
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
            context_text=item.claim.text,
        ))
        supported = [
            candidate for candidate in result.candidates
            if _candidate_supports_claim(item.claim.text, candidate.article_text or "")
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


def _candidate_supports_claim(claim_text: str, article_text: str) -> bool:
    normalized_article = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", article_text)
    without_citation = _CITATION_TEXT.sub("", claim_text)
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
    # 本地精编库与北大法宝的条文都以换行保留款边界（实测多款条文均含换行），
    # 故其单行条文即确为一款，可据此判定超范围款号；EUR-Lex 等其他来源不假定。
    reliable_paragraphs = (
        evidence is not None
        and evidence.data_source is not None
        and evidence.data_source.tier in (SourceTier.LOCAL_SQLITE, SourceTier.PKULAW_FALLBACK)
    )
    structure = (
        parse_article_structure(
            evidence.article_no or item.article_no or "",
            evidence.article_text,
            trust_single_paragraph=reliable_paragraphs,
        )
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


def _run_application_checks(
    semantic_checker: SemanticChecker | None,
    items: list[_CheckItem],
    lookup_results: dict[tuple, tuple[LookupResult, list[SourceTrace]]],
    judgments: dict[int, tuple[list[StatuteFinding], StatuteMeaningCheck | None]],
    document_text: str = "",
) -> dict[int, LegalApplicationCheck]:
    """按原文聚合全部可靠法条；法律适用与逐条引用判定独立执行。"""
    compare_application = (
        getattr(semantic_checker, "compare_application", None)
        if semantic_checker is not None else None
    )
    if not callable(compare_application):
        return {}

    blocking_codes = {
        StatuteErrorCode.SOURCE_NOT_FOUND,
        StatuteErrorCode.LAW_NAME_ERROR,
        StatuteErrorCode.ARTICLE_NOT_FOUND,
        StatuteErrorCode.ARTICLE_NUMBER_ERROR,
        StatuteErrorCode.CITATION_HIERARCHY_ERROR,
        StatuteErrorCode.SOURCE_REPEALED,
        StatuteErrorCode.SOURCE_AMENDED,
    }
    full_text = document_text.strip() or "\n".join(dict.fromkeys(
        item.claim.text for item in items if item.claim.text.strip()
    ))
    candidates: list[ApplicationCandidate] = []
    for index, item in enumerate(items):
        if item.skip_lookup or item.jurisdiction != "CN" or not item.article_no:
            continue
        lookup_pair = lookup_results.get(item.lookup_key)
        if lookup_pair is None:
            continue
        lookup_result, _ = lookup_pair
        evidence = lookup_result.evidence
        citation_findings, citation_semantic = judgments[index]
        if (
            lookup_result.status != LookupStatus.ARTICLE_FOUND
            or evidence is None
            or not evidence.article_text
            or citation_semantic is not None
            or any(finding.code in blocking_codes for finding in citation_findings)
            or item.relation_status in {
                "parent_failed", "parent_unavailable", "locator_mismatch", "insufficient",
            }
        ):
            continue
        candidates.append(ApplicationCandidate(
            item_index=index,
            claim_id="document",
            original_text=full_text,
            authority=ApplicationAuthority.from_evidence(
                _cited_source(item), evidence
            ),
        ))

    jobs = build_application_jobs(candidates)
    if not jobs:
        return {}
    unique_jobs = {job.job_id: job for job in jobs}
    with ThreadPoolExecutor(max_workers=_semantic_workers()) as pool:
        checks = dict(zip(
            unique_jobs,
            pool.map(
                lambda job: compare_application_with_llm(
                    semantic_checker,
                    job.original_text,
                    list(job.authorities),
                ),
                unique_jobs.values(),
            ),
        ))
    salvage_ids = [
        job_id for job_id, check in checks.items()
        if check.execution_status == ExecutionStatus.LLM_ERROR and check.retryable
    ][:_salvage_max()]
    for job_id in salvage_ids:
        job = unique_jobs[job_id]
        checks[job_id] = compare_application_with_llm(
            semantic_checker, job.original_text, list(job.authorities)
        )

    results: dict[int, LegalApplicationCheck] = {}
    for job in jobs:
        check = checks[job.job_id].model_copy(deep=True)
        check.job_id = job.job_id
        results[job.result_item_index] = check
    return results


def _build_statute_results(
    items: list[_CheckItem],
    lookup_results: dict[tuple, tuple[LookupResult, list[SourceTrace]]],
    judgments: dict[int, tuple[list[StatuteFinding], StatuteMeaningCheck | None]],
    application_checks: dict[int, LegalApplicationCheck] | None = None,
) -> list[StatuteVerificationResult]:
    application_checks = application_checks or {}
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
        application_check = application_checks.get(index)
        all_findings = [*findings, *(meaning_check.findings if meaning_check else [])]
        card_id = card_ids.setdefault(
            item.claim.claim_id, f"card_{len(card_ids) + 1:05d}"
        )
        results.append(
            StatuteVerificationResult(
                check_id=f"vc_{index + 1:05d}",
                card_id=card_id,
                display_group_id=display_group_id(item.claim),
                claim_id=item.claim.claim_id,
                claim_text=item.claim.text,
                law_title=item.display_title,
                recognition_form=item.recognition_form,
                law_identity_resolved=item.law_identity_resolved,
                law_identity_resolver=item.law_identity_resolver,
                jurisdiction=item.jurisdiction,
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
                    else _statute_outcome(
                        all_findings, meaning_check, item.reference_role,
                        item.jurisdiction, item.relation_status,
                        application_check,
                    )
                ),
                message=(
                    item.relation_message
                    or (meaning_check.notes if meaning_check else "")
                    or (application_check.notes if application_check else "")
                    or (
                        lookup_result.trace.message
                        if lookup_result is not None
                        and lookup_result.status in {
                            LookupStatus.SOURCE_ERROR,
                            LookupStatus.SOURCE_NOT_CONFIGURED,
                        }
                        else ""
                    )
                    or item.out_of_scope or item.not_verifiable or ""
                ),
                meaning_check=meaning_check,
                application_check=application_check,
                reference_role=item.reference_role,
                parent_check_id=(
                    f"vc_{item.parent_index + 1:05d}"
                    if item.parent_index is not None else None
                ),
                relation_status=item.relation_status,
                relation_message=item.relation_message,
                source_locations=item.claim.source_locations,
                note_context=item.claim.note_context,
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
            result.claim_text,
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
    jurisdiction: str = "CN",
    relation_status: str | None = None,
    application_check: LegalApplicationCheck | None = None,
) -> str:
    if any(finding.code == StatuteErrorCode.SOURCE_NAME_AMBIGUOUS for finding in findings):
        return "bug"
    if findings:
        return "issue"
    if relation_status in {"parent_failed", "parent_unavailable", "insufficient"}:
        return "bug"
    if reference_role == "nested" and relation_status in {"confirmed", "resolved"}:
        return "pass"
    if meaning_check is None:
        return _application_outcome(application_check)
    if meaning_check.execution_status != ExecutionStatus.COMPLETED:
        # 欧盟法规经 EUR-Lex 仅能核验存在性，无条文可作语义比对时语义会被
        # 跳过；存在性已确认即视为通过，不因此判为待核实。
        if jurisdiction == "EU" and meaning_check.execution_status == ExecutionStatus.SKIPPED:
            return "pass"
        return "bug"
    if meaning_check.verdict != CheckVerdict.PASS:
        return "bug"
    return _application_outcome(application_check)


def _application_outcome(check: LegalApplicationCheck | None) -> str:
    if check is None:
        return "pass"
    if check.execution_status != ExecutionStatus.COMPLETED:
        return "bug"
    return "review" if check.verdict == "review" else "pass"


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
        " ".join(item.claim.text.split()),
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
            _article_is_missing(item, lookup_results[item.lookup_key][0])
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
