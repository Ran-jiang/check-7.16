"""
CCiteheck 非核查范围候选过滤器。

过滤明显不属于法律引用核查范围的文本。

设计决策：
  - 含明确法源引用的候选绝不过滤（设计决策 2.3）
    理由：即使后半句包含法律观点或判断（如"被告应当承担违约责任"），
    后续至少需要检索该法条并返回原文，本层不评价观点对错
  - 主要拦截以下类型：
    1. 案卷证据事实（证据/合同/票据是否真实）
    2. 当事人行为事实（是否履约/侵权/通知/经营）
    3. 市场经营与技术数据（销售额/下载量/用户数/市场份额/系统日志）
    4. 外部登记/许可/备案事实（公司登记/商标注册/专利登记/版号/备案号）
    5. 单纯法律评价结论（是否构成侵权/违约/合同无效/应担责）
"""

from __future__ import annotations

import re

# ============================================================
# out-of-scope 关键词模式
# ============================================================

# 证据事实关键词
EVIDENCE_KEYWORDS = [
    r"证据.*[是否真伪虚假]",
    r"(该|此|上述|前述|案涉).*证据",
    r"(合同|协议|票据|收据|发票).*真实",
    r"真实性.*(证据|材料)",
]

# 行为事实关键词
BEHAVIOR_KEYWORDS = [
    r"(是否|有无|存在).*履约",
    r"(是否|构成|存在).*侵权",
    r"(是否|已经|尚未).*通知",
    r"(是否|有无).*(发送|送达|接收)",
    r"实际经营",
    r"(是否|有无).*(生产|销售|使用|进口)",
]

# 市场数据关键词
MARKET_KEYWORDS = [
    r"(销售|营业)额",
    r"下载量",
    r"用户数",
    r"市场份额",
    r"系统日志",
    r"(经营|财务|会计)数据",
]

# 登记/许可/备案关键词
REGISTRATION_KEYWORDS = [
    r"公司.*登记",
    r"商标.*注册",
    r"专利.*(登记|授权|申请)",
    r"(版号|备案号|许可证|批准文号)",
    r"工商.*(登记|注册|备案)",
    r"著作权.*(登记|备案)",
]

# 单纯法律评价结论关键词
# 这些模式用于拦截不含法源引用的纯法律评价句子。
PURE_LEGAL_CONCLUSION_KEYWORDS = [
    r"^(是否)?构成(商标)?侵权[\s。！？；]*$",
    r"^(是否)?构[成败]违约[\s。！？；]*$",
    r"^(是否)?(应当|需要|无需).*赔偿[\s。！？；]*$",
    r"^(是否)?(应当|需要|无需).*承担.*责任[\s。！？；]*$",
    r"裁判.*(正确|错误)",
    r"证据.*(充分|不足|不充分)",
    r"类案.*(适用|比对|参考)",
]

# 编译所有模式
OUT_OF_SCOPE_PATTERNS: list[re.Pattern] = []
for kw_list in [
    EVIDENCE_KEYWORDS,
    BEHAVIOR_KEYWORDS,
    MARKET_KEYWORDS,
    REGISTRATION_KEYWORDS,
    PURE_LEGAL_CONCLUSION_KEYWORDS,
]:
    for kw in kw_list:
        OUT_OF_SCOPE_PATTERNS.append(re.compile(kw))


def is_out_of_scope_text(text: str) -> bool:
    """
    对纯文本执行 out-of-scope 检测（不依赖候选结构）。

    用于 Arbiter 根据重建文本再次判断。

    Args:
        text: 待检查文本

    Returns:
        True 如果文本匹配 out-of-scope 模式
    """
    if not text:
        return False

    for pattern in OUT_OF_SCOPE_PATTERNS:
        if pattern.search(text):
            return True

    return False
