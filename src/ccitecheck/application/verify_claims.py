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
from ..domain.evidence import (
    ArticleEvidence,
    ArticleExcerpt,
    LookupStatus,
    SourceTier,
    SourceTrace,
)
from ..domain.result import RiskLevel, SemanticErrorType
from ..domain.legal_numbers import chinese_number_to_int
from ..domain.result import ComparisonVerdict
from ..tracing.sources.eurlex import article_number_from_citation, fetch_article_excerpt
from ..domain.result import FrontendVerificationDocument, SemanticCheckResult, SemanticIssue
from ..infrastructure.config import load_project_env
from ..infrastructure.database import (
    connect,
    find_law,
    list_articles_in_structure,
    list_current_articles,
    list_law_titles,
    resolve_structure_path,
    strip_version_annotation,
)
from ..judgment.cases import verify_case_claims
from ..judgment.deterministic import build_rule_findings, classify_not_verifiable
from ..judgment.semantic import SemanticChecker
from ..judgment.service import (
    add_article_suggestion,
    compare_with_llm,
    decide_semantic_gate,
    skipped_semantic_result,
)
from ..output.verification import CitationReferenceData, build_verification_document
from ..tracing.retrieval import retrieve_relevant_articles
from ..recognition.spans import locate_claim_article_spans
from ..tracing.service import build_default_sources, build_eu_sources, run_lookup_batch
from ..tracing.sources import (
    CaseNumberRecognizer,
    LookupRequest,
    LookupResult,
    PkulawCaseSource,
    StatuteSource,
)


def _semantic_workers() -> int:
    return max(1, int(os.getenv("QWEN_SEMANTIC_WORKERS", "4")))


def _salvage_max() -> int:
    return max(0, int(os.getenv("QWEN_SALVAGE_MAX", "8")))


def verify_claim_document(
    claim_document: ClaimDocument,
    database_path: str | Path,
    sources: Iterable[StatuteSource] | None = None,
    semantic_checker: SemanticChecker | None = None,
    case_recognizer: CaseNumberRecognizer | None = None,
    include_statutes: bool = True,
    include_cases: bool = True,
) -> FrontendVerificationDocument:
    """编排一份引用文档的溯源、判定和结果输出。"""
    source_chain = (
        list(sources) if sources is not None else build_default_sources(database_path)
    )
    recognizer = case_recognizer or PkulawCaseSource()
    items = _collect_check_items(claim_document) if include_statutes else []
    lookup_results = _run_lookups(source_chain, items)
    lookup_results.update(_run_structure_lookups(database_path, items))
    judgments = _run_judgments(
        semantic_checker,
        items,
        lookup_results,
        _load_known_titles(database_path),
        database_path,
    )
    reference_data = _build_reference_data(items, lookup_results, judgments)
    case_checks = (
        verify_case_claims(claim_document, recognizer)
        if include_cases
        else []
    )
    return build_verification_document(
        claim_document.claim_meta.claim_doc_id,
        reference_data,
        case_checks,
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
            jurisdiction = getattr(legal_source, "jurisdiction", "CN")
            out_of_scope = _classify_out_of_scope(jurisdiction)
            structures = getattr(legal_source, "structures", [])
            if not legal_source.articles and structures:
                # 章节引用（如《民法典》第三编第四章）：走本地结构解析
                for structure in structures:
                    items.append(
                        _CheckItem(
                            claim=claim,
                            law_title=legal_source.title,
                            source_type=legal_source.source_type.value,
                            article=None,
                            article_no=structure.label,
                            not_verifiable=not_verifiable,
                            jurisdiction=jurisdiction,
                            out_of_scope=out_of_scope,
                            structure=structure,
                        )
                    )
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


def _classify_out_of_scope(jurisdiction: str) -> str | None:
    """非中国法域引用的边界分类；返回给用户的说明文本。"""
    if jurisdiction == "FOREIGN":
        return "涉外法规（非中国/欧盟法域），超出本产品核查边界，请人工核验"
    load_project_env()
    if jurisdiction == "EU" and not os.getenv("EURLEX_MCP_GATEWAY"):
        return (
            "欧盟法规数据源未配置，暂无法核验；"
            "请人工核验，或配置 EURLEX_MCP_GATEWAY 后重新核查"
        )
    return None


def _run_lookups(
    source_chain: list[StatuteSource],
    items: list[_CheckItem],
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
    return results


def _run_structure_lookups(
    database_path: str | Path,
    items: list[_CheckItem],
) -> dict[tuple, tuple[LookupResult, list[SourceTrace]]]:
    results: dict[tuple, tuple[LookupResult, list[SourceTrace]]] = {}
    db_path = Path(database_path)
    for item in items:
        if item.structure is None or item.skip_lookup:
            continue
        if item.lookup_key not in results:
            results[item.lookup_key] = _lookup_structure(db_path, item)
    return results


def _lookup_structure(
    db_path: Path, item: _CheckItem
) -> tuple[LookupResult, list[SourceTrace]]:
    """在本地结构表解析章节引用；多候选原样返回，绝不猜测。"""
    trace = SourceTrace(
        tier=SourceTier.LOCAL_SQLITE,
        source_name="CCiteheck 本地章节结构",
        status=LookupStatus.LAW_NOT_FOUND,
    )
    label = item.structure.label
    if not db_path.exists():
        trace.status = LookupStatus.SOURCE_NOT_CONFIGURED
        trace.message = f"SQLite database not found: {db_path}"
        return LookupResult(trace.status, None, trace), [trace]
    with connect(db_path) as conn:
        law = find_law(conn, item.law_title)
        if law is None:
            trace.message = "本地法规库未收录该法规，章节引用无法核验"
            return LookupResult(trace.status, None, trace), [trace]
        tokens = [(unit.unit, unit.number) for unit in item.structure.units]
        candidates = resolve_structure_path(conn, int(law["id"]), tokens)
        if not candidates:
            trace.status = LookupStatus.LAW_FOUND_ARTICLE_MISSING
            trace.message = f"《{law['title']}》现行章节结构中不存在{label}"
            evidence = ArticleEvidence(
                law_title=law["title"],
                source_type=law["source_type"],
                article_no=label,
                data_source=trace,
            )
            return LookupResult(trace.status, evidence, trace), [trace]

        trace.status = LookupStatus.RELEVANT_ARTICLES_FOUND
        trace.metadata = {"candidate_count": len(candidates)}
        if len(candidates) == 1:
            node = candidates[0]
            members = list_articles_in_structure(conn, int(node["id"]))
            trace.message = f"已定位章节：{node['path_label']}（含 {len(members)} 条条文）"
            excerpts = [
                ArticleExcerpt(
                    article_no=member["article_no"],
                    article_text=member["text"][:200],
                    relevance_score=1.0,
                )
                for member in members[:3]
            ]
            evidence = ArticleEvidence(
                law_title=law["title"],
                source_type=law["source_type"],
                article_no=label,
                version_status=law["status"],
                source_metadata={"article_count": len(members)},
                related_articles=excerpts,
                structure_path=node["path_label"],
                data_source=trace,
            )
            return LookupResult(trace.status, evidence, trace), [trace]

        paths = [row["path_label"] for row in candidates[:5]]
        trace.message = (
            f"{label} 在《{law['title']}》中存在 {len(candidates)} 个候选章节，"
            "请人工确认或在文书中补充上级编号"
        )
        evidence = ArticleEvidence(
            law_title=law["title"],
            source_type=law["source_type"],
            article_no=label,
            version_status=law["status"],
            source_metadata={"candidate_count": len(candidates)},
            structure_path="候选：" + "；".join(paths),
            data_source=trace,
        )
        return LookupResult(trace.status, evidence, trace), [trace]


def _structure_judgment(
    item: _CheckItem,
    lookup_result: LookupResult,
) -> tuple[list[SemanticIssue], SemanticCheckResult | None]:
    """章节引用为存在性核验：不存在→定位错误；多候选→转人工。"""
    label = item.structure.label
    if lookup_result.status == LookupStatus.LAW_FOUND_ARTICLE_MISSING:
        return (
            [
                SemanticIssue(
                    error_type=SemanticErrorType.LOCATION_ERROR,
                    risk_level=RiskLevel.HIGH,
                    diff_summary=(
                        f"《{strip_version_annotation(item.law_title)}》现行章节结构中"
                        f"不存在{label}"
                    )[:300],
                    suggestion="请核实章节编号；该章节在现行有效版本中不存在。",
                )
            ],
            None,
        )
    candidate_count = int(
        (lookup_result.trace.metadata or {}).get("candidate_count", 1)
    )
    if candidate_count > 1:
        return [], skipped_semantic_result("structure_ambiguous")
    return [], None


def _run_judgments(
    semantic_checker: SemanticChecker | None,
    items: list[_CheckItem],
    lookup_results: dict[tuple, tuple[LookupResult, list[SourceTrace]]],
    known_titles: list[str],
    database_path: str | Path,
) -> dict[int, tuple[list[SemanticIssue], SemanticCheckResult | None]]:
    results: dict[int, tuple[list[SemanticIssue], SemanticCheckResult | None]] = {}
    semantic_jobs: list[tuple[int, _CheckItem, LookupResult]] = []

    for index, item in enumerate(items):
        if item.skip_lookup:
            results[index] = ([], None)
            continue
        lookup_result, attempts = lookup_results[item.lookup_key]
        if item.structure is not None:
            results[index] = _structure_judgment(item, lookup_result)
            continue
        findings = build_rule_findings(
            item.law_title,
            item.article_no,
            lookup_result,
            attempts,
            known_titles,
            paragraphs=list(item.article.paragraphs) if item.article else None,
        )
        if item.jurisdiction == "EU" and lookup_result.status != LookupStatus.ARTICLE_FOUND:
            # 只提法名的欧盟引用：存在性/时效核验，无条文可比对。
            # 取得具体 Article 原文的引用则继续走（跨语言）语义比对。
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
        for job_id, (item, _) in unique_jobs.items():
            _add_suggestion(
                database_path,
                semantic_checker,
                item,
                comparisons[job_id],
            )
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
                _add_suggestion(database_path, semantic_checker, item, recovered)
            else:
                comparisons[job_id] = recovered
        for index, item, _ in semantic_jobs:
            comparison = comparisons[job_ids[index]].model_copy(deep=True)
            comparison.semantic_job_id = job_ids[index]
            findings, _ = results[index]
            results[index] = (findings, comparison)
        for index, item, lookup_result in semantic_jobs:
            if item.jurisdiction == "EU":
                _append_eu_suggested_article(item, lookup_result, results[index][1])
    return results


_EU_CN_ARTICLE_PATTERN = re.compile(r"第([一二三四五六七八九十百千零两0-9]+)条")
_EU_EN_ARTICLE_PATTERN = re.compile(r"Article\s+(\d+)", re.IGNORECASE)


def _append_eu_suggested_article(item, lookup_result, comparison) -> None:
    """欧盟引用抓错且判定文本指向唯一的另一条时，补取该条原文并入参考条款。"""
    if comparison is None or comparison.verdict != ComparisonVerdict.ISSUE:
        return
    evidence = lookup_result.evidence
    if evidence is None:
        return
    celex = (evidence.source_metadata or {}).get("celex")
    if not celex:
        return
    cited = article_number_from_citation(item.article_no)
    numbers: set[int] = set()
    for issue in comparison.issues:
        text = f"{issue.diff_summary} {issue.suggestion}"
        for match in _EU_CN_ARTICLE_PATTERN.finditer(text):
            number = chinese_number_to_int(match.group(1))
            if number:
                numbers.add(number)
        for match in _EU_EN_ARTICLE_PATTERN.finditer(text):
            numbers.add(int(match.group(1)))
    numbers.discard(cited)
    if len(numbers) != 1:
        return
    number = numbers.pop()
    label = f"Article {number}"
    if any(excerpt.article_no == label for excerpt in evidence.related_articles):
        return
    excerpt = fetch_article_excerpt(celex, number)
    if excerpt is not None:
        evidence.related_articles.append(excerpt)


def _compare_job(
    semantic_checker: SemanticChecker,
    item: _CheckItem,
    lookup_result: LookupResult,
) -> SemanticCheckResult:
    if lookup_result.evidence is None:
        raise ValueError("语义核查任务缺少法条证据")
    return compare_with_llm(
        semantic_checker,
        item.document_quote,
        item.claim.text,
        _cited_source(item),
        lookup_result.evidence,
        # 款级切片按中文条文的分款体例定位，外文条文不适用
        paragraphs=(
            list(item.article.paragraphs)
            if item.article and item.jurisdiction != "EU"
            else None
        ),
    )


def _add_suggestion(
    database_path: str | Path,
    semantic_checker: SemanticChecker,
    item: _CheckItem,
    comparison: SemanticCheckResult,
) -> None:
    path = Path(database_path)
    if not path.exists():
        return
    with connect(path) as connection:
        articles = list_current_articles(connection, item.law_title)
    excerpts = retrieve_relevant_articles(item.document_quote, articles) if articles else []
    candidates = [
        {"article_no": excerpt.article_no, "article_text": excerpt.article_text}
        for excerpt in excerpts
    ]
    add_article_suggestion(
        semantic_checker,
        item.document_quote,
        item.article_no,
        candidates,
        comparison,
    )


def _build_reference_data(
    items: list[_CheckItem],
    lookup_results: dict[tuple, tuple[LookupResult, list[SourceTrace]]],
    judgments: dict[int, tuple[list[SemanticIssue], SemanticCheckResult | None]],
) -> list[CitationReferenceData]:
    data: list[CitationReferenceData] = []
    for index, item in enumerate(items):
        lookup_result = attempts = None
        if not item.skip_lookup:
            lookup_result, attempts = lookup_results[item.lookup_key]
        findings, comparison = judgments[index]
        data.append(
            CitationReferenceData(
                claim=item.claim,
                law_title=item.law_title,
                article_no=item.article_no,
                paragraphs=list(item.article.paragraphs) if item.article else [],
                items=list(item.article.items) if item.article else [],
                cited_text=(
                    item.claim.text[item.article.citation_span[0]:item.article.citation_span[1]]
                    if item.article and item.article.citation_span else _cited_source(item)
                ),
                reference_role=item.reference_role,
                mention_span=item.article.mention_span if item.article else None,
                citation_span=item.article.citation_span if item.article else None,
                quote_span=item.article.quote_span if item.article else None,
                not_verifiable=item.not_verifiable,
                out_of_scope=item.out_of_scope,
                lookup_status=(lookup_result.status if lookup_result else None),
                evidence=(lookup_result.evidence if lookup_result else None),
                source_attempts=attempts or [],
                rule_findings=findings,
                semantic_comparison=comparison,
                verification_scope=(
                    "existence_only"
                    if item.reference_role == "nested"
                    or item.structure is not None
                    or (
                        item.jurisdiction == "EU"
                        and (
                            lookup_result is None
                            or lookup_result.status != LookupStatus.ARTICLE_FOUND
                        )
                    )
                    else "full"
                ),
                jurisdiction=item.jurisdiction,
            )
        )
    return data


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
