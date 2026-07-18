"""把 MCP 候选条文解析为可验证的条、款、项定位候选。"""

from __future__ import annotations

import re

from ...domain.evidence import ArticleEvidence
from ...domain.statute_results import (
    StatuteLocationCandidate,
    StatuteLocationResolution,
    StatuteLocator,
)
from .structure import parse_article_structure

_NON_TEXT = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]")


def resolve_location_candidates(
    document_quote: str,
    evidence: list[ArticleEvidence],
) -> StatuteLocationResolution:
    """仅以完整文本包含关系确认候选；不进行模糊分数推断。"""
    normalized_quote = _normalize(document_quote)
    candidates: list[StatuteLocationCandidate] = []
    for article in evidence:
        if not article.article_no or not article.article_text:
            continue
        structure = parse_article_structure(article.article_no, article.article_text)
        if structure is None:
            continue
        for paragraph in structure.paragraphs:
            if paragraph.items:
                for item in paragraph.items:
                    if _contained(item.text, normalized_quote):
                        candidates.append(StatuteLocationCandidate(
                            locator=StatuteLocator(
                                article_no=article.article_no,
                                paragraph_no=paragraph.paragraph_no,
                                item_no=item.item_no,
                            ),
                            text=item.text,
                            source_url=article.data_source.source_url,
                        ))
            elif _contained(paragraph.text, normalized_quote):
                candidates.append(StatuteLocationCandidate(
                    locator=StatuteLocator(
                        article_no=article.article_no,
                        paragraph_no=paragraph.paragraph_no,
                    ),
                    text=paragraph.text,
                    source_url=article.data_source.source_url,
                ))
    unique = _unique_candidates(candidates)
    if len(unique) == 1:
        return StatuteLocationResolution(status="resolved", candidates=unique)
    if unique:
        return StatuteLocationResolution(status="candidates_pending", candidates=unique)
    return StatuteLocationResolution(status="not_found")


def _contained(candidate_text: str, normalized_quote: str) -> bool:
    normalized_candidate = _normalize(candidate_text)
    return bool(normalized_candidate and normalized_candidate in normalized_quote)


def _normalize(value: str) -> str:
    return _NON_TEXT.sub("", value)


def _unique_candidates(
    candidates: list[StatuteLocationCandidate],
) -> list[StatuteLocationCandidate]:
    result: list[StatuteLocationCandidate] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for candidate in candidates:
        key = (
            candidate.locator.article_no,
            candidate.locator.paragraph_no,
            candidate.locator.item_no,
        )
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


__all__ = ["resolve_location_candidates"]
