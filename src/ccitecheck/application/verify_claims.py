"""引用溯源、判定和结果输出的应用层编排。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..domain.citation import ArticleRef, Claim, ClaimDocument, ClaimType
from ..domain.evidence import SourceTrace
from ..domain.result import FrontendVerificationDocument, SemanticComparison, SemanticIssue
from ..infrastructure.database import connect, list_current_articles, list_law_titles
from ..judgment.cases import verify_case_claims
from ..judgment.deterministic import build_rule_findings, classify_not_verifiable
from ..judgment.semantic import SemanticChecker
from ..judgment.service import (
    NEEDS_LLM,
    add_article_suggestion,
    compare_with_llm,
    semantic_without_llm,
)
from ..output.verification import LegalCheckData, build_verification_document
from ..tracing.retrieval import retrieve_relevant_articles
from ..tracing.service import build_default_sources, run_lookup_batch
from ..tracing.sources import (
    CaseNumberRecognizer,
    LookupRequest,
    LookupResult,
    PkulawCaseSource,
    StatuteSource,
)

_SEMANTIC_WORKERS = 4


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
    legal_data = _build_legal_data(items, lookup_results, judgments)
    case_checks = (
        verify_case_claims(claim_document, recognizer)
        if include_cases
        else []
    )
    return build_verification_document(
        claim_document.claim_meta.claim_doc_id,
        legal_data,
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


def _collect_check_items(claim_document: ClaimDocument) -> list[_CheckItem]:
    items: list[_CheckItem] = []
    for claim in claim_document.claims:
        if claim.claim_type != ClaimType.LEGAL_SOURCE_CLAIM:
            continue
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
) -> dict[int, tuple[list[SemanticIssue], SemanticComparison | None]]:
    results: dict[int, tuple[list[SemanticIssue], SemanticComparison | None]] = {}
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
        if findings:
            results[index] = (findings, None)
            continue
        comparison = semantic_without_llm(
            semantic_checker,
            _cited_source(item),
            lookup_result,
        )
        if comparison is not NEEDS_LLM:
            results[index] = (findings, comparison)
            continue
        results[index] = (findings, None)
        semantic_jobs.append((index, item, lookup_result))

    if semantic_jobs and semantic_checker is not None:
        with ThreadPoolExecutor(max_workers=_SEMANTIC_WORKERS) as pool:
            comparisons = list(
                pool.map(
                    lambda job: _compare_job(semantic_checker, job[1], job[2]),
                    semantic_jobs,
                )
            )
        for (index, item, _), comparison in zip(semantic_jobs, comparisons):
            _add_suggestion(
                database_path,
                semantic_checker,
                item,
                comparison,
            )
            findings, _ = results[index]
            results[index] = (findings, comparison)
    return results


def _compare_job(
    semantic_checker: SemanticChecker,
    item: _CheckItem,
    lookup_result: LookupResult,
) -> SemanticComparison:
    if lookup_result.evidence is None:
        raise ValueError("语义核查任务缺少法条证据")
    return compare_with_llm(
        semantic_checker,
        item.claim.text,
        item.claim.context_text or item.claim.text,
        _cited_source(item),
        lookup_result.evidence,
    )


def _add_suggestion(
    database_path: str | Path,
    semantic_checker: SemanticChecker,
    item: _CheckItem,
    comparison: SemanticComparison,
) -> None:
    path = Path(database_path)
    if not path.exists():
        return
    with connect(path) as connection:
        articles = list_current_articles(connection, item.law_title)
    excerpts = retrieve_relevant_articles(item.claim.text, articles) if articles else []
    candidates = [
        {"article_no": excerpt.article_no, "article_text": excerpt.article_text}
        for excerpt in excerpts
    ]
    add_article_suggestion(
        semantic_checker,
        item.claim.text,
        item.article_no,
        candidates,
        comparison,
    )


def _build_legal_data(
    items: list[_CheckItem],
    lookup_results: dict[tuple, tuple[LookupResult, list[SourceTrace]]],
    judgments: dict[int, tuple[list[SemanticIssue], SemanticComparison | None]],
) -> list[LegalCheckData]:
    data: list[LegalCheckData] = []
    for index, item in enumerate(items):
        lookup_result = attempts = None
        if item.not_verifiable is None:
            lookup_result, attempts = lookup_results[item.lookup_key]
        findings, comparison = judgments[index]
        data.append(
            LegalCheckData(
                claim=item.claim,
                law_title=item.law_title,
                article_no=item.article_no,
                not_verifiable=item.not_verifiable,
                lookup_status=(lookup_result.status if lookup_result else None),
                evidence=(lookup_result.evidence if lookup_result else None),
                source_attempts=attempts or [],
                rule_findings=findings,
                semantic_comparison=comparison,
            )
        )
    return data


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
