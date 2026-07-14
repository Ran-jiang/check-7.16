"""Source adapters for statute lookup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol
from laws.sqlite_store import (
    connect,
    find_current_article,
    find_law,
    list_current_articles,
    normalize_title,
    strip_version_annotation,
)

from .article_retrieval import retrieve_relevant_articles
from .pkulaw_cache import CachedPkulawClient, cache_enabled
from .pkulaw_mcp import (
    PkulawArticle,
    PkulawCaseNumber,
    PkulawLawRecord,
    PkulawMcpClient,
    PkulawMcpError,
    PkulawNotFoundError,
)
from .schema import ArticleEvidence, LookupStatus, SourceTier, SourceTrace


@dataclass
class LookupRequest:
    law_title: str
    source_type: str
    article_no: Optional[str] = None
    context_text: str = ""


@dataclass
class LookupResult:
    status: LookupStatus
    evidence: Optional[ArticleEvidence]
    trace: SourceTrace


class StatuteSource(Protocol):
    def lookup(self, request: LookupRequest) -> LookupResult:
        ...


class CaseNumberRecognizer(Protocol):
    def recognize(self, text: str) -> list[PkulawCaseNumber]:
        ...


class PkulawCaseSource:
    def __init__(self, client: Optional[PkulawMcpClient] = None):
        self.client = client

    def recognize(self, text: str) -> list[PkulawCaseNumber]:
        return self._client().recognize_case_numbers(text)

    def _client(self) -> PkulawMcpClient:
        if self.client is None:
            self.client = PkulawMcpClient()
        return self.client


class LocalSQLiteSource:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def lookup(self, request: LookupRequest) -> LookupResult:
        trace = SourceTrace(
            tier=SourceTier.LOCAL_SQLITE,
            source_name="CCitecheck 本地 SQLite 法规库",
            status=LookupStatus.LAW_NOT_FOUND,
        )
        if not self.db_path.exists():
            trace.status = LookupStatus.SOURCE_NOT_CONFIGURED
            trace.message = f"SQLite database not found: {self.db_path}"
            return LookupResult(trace.status, None, trace)

        with connect(self.db_path) as conn:
            if request.article_no:
                article = find_current_article(conn, request.law_title, request.article_no)
                if article:
                    trace.status = LookupStatus.ARTICLE_FOUND
                    trace.source_name = article["source_name"] or trace.source_name
                    trace.source_url = article["source_url"]
                    trace.fetched_at = article["source_fetched_at"] or trace.fetched_at
                    evidence = ArticleEvidence(
                        law_title=article["title"],
                        source_type=article["source_type"],
                        article_no=article["article_no"],
                        article_text=article["text"],
                        version_label=article["version_label"] or article["timeliness"],
                        version_status=article["version_status"] or article["law_status"],
                        effective_from=article["effective_from"] or article["effective_at"],
                        source_metadata={
                            "version_key": article["version_key"],
                            "timeliness": article["timeliness"],
                            "effectiveness": article["effectiveness"],
                            "issued_at": article["issued_at"],
                            "effective_from": article["effective_from"],
                            "effective_to": article["effective_to"],
                            "effective_at": article["effective_at"],
                        },
                        data_source=trace,
                    )
                    return LookupResult(trace.status, evidence, trace)

            law = find_law(conn, request.law_title)
            if law:
                if not request.article_no:
                    related_articles = retrieve_relevant_articles(
                        request.context_text,
                        list_current_articles(conn, request.law_title),
                    )
                    if related_articles:
                        trace.status = LookupStatus.RELEVANT_ARTICLES_FOUND
                        trace.message = "文书未注明条号，已从本地全文召回相关条款"
                        evidence = ArticleEvidence(
                            law_title=law["title"],
                            source_type=law["source_type"],
                            article_text="\n\n".join(
                                f"{item.article_no}　{item.article_text}"
                                for item in related_articles
                            ),
                            version_status=law["status"],
                            source_metadata={
                                "authority": law["authority"],
                                "category": law["category"],
                                "retrieval_method": "local_article_lexical_retrieval",
                            },
                            related_articles=related_articles,
                            data_source=trace,
                        )
                        return LookupResult(trace.status, evidence, trace)
                trace.status = (
                    LookupStatus.LAW_FOUND_ARTICLE_MISSING
                    if request.article_no
                    else LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE
                )
                local_article_count = len(
                    list_current_articles(conn, request.law_title)
                )
                trace.metadata = {"local_article_count": local_article_count}
                trace.message = (
                    "本地库已收录该法规，但无可用条文全文"
                    if not request.article_no
                    else f"本地库已收录该法规，但未找到{request.article_no}"
                )
                evidence = ArticleEvidence(
                    law_title=law["title"],
                    source_type=law["source_type"],
                    article_no=request.article_no,
                    article_text=None,
                    version_status=law["status"],
                    source_metadata={
                        "authority": law["authority"],
                        "category": law["category"],
                    },
                    data_source=trace,
                )
                return LookupResult(trace.status, evidence, trace)

        trace.message = "本地法规库未收录该法规"
        return LookupResult(trace.status, None, trace)


class PkulawFallbackSource:
    def __init__(self, client: Optional[PkulawMcpClient] = None):
        self.client = client

    def lookup(self, request: LookupRequest) -> LookupResult:
        if request.article_no:
            return self._lookup_article(request)
        return self._lookup_law_list(request, fulltext="")

    def _lookup_article(self, request: LookupRequest) -> LookupResult:
        trace = SourceTrace(
            tier=SourceTier.PKULAW_FALLBACK,
            source_name="北大法宝 MCP：精准查找法条-关键词",
            status=LookupStatus.LAW_NOT_FOUND,
        )
        try:
            article = self._client().get_law_item_content(request.law_title, request.article_no or "")
        except PkulawNotFoundError:
            # 精准查条未命中：再查法规列表，区分"法规存在但无此条"与"法规不存在"
            return self._lookup_after_article_miss(request, trace)
        except PkulawMcpError as exc:
            trace.status = _pkulaw_error_status(exc)
            trace.message = str(exc)
            return LookupResult(trace.status, None, trace)

        # fatiao 的标题匹配非常宽松（查《合同法》可能返回《民法典》条文），
        # 返回法规与请求法规不同名时不得作为证据，转入法规列表精确匹配流程
        if _match_law_record(request.law_title, [article]) is None:
            trace.message = f"精准查条返回了不同名法规《{article.title}》，已忽略"
            return self._lookup_after_article_miss(request, trace)

        trace.status = LookupStatus.ARTICLE_FOUND
        trace.source_url = article.url
        trace.metadata = _article_metadata(article)
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

    def _lookup_after_article_miss(
        self, request: LookupRequest, trace: SourceTrace
    ) -> LookupResult:
        try:
            records = self._client().get_law_list(title=request.law_title)
        except PkulawNotFoundError:
            records = []
        except PkulawMcpError as exc:
            trace.status = _pkulaw_error_status(exc)
            trace.message = f"精准查条未命中，法规列表查询失败：{exc}"
            return LookupResult(trace.status, None, trace)

        matched = _match_law_record(request.law_title, records)
        if matched is None:
            trace.status = LookupStatus.LAW_NOT_FOUND
            trace.message = "北大法宝检索完成，未找到该法规"
            trace.metadata = {
                "search_completed": True,
                # 法宝模糊检索返回的相近标题，供法名纠错建议使用
                "candidate_titles": [record.title for record in records[:5]],
            }
            return LookupResult(trace.status, None, trace)

        trace.status = LookupStatus.LAW_FOUND_ARTICLE_MISSING
        trace.message = f"北大法宝已收录该法规，但未返回{request.article_no}"
        trace.source_url = matched.url
        trace.metadata = {
            "search_completed": True,
            **_law_record_metadata(matched),
        }
        evidence = ArticleEvidence(
            law_title=matched.title,
            source_type=request.source_type,
            article_no=request.article_no,
            article_text=None,
            version_label=_first(matched.timeliness),
            version_status=_first(matched.timeliness),
            effective_from=matched.implement_date,
            source_metadata=_law_record_metadata(matched),
            data_source=trace,
        )
        return LookupResult(trace.status, evidence, trace)

    def _lookup_law_list(self, request: LookupRequest, fulltext: str) -> LookupResult:
        trace = SourceTrace(
            tier=SourceTier.PKULAW_FALLBACK,
            source_name="北大法宝 MCP：检索法律法规-关键词",
            status=LookupStatus.LAW_NOT_FOUND,
        )
        try:
            records = self._client().get_law_list(title=request.law_title, fulltext=fulltext)
        except PkulawNotFoundError as exc:
            trace.message = str(exc)
            trace.metadata = {"search_completed": True}
            return LookupResult(trace.status, None, trace)
        except PkulawMcpError as exc:
            trace.status = _pkulaw_error_status(exc)
            trace.message = str(exc)
            return LookupResult(trace.status, None, trace)

        if not records:
            trace.message = "No Pkulaw candidate law found"
            trace.metadata = {"search_completed": True}
            return LookupResult(trace.status, None, trace)

        top = records[0]
        trace.status = LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE
        trace.message = (
            "北大法宝已检索到该法规，但当前 MCP 工具在无条号时"
            "只返回法规与时效元数据，不返回匹配条号和条文全文"
        )
        trace.source_url = top.url
        trace.metadata = {
            "candidate_count": len(records),
            "candidates": [_law_record_metadata(record) for record in records],
        }
        evidence = ArticleEvidence(
            law_title=top.title,
            source_type=request.source_type,
            article_no=request.article_no,
            article_text=None,
            version_label=_first(top.timeliness),
            version_status=_first(top.timeliness),
            effective_from=top.implement_date,
            source_metadata=_law_record_metadata(top),
            data_source=trace,
        )
        return LookupResult(trace.status, evidence, trace)

    def _client(self):
        if self.client is None:
            client = PkulawMcpClient()
            if cache_enabled():
                # 法条/法规元数据查询默认走 SQLite 缓存（案号识别不缓存）
                client = CachedPkulawClient(client)
            self.client = client
        return self.client


def _pkulaw_error_status(exc: PkulawMcpError) -> LookupStatus:
    if "PKULAW_ACCESS_TOKEN" in str(exc):
        return LookupStatus.SOURCE_NOT_CONFIGURED
    return LookupStatus.SOURCE_ERROR


def _match_law_record(
    law_title: str, records: list[PkulawLawRecord]
) -> Optional[PkulawLawRecord]:
    """从法宝模糊列表中挑出与文书法名真正同名的法规。

    法宝的标题检索会连带返回相关司法解释等，只有归一化后
    标题一致（或仅差"中华人民共和国"前缀/版本注记）才算命中。
    """
    target = strip_version_annotation(normalize_title(law_title))
    target_full = (
        target if target.startswith("中华人民共和国") else f"中华人民共和国{target}"
    )
    for record in records:
        candidate = strip_version_annotation(normalize_title(record.title))
        if candidate in (target, target_full):
            return record
    # 部门规范性文件常以《关于印发〈XXX〉的通知》为载体发布，本身没有独立同名条目；
    # 标题内嵌目标法名且为印发/发布类通知的，视为该文件存在的证据。
    for record in records:
        title = record.title or ""
        if f"《{target}》" in title and ("印发" in title or "发布" in title):
            return record
    return None


def _article_metadata(article: PkulawArticle) -> dict:
    return {
        **_law_record_metadata(article),
        "article_no": article.article_no,
    }


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
