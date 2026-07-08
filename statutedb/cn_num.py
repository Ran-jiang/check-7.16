"""
中文数字与条款号标签的双向转换。

设计约束：
  - 法条条号范围现实上限约五千（民法典 1260 条），支持到"万"即可
  - 引注文本中可能混用阿拉伯数字（"第52条"），必须兼容
  - "第一百八十四条之一" 解析为 (184, 1)；无"之X"后缀时 suffix=0
"""

from __future__ import annotations

import re
from typing import Optional

_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}

_CN_NUM_CHARS = "零一二两三四五六七八九十百千万0123456789"

# 条款号标签：第X条(之Y)?
ARTICLE_LABEL_PATTERN = re.compile(
    rf"^第([{_CN_NUM_CHARS}]+)条(?:之([{_CN_NUM_CHARS}]+))?$"
)


def cn_to_int(text: str) -> int:
    """
    中文数字（可混用阿拉伯数字）→ int。

    支持："一百八十四"、"一千零八十四"、"十"、"二十"、"52"、"一万"。
    不合法输入抛 ValueError。
    """
    text = text.strip()
    if not text:
        raise ValueError("empty numeral")

    # 纯阿拉伯数字直接转
    if text.isdigit():
        return int(text)

    result = 0      # 已结算的万段之前部分
    section = 0     # 当前万段内已结算部分
    current = 0     # 当前待乘单位的数字
    for ch in text:
        if ch in _DIGITS:
            current = _DIGITS[ch]
        elif ch.isdigit():
            current = current * 10 + int(ch)
        elif ch in _UNITS:
            unit = _UNITS[ch]
            if unit == 10000:
                section = (section + (current or 0)) or 1
                result += section * unit
                section = 0
                current = 0
            else:
                # "十" 开头（如"十五"）视为 1×10
                if current == 0:
                    current = 1
                section += current * unit
                current = 0
        else:
            raise ValueError(f"invalid numeral char {ch!r} in {text!r}")
    return result + section + current


def int_to_cn(num: int) -> str:
    """
    int → 中文数字（法条标准写法）。

    与官方条文写法一致：184 → "一百八十四"，1084 → "一千零八十四"，
    10 → "十"，25 → "二十五"，110 → "一百一十"。
    """
    if num <= 0:
        raise ValueError(f"expect positive int, got {num}")
    if num >= 100000:
        raise ValueError(f"number too large for article label: {num}")

    digits_cn = "零一二三四五六七八九"
    units = ["", "十", "百", "千", "万"]

    digits = []
    n = num
    while n > 0:
        digits.append(n % 10)
        n //= 10
    # digits[i] 为 10^i 位

    parts: list[str] = []
    zero_pending = False
    for i in range(len(digits) - 1, -1, -1):
        d = digits[i]
        if d == 0:
            if parts:
                zero_pending = True
            continue
        if zero_pending:
            parts.append("零")
            zero_pending = False
        parts.append(digits_cn[d] + units[i])
    text = "".join(parts)

    # "一十X" → "十X"（10-19 的标准写法）
    if text.startswith("一十"):
        text = text[1:]
    return text


def parse_article_label(label: str) -> Optional[tuple[int, int]]:
    """
    条款号标签 → (article_num, suffix)。

    "第一百八十四条" → (184, 0)
    "第一百八十四条之一" → (184, 1)
    "第52条" → (52, 0)
    不匹配返回 None。
    """
    m = ARTICLE_LABEL_PATTERN.match(label.strip())
    if not m:
        return None
    try:
        num = cn_to_int(m.group(1))
        suffix = cn_to_int(m.group(2)) if m.group(2) else 0
    except ValueError:
        return None
    if num <= 0:
        return None
    return (num, suffix)


def compose_article_label(num: int, suffix: int = 0) -> str:
    """(184, 1) → "第一百八十四条之一"。"""
    label = f"第{int_to_cn(num)}条"
    if suffix > 0:
        label += f"之{int_to_cn(suffix)}"
    return label


# 款/项标签解析（"第二款" → 2；"第（三）项"/"第(三)项"/"第三项" → 3）
_PARAGRAPH_LABEL_PATTERN = re.compile(rf"^第([{_CN_NUM_CHARS}]+)款$")
_ITEM_LABEL_PATTERN = re.compile(rf"^第[（(]?([{_CN_NUM_CHARS}]+)[）)]?项$")


def parse_paragraph_label(label: str) -> Optional[int]:
    """"第二款" → 2；不匹配返回 None。"""
    m = _PARAGRAPH_LABEL_PATTERN.match(label.strip())
    if not m:
        return None
    try:
        return cn_to_int(m.group(1))
    except ValueError:
        return None


def parse_item_label(label: str) -> Optional[int]:
    """"第（三）项" / "第三项" → 3；不匹配返回 None。"""
    m = _ITEM_LABEL_PATTERN.match(label.strip())
    if not m:
        return None
    try:
        return cn_to_int(m.group(1))
    except ValueError:
        return None
