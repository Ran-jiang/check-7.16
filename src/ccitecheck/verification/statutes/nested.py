"""内部转引关系的权威证据比较。"""

from __future__ import annotations

import re
from typing import Protocol

from ...domain.citation import ArticleRef, Claim
from ...domain.evidence import SourceTrace
from ...domain.statute_results import (
    NestedReferenceMatch,
)
from ...infrastructure.database import normalize_title
from ..semantic import SemanticChecker, SemanticCheckError
from ...recognition.relations import relation_candidates


class NestedItem(Protocol):
    claim: Claim
    law_title: str
    display_title: str
    article: ArticleRef | None
    article_no: str | None
    parent_index: int | None
    relation_status: str | None
    relation_message: str
    relation_candidate_article_no: str | None
    relation_parent_authoritative_text: str
    citation_span: tuple[int, int] | None
    reference_role: str

    @property
    def lookup_key(self) -> tuple: ...


_ARTICLE_MENTION = re.compile(
    r"第[〇零一二三四五六七八九十百千万两0-9]+条"
    r"(?:之[〇零一二三四五六七八九十百千万两0-9]+)?"
)


def resolve_nested_relations(
    items: list[NestedItem],
    lookup_results: dict[tuple, tuple[object, list[SourceTrace]]],
    semantic_checker: SemanticChecker | None,
) -> None:
    """以文本结构召回候选，以主法条和 child 现行原文确认关系。"""
    candidates = relation_candidates(items)
    for child_index, parent_indices in candidates.items():
        child = items[child_index]
        unavailable_parent: int | None = None
        insufficient: tuple[int, str] | None = None
        for parent_index in parent_indices:
            parent = items[parent_index]
            parent_lookup = lookup_results.get(parent.lookup_key)
            if parent_lookup is None or parent_lookup[0].evidence is None:
                unavailable_parent = parent_index
                continue
            parent_text = parent_lookup[0].evidence.article_text or ""
            if not parent_text:
                unavailable_parent = parent_index
                continue
            child_lookup = lookup_results.get(child.lookup_key)
            child_evidence = child_lookup[0].evidence if child_lookup else None
            if not _parent_could_reference_child(parent_text, parent, child):
                continue
            if child_evidence is None or not child_evidence.article_text:
                insufficient = (parent_index, "主法条疑似存在该转引，但未取得所引条文原文")
                continue
            if semantic_checker is None:
                insufficient = (parent_index, "内部转引语义核查服务不可用")
                continue
            try:
                verdict = semantic_checker.compare_nested_reference(
                    parent_source=_reference_label(parent),
                    parent_text=parent_text,
                    child_source=_reference_label(child),
                    child_text=child_evidence.article_text,
                )
            except SemanticCheckError as exc:
                verdict = NestedReferenceMatch(verdict="insufficient", reason=str(exc))
            if verdict.verdict == "not_nested":
                continue
            if verdict.verdict == "insufficient":
                insufficient = (parent_index, verdict.reason)
                continue
            _confirm(
                child,
                parent,
                parent_index,
                parent_text,
                "confirmed" if verdict.verdict == "match" else "locator_mismatch",
                verdict.reason,
                verdict.matched_locator,
            )
            break
        else:
            if insufficient is not None:
                parent_index, reason = insufficient
                parent = items[parent_index]
                evidence = lookup_results[parent.lookup_key][0].evidence
                _confirm(
                    child, parent, parent_index,
                    evidence.article_text if evidence else "",
                    "insufficient", reason,
                )
            elif unavailable_parent is not None:
                parent = items[unavailable_parent]
                _confirm(
                    child, parent, unavailable_parent, "", "parent_unavailable",
                    "主引用原文不可核验，无法确认转引关系",
                )


def _parent_could_reference_child(
    parent_text: str, parent: NestedItem, child: NestedItem,
) -> bool:
    if not _ARTICLE_MENTION.search(parent_text):
        return False
    if normalize_title(parent.law_title) == normalize_title(child.law_title):
        return True
    normalized_parent = normalize_title(parent_text)
    variants = {
        normalize_title(child.display_title),
        normalize_title(child.law_title),
        normalize_title(child.law_title).removeprefix("中华人民共和国"),
    }
    return any(variant and variant in normalized_parent for variant in variants)


def _confirm(
    child: NestedItem,
    parent: NestedItem,
    parent_index: int,
    parent_text: str,
    status: str,
    message: str,
    candidate_article_no: str | None = None,
) -> None:
    child.parent_index = parent_index
    child.relation_status = status
    child.relation_message = message
    child.relation_candidate_article_no = candidate_article_no
    child.relation_parent_authoritative_text = parent_text
    child.reference_role = "nested"


def _reference_label(item: NestedItem) -> str:
    return f"《{item.law_title}》{item.article_no or ''}"


__all__ = ["resolve_nested_relations"]
