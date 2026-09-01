"""法规引用的法域识别。

国别线索通常在书名号之外（如「德国《著作权法》」），识别时结合
书名号前的紧邻修饰词与常见涉外法名别名表判定法域。只认紧邻前缀，
「大陆法系国家多规定」这类泛称不触发。
"""

from __future__ import annotations

import re

JURISDICTION_CN = "CN"
JURISDICTION_EU = "EU"
JURISDICTION_FOREIGN = "FOREIGN"

_EU_PREFIXES = ("欧盟", "欧洲联盟", "欧共体", "欧洲议会")

_FOREIGN_PREFIXES = (
    "德国", "法国", "美国", "日本", "英国", "韩国", "俄罗斯", "新加坡",
    "意大利", "西班牙", "荷兰", "瑞士", "瑞典", "挪威", "丹麦", "芬兰",
    "澳大利亚", "加拿大", "印度", "巴西", "南非", "泰国", "越南",
    "马来西亚", "印度尼西亚", "阿根廷", "墨西哥", "联合国",
)

# 知名涉外法规的名称直接映射（书名号内即可判定，无需前缀）。
_TITLE_ALIASES: dict[str, str] = {
    "通用数据保护条例": JURISDICTION_EU,
    "一般数据保护条例": JURISDICTION_EU,
    "人工智能法案": JURISDICTION_EU,
    "数字市场法": JURISDICTION_EU,
    "数字服务法": JURISDICTION_EU,
    "数据法案": JURISDICTION_EU,
    "知识产权法典": JURISDICTION_FOREIGN,
}

_GDPR_PATTERN = re.compile(r"\bGDPR\b", re.IGNORECASE)

# 书名号前允许略过的收尾字符（顿号、空白），再往前取紧邻修饰词。
_PREFIX_WINDOW = 8


def detect_jurisdiction(title: str, preceding_text: str = "") -> str:
    """判定一条《》引用的法域。

    Args:
        title: 书名号内的法规名（不含书名号）。
        preceding_text: 书名号之前的原文片段（任意长度，取尾部窗口判断）。
    """
    alias = _TITLE_ALIASES.get(title.strip())
    if alias is not None:
        return alias
    if _GDPR_PATTERN.search(title):
        return JURISDICTION_EU

    window = preceding_text[-_PREFIX_WINDOW:].rstrip("、 \t　")
    for prefix in _EU_PREFIXES:
        if window.endswith(prefix):
            return JURISDICTION_EU
    for prefix in _FOREIGN_PREFIXES:
        if window.endswith(prefix):
            return JURISDICTION_FOREIGN
    return JURISDICTION_CN


__all__ = [
    "JURISDICTION_CN",
    "JURISDICTION_EU",
    "JURISDICTION_FOREIGN",
    "detect_jurisdiction",
]
