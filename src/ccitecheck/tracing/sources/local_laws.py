"""从本地 SQLite 数据库取得法规与条款证据的适配器。

本适配器只读取配置的本地法规库；引用未写明条号时执行确定性文本召回。
它返回证据和查询轨迹，不作通过或问题判定，也不调用远程回退服务。
"""

from __future__ import annotations

from pathlib import Path

from ...infrastructure.database import (
    connect,
    find_current_article,
    find_law,
    get_structure_path_for_article,
    list_current_articles,
)

from ...domain.evidence import ArticleEvidence, LookupStatus, SourceTier, SourceTrace
from ..retrieval import retrieve_relevant_articles
from .base import LookupRequest, LookupResult


class LocalSQLiteSource:
    """从本地数据库取得当前可用的最佳法规证据。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def lookup(self, request: LookupRequest) -> LookupResult:
        trace = SourceTrace(
            tier=SourceTier.LOCAL_SQLITE,
            source_name="CCiteheck 本地 SQLite 法规库",
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
                        structure_path=get_structure_path_for_article(
                            conn, int(article["article_id"])
                        ),
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


__all__ = ["LocalSQLiteSource"]
