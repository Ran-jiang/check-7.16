"""
第一层数据源：本地 SQLite 法条库。

本地库被视为权威且完整：法规命中而条号不存在时给出
ARTICLE_NOT_FOUND（引注错误信号），路由器不再下探外部源。
"""

from __future__ import annotations

from typing import Optional

from statutedb.cn_num import (
    parse_article_label,
    parse_item_label,
    parse_paragraph_label,
)
from statutedb.store import StatuteStore

from .schema import (
    ProvisionEvidence,
    ProvisionQuery,
    ProvisionResult,
    RetrievalStatus,
    SuggestedArticle,
)

PROVIDER_NAME = "local"

# 全文反查：仅当 bm25 得分足够可信时才用于自动定位
_FTS_LOCATE_LIMIT = 3


class LocalSource:
    """statutedb 的检索层包装。"""

    def __init__(self, store: StatuteStore):
        self.store = store

    def lookup(self, query: ProvisionQuery) -> Optional[ProvisionResult]:
        """
        本地库查询。

        Returns:
            ProvisionResult — 法规在库内（含条号错误情形）
            None — 法规不在库内，路由器继续下探
        """
        law = self.store.resolve_law(query.law_title)
        if law is None:
            return None

        # ---- 法规级引注（无条款号）----
        if not query.article_label:
            return self._law_level_result(query, law)

        # ---- 条款直取 ----
        parsed = parse_article_label(query.article_label)
        if parsed is None:
            # 条号标签本身不合法（如"第X条"漏字）：按未命中处理并给建议
            return self._article_not_found(query, law)

        article = self.store.get_article(law.law_id, parsed[0], parsed[1])
        if article is None:
            return self._article_not_found(query, law)

        evidence = ProvisionEvidence(
            provider=PROVIDER_NAME,
            law_title=law.title,
            article_label=article.article_label,
            text=article.text,
            section_path=article.section_path or None,
            source_url=law.source_url,
            law_status=law.status,
            clause_texts=self._clause_texts(article.article_id, query),
        )
        return ProvisionResult(
            query=query,
            status=RetrievalStatus.FOUND,
            evidence=evidence,
            providers_tried=[PROVIDER_NAME],
        )

    # ------------------------------------------------------------

    def _law_level_result(self, query: ProvisionQuery, law) -> ProvisionResult:
        """无条款号：确认法规存在；有转述文本时尝试全文定位条文。"""
        if query.quote_text:
            hits = self.store.search_fulltext(
                query.quote_text, law_id=law.law_id, limit=_FTS_LOCATE_LIMIT
            )
            if hits:
                best = hits[0]
                evidence = ProvisionEvidence(
                    provider=PROVIDER_NAME,
                    law_title=law.title,
                    article_label=best.article.article_label,
                    text=best.article.text,
                    section_path=best.article.section_path or None,
                    source_url=law.source_url,
                    law_status=law.status,
                    note="引注无条款号，经转述文本全文反查定位",
                )
                return ProvisionResult(
                    query=query,
                    status=RetrievalStatus.FOUND,
                    evidence=evidence,
                    suggestions=_hits_to_suggestions(hits[1:]),
                    providers_tried=[PROVIDER_NAME],
                )

        evidence = ProvisionEvidence(
            provider=PROVIDER_NAME,
            law_title=law.title,
            source_url=law.source_url,
            law_status=law.status,
            note="法规级引注，本地库确认该法规存在",
        )
        return ProvisionResult(
            query=query,
            status=RetrievalStatus.LAW_FOUND_NO_ARTICLE,
            evidence=evidence,
            providers_tried=[PROVIDER_NAME],
        )

    def _article_not_found(self, query: ProvisionQuery, law) -> ProvisionResult:
        """条号未命中：附全文反查候选，帮助下游判断正确条号。"""
        suggestions: list[SuggestedArticle] = []
        if query.quote_text:
            hits = self.store.search_fulltext(
                query.quote_text, law_id=law.law_id, limit=_FTS_LOCATE_LIMIT
            )
            suggestions = _hits_to_suggestions(hits)
        return ProvisionResult(
            query=query,
            status=RetrievalStatus.ARTICLE_NOT_FOUND,
            suggestions=suggestions,
            providers_tried=[PROVIDER_NAME],
        )

    def _clause_texts(self, article_id: int, query: ProvisionQuery) -> list[str]:
        """引注到款/项时取细粒度原文。"""
        texts: list[str] = []
        para_nums = [
            n for n in (parse_paragraph_label(p) for p in query.paragraph_labels)
            if n is not None
        ]
        item_nums = [
            n for n in (parse_item_label(i) for i in query.item_labels)
            if n is not None
        ]
        if para_nums:
            for pn in para_nums:
                for clause in self.store.get_clauses(article_id, para_num=pn):
                    # 未指定项时只取款本身；指定了项则取对应项
                    if not item_nums and clause.item_num == 0:
                        texts.append(clause.text)
                    elif item_nums and clause.item_num in item_nums:
                        texts.append(clause.text)
        elif item_nums:
            # 只有项号（款号省略，通常指第一款）
            for clause in self.store.get_clauses(article_id):
                if clause.item_num in item_nums:
                    texts.append(clause.text)
        return texts


def _hits_to_suggestions(hits) -> list[SuggestedArticle]:
    return [
        SuggestedArticle(
            law_title=h.law_title,
            article_label=h.article.article_label,
            text=h.article.text,
            score=h.score,
        )
        for h in hits
    ]
