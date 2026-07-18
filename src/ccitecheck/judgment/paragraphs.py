"""法条正文的款级切分与定位工具。

条文正文按自然段存储，每款一段；款内的「（一）（二）…」项列举行
不构成独立的款，切分时并回所属款。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..domain.legal_numbers import chinese_number_to_int

_PARAGRAPH_NO_PATTERN = re.compile(r"第([一二三四五六七八九十百千零两\d]+)款")
_ITEM_LINE_PATTERN = re.compile(r"^[（(][一二三四五六七八九十\d]+[）)]")


@dataclass(frozen=True)
class ParagraphLocation:
    """一次款级定位的结果。"""

    number: int
    text: str | None
    total: int


def split_paragraphs(article_text: str) -> list[str]:
    """按自然段切分条文正文为款；项列举行并入上一款。"""
    segments: list[str] = []
    for line in article_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if segments and _ITEM_LINE_PATTERN.match(stripped):
            segments[-1] = f"{segments[-1]}\n{stripped}"
        else:
            segments.append(stripped)
    return segments


def resolve_paragraph(
    paragraph_field: str, article_text: str
) -> ParagraphLocation | None:
    """把「第X款」定位到条文中的对应自然段；款号越界时 text 为 None。"""
    match = _PARAGRAPH_NO_PATTERN.search(paragraph_field)
    if not match:
        return None
    number = chinese_number_to_int(match.group(1))
    if not number or number < 1:
        return None
    segments = split_paragraphs(article_text)
    if not segments:
        return None
    text = segments[number - 1] if number <= len(segments) else None
    return ParagraphLocation(number=number, text=text, total=len(segments))


__all__ = ["ParagraphLocation", "resolve_paragraph", "split_paragraphs"]
