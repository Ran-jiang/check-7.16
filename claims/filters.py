"""
CCitecheck v0.2 out-of-scope 过滤器。

过滤明显越界候选——主要是拦截 LLM 抽取器可能输出的非可验证主张类型。

设计决策：
  - 含明确法源引用的候选绝不过滤（设计决策 2.3）
    理由：即使后半句包含法律观点或判断（如"被告应当承担违约责任"），
    后续至少需要检索该法条并返回原文，v0.2 不评价观点对错
  - 主要拦截以下类型：
    1. 案卷证据事实（证据/合同/票据是否真实）
    2. 当事人行为事实（是否履约/侵权/通知/经营）
    3. 市场经营与技术数据（销售额/下载量/用户数/市场份额/系统日志）
    4. 外部登记/许可/备案事实（公司登记/商标注册/专利登记/版号/备案号）
    5. 单纯法律评价结论（是否构成侵权/违约/合同无效/应担责）
"""

from __future__ import annotations

import re

from .schema import ClaimCandidate, ClaimType


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
# 注意：含法源的候选不在此列（在 is_out_of_scope_candidate 中特殊处理）
# 这些模式用于拦截不含法源引用的纯法律评价句子
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


# ============================================================
# is_out_of_scope_candidate
# ============================================================

def is_out_of_scope_candidate(candidate: ClaimCandidate) -> bool:
    """
    判断候选主张是否为 out-of-scope（越界输出）。

    主要拦截 LLM 抽取器可能输出的非可验证主张类型。

    关键规则（设计决策 2.3）：
      含明确法源引用的候选绝不过滤。即使文本后半句包含法律观点或判断
      （如"依据《民法典》第五百七十七条，被告应当承担违约责任。"），
      也保留为 legal_source_claim。理由：后续至少需要检索该法条并返回原文，
      v0.2 不评价观点对错。

    Args:
        candidate: 候选主张

    Returns:
        True 如果是越界输出，应被丢弃
    """
    # 含法源引用的候选绝不过滤
    # 检查 entities 是否含有 legal_sources（legal_source_claim）
    entities = candidate.entities
    if hasattr(entities, "legal_sources") and entities.legal_sources:
        return False

    # 案例引用的候选暂不过滤（案例检索是合法的验证路径）
    if candidate.claim_type in (ClaimType.CASE_CITATION, ClaimType.CASE_HOLDING_PARAPHRASE):
        return False

    # 需要检查文本内容，取 llm_text（如果有）或候选的 anchor 文本
    # 这里只能访问 candidate 的已知字段
    # 如果没有文本，默认不过滤（由 Arbiter 的重建文本后再次判断）
    text_to_check = candidate.llm_text or ""

    # 如果完全没有文本可用，默认不过滤（避免因信息不足误杀）
    if not text_to_check:
        return False

    # 匹配 out-of-scope 关键词
    for pattern in OUT_OF_SCOPE_PATTERNS:
        if pattern.search(text_to_check):
            return True

    return False


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
