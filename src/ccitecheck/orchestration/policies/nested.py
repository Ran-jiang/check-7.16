"""Scheduler 的 nested parent dependency 策略。"""

from __future__ import annotations

from typing import Protocol

from ...domain.checks import CheckVerdict, ExecutionStatus
from ...domain.statute_results import StatuteFinding, StatuteMeaningCheck
from ...verification.service import skipped_semantic_result


class NestedDependencyItem(Protocol):
    parent_index: int | None
    relation_status: str | None
    relation_message: str


def finalize_nested_dependencies(
    items: list[NestedDependencyItem],
    judgments: dict[int, tuple[list[StatuteFinding], StatuteMeaningCheck | None]],
) -> None:
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


__all__ = ["finalize_nested_dependencies"]
