"""北大法宝法规回退源及其确定性三级路由。"""

from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Protocol

from ....domain.evidence import (
    ArticleEvidence,
    ArticleExcerpt,
    LookupStatus,
    SourceTier,
    SourceTrace,
)
from ...queries import (
    build_article_exact_title,
    build_law_semantic_query,
    has_substantive_content,
)
from ...retrieval import retrieve_relevant_articles
from ..base import LocationCandidateResult, LookupRequest, LookupResult
from .cache import CachedPkulawClient, cache_enabled
from .client import (
    PkulawArticle,
    PkulawLawRecord,
    PkulawMcpClient,
    PkulawMcpError,
    PkulawNotConfiguredError,
    PkulawNotFoundError,
    normalize_article_no,
)
from .matching import match_law_record


class PkulawFallbackSource:
    def __init__(self, client: Optional["StatuteLookupClient"] = None):
        self.client = client

    def lookup(self, request: LookupRequest) -> LookupResult:
        return (
            self._lookup_article(request)
            if request.article_no
            else self._lookup_without_article(request)
        )

    def locate_candidates(self, request: LookupRequest) -> LocationCandidateResult:
        """通过北大法宝语义检索召回同一法规的定位候选。"""
        trace = self._trace()
        trace.metadata["route_attempts"].append({
            "service": "law_semantic",
            "purpose": "citation_location",
            "status": "started",
        })
        attempt = trace.metadata["route_attempts"][-1]
        try:
            articles = self._client().search_law_articles(
                build_law_semantic_query(request.context_text, request.law_title)
            )
        except PkulawNotFoundError:
            articles = []
            attempt["status"] = "not_found"
        except PkulawMcpError as exc:
            attempt.update(status="error", message=str(exc))
            trace.status = _pkulaw_error_status(exc)
            trace.message = str(exc)
            return LocationCandidateResult([], trace)

        filtered = [
            article
            for article in articles
            if match_law_record(request.law_title, [article]) is not None
        ]
        attempt.update(status="completed", candidate_count=len(filtered))
        if not filtered and request.article_no:
            filtered = self._scan_nearby_articles(request, trace)
        trace.status = (
            LookupStatus.RELEVANT_ARTICLES_FOUND
            if filtered
            else LookupStatus.LAW_NOT_FOUND
        )
        trace.message = "定位候选检索完成"
        return LocationCandidateResult(
            candidates=[self._candidate_evidence(request, trace, article) for article in filtered],
            trace=trace,
        )

    def _scan_nearby_articles(self, request: LookupRequest, trace: SourceTrace) -> list[PkulawArticle]:
        """语义服务无结果时，对引用条号附近做受限精确扫描。"""
        from ....domain.legal_numbers import chinese_number_to_int
        import re

        match = re.fullmatch(r"第([一二三四五六七八九十百千万两零〇0-9]+)条", request.article_no or "")
        if not match:
            return []
        token = match.group(1)
        try:
            cited = int(token) if token.isdigit() else chinese_number_to_int(token)
        except ValueError:
            return []
        numbers = list(range(max(1, cited - 10), cited + 11))
        title = build_article_exact_title(request.law_title)

        def fetch(number: int):
            try:
                return self._client().get_article(title, f"第{number}条")
            except (PkulawNotFoundError, PkulawMcpError):
                return None

        with ThreadPoolExecutor(max_workers=6) as pool:
            articles = [article for article in pool.map(fetch, numbers) if article is not None]
        matched = [article for article in articles if match_law_record(request.law_title, [article])]
        trace.metadata["route_attempts"].append({
            "service": "law_exact_nearby_scan",
            "purpose": "citation_location",
            "status": "completed",
            "range": [numbers[0], numbers[-1]],
            "candidate_count": len(matched),
        })
        return matched

    def locate_successor_candidates(self, request: LookupRequest) -> LocationCandidateResult:
        """检索废止规则在现行法规中的继受条文；这里只召回，不作结论。"""
        trace = self._trace()
        query = f"检索现行有效法规中与以下已废止规则对应的条文：{request.context_text}"[:500]
        try:
            articles = self._client().search_law_articles(query)
        except PkulawNotFoundError:
            articles = []
        except PkulawMcpError as exc:
            trace.status = _pkulaw_error_status(exc)
            trace.message = str(exc)
            return LocationCandidateResult([], trace)
        current = []
        for article in articles:
            if match_law_record(request.law_title, [article]) is not None:
                continue
            try:
                laws = self._client().get_law_list(title=article.title)
            except (PkulawNotFoundError, PkulawMcpError):
                continue
            law = match_law_record(article.title, laws)
            statuses = "".join((law.timeliness if law else []) + (law.effectiveness if law else []))
            if law is None or not any(token in statuses for token in ("现行有效", "有效")):
                continue
            if any(token in statuses for token in ("废止", "失效")):
                continue
            current.append(replace(
                article,
                timeliness=law.timeliness,
                effectiveness=law.effectiveness,
                implement_date=law.implement_date,
                issue_date=law.issue_date,
            ))
        trace.status = LookupStatus.RELEVANT_ARTICLES_FOUND if current else LookupStatus.LAW_NOT_FOUND
        trace.message = "现行继受法候选检索完成"
        trace.metadata.update(
            purpose="successor_law",
            candidate_count=len(current),
            query=query,
        )
        return LocationCandidateResult(
            [self._candidate_evidence(request, trace, article) for article in current],
            trace,
        )

    def _trace(self) -> SourceTrace:
        return SourceTrace(
            tier=SourceTier.PKULAW_FALLBACK,
            source_name="北大法宝 MCP",
            status=LookupStatus.LAW_NOT_FOUND,
            metadata={"route_attempts": []},
        )

    def _lookup_article(self, request: LookupRequest) -> LookupResult:
        trace = self._trace()
        attempts = trace.metadata["route_attempts"]
        title = build_article_exact_title(request.law_title)
        attempts.append({"service": "law_search_get_article", "status": "started"})
        try:
            article = self._client().get_article(title, request.article_no or "")
        except PkulawNotFoundError:
            attempts[-1]["status"] = "not_found"
        except PkulawMcpError as exc:
            attempts[-1].update(status="error", message=str(exc))
            return self._error(trace, exc)
        else:
            if match_law_record(request.law_title, [article]) is not None:
                attempts[-1]["status"] = "completed"
                article = self._enrich_timeliness(article, title, attempts)
                return self._article_result(request, trace, article)
            attempts[-1].update(status="mismatched", returned_title=article.title)

        attempts.append({"service": "law_semantic_exact", "status": "started"})
        semantic_error: PkulawMcpError | None = None
        try:
            articles = self._client().search_law_articles_for_article(
                title, request.article_no or ""
            )
            attempts[-1].update(status="completed", candidate_count=len(articles))
        except PkulawNotFoundError:
            articles = []
            attempts[-1]["status"] = "not_found"
        except PkulawMcpError as exc:
            articles = []
            semantic_error = exc
            attempts[-1].update(status="error", message=str(exc))

        filtered = [
            a for a in articles if match_law_record(request.law_title, [a]) is not None
        ]
        wanted = normalize_article_no(request.article_no or "")
        exact = next(
            (a for a in filtered if normalize_article_no(a.article_no) == wanted), None
        )
        if exact is not None:
            exact = self._enrich_timeliness(exact, title, attempts)
            return self._article_result(request, trace, exact)
        related = _rank_articles(request.context_text, filtered)
        if related:
            return self._related_result(
                request,
                trace,
                filtered[0],
                related,
                "精确查询未命中，以下为该法规内语义召回的相关条款",
            )

        attempts.append({"service": "law_keyword", "status": "started"})
        try:
            records = self._client().get_law_list(title=title)
            attempts[-1].update(status="completed", candidate_count=len(records))
        except PkulawNotFoundError:
            records = []
            attempts[-1]["status"] = "not_found"
        except PkulawMcpError as exc:
            attempts[-1].update(status="error", message=str(exc))
            return self._error(trace, exc)
        matched = match_law_record(request.law_title, records)
        if semantic_error is not None:
            return self._error(trace, semantic_error)
        if matched is None:
            trace.status = LookupStatus.LAW_NOT_FOUND
            trace.message = "北大法宝检索完成，未找到该法规"
            trace.metadata.update(
                search_completed=True, candidate_titles=[r.title for r in records[:5]]
            )
            return LookupResult(trace.status, None, trace)
        trace.status = LookupStatus.LAW_FOUND_ARTICLE_MISSING
        trace.message = f"北大法宝已收录该法规，但未返回{request.article_no}"
        trace.source_url = matched.url
        trace.metadata.update(search_completed=True, **_law_record_metadata(matched))
        return LookupResult(
            trace.status, self._metadata_evidence(request, trace, matched), trace
        )

    def _lookup_without_article(self, request: LookupRequest) -> LookupResult:
        trace = self._trace()
        attempts = trace.metadata["route_attempts"]
        title = build_article_exact_title(request.law_title)
        attempts.append({"service": "law_keyword", "status": "started"})
        try:
            records = self._client().get_law_list(title=title)
            attempts[-1].update(status="completed", candidate_count=len(records))
        except PkulawNotFoundError:
            records = []
            attempts[-1]["status"] = "not_found"
        except PkulawMcpError as exc:
            attempts[-1].update(status="error", message=str(exc))
            return self._error(trace, exc)
        matched = match_law_record(request.law_title, records)
        if matched is not None and not has_substantive_content(
            request.context_text, request.law_title
        ):
            return self._law_text_unavailable(request, trace, matched, records)

        attempts.append({"service": "law_semantic", "status": "started"})
        semantic_error: PkulawMcpError | None = None
        try:
            articles = self._client().search_law_articles(
                build_law_semantic_query(request.context_text, request.law_title)
            )
            attempts[-1].update(status="completed", candidate_count=len(articles))
        except PkulawNotFoundError:
            articles = []
            attempts[-1]["status"] = "not_found"
        except PkulawMcpError as exc:
            articles = []
            semantic_error = exc
            attempts[-1].update(status="error", message=str(exc))
        filtered = [
            a for a in articles if match_law_record(request.law_title, [a]) is not None
        ]
        related = _rank_articles(request.context_text, filtered)
        if related:
            return self._related_result(
                request, trace, filtered[0], related, "已通过法规语义检索召回相关条款"
            )
        if matched is not None:
            return self._law_text_unavailable(request, trace, matched, records)
        if semantic_error is not None:
            return self._error(trace, semantic_error)
        trace.status = LookupStatus.LAW_NOT_FOUND
        trace.message = "北大法宝检索完成，未找到该法规"
        trace.metadata.update(
            search_completed=True, candidate_titles=[r.title for r in records[:5]]
        )
        return LookupResult(trace.status, None, trace)

    def _enrich_timeliness(self, article, title, attempts):
        attempt = {
            "service": "law_keyword",
            "purpose": "timeliness_enrichment",
            "status": "started",
        }
        attempts.append(attempt)
        try:
            records = self._client().get_law_list(title=title)
            matched = match_law_record(title, records)
            attempt.update(status="completed", candidate_count=len(records))
            if matched:
                return replace(
                    article,
                    timeliness=matched.timeliness,
                    effectiveness=matched.effectiveness,
                    implement_date=matched.implement_date,
                    issue_date=matched.issue_date,
                    document_no=matched.document_no,
                )
        except PkulawNotFoundError:
            attempt["status"] = "not_found"
        except PkulawMcpError as exc:
            attempt.update(status="error", message=str(exc))
        return article

    def _article_result(self, request, trace, article):
        trace.status = LookupStatus.ARTICLE_FOUND
        trace.source_url = article.url
        trace.metadata.update(_article_metadata(article))
        evidence = ArticleEvidence(
            law_title=article.title,
            source_type=request.source_type,
            article_no=article.article_no,
            article_text=article.article_text,
            version_label=_first(article.timeliness),
            version_status=_first(article.timeliness),
            effective_from=article.implement_date,
            source_metadata=trace.metadata,
            data_source=trace,
        )
        return LookupResult(trace.status, evidence, trace)

    def _related_result(self, request, trace, top, related, message):
        trace.status = LookupStatus.RELEVANT_ARTICLES_FOUND
        trace.message = message
        trace.source_url = top.url
        trace.metadata["retrieval_method"] = "pkulaw_law_semantic"
        evidence = ArticleEvidence(
            law_title=top.title,
            source_type=request.source_type,
            article_text="\n\n".join(_format_excerpt(x) for x in related),
            version_label=_first(top.timeliness),
            version_status=_first(top.timeliness),
            effective_from=top.implement_date,
            source_metadata=trace.metadata,
            related_articles=related,
            data_source=trace,
        )
        return LookupResult(trace.status, evidence, trace)

    def _law_text_unavailable(self, request, trace, matched, records):
        trace.status = LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE
        trace.message = "北大法宝已检索到该法规，但文书未注明条号，当前未取得条文全文"
        trace.source_url = matched.url
        trace.metadata.update(
            candidate_count=len(records),
            candidates=[_law_record_metadata(r) for r in records],
        )
        return LookupResult(
            trace.status, self._metadata_evidence(request, trace, matched), trace
        )

    def _metadata_evidence(self, request, trace, record):
        return ArticleEvidence(
            law_title=record.title,
            source_type=request.source_type,
            article_no=request.article_no,
            article_text=None,
            version_label=_first(record.timeliness),
            version_status=_first(record.timeliness),
            effective_from=record.implement_date,
            source_metadata=_law_record_metadata(record),
            data_source=trace,
        )

    def _candidate_evidence(self, request, trace, article):
        return ArticleEvidence(
            law_title=article.title,
            source_type=request.source_type,
            article_no=article.article_no,
            article_text=article.article_text,
            version_label=_first(article.timeliness),
            version_status=_first(article.timeliness),
            effective_from=article.implement_date,
            source_metadata=_article_metadata(article),
            data_source=trace.model_copy(update={"source_url": article.url}),
        )

    def _error(self, trace, exc):
        trace.status = _pkulaw_error_status(exc)
        trace.message = str(exc)
        return LookupResult(trace.status, None, trace)

    def _client(self):
        if self.client is None:
            client = PkulawMcpClient()
            self.client = CachedPkulawClient(client) if cache_enabled() else client
        return self.client


class StatuteLookupClient(Protocol):
    def get_article(self, title: str, article_no: str) -> PkulawArticle: ...

    def get_law_list(
        self, title: str = "", fulltext: str = ""
    ) -> list[PkulawLawRecord]: ...

    def search_law_articles(self, text: str) -> list[PkulawArticle]: ...

    def search_law_articles_for_article(
        self, title: str, article_no: str
    ) -> list[PkulawArticle]: ...


def _rank_articles(
    context_text: str, articles: list[PkulawArticle]
) -> list[ArticleExcerpt]:
    rows = [
        {"text": a.article_text, "article_no": a.article_no, "article_key": i}
        for i, a in enumerate(articles)
    ]
    return retrieve_relevant_articles(context_text, rows, limit=3)


def _format_excerpt(excerpt: ArticleExcerpt) -> str:
    prefix = f"{excerpt.article_no}　" if excerpt.article_no else ""
    return f"{prefix}{excerpt.article_text}"


def _pkulaw_error_status(exc: PkulawMcpError) -> LookupStatus:
    return (
        LookupStatus.SOURCE_NOT_CONFIGURED
        if isinstance(exc, PkulawNotConfiguredError)
        else LookupStatus.SOURCE_ERROR
    )


def _article_metadata(article: PkulawArticle) -> dict:
    return {**_law_record_metadata(article), "article_no": article.article_no}


def _law_record_metadata(record: PkulawLawRecord) -> dict:
    return {
        "title": record.title,
        "url": record.url,
        "category": record.category,
        "document_no": record.document_no,
        "issue_department": record.issue_department,
        "issue_date": record.issue_date,
        "implement_date": record.implement_date,
        "timeliness": record.timeliness,
        "effectiveness": record.effectiveness,
    }


def _first(values: list[str]) -> Optional[str]:
    return values[0] if values else None


__all__ = ["PkulawFallbackSource"]
