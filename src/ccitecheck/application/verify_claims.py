"""引用溯源、判定和结果输出的应用层编排。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Iterable

from ..domain.citation import ArticleRef, Claim, ClaimDocument, ClaimType
from ..domain.evidence import SourceTrace
from ..domain.result import FrontendVerificationDocument, SemanticCheckResult, SemanticIssue
from ..infrastructure.database import connect, list_current_articles, list_law_titles
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
from ..tracing.service import build_default_sources, run_lookup_batch
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
    source_chain: list[StatuteSource],
    items: list[_CheckItem],
) -> dict[tuple, tuple[LookupResult, list[SourceTrace]]]:
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
    return run_lookup_batch(source_chain, requests)


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
        if item.not_verifiable is not None:
            results[index] = ([], None)
            continue
        lookup_result, attempts = lookup_results[item.lookup_key]
        findings = build_rule_findings(
            item.law_title,
            item.article_no,
            lookup_result,
            attempts,
            known_titles,
        )
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
    return results


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
        if item.not_verifiable is None:
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
                lookup_status=(lookup_result.status if lookup_result else None),
                evidence=(lookup_result.evidence if lookup_result else None),
                source_attempts=attempts or [],
                rule_findings=findings,
                semantic_comparison=comparison,
                verification_scope=("existence_only" if item.reference_role == "nested" else "full"),
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
