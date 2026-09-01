"""查询形式归一化。"""

import re

from ..infrastructure.database import normalize_title

_VERSION = re.compile(r"[（(](?P<year>\d{4})\s*年?\s*(?P<kind>修正|修订)[）)]")


def normalize_identity_title(value: str) -> str:
    return normalize_title(value.strip().removeprefix("《").removesuffix("》"))


def split_version_annotation(value: str) -> tuple[str, int | None, str | None]:
    match = _VERSION.search(value)
    if not match:
        return normalize_identity_title(value), None, None
    title = _VERSION.sub("", value)
    kind = "amended" if match.group("kind") == "修正" else "revised"
    return normalize_identity_title(title), int(match.group("year")), kind


__all__ = ["normalize_identity_title", "split_version_annotation"]
