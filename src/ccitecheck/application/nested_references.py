"""内部转引关系的候选生成、权威确认与父子结果依赖。"""

from __future__ import annotations

import re
from typing import Protocol

from ..domain.checks import CheckVerdict, ExecutionStatus
from ..domain.citation import ArticleRef, Claim
from ..domain.evidence import SourceTrace
from ..domain.statute_results import (
    NestedReferenceMatch,
    StatuteFinding,
    StatuteMeaningCheck,
)
from ..infrastructure.database import normalize_title
from ..judgment.semantic import SemanticChecker, SemanticCheckError
from ..judgment.service import skipped_semantic_result
from ..tracing.sources import LookupResult


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
    nested_context: str

    @property
    def lookup_key(self) -> tuple: ...


_PARENT_REPORTING = re.compile(
    r"^\s*(?:的|之)?(?:明确)?(?:规定|指出|载明|所称|明确)"
)
_ARTICLE_MENTION = re.compile(
    r"第[〇零一二三四五六七八九十百千万两0-9]+条"
    r"(?:之[〇零一二三四五六七八九十百千万两0-9]+)?"
)


def resolve_nested_relations(
    items: list[NestedItem],
    lookup_results: dict[tuple, tuple[LookupResult, list[SourceTrace]]],
    semantic_checker: SemanticChecker | None,
) -> None:
    """以文本结构召回候选，以主法条和 child 现行原文确认关系。"""
    candidates = _relation_candidates(items)
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


def finalize_nested_dependencies(
    items: list[NestedItem],
    judgments: dict[int, tuple[list[StatuteFinding], StatuteMeaningCheck | None]],
) -> None:
    """主引用未通过时，内部转引随主引用处理，不作独立结论。"""
    for index, item in enumerate(items):
        if item.parent_index is None or item.relation_status not in {"confirmed", "resolved"}:
            continue
        parent_findings, parent_meaning = judgments[item.parent_index]
        if _judgment_passed(parent_findings, parent_meaning):
            continue
        item.relation_status = "parent_failed"
        item.relation_message = (
            "该条属于主法条中的内部转引；主引用当前未通过或未完成核验，"
            "本条随主引用处理，不单独作出通过或未通过结论。"
        )
        judgments[index] = ([], skipped_semantic_result("nested_parent_not_passed"))


def _relation_candidates(items: list[NestedItem]) -> dict[int, list[int]]:
    result: dict[int, list[int]] = {}
    by_claim: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        if item.article is not None and item.article.citation_span is not None:
            by_claim.setdefault(item.claim.claim_id, []).append(index)
    for indices in by_claim.values():
        indices.sort(key=lambda index: items[index].article.citation_span[0])
        for position, child_index in enumerate(indices[1:], start=1):
            child = items[child_index]
            child_start = child.article.citation_span[0]
            parents: list[int] = []
            for parent_index in reversed(indices[:position]):
                parent = items[parent_index]
                parent_end = parent.article.citation_span[1]
                bridge = child.claim.text[parent_end:child_start]
                if "\n" in bridge or len(bridge) > 1200:
                    break
                if _bridge_stays_in_parent_scope(bridge):
                    parents.append(parent_index)
            if parents:
                result[child_index] = parents
    return result


def _bridge_stays_in_parent_scope(bridge: str) -> bool:
    reporting = _PARENT_REPORTING.match(bridge)
    if reporting is None:
        return False
    governed = bridge[reporting.end():]
    last_open = max(governed.rfind(mark) for mark in "“‘")
    last_close = max(governed.rfind(mark) for mark in "”’")
    if last_open >= 0:
        return last_open > last_close
    return re.search(r"[。！？]", governed) is None


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
    child.nested_context = parent_text
    if child.article is not None:
        child.article.reference_role = "nested"
        child.article.parent_reference_id = (parent.law_title, parent.article_no or "")
        child.article.quote_span = None


def _reference_label(item: NestedItem) -> str:
    return f"《{item.law_title}》{item.article_no or ''}"


def _judgment_passed(
    findings: list[StatuteFinding], meaning: StatuteMeaningCheck | None,
) -> bool:
    if findings:
        return False
    if meaning is None:
        return True
    return (
        meaning.execution_status == ExecutionStatus.COMPLETED
        and meaning.verdict == CheckVerdict.PASS
    ) or meaning.skipped_reason == "nested_reference"


__all__ = ["finalize_nested_dependencies", "resolve_nested_relations"]
