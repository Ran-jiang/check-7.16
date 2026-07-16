"""
CCiteheck 案号与案例线索识别。

负责：
  1. 识别有案号的案例引用（with_case_number）
  2. 识别无案号的案例线索（without_case_number）——白名单模式，宁漏勿错
  3. 从上下文提取法院名称
  4. 检测观点触发词（用于 case_holding_paraphrase 判定）

设计决策：
  - 有案号：精确匹配 [（(〔] + 年份 + 法院简称 + 案件类型字 + 号
  - 无案号：仅识别白名单中的可检索线索（指导案例、公报案例、典型案例、命名案）
  - 显式排除指代词：本案、该案、此案、上述案件、前案、原案
  - 没有明确 case_ref 时绝不因"法院认为""本院认为"而抽取 case_holding_paraphrase
"""

from __future__ import annotations

import re
from typing import Optional

from ..domain.citation import CaseRef, CaseReferenceType


# ============================================================
# 案号正则
# ============================================================

# 案号模式：
# 括号（全角/半角/方括号）+ 年份（1或2开头的4位数字）+ 法院简称 + 案件类型字 + 数字 + 号
# 示例：（2021）最高法民申1234号 / (2021)京73民终123号 / 〔2019〕粤民再56号
# 注意：括号形态有3种——全角（）、半角()、方头括号〔〕
CASE_NUMBER_PATTERN = re.compile(
    r"[（(〔]"
    r"([12]\d{3})"           # 年份（1xxx 或 2xxx）
    r"[）)〕]"
    r"[^\s，。；：、（）()〔〕]{0,12}?"  # 有界法院代码（旧案号可省略）
    r"[民刑行知赔执破清海商]"             # 案件类型字
    r"[^\s，。；：、（）()〔〕]{0,6}?"   # 初/终/再/申等附加分类
    r"(?:字第)?\d+"                         # 新旧案号数字格式
    r"号"
)

# 无案号线索 — 白名单模式

# 指导案例：指导案例第X号 或 指导性案例第X号
GUIDING_CASE_PATTERN = re.compile(
    r"指导(?:性)?案例(?:第)?[一二三四五六七八九十百\d]+号"
)

# 公报案例 / 典型案例
GAZETTE_TYPICAL_PATTERN = re.compile(
    r"(公报案例|典型案例)"
)

# 命名案模式：X诉Y……案 / X诉Y……纠纷案
# "A诉B……案"结构，A和B至少各2个字符（人名/公司名）
# 约束：
#   1. 当事人名限长 2-10 字（原 \S{2,}? 无上限，会把
#      "投诉情况……做出预案"这类整句误判为案名）
#   2. "诉"前不能是被/投/申/上/起/控（排除被诉、投诉、申诉、上诉、起诉、控诉）
#   3. "诉"后不能是讼/称/求/请（排除诉讼、诉称、诉求、诉请）
#   4. "诉"到"案"之间最多12字，且不得跨越句读（。！？；，、）
#   5. "案"前不能是预/方/草/议/答/备/提/立/档（排除预案、方案、草案等）
NAMED_CASE_PATTERN = re.compile(
    r"[^。！？；，\n]{2,60}?(?<![被投申上起控])诉(?![讼称求请]|行为)"
    r"[^。！？；，\n]{1,60}?(?:纠纷)?(?<![预方草议答备提立档])案[）)]?"
)

# 指代词（指向当前文书内部，不可外部检索）— 显式排除
PRONOUN_PATTERNS = [
    re.compile(r"本案"),
    re.compile(r"该案"),
    re.compile(r"此案"),
    re.compile(r"上述案件"),
    re.compile(r"前案"),
    re.compile(r"原案"),
]

# 观点触发词（用于 case_holding_paraphrase 判定）
HOLDING_TRIGGER_PATTERN = re.compile(
    r"(裁判要旨|裁判规则|裁判认为|法院认为|认为|指出|明确)"
)


# ============================================================
# 法院名提取
# ============================================================

# 常见法院名关键词
COURT_KEYWORDS = [
    "最高人民法院", "最高人民检察院",
    "高级人民法院", "中级人民法院", "基层人民法院",
    "知识产权法院", "互联网法院", "金融法院", "海事法院",
    "军事法院", "铁路运输法院", "铁路运输中级法院",
]


def extract_court_from_context(text: str) -> Optional[str]:
    """
    从文本中提取法院名称。

    搜索常见法院名关键词，返回找到的名称。
    如果有多个法院名，返回距离案号最近的一个。

    Args:
        text: 完整文本

    Returns:
        法院名称或 None
    """
    best_court = None
    for keyword in COURT_KEYWORDS:
        if keyword in text:
            # 尝试向左右扩展获取完整法院名
            pos = text.find(keyword)
            # 向前查找省市区前缀
            prefix_end = pos
            prefix_start = max(0, pos - 10)
            prefix = text[prefix_start:prefix_end]
            # 简单匹配：省/市/自治区名
            province_match = re.search(
                r"([一-鿿]{2,4}(?:省|市|自治区|特别行政区))?$",
                prefix
            )
            if province_match and province_match.group(1):
                court_name = province_match.group(1) + keyword
            else:
                court_name = keyword
            best_court = court_name
            # 在案号上下文中，court 通常是距离案号最近的那个
            # 此处简化处理，返回最后找到的
    return best_court


# ============================================================
# 案例引用提取
# ============================================================

def _has_self_reference(text: str) -> bool:
    """
    检查文本是否含指代词（指向当前文书本身）。

    这些词指向当前文书内部，不可外部检索，
    因此不构成案例引用。

    Args:
        text: 待检查文本

    Returns:
        True 如果含指代词
    """
    for pattern in PRONOUN_PATTERNS:
        if pattern.search(text):
            return True
    return False


def extract_case_refs(text: str) -> list[CaseRef]:
    """
    从文本中提取所有案例引用。

    有案号 → CaseRef(reference_type=with_case_number, case_number=...)
    无案号但命中白名单 → CaseRef(reference_type=without_case_number, ...)
    含指代词 → 不提取

    Args:
        text: 待分析文本（通常是一个 anchor 的文本）

    Returns:
        CaseRef 列表
    """
    # 纯“本案/该案”等指代不可检索；同句另有明确外部案例线索时仍保留。
    has_explicit_reference = any(
        pattern.search(text)
        for pattern in (
            CASE_NUMBER_PATTERN,
            GUIDING_CASE_PATTERN,
            GAZETTE_TYPICAL_PATTERN,
            NAMED_CASE_PATTERN,
        )
    )
    if _has_self_reference(text) and not has_explicit_reference:
        return []

    case_refs: list[CaseRef] = []

    # 1. 有案号
    case_number_matches = CASE_NUMBER_PATTERN.finditer(text)
    for m in case_number_matches:
        full_match = m.group(0)
        court = extract_court_from_context(text[:m.start()])
        case_refs.append(CaseRef(
            reference_type=CaseReferenceType.WITH_CASE_NUMBER,
            case_number=full_match,
            case_name=None,
            court=court,
        ))

    # 2. 指导案例
    for m in GUIDING_CASE_PATTERN.finditer(text):
        case_refs.append(CaseRef(
            reference_type=CaseReferenceType.WITHOUT_CASE_NUMBER,
            case_number=None,
            case_name=m.group(0),
            court=None,
        ))

    # 3. 公报案例 / 典型案例
    for m in GAZETTE_TYPICAL_PATTERN.finditer(text):
        prefix = text[max(0, m.start() - 8):m.start()]
        # “附录二：典型案例”只是章节标题，不是可外部检索的具体案例。
        if re.search(r"附录[一二三四五六七八九十\d]*[：:]?\s*$", prefix):
            continue
        case_refs.append(CaseRef(
            reference_type=CaseReferenceType.WITHOUT_CASE_NUMBER,
            case_number=None,
            case_name=m.group(0),
            court=None,
        ))

    # 4. 命名案（X诉Y……案）
    for m in NAMED_CASE_PATTERN.finditer(text):
        case_name = _clean_named_case_name(m.group(0))
        if not case_name:
            continue
        numbered = [ref for ref in case_refs if ref.case_number]
        guiding = [
            ref
            for ref in case_refs
            if ref.case_name and GUIDING_CASE_PATTERN.fullmatch(ref.case_name)
        ]
        # “命名案（案号）”及“指导案例X号：命名案”都是同一案例的两种线索，
        # 合并到一个 CaseRef，避免重复核验与重复结果卡片。
        if len(numbered) == 1:
            numbered[0].case_name = case_name
            continue
        if len(guiding) == 1:
            guiding[0].case_name = f"{guiding[0].case_name}：{case_name}"
            continue
        # 避免与已添加的重复
        if not any(c.case_name == case_name for c in case_refs):
            case_refs.append(CaseRef(
                reference_type=CaseReferenceType.WITHOUT_CASE_NUMBER,
                case_number=None,
                case_name=case_name,
                court=None,
            ))

    return case_refs


def _clean_named_case_name(value: str) -> str:
    """清除指导案例编号、引导词等非案名边界文本。"""
    name = value.strip()
    if "：" in name or ":" in name:
        name = re.split(r"[：:]", name)[-1].strip()
    name = re.sub(r"^(?:可参见|参见|例如|譬如|如|案例)\s*", "", name)
    name = name.lstrip("0123456789一二三四五六七八九十百号、：: ")
    if "诉" not in name or len(name) > 90:
        return ""
    return name


def find_holding_trigger_position(
    text: str,
    case_refs: list[CaseRef],
) -> Optional[int]:
    """
    检测是否存在观点触发词，返回观点文本的起始位置。

    前提：必须有明确的 case_ref。
    没有明确 case_ref 时，即使出现"法院认为""本院认为"也不触发。

    规则：查找最后一个 case_ref 之后是否出现"认为/指出/
    明确/裁判认为/法院认为/裁判要旨/裁判规则"等观点触发词。

    Args:
        text: 完整文本
        case_refs: 已提取的案例引用列表

    Returns:
        观点文本起始位置，如果没有触发则返回 None
    """
    if not case_refs:
        return None

    # 找到最后一个 case_ref 在文本中的位置
    last_ref_pos = -1
    for cr in case_refs:
        if cr.case_number:
            pos = text.find(cr.case_number)
        elif cr.case_name:
            pos = text.find(cr.case_name)
        else:
            continue
        if pos > last_ref_pos:
            last_ref_pos = pos

    if last_ref_pos < 0:
        return None

    # 在 case_ref 之后查找观点触发词
    after_ref = text[last_ref_pos:]
    # 先跳过 case_ref 本身
    # 找到第一个非 case_ref 内容的起始
    # 简化处理：在整段后面搜索触发词
    trigger_match = HOLDING_TRIGGER_PATTERN.search(after_ref)
    if not trigger_match:
        return None

    trigger_end = trigger_match.end()
    remaining = after_ref[trigger_end:]

    # 去除触发词后紧跟的逗号、冒号
    remaining = re.sub(r"^[，,：:\s]+", "", remaining)

    if not remaining or not remaining.strip():
        return None

    # 计算在原文中的绝对位置
    absolute_start = last_ref_pos + len(after_ref) - len(remaining)
    return absolute_start


def has_holding_trigger(text: str, case_refs: list[CaseRef]) -> bool:
    """
    判断同一句中是否同时有 case_ref 和观点触发词。

    没有 case_ref 时直接返回 False——即使文本中有"法院认为""本院认为"。
    这是因为裁判文书正文大量出现这两个词，它们指当前文书自身。

    Args:
        text: 完整文本
        case_refs: 已提取的案例引用列表

    Returns:
        True 如果同时有 case_ref 和观点触发词
    """
    if not case_refs:
        return False
    return find_holding_trigger_position(text, case_refs) is not None
