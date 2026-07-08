"""
法规名规范化与别名 key 生成。

引注中的法规名写法不统一：全称/简称、含版本注记、全半角混用。
本地库用"规范化别名精确匹配"而非模糊搜索来解析法规名——
匹配是 O(1) 且零误报，代价是别名要在导入时枚举（自动 + 人工补充）。
"""

from __future__ import annotations

import re
import unicodedata

# 末尾括号注记：（2020年修正）（2023修订）（试行）等
_PARENTHETICAL_PATTERN = re.compile(r"[（(][^）)]*[）)]$")

_PRC_PREFIX = "中华人民共和国"


def normalize_title(title: str) -> str:
    """
    法规名规范化：NFKC 归一 → 去空白 → 去首尾书名号。

    不剥离版本注记（那是别名变体的职责），保证规范化是无损的。
    """
    text = unicodedata.normalize("NFKC", title)
    text = re.sub(r"\s+", "", text)
    text = text.strip("《》〈〉")
    # 司法解释名称内嵌的法名书名号有《》/〈〉两种写法
    # （官方文本用《》，引注嵌套时规范写法是〈〉），统一为《》
    text = text.replace("〈", "《").replace("〉", "》")
    return text


def strip_parenthetical(title: str) -> str:
    """剥离末尾括号注记："反不正当竞争法（2019年修正）" → "反不正当竞争法"。"""
    return _PARENTHETICAL_PATTERN.sub("", title).strip()


def alias_variants(title: str) -> list[str]:
    """
    从法规全称生成规范化别名变体（导入时调用）。

    变体：原名、去括号注记、去"中华人民共和国"前缀、两者叠加。
    司法解释的通用简称（如"民法典总则编解释"）无法可靠自动生成，
    由导入时 --alias 人工补充。
    """
    base = normalize_title(title)
    variants = {base}
    stripped = strip_parenthetical(base)
    variants.add(stripped)
    for v in list(variants):
        if v.startswith(_PRC_PREFIX):
            variants.add(v[len(_PRC_PREFIX):])
    return sorted(v for v in variants if v)


def query_variants(title: str) -> list[str]:
    """
    从引注中的法规名生成查询变体（检索时调用），按优先级排列。

    与 alias_variants 对称：先查原名，再查去注记/去前缀变体。
    """
    base = normalize_title(title)
    ordered: list[str] = [base]
    stripped = strip_parenthetical(base)
    if stripped != base:
        ordered.append(stripped)
    for v in list(ordered):
        if v.startswith(_PRC_PREFIX):
            candidate = v[len(_PRC_PREFIX):]
            if candidate not in ordered:
                ordered.append(candidate)
    return [v for v in ordered if v]
