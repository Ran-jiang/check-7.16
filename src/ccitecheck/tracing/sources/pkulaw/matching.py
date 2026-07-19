"""北大法宝法规候选的确定性同名匹配。"""

from __future__ import annotations

import re
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

# 法宝用纯年份后缀区分同名法的不同版本（如"…国家安全法(2015)"），
# strip_version_annotation 只认"修正/修订/修改"，此处补齐纯年份形态。
_BARE_VERSION = re.compile(r"[（(](?:\d{4}年?(?:修正|修订|修改)?|修正|修订|修改)[）)]$")
_CURRENT_MARKER = "现行有效"
_REPEALED_MARKERS = ("废止", "失效")


def _base_title(title: str) -> str:
    return _BARE_VERSION.sub("", normalize_title(title or ""))


def _is_current(record: TitledRecord) -> bool:
    values = getattr(record, "timeliness", None) or []
    return any(_CURRENT_MARKER in value for value in values) and not any(
        marker in value for value in values for marker in _REPEALED_MARKERS
    )


def match_law_record(law_title: str, records: list[RecordT]) -> RecordT | None:
    """匹配同名法规及作为规范性文件发布载体的印发/发布通知。"""
    # 同一法名对应多个版本（旧版废止、新版现行）时，裸名引用指向现行版本，
    # 优先返回唯一的现行有效版本，避免把重新制定后仍在施行的法误判为废止。
    base_target = _base_title(law_title)
    base_target_full = (
        base_target if base_target.startswith("中华人民共和国")
        else f"中华人民共和国{base_target}"
    )
    same_base = [
        record for record in records
        if _base_title(record.title) in (base_target, base_target_full)
    ]
    if len(same_base) > 1:
        current = [record for record in same_base if _is_current(record)]
        if len(current) == 1:
            return current[0]

    exact_target = _normalized_exact_title(law_title)
    exact_matches = [
        record for record in records
        if _normalized_exact_title(record.title) == exact_target
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]

    target = _normalized_title(law_title)
    target_full = (
        target if target.startswith("中华人民共和国") else f"中华人民共和国{target}"
    )
    matches: list[RecordT] = []
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
            matches.append(record)
    if len(matches) == 1:
        return matches[0]
    if matches:
        return None
    # 部门规范性文件可能只存在于印发/发布通知中，没有独立同名条目。
    # 仅接受书名号内嵌完整目标法名的发布载体。
    for record in records:
        title = record.title or ""
        if f"《{target}》" in title and ("印发" in title or "发布" in title):
            return record
    return None


def _normalized_title(title: str) -> str:
    # 先剥"修正/修订"注记，再剥纯年份版本后缀（法宝用"(2015)"区分同名版本），
    # 使裸名引用能匹配到带版本后缀的条文记录。
    return _BARE_VERSION.sub("", strip_version_annotation(normalize_title(title)))


def _normalized_exact_title(title: str) -> str:
    return normalize_title(title).translate(str.maketrans({"（": "(", "）": ")"}))


__all__ = ["match_law_record"]
