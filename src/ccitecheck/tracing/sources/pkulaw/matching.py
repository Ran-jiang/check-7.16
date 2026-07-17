"""北大法宝法规候选的确定性同名匹配。"""

from __future__ import annotations

from typing import Protocol, TypeVar

from ....infrastructure.database import normalize_title, strip_version_annotation


class TitledRecord(Protocol):
    title: str


RecordT = TypeVar("RecordT", bound=TitledRecord)

_ISSUING_AUTHORITY_PREFIXES = (
    "最高人民法院、最高人民检察院",
    "最高人民法院最高人民检察院",
    "最高人民法院",
    "最高人民检察院",
)


def match_law_record(law_title: str, records: list[RecordT]) -> RecordT | None:
    """匹配同名法规及作为规范性文件发布载体的印发/发布通知。"""
    target = _normalized_title(law_title)
    target_full = (
        target if target.startswith("中华人民共和国") else f"中华人民共和国{target}"
    )
    for record in records:
        candidate = _normalized_title(record.title)
        candidate_without_issuer = next(
            (
                candidate.removeprefix(prefix)
                for prefix in _ISSUING_AUTHORITY_PREFIXES
                if candidate.startswith(prefix)
            ),
            candidate,
        )
        if candidate in (target, target_full) or candidate_without_issuer in (
            target,
            target_full,
        ):
            return record
    # 部门规范性文件可能只存在于印发/发布通知中，没有独立同名条目。
    # 仅接受书名号内嵌完整目标法名的发布载体。
    for record in records:
        title = record.title or ""
        if f"《{target}》" in title and ("印发" in title or "发布" in title):
            return record
    return None


def _normalized_title(title: str) -> str:
    return strip_version_annotation(normalize_title(title))


__all__ = ["match_law_record"]
