"""把权威条文文本解析为条、款、项结构。"""

from __future__ import annotations

import re

from ...domain.statute_results import StructuredArticle, StructuredItem, StructuredParagraph
from ...domain.legal_numbers import chinese_number_to_int

_ARTICLE_HEADING = re.compile(
    r"^\s*第[〇零一二三四五六七八九十百千万两0-9]+条"
    r"(?:之[〇零一二三四五六七八九十百千万两0-9]+)?[\s　]*"
)
_ITEM_MARKER = re.compile(r"（([〇零一二三四五六七八九十百千万两0-9]+)）")
_CHINESE_DIGITS = "零一二三四五六七八九十百千万"


def parse_article_structure(article_no: str, article_text: str) -> StructuredArticle | None:
    """解析保留自然段边界的条文；空文本或孤立项结构返回 None。"""
    body = _ARTICLE_HEADING.sub("", article_text, count=1).strip()
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        return None

    paragraph_lines: list[list[str]] = []
    for line in lines:
        if _ITEM_MARKER.match(line):
            if not paragraph_lines:
                return None
            paragraph_lines[-1].append(line)
        else:
            paragraph_lines.append([line])

    paragraphs = [
        StructuredParagraph(
            paragraph_no=f"第{_integer_to_chinese(index)}款",
            text="\n".join(parts),
            items=_parse_items("\n".join(parts)),
        )
        for index, parts in enumerate(paragraph_lines, start=1)
    ]
    return StructuredArticle(
        article_no=article_no,
        raw_text=article_text,
        paragraph_boundaries_reliable=len(lines) > 1,
        paragraphs=paragraphs,
    )


def locator_ordinal(value: str, suffix: str) -> int | None:
    match = re.fullmatch(
        rf"第?([〇零一二三四五六七八九十百千万两0-9]+){suffix}", value.strip()
    )
    if not match:
        return None
    token = match.group(1)
    try:
        return int(token) if token.isdigit() else chinese_number_to_int(token)
    except ValueError:
        return None


def _parse_items(text: str) -> list[StructuredItem]:
    matches = list(_ITEM_MARKER.finditer(text))
    return [
        StructuredItem(
            item_no=f"第{_integer_to_chinese(_number(match.group(1)))}项",
            text=text[match.start():matches[index + 1].start()].strip()
            if index + 1 < len(matches)
            else text[match.start():].strip(),
        )
        for index, match in enumerate(matches)
    ]


def _number(value: str) -> int:
    return int(value) if value.isdigit() else chinese_number_to_int(value)


def _integer_to_chinese(value: int) -> str:
    if value <= 10:
        return _CHINESE_DIGITS[value]
    if value < 20:
        return "十" + (_CHINESE_DIGITS[value % 10] if value % 10 else "")
    if value < 100:
        return _CHINESE_DIGITS[value // 10] + "十" + (
            _CHINESE_DIGITS[value % 10] if value % 10 else ""
        )
    return str(value)


__all__ = ["locator_ordinal", "parse_article_structure"]
