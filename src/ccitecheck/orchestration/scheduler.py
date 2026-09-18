"""CCitecheck 核查控制平面与 Word 兼容流水线。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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
from ..domain.checks import ExecutionStatus
from ..domain.result import FrontendVerificationDocument, VERIFICATION_SCHEMA_VERSION
from ..domain.statute_results import (
    LegalApplicationCheck,
    StatuteErrorCode,
    StatuteFinding,
    StatuteLocationResolution,
    StatuteLocator,
    StatuteVerificationResult,
    StatuteVersion,
)
from ..domain.revisions import RevisionProposal
from ..domain.claims import (
    RawClaim,
    RawClaimDocument,
    from_legacy_claim,
    from_legacy_document,
)
from ..domain.evidence import RetrievalEvidence, TechnicalStatus
from ..domain.law_titles import cn_title_shape_key
from ..domain.queries import (
    HypothesisFeedback,
    IdentityCandidate,
    RepairPlan,
    SearchHypothesis,
    SourcePlanItem,
)
from ..domain.runs import RunState, SourceAttempt, VerificationRun
from ..domain.verification import VerificationResult, VerificationStatus
from ..query_construction import (
    build_document_context,
    QueryResources,
    build_initial_hypothesis,
    rebuild_hypothesis,
)
from ..query_construction.planners import (
    fuzzy_title_candidates,
    repair_target_is_allowed,
)
from ..query_construction.matching import equivalent_law_titles
from ..query_construction.jurisdiction import detect_jurisdiction
from ..query_construction.versioning import explicit_version_hint
from ..retrieval import RetrievalTask, SourceRegistry, execute as execute_retrieval
from ..verification import VerificationContext, verify as verify_evidence
from ..infrastructure.database import (
    connect,
    find_law,
    list_articles_in_structure,
    list_historical_article_versions,
    list_law_titles,
    normalize_title,
    normalize_article_key,
    resolve_structure_path,
)
from ..verification.statutes.nested import resolve_nested_relations
from .cases import verify_case_claims
from ..verification.semantic import SemanticChecker
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
)
from ..recognition.statutes import invalid_number_tokens
from ..recognition.spans import locate_claim_article_spans
from ..verification.statutes.locator import supports_assertion
from ..retrieval.sources.local_laws import LocalSQLiteSource
from ..retrieval.ranking import retrieve_relevant_articles
from ..retrieval.service import build_default_sources, sources_for_jurisdiction
from ..retrieval.sources import (
    CaseSearcher,
    LookupRequest,
    LookupResult,
    PkulawCaseSource,
    StatuteSource,
)
from ..retrieval.sources.pkulaw.client import PkulawMcpClient
from .policies.retrieval import (
    lookup_with_chain,
    run_lookup_batch,
    run_with_technical_retries,
)
from .policies.nested import finalize_nested_dependencies
from ..infrastructure.debug_timing import bind_current_timer, measure, timing_session

@dataclass
class SchedulerContext:
    law_db: str | Path
    source_registry: SourceRegistry | None = None
    verification_context: VerificationContext | None = None
    semantic_checker: SemanticChecker | None = None
    case_searcher: CaseSearcher | None = None


def _plan_repair(
    semantic_checker: SemanticChecker | None,
    *,
    raw_text: str,
    raw_title: str,
    raw_time: str | None,
    article_no: str | None,
    retrieval_status: str,
    candidate_titles: list[str],
    known_titles: list[str],
    current_title: str,
) -> tuple[RepairPlan | None, str | None]:
    """唯一的 repair planner 调用、白名单和目标校验入口。"""
    planner = getattr(semantic_checker, "plan_repair", None)
    if not callable(planner):
        return None, None
    fuzzy = fuzzy_title_candidates(raw_title, known_titles)
    try:
        with measure("query_construction.total"), measure("query.repair_planner"):
            plan = RepairPlan.model_validate(planner(
                raw_text=raw_text,
                raw_title=raw_title,
                raw_time=raw_time,
                article_no=article_no,
                retrieval_status=retrieval_status,
                candidate_titles=candidate_titles,
                fuzzy_candidates=fuzzy,
            ))
    except Exception as exc:
        return None, f"Repair Planner 失败：{exc}"
    retry = plan.retry_request
    if retry is None:
        return None, plan.diagnosis.message
    if not repair_target_is_allowed(plan, raw_title, candidate_titles, fuzzy):
        return None, "Repair Planner 提出的法规名不在确定性候选列表中，未执行重试。"
    if retry.route != "statute_exact" or not retry.target_name or not retry.article_no:
        return None, "Repair Planner 未返回可执行的精确检索计划。"
    if (
        cn_title_shape_key(normalize_title(retry.target_name))
        == cn_title_shape_key(normalize_title(current_title))
        and normalize_article_key(retry.article_no)
        == normalize_article_key(article_no or "")
    ):
        return None, plan.diagnosis.message
    return plan, None


def _rebuild_from_repair(
    claim: RawClaim,
    hypothesis: SearchHypothesis,
    plan: RepairPlan,
    resources: QueryResources,
) -> SearchHypothesis:
    retry = plan.retry_request
    if retry is None:
        return hypothesis
    rebuilt = rebuild_hypothesis(
        claim,
        hypothesis,
        HypothesisFeedback(
            trigger_reason="repair_planner",
            identity_title=retry.target_name,
            article_raw=retry.article_no,
            basis=plan.diagnosis.message,
        ),
        resources,
    )
    if rebuilt.query_plan is not None:
        identities = list(rebuilt.identity_candidates)
        if identities:
            identities[0] = identities[0].model_copy(
                update={"title": retry.target_name}
            )
        rebuilt = rebuilt.model_copy(update={
            "identity_candidates": identities,
            "query_plan": rebuilt.query_plan.model_copy(
                update={
                    "target_name": retry.target_name,
                    "version_hint": retry.version_hint
                    or rebuilt.query_plan.version_hint,
                }
            )
        })
    return rebuilt


class VerificationScheduler:
    """只控制下一步动作，不修改 RawClaim 或作法律结论。"""

    def __init__(self, context: SchedulerContext):
        self.context = context

    def verify_claim(self, claim: RawClaim) -> VerificationRun:
        with timing_session() as timer:
            return self._verify_claim(claim, timer)

    def _retrieve_evidence(
        self,
        run: VerificationRun,
        registry: SourceRegistry,
        claim: RawClaim,
        hypothesis: SearchHypothesis,
        identity: IdentityCandidate,
        planned: SourcePlanItem,
        timer,
    ) -> RetrievalEvidence:
        """按假设计划对单个 (identity, source) 目标执行检索并记录尝试历史。"""
        run.transition(RunState.RETRIEVING)
        with timer.measure("retrieval.total"):
            evidence, retrieval_attempts = run_with_technical_retries(
                lambda: execute_retrieval(
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
                ),
                lambda item: item.technical_status
                == TechnicalStatus.SOURCE_ERROR,
                max_retries=run.max_technical_retries,
            )
        run.technical_retry_count += len(retrieval_attempts) - 1
        for attempt in retrieval_attempts:
            run.evidence_history.append(attempt.evidence_id)
            run.source_attempts.append(SourceAttempt(
                source_id=planned.source_id,
                hypothesis_id=hypothesis.hypothesis_id,
                evidence_id=attempt.evidence_id,
                technical_status=attempt.technical_status.value,
            ))
        return evidence

    def _verify_retrieved(
        self,
        run: VerificationRun,
        claim: RawClaim,
        evidence: RetrievalEvidence,
        timer,
    ) -> VerificationResult:
        """对已有证据执行内容判定并记录校验历史。"""
        run.transition(RunState.EVIDENCE_READY)
        run.transition(RunState.VERIFYING)
        with timer.measure("comparison.total"):
            result = verify_evidence(
                claim, evidence, self.context.verification_context
            )
        run.verification_history.append(result.verification_id)
        run.transition(RunState.VERIFIED)
        return result

    def _verify_claim(self, claim: RawClaim, timer) -> VerificationRun:
        run = VerificationRun(claim_id=claim.claim_id)
        resources = QueryResources(law_db=self.context.law_db)
        with timer.measure("query_construction.total"), timer.measure("query.primary"):
            hypothesis = build_initial_hypothesis(
                claim, resources, planner=self.context.semantic_checker
            )
        run.hypothesis_history.append(hypothesis.hypothesis_id)
        registry = self.context.source_registry or SourceRegistry.default(
            self.context.law_db
        )
        attempted_targets: set[tuple[str, str | None, str | None]] = set()
        repair_planned = False

        while True:
            run.transition(RunState.HYPOTHESIS_READY)
            candidate_titles: list[str] = []
            last_retrieval_status = "UNKNOWN"
            for identity in sorted(hypothesis.identity_candidates, key=lambda item: item.priority):
                identity_key = cn_title_shape_key(normalize_title(identity.title))
                target_key = (
                    identity_key,
                    hypothesis.query_plan.article_no if hypothesis.query_plan else None,
                    hypothesis.query_plan.query_text if hypothesis.query_plan else None,
                )
                if target_key in attempted_targets:
                    continue
                attempted_targets.add(target_key)
                for planned in sorted(hypothesis.source_plan, key=lambda item: item.priority):
                    evidence = self._retrieve_evidence(
                        run, registry, claim, hypothesis, identity, planned, timer
                    )
                    last_retrieval_status = evidence.retrieval_status.name
                    candidate_titles.extend(
                        str(title)
                        for title in evidence.provider_metadata.get("candidate_titles", [])
                        if title
                    )
                    if evidence.technical_status != TechnicalStatus.COMPLETED:
                        continue
                    result = self._verify_retrieved(run, claim, evidence, timer)
                    if result.overall_status != VerificationStatus.INSUFFICIENT_EVIDENCE:
                        run.transition(RunState.OUTPUT)
                        run.terminal_reason = "verification_complete"
                        return run

            next_title = next((
                title for title in candidate_titles
                if not any(
                    key[0] == cn_title_shape_key(normalize_title(title))
                    for key in attempted_targets
                )
            ), None)
            mention = next((
                item for item in claim.legal_mentions
                if item.mention_id == hypothesis.mention_id
            ), None)
            if (
                not repair_planned
                and mention is not None
                and hypothesis.query_plan is not None
                and hypothesis.query_plan.route == "statute_exact"
                and run.hypothesis_retry_count < run.max_hypothesis_retries
            ):
                candidates = list(dict.fromkeys(candidate_titles))
                repair_plan, _ = _plan_repair(
                    self.context.semantic_checker,
                    raw_text=claim.raw_text,
                    raw_title=mention.raw_title or mention.raw_title_candidate or "",
                    raw_time=mention.raw_time,
                    article_no=mention.article_raw,
                    retrieval_status=last_retrieval_status,
                    candidate_titles=candidates,
                    known_titles=_load_known_titles(self.context.law_db),
                    current_title=(
                        hypothesis.query_plan.target_name
                        or mention.raw_title
                        or mention.raw_title_candidate
                        or ""
                    ),
                )
                repair_planned = True
                if repair_plan is not None and repair_plan.retry_request is not None:
                    hypothesis = _rebuild_from_repair(
                        claim,
                        hypothesis,
                        repair_plan,
                        resources,
                    )
                    run.hypothesis_retry_count += 1
                    run.hypothesis_history.append(hypothesis.hypothesis_id)
                    continue
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

            run.transition(RunState.STOPPED)
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
    # 外部模型连接受网络与上游并发限制影响；硬上限避免配置过大时形成
    # 连接风暴，把原本可完成的单次请求放大成批量 transport/timeout。
    return max(1, min(4, int(os.getenv("QWEN_SEMANTIC_WORKERS", "4"))))


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
    with timing_session() as timer:
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
        with (
            timer.measure("recognition.total"),
            timer.measure("recognition.check_item_preparation"),
            timer.measure("query.primary"),
        ):
            items = (
                _collect_check_items(
                    claim_document, database_path, semantic_checker
                )
                if include_statutes else []
            )
        with timer.measure("retrieval.total"):
            lookup_results = _run_lookups(
                source_chain, items, database_path, semantic_checker
            )
        known_titles = _load_known_titles(database_path)
        with timer.measure("comparison.total"):
            resolve_nested_relations(items, lookup_results)
        with timer.measure("retrieval.total"):
            historical_versions = _load_historical_versions(
                database_path, items, lookup_results
            )
            location_repairs = _run_location_repairs(
                source_chain, items, lookup_results, historical_versions, semantic_checker
            )
        _run_repair_plans(
            source_chain,
            items,
            lookup_results,
            semantic_checker,
            known_titles,
        )
        if any(item.correction_evidence is not None and not item.repair_verified for item in items):
            location_repairs.update(_run_location_repairs(
                source_chain, items, lookup_results, historical_versions, semantic_checker,
                retry_only=True,
            ))
        with timer.measure("comparison.total"):
            judgments = _run_judgments(
                items,
                lookup_results,
                known_titles,
                historical_versions,
                location_repairs,
            )
        with timer.measure("retrieval.total"):
            _resolve_repealed_successors(source_chain, items, judgments)
        with timer.measure("comparison.total"):
            finalize_nested_dependencies(items, judgments)
            application_checks = _run_application_checks(
                semantic_checker,
                items,
                lookup_results,
                judgments,
            )
        with timer.measure("output"):
            statute_results = _aggregate_duplicate_statute_results(
                _build_statute_results(
                    items, lookup_results, judgments, application_checks
                )
            )
        with timer.measure("case_verification.total"):
            case_results = (
                verify_case_claims(claim_document, searcher, semantic_checker)
                if include_cases
                else []
            )
        with timer.measure("output"):
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
    raw_claim: RawClaim | None = None
    hypothesis: SearchHypothesis | None = None
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
    raw_time: str | None = None
    version_hint: str | None = None
    related_query: str | None = None
    planner_failure: str | None = None
    repair_reason: str | None = None
    repair_message: str = ""
    repaired_title: str | None = None
    repaired_article_no: str | None = None
    repair_verified: bool = False
    correction_evidence: ArticleEvidence | None = None
    location_resolution: StatuteLocationResolution | None = None
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
            return (
                self.jurisdiction,
                self.law_title,
                self.article_no,
                self.version_hint or _explicit_version_hint(self.raw_time),
            )
        return (
            self.jurisdiction,
            self.law_title,
            None,
            self.related_query or self.claim.context_text or self.claim.text,
        )

def _collect_check_items(
    claim_document: ClaimDocument,
    database_path: str | Path | None = None,
    semantic_checker: SemanticChecker | None = None,
) -> list[_CheckItem]:
    """保留旧入口签名；内部统一经过 SearchHypothesis 构造查询。"""
    claims = list(claim_document.claims)
    for claim in claims:
        if claim.claim_type != ClaimType.LEGAL_SOURCE_CLAIM:
            continue
        if not getattr(claim.entities, "citations", []):
            locate_claim_article_spans(claim)
        ensure_claim_citation_integrity(claim)

    raw_document = (
        from_legacy_document(claim_document)
        if isinstance(claim_document, ClaimDocument)
        else RawClaimDocument(
            source_claim_doc_id=getattr(
                getattr(claim_document, "claim_meta", None), "claim_doc_id", ""
            ),
            source_doc_id=getattr(
                getattr(claim_document, "claim_meta", None), "source_doc_id", ""
            ),
            document_text=getattr(claim_document, "document_text", ""),
            claims=[from_legacy_claim(claim) for claim in claims],
        )
    )
    resources = QueryResources(law_db=database_path)
    document_context = build_document_context(raw_document)
    tasks = [
        (claim, mention)
        for claim in raw_document.claims
        if claim.claim_type == ClaimType.LEGAL_SOURCE_CLAIM
        for mention in claim.legal_mentions
    ]

    def construct(task):
        claim, mention = task
        return mention.mention_id, build_initial_hypothesis(
            claim,
            resources,
            mention_id=mention.mention_id,
            planner=semantic_checker,
            document_context=document_context,
        )

    direct = [task for task in tasks if task[1].recognition_form != "inherited"]
    inherited = [task for task in tasks if task[1].recognition_form == "inherited"]
    hypotheses: dict[str, SearchHypothesis] = {}

    def build_batch(batch):
        groups: dict[str, list] = {}
        for task in batch:
            groups.setdefault(task[0].claim_id, []).append(task)

        def construct_group(group):
            return [construct(task) for task in group]

        grouped = list(groups.values())
        if semantic_checker is not None and len(grouped) > 1:
            with ThreadPoolExecutor(max_workers=_semantic_workers()) as pool:
                built = pool.map(bind_current_timer(construct_group), grouped)
                return [item for group in built for item in group]
        return [item for group in map(construct_group, grouped) for item in group]

    hypotheses.update(build_batch(direct))
    # 并发构造可能以任意顺序写入承前缓存；按文档顺序重建后再处理 inherited。
    document_context.resolved_by_anchor.clear()
    for claim, mention in direct:
        document_context.remember(claim, mention, hypotheses[mention.mention_id])
    hypotheses.update(build_batch(inherited))
    return check_items_from_hypotheses(claim_document, hypotheses, resources)


def check_items_from_hypotheses(
    claim_document,
    hypotheses: dict[str, SearchHypothesis],
    resources: QueryResources,
) -> list[_CheckItem]:
    """把查询假设投影回现有可变 CheckItem，保留下游隐式契约。"""
    items: list[_CheckItem] = []
    for claim in claim_document.claims:
        if claim.claim_type != ClaimType.LEGAL_SOURCE_CLAIM:
            continue
        raw_claim = from_legacy_claim(claim)
        located_aliases = {
            normalize_title(source.title)
            for source in claim.entities.legal_sources
            if source.articles or source.structures
        }
        shadowed_full_names = {
            normalize_title(declaration.full_name_raw)
            for declaration in getattr(claim.entities, "alias_declarations", [])
            if normalize_title(declaration.alias_raw) in located_aliases
        }
        eligible_sources = [
            source for source in claim.entities.legal_sources
            if not (
                not source.articles
                and not source.structures
                and normalize_title(source.title) in shadowed_full_names
            )
        ]
        unresolved_mentions = list(getattr(
            claim.entities, "unresolved_legal_mentions", []
        ))
        if not eligible_sources and not unresolved_mentions:
            continue
        citations = getattr(claim.entities, "citations", [])
        if citations:
            source_by_title = legal_source_alias_index(claim.entities.legal_sources)
            for citation_index, citation in enumerate(citations, 1):
                source = source_by_title[citation.law_title]
                hypothesis = hypotheses.get(
                    citation.mention_id or f"{claim.claim_id}:law:{citation_index}"
                )
                query_title, jurisdiction, related_query, version_hint, planner_failure = (
                    _hypothesis_projection(hypothesis, source.title)
                )
                recognized_article = next((
                    value for value in source.articles
                    if normalize_article_key(value.article)
                    == normalize_article_key(citation.locator.article)
                ), None)
                article = ArticleRef(
                    article=citation.locator.article,
                    raw_locator=recognized_article.raw_locator if recognized_article else None,
                    paragraphs=(
                        [citation.locator.paragraph]
                        if citation.locator.paragraph else []
                    ),
                    items=[citation.locator.item] if citation.locator.item else [],
                )
                display_title = _display_title_for_source(
                    source, resources, citation.law_title
                )
                items.append(_CheckItem(
                    claim=claim,
                    law_title=query_title,
                    display_title=display_title,
                    article=article,
                    article_no=article.article,
                    not_verifiable=classify_not_verifiable(display_title),
                    raw_claim=raw_claim,
                    hypothesis=hypothesis,
                    jurisdiction=jurisdiction,
                    out_of_scope=_out_of_scope_message(jurisdiction),
                    recognition_form=source.recognition.form,
                    law_identity_resolver=source.recognition.resolver,
                    citation_span=citation.citation_span,
                    reference_role=citation.role,
                    span_status=citation.span_status,
                    raw_time=source.raw_time,
                    version_hint=version_hint,
                    related_query=related_query,
                    planner_failure=planner_failure,
                ))
            # citations 已覆盖所有已确认法源的条款引用；无条款/章节仍走旧路径。
            if all(source.articles for source in eligible_sources):
                legal_sources = []
            else:
                legal_sources = [
                    source for source in eligible_sources
                    if not source.articles
                ]
        else:
            legal_sources = eligible_sources
        source_indices = {
            id(source): index
            for index, source in enumerate(claim.entities.legal_sources, 1)
        }
        for legal_source in legal_sources:
            source_index = source_indices[id(legal_source)]
            hypothesis = hypotheses.get(
                f"{claim.claim_id}:law:{source_index}:1"
            )
            query_title, jurisdiction, related_query, version_hint, planner_failure = (
                _hypothesis_projection(hypothesis, legal_source.title)
            )
            display_title = _display_title_for_source(
                legal_source, resources, legal_source.title
            )
            not_verifiable = classify_not_verifiable(display_title)
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
                        raw_claim=raw_claim,
                        hypothesis=hypothesis,
                        jurisdiction=jurisdiction,
                        out_of_scope=out_of_scope,
                        structure=structure,
                        recognition_form=legal_source.recognition.form,
                        law_identity_resolver=legal_source.recognition.resolver,
                        raw_time=legal_source.raw_time,
                        version_hint=version_hint,
                        related_query=related_query,
                        planner_failure=planner_failure,
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
                        raw_claim=raw_claim,
                        hypothesis=hypothesis,
                        jurisdiction=jurisdiction,
                        out_of_scope=out_of_scope,
                        recognition_form=legal_source.recognition.form,
                        law_identity_resolver=legal_source.recognition.resolver,
                        raw_time=legal_source.raw_time,
                        version_hint=version_hint,
                        related_query=related_query,
                        planner_failure=planner_failure,
                    )
                )
        for unresolved_index, mention in enumerate(unresolved_mentions, 1):
            matched = resources.law_lexicon().longest_suffix_match(
                mention.raw_text
            )
            if matched is None:
                continue
            for article_index, article in enumerate(mention.articles or [None], 1):
                hypothesis = hypotheses.get(
                    f"{claim.claim_id}:bare:{unresolved_index}:{article_index}"
                )
                query_title, jurisdiction, related_query, version_hint, planner_failure = (
                    _hypothesis_projection(hypothesis, matched.canonical_title)
                )
                items.append(_CheckItem(
                    claim=claim,
                    law_title=query_title,
                    display_title=matched.surface_title,
                    article=article,
                    article_no=article.article if article is not None else None,
                    not_verifiable=None,
                    raw_claim=raw_claim,
                    hypothesis=hypothesis,
                    jurisdiction=jurisdiction,
                    recognition_form="bare",
                    law_identity_resolved=True,
                    law_identity_resolver="lexicon",
                    raw_title_candidate=mention.raw_text,
                    version_hint=version_hint,
                    related_query=related_query,
                    planner_failure=planner_failure,
                ))
    return items


def _hypothesis_projection(
    hypothesis: SearchHypothesis | None,
    fallback_title: str,
) -> tuple[str, str, str | None, str | None, str | None]:
    if hypothesis is None:
        return fallback_title, detect_jurisdiction(fallback_title), None, None, None
    plan = hypothesis.query_plan
    title = (
        plan.target_name if plan and plan.target_name
        else hypothesis.identity_candidates[-1].title
        if hypothesis.identity_candidates else fallback_title
    )
    failure = next((
        str(item.value)
        for item in hypothesis.assumptions
        if item.code == "planner_failure"
        and item.value != "related_planner_not_configured"
    ), None)
    return (
        title,
        hypothesis.jurisdiction.code,
        plan.query_text if plan and plan.route == "statute_related" else None,
        plan.version_hint if plan else None,
        failure,
    )


def _display_title_for_source(source, resources: QueryResources, fallback: str) -> str:
    """裸法名展示使用词典命中的正式表面名，不回显误吸收的上下文。"""
    if source.recognition.form == "bare":
        raw = source.raw_title_candidate or source.title
        matched = resources.law_lexicon().longest_suffix_match(raw)
        if matched is not None:
            return matched.surface_title
    return source.title or fallback


def _out_of_scope_message(jurisdiction: str) -> str | None:
    if jurisdiction in {"UNKNOWN", "FOREIGN"}:
        return "该法规法域不明，当前未覆盖自动核查，请人工核验。"
    return None


def _run_lookups(
    source_chain: list[StatuteSource],
    items: list[_CheckItem],
    database_path: str | Path,
    reranker=None,
) -> dict[tuple, tuple[LookupResult, list[SourceTrace]]]:
    for source in source_chain:
        if isinstance(source, LocalSQLiteSource):
            source.clear_cache()
    # 按法域分组：CN → 注入的中国法链；EU → eurlex+ansvar；
    # 具体涉外法域 → ansanr；UNKNOWN → 空链（不自动检索）。
    groups: dict[str, dict[tuple, LookupRequest]] = {}
    for item in items:
        if item.skip_lookup or item.structure is not None:
            continue
        bucket = groups.setdefault(item.jurisdiction, {})
        bucket.setdefault(
            item.lookup_key,
            LookupRequest(
                law_title=item.law_title,
                article_no=item.article_no,
                context_text=item.claim.context_text or item.claim.text,
                query_text=item.related_query,
                version_hint=item.version_hint or _explicit_version_hint(item.raw_time),
                existence_only=(
                    not item.article_no
                    and item.structure is None
                    and item.related_query is None
                ),
                jurisdiction=item.jurisdiction,
            ),
        )
    results: dict[tuple, tuple[LookupResult, list[SourceTrace]]] = {}
    for jurisdiction, requests in groups.items():
        if not requests:
            continue
        chain = sources_for_jurisdiction(jurisdiction, cn_chain=source_chain)
        if not chain:
            continue
        results.update(run_lookup_batch(chain, requests, reranker=reranker))
    results.update(_run_structure_lookups(items, database_path))
    return results


def _run_repair_plans(
    source_chain: list[StatuteSource],
    items: list[_CheckItem],
    lookup_results: dict[tuple, tuple[LookupResult, list[SourceTrace]]],
    semantic_checker: SemanticChecker | None,
    known_titles: list[str],
) -> None:
    """精确检索失败后最多规划并验证一次受限重试。"""
    if not callable(getattr(semantic_checker, "plan_repair", None)) or not source_chain:
        return
    targets = [
        item for item in items
        if item.article_no
        and not item.skip_lookup
        and not item.repair_verified
        and item.structure is None
        and item.lookup_key in lookup_results
        and lookup_results[item.lookup_key][0].status in {
            LookupStatus.LAW_NOT_FOUND,
            LookupStatus.LAW_FOUND_ARTICLE_MISSING,
            LookupStatus.RELEVANT_ARTICLES_FOUND,
        }
    ]

    def execute(item: _CheckItem):
        result, attempts = lookup_results[item.lookup_key]
        candidates = _trace_candidate_titles(attempts)
        plan, error = _plan_repair(
            semantic_checker,
            raw_text=item.claim.text,
            raw_title=item.display_title,
            raw_time=item.raw_time,
            article_no=item.article_no,
            retrieval_status=result.status.name,
            candidate_titles=candidates,
            known_titles=known_titles,
            current_title=item.law_title,
        )
        if plan is None:
            return None, error or "", None
        item.repair_reason = plan.diagnosis.reason
        item.repair_message = plan.diagnosis.message
        retry = plan.retry_request
        if retry is None:
            return None, plan.diagnosis.message, None
        try:
            with measure("retrieval.total"):
                retried = lookup_with_chain(
                    sources_for_jurisdiction(item.jurisdiction, cn_chain=source_chain),
                    LookupRequest(
                        law_title=retry.target_name,
                        article_no=retry.article_no,
                        context_text=item.claim.context_text or item.claim.text,
                        version_hint=(
                            retry.version_hint
                            or item.version_hint
                            or _explicit_version_hint(item.raw_time)
                        ),
                        jurisdiction=item.jurisdiction,
                ))
        except Exception as exc:
            return None, f"{plan.diagnosis.message}；修复重试失败：{exc}", None
        if retried[0].status != LookupStatus.ARTICLE_FOUND:
            return None, f"{plan.diagnosis.message}；候选经权威源重试仍未命中。", retried
        return retry, f"{plan.diagnosis.message}；已按候选重试并取得权威条文。", retried

    with ThreadPoolExecutor(max_workers=_semantic_workers()) as pool:
        outcomes = list(pool.map(bind_current_timer(execute), targets))
    for item, (retry, message, retried) in zip(targets, outcomes):
        item.repair_message = message
        if retried is not None and retry is not None:
            _, initial_attempts = lookup_results[item.lookup_key]
            retry_result, retry_attempts = retried
            retry_result.trace.metadata.update(
                repair_reason=item.repair_reason,
                repaired_from_title=item.display_title,
                repaired_from_article_no=item.article_no,
            )
            item.repaired_title = retry.target_name
            item.repaired_article_no = retry.article_no
            item.correction_evidence = retry_result.evidence
            # 真实存在只说明检索成功；内容定位链负责验证。
            lookup_results[item.lookup_key] = (
                lookup_results[item.lookup_key][0], [*initial_attempts, *retry_attempts]
            )


def _trace_candidate_titles(attempts: list[SourceTrace]) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for trace in attempts:
        raw_candidates = trace.metadata.get("candidate_titles", [])
        if not isinstance(raw_candidates, list):
            raw_candidates = []
        values = [
            *raw_candidates,
            trace.metadata.get("suggested_title"),
        ]
        for value in values:
            if not isinstance(value, str) or not value.strip():
                continue
            key = cn_title_shape_key(normalize_title(value))
            if key not in seen:
                seen.add(key)
                candidates.append(value.strip())
    return candidates


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
    semantic_checker=None,
    *, retry_only: bool = False,
) -> dict[int, StatuteLocationResolution]:
    locator_source = next((source for source in source_chain
                           if callable(getattr(source, "locate_candidates", None))), None)
    local = next((source for source in source_chain if isinstance(source, LocalSQLiteSource)), None)
    repairs = {}
    for index, item in enumerate(items):
        if retry_only and (item.correction_evidence is None or item.repair_verified):
            continue
        if item.skip_lookup or not item.article_no or item.structure or item.lookup_key not in lookup_results:
            continue
        if (
            item.reference_role == "nested"
            or not item.claim.text.strip()
            or len(getattr(item.claim.entities, "citations", [])) != 1
        ):
            continue
        result, attempts = lookup_results[item.lookup_key]
        if _pkulaw_absence_conflicts(attempts):
            continue
        pkulaw_absence = _pkulaw_confirms_absence(attempts)
        if result.status not in {LookupStatus.ARTICLE_FOUND, LookupStatus.LAW_FOUND_ARTICLE_MISSING,
                                 LookupStatus.RELEVANT_ARTICLES_FOUND, LookupStatus.LAW_NOT_FOUND}:
            continue
        current = result.evidence
        target_title = item.repaired_title if result.status == LookupStatus.LAW_NOT_FOUND and item.repaired_title else item.law_title
        quote = _location_query_text(item)
        current_text = ""
        if result.status == LookupStatus.ARTICLE_FOUND and current:
            location = _assess_item_location(item, result)
            current_text = location.authoritative_text or ""
            # 先检查当前引用位置；其他地方相似不能推翻已有支持。
            if location.status == LocationStatus.VALID:
                structure = parse_article_structure(item.article_no, current_text)
                scopes = [p.text for p in structure.paragraphs] if structure else []
                supported = any(supports_assertion(quote, scope) for scope in scopes)
                if supported:
                    continue
        pool = []
        if current and result.status == LookupStatus.ARTICLE_FOUND:
            pool.append(current)
        if item.correction_evidence:
            pool.append(item.correction_evidence)
        if current:
            pool.extend(current.model_copy(update={"article_no": part.article_no,
                        "law_title": part.law_title or current.law_title,
                        "source_metadata": {"version_key": part.version_key},
                        "data_source": current.data_source.model_copy(update={"metadata": {"version_key": part.version_key}, "source_url": part.source_url or current.data_source.source_url}),
                        "article_text": part.article_text, "related_articles": []})
                        for part in current.related_articles)
        basis = current or item.correction_evidence
        version = (basis.source_metadata.get("version_key") if basis else None) or result.trace.metadata.get("version_key")
        version_confirmed = (result.trace.metadata.get("version_confirmed") is True
                             or bool(basis and (basis.source_metadata.get("version_confirmed") is True or basis.data_source.metadata.get("version_confirmed") is True)))
        if local and local.db_path.exists():
            rows = local.current_articles(target_title)
            metadata = local.corpus_metadata(rows[0]["title"] if rows else target_title, rows)
            version = version or metadata.get("version_key")
            version_confirmed = version_confirmed or metadata.get("version_confirmed") is True
            selected = [r for r in rows if r["version_key"] == version]
            ranked = retrieve_relevant_articles(quote, selected, limit=3)
            keys = {normalize_article_key(r.article_no) for r in ranked}
            rank_confirmed = bool(
                len(ranked) > 1
                and ranked[0].relevance_score >= ranked[1].relevance_score * 1.5
            )
            rank_scores = {
                normalize_article_key(r.article_no): r.relevance_score for r in ranked
            }
            # 连续片段补召回，全文在同次核查中只读取一次。
            for row in selected:
                if normalize_article_key(row["article_no"]) not in keys and not supports_assertion(quote, row["text"]):
                    continue
                trace = SourceTrace(tier=SourceTier.LOCAL_SQLITE, source_name=row["source_name"] or "本地法规库",
                                    status=LookupStatus.ARTICLE_FOUND,
                                    source_url=row["source_url"] or result.trace.source_url, metadata=metadata)
                pool.append(ArticleEvidence(law_title=row["title"], article_no=row["article_no"],
                            article_text=row["text"], version_label=row["version_label"],
                            source_metadata={
                                "version_key": row["version_key"],
                                "relevance_score": rank_scores.get(normalize_article_key(row["article_no"])),
                                "article_rank_confirmed": rank_confirmed and normalize_article_key(
                                    row["article_no"]
                                ) == normalize_article_key(ranked[0].article_no),
                            }, data_source=trace))
        if item.raw_time and item.raw_time != "现行":
            year = re.search(r"\d{4}", item.raw_time)
            version_label = (basis.version_label if basis else "") or ""
            local_metadata = metadata if local and local.db_path.exists() else {}
            identified = version_confirmed or local_metadata.get("version_identified") is True
            version_confirmed = bool(identified and year and year.group() in (
                str(version) + version_label + str(local_metadata.get("version_label", ""))))
        current_authority = _authority_marks_current(basis) and not (
            item.raw_time and item.raw_time != "现行"
        )
        version_confirmed = version_confirmed or current_authority

        def resolve(candidates):
            eligible = []
            for evidence in candidates:
                if not equivalent_law_titles(evidence.law_title, target_title):
                    continue
                candidate_version = (
                    evidence.source_metadata.get("version_key")
                    or evidence.data_source.metadata.get("version_key")
                )
                if pkulaw_absence or (
                    candidate_version is not None and candidate_version == version
                ) or (current_authority and _authority_marks_current(evidence)) or (
                    not version_confirmed
                    and not (item.raw_time and item.raw_time != "现行")
                    and evidence.source_metadata.get("version_confirmed") is True
                    and _authority_marks_current(evidence)
                ):
                    eligible.append(evidence)
            resolution = resolve_location_candidates(
                quote, eligible, current_text=current_text,
                cited_article_no=item.article_no,
            )
            resolved_authority = bool(
                resolution.status == "resolved"
                and any(
                    normalize_article_key(evidence.article_no or "")
                    == normalize_article_key(resolution.candidates[0].locator.article_no or "")
                    and (
                        evidence.source_metadata.get("version_confirmed") is True
                        or evidence.data_source.metadata.get("version_confirmed") is True
                        or _authority_marks_current(evidence)
                    )
                    for evidence in eligible
                )
            )
            if not version_confirmed and not resolved_authority and not pkulaw_absence and resolution.status == "resolved":
                resolution.status = "candidates_pending"
            return resolution, eligible
        resolution, eligible = resolve(pool)
        if (resolution.status != "resolved" and locator_source is not None
                and item.jurisdiction == "CN" and version != "current" and not retry_only):
            remote = locator_source.locate_candidates(LookupRequest(
                law_title=target_title, article_no=item.article_no, context_text=quote))
            attempts.append(remote.trace)
            pool.extend(remote.candidates)
            resolution, eligible = resolve(pool)
            resolution.source_trace = remote.trace
        if resolution.status == "resolved":
            target = resolution.candidates[0].locator
            cited = _item_locators(item)[0]
            same_article = normalize_article_key(target.article_no or "") == normalize_article_key(item.article_no)
            # 没有限定款项的条级引用已覆盖该位置；无须补写层级。
            title_changed = not equivalent_law_titles(target_title, item.law_title)
            if same_article and not title_changed and not cited.paragraph_no and not cited.item_no:
                continue
            paragraph_matches = (
                _locator_key(cited.paragraph_no) == _locator_key(target.paragraph_no)
                if cited.paragraph_no or cited.item_no and target.paragraph_no
                else True
            )
            item_matches = (
                not cited.item_no
                or _locator_key(cited.item_no) == _locator_key(target.item_no)
            )
            if same_article and not title_changed and paragraph_matches and item_matches:
                continue
            if same_article and not title_changed and repair_level_missing(cited, target):
                resolution.status = "candidates_pending"
            item.correction_evidence = next((e for e in eligible if normalize_article_key(e.article_no or "") == normalize_article_key(target.article_no or "")), None)
            item.repair_verified = item.correction_evidence is not None and resolution.status == "resolved"
        item.location_resolution = resolution
        repairs[index] = resolution
    return repairs


def _location_query_text(item: _CheckItem) -> str:
    """单引用纠错时只去掉已定位的引用字面。"""
    span = item.citation_span
    if span is None:
        return item.claim.text
    start, end = span
    if not 0 <= start < end <= len(item.claim.text):
        return item.claim.text
    tail = re.sub(
        r"^\s*(?:之规定|规定|明确|指出|载明|要求|所称)?\s*[：:,，]?\s*",
        "",
        item.claim.text[end:],
        count=1,
    )
    return (item.claim.text[:start] + tail).strip(" \t，,。；;")


def repair_level_missing(cited: StatuteLocator, target: StatuteLocator) -> bool:
    return bool((cited.paragraph_no and not target.paragraph_no) or (cited.item_no and not target.item_no))


def _authority_marks_current(evidence: ArticleEvidence | None) -> bool:
    if evidence is None or evidence.data_source.tier != SourceTier.PKULAW_FALLBACK:
        return False
    values = [evidence.version_label, evidence.version_status]
    timeliness = evidence.source_metadata.get("timeliness", [])
    values.extend(timeliness if isinstance(timeliness, list) else [timeliness])
    return any("现行有效" in str(value) for value in values if value)


def _locator_key(value: str | None) -> str:
    return normalize_article_key((value or "").replace("款", "条").replace("项", "条").replace("（", "").replace("）", ""))


def _location_suggestion(
    resolution: StatuteLocationResolution | None,
) -> str:
    if resolution is None or resolution.status == "not_found":
        return "请核实所引款、项编号。"
    if resolution.status == "candidates_pending":
        return "已召回相关条文待核查，请结合上下文及适用版本人工确认。"
    locator = resolution.candidates[0].locator
    target = "".join(filter(None, (
        locator.article_no,
        locator.paragraph_no,
        locator.item_no,
    )))
    return f"经候选原文核对，引文内容对应{target}，请更正引用位置。"


def _location_user_message(
    problem: str,
    resolution: StatuteLocationResolution | None,
) -> str:
    suggestion = _location_suggestion(resolution)
    if resolution is not None and resolution.status == "resolved":
        return suggestion
    return f"{problem}，{suggestion}"


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
            or normalize_article_key(result.evidence.article_no or "") != normalize_article_key(item.article_no or "")
        )
    )


def _format_findings(item: _CheckItem) -> list[StatuteFinding]:
    """编号书写规范检查（条、款、项编号格式与条号缺失）。"""
    raw_locator = item.claim.text[slice(*item.citation_span)] if item.citation_span else item.article_no or ""
    findings: list[StatuteFinding] = []
    invalid = invalid_number_tokens(raw_locator)
    if invalid:
        findings.append(StatuteFinding(code=StatuteErrorCode.FORMAT_ERROR, risk_level="HIGH",
            summary="引用编号写法不规范：" + "、".join(invalid), suggestion="请按规范数字形式书写条、款、项编号。"))
    if item.article and item.article.raw_locator:
        findings.append(StatuteFinding(
            code=StatuteErrorCode.FORMAT_ERROR,
            risk_level="HIGH",
            summary=f"{item.article.raw_locator}缺少所属条号",
            suggestion="款号不能脱离条号单独引用；请改为正确的第X条，并按需补充第Y款。",
        ))
    return findings


def _absence_override_findings(
    item: _CheckItem,
    lookup_result: LookupResult,
    attempts: list[SourceTrace],
    findings: list[StatuteFinding],
) -> list[StatuteFinding] | None:
    """北大法宝权威确认条号缺席时，用确定性结论覆盖待定结论。"""
    absence = _pkulaw_confirms_absence(attempts)
    if not (
        absence
        and lookup_result.status == LookupStatus.LAW_FOUND_ARTICLE_MISSING
        and not any(f.code in {
            StatuteErrorCode.SOURCE_REPEALED,
            StatuteErrorCode.SOURCE_AMENDED,
        } for f in findings)
    ):
        return None
    authoritative_title = absence.get("title") or item.display_title
    return [StatuteFinding(
        code=StatuteErrorCode.ARTICLE_NUMBER_ERROR,
        risk_level="HIGH",
        summary=(
            f"《{item.display_title}》（{authoritative_title}）"
            f"不存在所引条号{item.article_no}"
        ),
        suggestion=(
            f"条号引用错误，《{item.display_title}》不存在"
            f"{item.article_no}，请核对条文序号。"
        ),
        cited_locator=_item_locators(item)[0],
    )]


def _run_judgments(
    items: list[_CheckItem],
    lookup_results: dict[tuple, tuple[LookupResult, list[SourceTrace]]],
    known_titles: list[str],
    historical_versions: dict[tuple, list[StatuteVersion]],
    location_repairs: dict[int, StatuteLocationResolution],
) -> dict[int, list[StatuteFinding]]:
    results: dict[int, list[StatuteFinding]] = {}

    for index, item in enumerate(items):
        if item.skip_lookup:
            results[index] = []
            continue
        if item.relation_status in {"parent_unavailable", "insufficient"}:
            results[index] = []
            continue
        if item.relation_status == "locator_mismatch":
            corrected = f"《{item.display_title}》{item.relation_candidate_article_no or ''}"
            resolved = StatuteLocator(article_no=item.relation_candidate_article_no)
            results[index] = [StatuteFinding(
                code=StatuteErrorCode.ARTICLE_NUMBER_ERROR,
                risk_level="HIGH",
                summary="内部转引与主法条原文明示援引的条号不一致",
                suggestion=_verified_correction_suggestion(
                    StatuteErrorCode.ARTICLE_NUMBER_ERROR, corrected
                ),
                resolved_locator=resolved,
                revision=_locator_revision(item, resolved),
            )]
            continue
        lookup_result, attempts = lookup_results[item.lookup_key]
        if item.structure is not None:
            if (
                lookup_result.status == LookupStatus.LAW_FOUND_ARTICLE_MISSING
                or int(lookup_result.trace.metadata.get("candidate_count", 1)) > 1
            ):
                results[index] = [StatuteFinding(
                    code=StatuteErrorCode.CITATION_HIERARCHY_ERROR,
                    risk_level=(
                        "HIGH" if lookup_result.status == LookupStatus.LAW_FOUND_ARTICLE_MISSING
                        else "MEDIUM"
                    ),
                    summary=lookup_result.trace.message,
                    suggestion=f"{lookup_result.trace.message}，请核实章节编号及其上级结构。",
                )]
            else:
                results[index] = []
            continue
        findings = assess_statute(
            item.law_title,
            item.article_no,
            lookup_result,
            attempts,
            known_titles,
            historical_versions.get(item.lookup_key),
            claim_text=item.claim.text,
        )
        findings.extend(_format_findings(item))
        article_missing = _article_is_missing(item, lookup_result)
        repair = location_repairs.get(index)
        repair_finding = _verified_repair_finding(item)
        if repair_finding is not None:
            findings = [f for f in findings if f.code not in {
                StatuteErrorCode.ARTICLE_NOT_FOUND, StatuteErrorCode.ARTICLE_NUMBER_ERROR,
                StatuteErrorCode.CITATION_HIERARCHY_ERROR, StatuteErrorCode.SOURCE_NOT_FOUND,
                StatuteErrorCode.LAW_NAME_ERROR}]
            findings.append(repair_finding)
            results[index] = findings
            continue
        absence_override = _absence_override_findings(item, lookup_result, attempts, findings)
        if absence_override is not None:
            results[index] = absence_override
            continue

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
                    suggestion="法源版本或效力错误，请核对适用时间和现行规定。",
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
                    resolved = repair.candidates[0].locator
                    corrected = "".join(filter(None, (
                        resolved.article_no, resolved.paragraph_no, resolved.item_no,
                    )))
                    finding.suggestion = _verified_correction_suggestion(
                        StatuteErrorCode.CITATION_HIERARCHY_ERROR,
                        f"《{item.display_title}》{corrected}",
                    )
                    finding.resolved_locator = resolved
                    finding.revision = _locator_revision(item, resolved)
                findings.append(finding)
        elif location.status == LocationStatus.STRUCTURE_UNAVAILABLE:
            findings.append(StatuteFinding(
                code=StatuteErrorCode.CITATION_HIERARCHY_ERROR,
                risk_level="MEDIUM",
                summary=location.message,
                suggestion=f"{location.message}，请分别注明各款、项的对应关系。",
            ))
        results[index] = findings
    return results


def _pkulaw_confirms_absence(attempts: list[SourceTrace]) -> dict | None:
    return next((
        attempt.metadata
        for attempt in attempts
        if attempt.tier == SourceTier.PKULAW_FALLBACK
        and attempt.status == LookupStatus.LAW_FOUND_ARTICLE_MISSING
        and attempt.metadata.get("article_absent_confirmed") is True
    ), None)


def _pkulaw_absence_conflicts(attempts: list[SourceTrace]) -> bool:
    return any(
        attempt.tier == SourceTier.PKULAW_FALLBACK
        and attempt.metadata.get("article_absence_confirmation") == "conflict"
        for attempt in attempts
    )


def _verified_repair_finding(item: _CheckItem) -> StatuteFinding | None:
    resolution = item.location_resolution
    if not item.repair_verified or not item.correction_evidence or not resolution or resolution.status != "resolved":
        return None
    if len(resolution.candidates) != 1:
        return None
    resolved = resolution.candidates[0].locator
    title = item.correction_evidence.law_title
    if not equivalent_law_titles(title, item.law_title):
        code = StatuteErrorCode.LAW_NAME_ERROR
    elif normalize_article_key(resolved.article_no or "") != normalize_article_key(item.article_no or ""):
        code = StatuteErrorCode.ARTICLE_NUMBER_ERROR
    else:
        if not resolution.candidates[0].supported:
            return None
        code = StatuteErrorCode.CITATION_HIERARCHY_ERROR
    target = "".join(filter(None, (resolved.article_no, resolved.paragraph_no, resolved.item_no)))
    corrected = f"《{title if code == StatuteErrorCode.LAW_NAME_ERROR else item.display_title}》"
    if code != StatuteErrorCode.LAW_NAME_ERROR:
        corrected += target
    return StatuteFinding(code=code, risk_level="HIGH", summary=f"引文内容对应{target}",
        suggestion=_verified_correction_suggestion(code, corrected),
        cited_locator=_item_locators(item)[0], resolved_locator=resolved,
        revision=_locator_revision(item, resolved) if code != StatuteErrorCode.LAW_NAME_ERROR else None)


def _verified_correction_suggestion(
    code: StatuteErrorCode, corrected: str
) -> str:
    prefix = {
        StatuteErrorCode.LAW_NAME_ERROR: "法律名称引用错误",
        StatuteErrorCode.ARTICLE_NUMBER_ERROR: "条号引用错误",
        StatuteErrorCode.CITATION_HIERARCHY_ERROR: "条款项层级引用错误",
        StatuteErrorCode.SOURCE_REPEALED: "所引版本已更新",
        StatuteErrorCode.SOURCE_AMENDED: "所引版本已更新",
    }[code]
    return f"{prefix}，应为{corrected}。"


def _is_title_normalization_only_repair(item: _CheckItem) -> bool:
    return bool(
        item.repaired_title
        and equivalent_law_titles(item.display_title, item.repaired_title)
        and (not item.repaired_article_no or item.repaired_article_no == item.article_no)
    )


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


_CITATION_TEXT = re.compile(r"《[^》]+》第[^，。；：]{1,30}条(?:第[^，。；：]{1,12}[款项])?")


def _resolve_repealed_successors(
    source_chain: list[StatuteSource],
    items: list[_CheckItem],
    judgments: dict[int, list[StatuteFinding]],
) -> None:
    """仅批准能由候选权威正文直接支持的唯一跨法源替换。"""
    source = next((
        candidate for candidate in source_chain
        if callable(getattr(candidate, "locate_successor_candidates", None))
    ), None)
    if source is None:
        return
    for index, item in enumerate(items):
        findings = judgments[index]
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
        status_note = repealed.suggestion.rstrip("。")
        if len(unique) != 1:
            repealed.suggestion = (
                f"{status_note}；北大法宝未检索到可确认的现行继受法。"
                if not unique
                else f"{status_note}；北大法宝返回了多个可直接支持原规则的现行条文，请人工确认继受关系。"
            )
            continue
        candidate = next(iter(unique.values()))
        revision = _successor_revision(item, candidate)
        if revision is None:
            continue
        locate_basis = getattr(source, "locate_repeal_basis", None)
        if callable(locate_basis):
            basis_result = locate_basis(
                LookupRequest(law_title=item.law_title), candidate.law_title
            )
            bases = {
                (basis.law_title, basis.article_no): basis
                for basis in basis_result.candidates
                if basis.article_no
            }
            if len(bases) == 1:
                basis = next(iter(bases.values()))
                date = f"于{candidate.effective_from}" if candidate.effective_from else ""
                status_note = (
                    f"《{item.law_title}》已{date}因《{basis.law_title}》施行而废止"
                    f"（{basis.article_no}）"
                )
        repealed.resolved_locator = StatuteLocator(article_no=candidate.article_no)
        repealed.suggestion = (
            f"{status_note}；经北大法宝条文直接核验，现行对应规则由"
            f"《{candidate.law_title}》{candidate.article_no}规定。"
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


def _explicit_version_hint(raw_time: str | None) -> str | None:
    hint = explicit_version_hint(raw_time)
    return raw_time if hint == "current" else hint


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
    judgments: dict[int, list[StatuteFinding]],
) -> dict[int, LegalApplicationCheck]:
    """按单个 Claim 聚合可靠法条；法律适用与逐条引用判定独立执行。"""
    compare_application = (
        getattr(semantic_checker, "compare_application", None)
        if semantic_checker is not None else None
    )
    if not callable(compare_application):
        return {}

    candidates: list[ApplicationCandidate] = []
    for index, item in enumerate(items):
        if (
            item.skip_lookup
            or (not item.article_no and not item.related_query)
        ):
            continue
        lookup_pair = lookup_results.get(item.lookup_key)
        if lookup_pair is None:
            continue
        lookup_result, _ = lookup_pair
        evidence = item.correction_evidence if item.repair_verified else lookup_result.evidence
        citation_findings = judgments[index]
        correction = next((f for f in citation_findings if f.code in {
            StatuteErrorCode.ARTICLE_NUMBER_ERROR, StatuteErrorCode.CITATION_HIERARCHY_ERROR
        } and f.resolved_locator), None)
        if (
            (lookup_result.status not in {
                LookupStatus.ARTICLE_FOUND,
                LookupStatus.RELEVANT_ARTICLES_FOUND,
            } and not item.repair_verified)
            or evidence is None
            or not evidence.article_text
            or item.planner_failure
            or lookup_result.trace.metadata.get("related_failure")
            or (citation_findings and correction is None)
            or item.relation_status in {
                "parent_failed", "parent_unavailable", "locator_mismatch", "insufficient",
            }
        ):
            continue
        location = _assess_item_location(item, lookup_result) if correction is None else assess_location(
            parse_article_structure(evidence.article_no, evidence.article_text,
                trust_single_paragraph=evidence.data_source.tier == SourceTier.LOCAL_SQLITE),
            [correction.resolved_locator])
        if location.authoritative_text:
            evidence = evidence.model_copy(
                update={"article_text": location.authoritative_text}
            )
        candidates.append(ApplicationCandidate(
            item_index=index,
            claim_id=item.claim.claim_id,
            original_text=item.claim.text,
            authority=ApplicationAuthority.from_evidence(
                _cited_source(item) if correction is None else f"《{item.display_title}》{evidence.article_no}", evidence
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
                bind_current_timer(
                    lambda job: compare_application_with_llm(
                        semantic_checker,
                        job.original_text,
                        list(job.authorities),
                    )
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
    judgments: dict[int, list[StatuteFinding]],
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
        findings = judgments[index]
        application_check = application_checks.get(index)
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
                correction_evidence=item.correction_evidence,
                location_resolution=item.location_resolution,
                findings=findings,
                outcome=(
                    "bug" if item.out_of_scope or item.not_verifiable
                    else "review" if lookup_result and lookup_result.trace.metadata.get("related_pending")
                    else "review" if item.location_resolution and item.location_resolution.status != "resolved" and not any(f.risk_level == "HIGH" for f in findings)
                    else _statute_outcome(
                        findings, item.reference_role, item.relation_status,
                        application_check,
                        system_failed=bool(
                            item.planner_failure
                            or (
                                lookup_result is not None
                                and (
                                    lookup_result.status in {
                                        LookupStatus.LAW_NOT_FOUND,
                                        LookupStatus.SOURCE_ERROR,
                                        LookupStatus.SOURCE_NOT_CONFIGURED,
                                    }
                                    or lookup_result.trace.metadata.get("related_failure")
                                )
                            )
                        ),
                    )
                ),
                message=(
                    item.relation_message
                    or (application_check.notes if application_check else "")
                    or ("模型服务暂时不可用" if item.planner_failure else "")
                    or (
                        lookup_result.trace.message
                        if lookup_result is not None
                        and (
                            lookup_result.status in {
                                LookupStatus.LAW_NOT_FOUND,
                                LookupStatus.SOURCE_ERROR,
                                LookupStatus.SOURCE_NOT_CONFIGURED,
                            }
                            or lookup_result.trace.metadata.get("related_failure")
                        )
                        else ""
                    )
                    or (
                        item.repair_message
                        if not _is_title_normalization_only_repair(item) else ""
                    )
                    or ("引文内容与所引规范的对应关系尚待核查" if item.location_resolution and item.location_resolution.status != "resolved" else "")
                    or item.out_of_scope or item.not_verifiable or ""
                ),
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
    reference_role: str,
    relation_status: str | None = None,
    application_check: LegalApplicationCheck | None = None,
    *,
    system_failed: bool = False,
) -> str:
    if findings:
        return "issue"
    if system_failed:
        return "bug"
    if relation_status in {"parent_failed", "parent_unavailable", "insufficient"}:
        return "bug"
    if reference_role == "nested" and relation_status in {"confirmed", "resolved"}:
        return "pass"
    return _application_outcome(application_check)


def _application_outcome(check: LegalApplicationCheck | None) -> str:
    if check is None:
        return "pass"
    if check.execution_status != ExecutionStatus.COMPLETED:
        return "bug"
    return "review" if check.verdict == "review" else "pass"


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
