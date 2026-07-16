"""对本地已有条款执行确定性文本召回。"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping

from ..domain.evidence import ArticleExcerpt

BOOK_TITLE_RE = re.compile(r"《[^》]+》")
ALNUM_RE = re.compile(r"[\u3400-\u9fffA-Za-z0-9]")


def retrieve_relevant_articles(
    context_text: str,
    articles: Iterable[Mapping],
    limit: int = 3,
) -> list[ArticleExcerpt]:
    rows = list(articles)
    query = _normalize(BOOK_TITLE_RE.sub("", context_text))
    query_terms = _ngrams(query)
    if not query_terms or not rows:
        return []

    article_terms = [_ngrams(_normalize(row["text"])) for row in rows]
    document_frequency = Counter(
        term for terms in article_terms for term in set(terms)
    )
    article_count = len(rows)
    ranked = []
    for row, terms in zip(rows, article_terms):
        shared = query_terms.intersection(terms)
        if not shared:
            continue
        score = sum(
            math.log((article_count + 1) / (document_frequency[term] + 1)) + 1
            for term in shared
        )
        score *= len(shared) / len(query_terms)
        ranked.append((score, row))

    ranked.sort(key=lambda item: (-item[0], item[1]["article_key"]))
    return [
        ArticleExcerpt(
            article_no=row["article_no"],
            article_text=row["text"],
            relevance_score=round(score, 6),
        )
        for score, row in ranked[:limit]
        if score > 0
    ]


def _normalize(text: str) -> str:
    return "".join(ALNUM_RE.findall(text)).lower()


def _ngrams(text: str) -> set[str]:
    return {
        text[index:index + size]
        for size in (2, 3)
        for index in range(len(text) - size + 1)
    }
