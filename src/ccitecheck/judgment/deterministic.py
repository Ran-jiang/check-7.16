"""不依赖大模型的法规引用判定规则。

本模块只根据文件类型、检索状态、法规时效和本地全文元数据生成确定性
问题。它不负责查询数据源，也不负责执行千问语义比较。
"""

from __future__ import annotations

import difflib
import re
from typing import Optional

from ..infrastructure.database import normalize_title, strip_version_annotation

from ..domain.evidence import LookupStatus, SourceTier, SourceTrace
from ..domain.result import (
    RiskLevel,
    SemanticErrorType,
    SemanticIssue,
)
from ..tracing.sources.base import LookupResult

_GB_STANDARD_PATTERN = re.compile(r"GB\s*/?\s*[TZ]?\s*\d{3,6}")
_REPEALED_PATTERN = re.compile(r"废止|失效")


def classify_not_verifiable(law_title: str) -> Optional[str]:
    """识别不适合按法规条文核验的文件，并返回人工处理原因。"""
    if "征求意见稿" in law_title:
        return "征求意见稿尚未生效，不属于可核验的现行法源，请人工确认引用意图"
    if _GB_STANDARD_PATTERN.search(law_title):
        return "国家/行业标准不在法规库核验范围内，请以标准全文出版物为准"
    if "专项通知" in law_title:
        return "专项通知不按法条编号核验，请以发布机关原文件及适用期限为准"
    return None


def build_rule_findings(
    law_title: str,
    article_no: Optional[str],
    result: LookupResult,
    attempts: list[SourceTrace],
    known_titles: list[str],
) -> list[SemanticIssue]:
    """依据溯源结果生成无需大模型参与的确定性问题。"""
    findings: list[SemanticIssue] = []

    # 规则一：证据中的任一时效字段显示废止或失效。
    evidence = result.evidence
    repealed = False
    if evidence is not None:
        values = [
            evidence.version_status or "",
            evidence.version_label or "",
            str(evidence.source_metadata.get("timeliness", "")),
        ]
        repealed = any(_REPEALED_PATTERN.search(value) for value in values)
        if repealed:
            findings.append(SemanticIssue(
                error_type=SemanticErrorType.OUTDATED_SOURCE,
                risk_level=RiskLevel.HIGH,
                diff_summary=(f"《{strip_version_annotation(law_title)}》时效状态为"
                              "『废止或失效』，不应作为现行依据引用")[:80],
                suggestion="请改引现行有效的替代法规，并核对对应条文内容。",
            ))

    # 规则二：已确认法规存在，但现行全文或法宝均没有该条号。
    if result.status == LookupStatus.LAW_FOUND_ARTICLE_MISSING and article_no:
        local_count = next((
            trace.metadata.get("local_article_count", 0)
            for trace in attempts if trace.tier == SourceTier.LOCAL_SQLITE
        ), 0)
        if local_count > 0:
            findings.append(SemanticIssue(
                error_type=SemanticErrorType.LOCATION_ERROR,
                risk_level=RiskLevel.HIGH,
                diff_summary=(f"《{strip_version_annotation(law_title)}》现行全文共"
                              f"{local_count}条，其中不存在{article_no}")[:80],
                suggestion="请核实条文编号；该条在现行有效版本中不存在。",
            ))
        elif result.evidence is not None and not repealed:
            findings.append(SemanticIssue(
                error_type=SemanticErrorType.LOCATION_ERROR,
                risk_level=RiskLevel.MEDIUM,
                diff_summary=f"北大法宝已收录该法规，但未检索到{article_no}"[:80],
                suggestion="请人工核实该条文编号是否存在。",
            ))

    # 规则三：所有权威来源均未找到法源，或法名疑似存在拼写错误。
    if result.status == LookupStatus.LAW_NOT_FOUND:
        search_completed = any(t.metadata.get("search_completed") for t in attempts)
        candidates = [title for t in attempts for title in t.metadata.get("candidate_titles", [])]
        suggestion_title = suggest_similar_title(law_title, known_titles + candidates)
        suggestion = (f"疑似应为《{suggestion_title}》，请核实法规名称。"
                      if suggestion_title else "请核实法规名称、发布机关及发文字号。")
        if search_completed:
            findings.append(SemanticIssue(
                error_type=SemanticErrorType.SOURCE_NOT_FOUND,
                risk_level=RiskLevel.HIGH,
                diff_summary=(f"本地法规库与北大法宝均未检索到"
                              f"《{strip_version_annotation(law_title)}》")[:80],
                suggestion=suggestion,
            ))
        elif suggestion_title:
            findings.append(SemanticIssue(
                error_type=SemanticErrorType.SOURCE_NOT_FOUND,
                risk_level=RiskLevel.MEDIUM,
                diff_summary=(f"未检索到《{strip_version_annotation(law_title)}》，"
                              f"名称与《{suggestion_title}》高度相似")[:80],
                suggestion=suggestion,
            ))
    return findings


def suggest_similar_title(law_title: str, known_titles: list[str]) -> Optional[str]:
    """在已知法规标题中寻找可信度足够高的单一相似标题。"""
    if not known_titles:
        return None
    target = strip_version_annotation(normalize_title(law_title))
    matches = difflib.get_close_matches(target, known_titles, n=1, cutoff=0.8)
    if matches:
        return matches[0]
    short = target.replace("中华人民共和国", "", 1)
    shorts = [title.replace("中华人民共和国", "", 1) for title in known_titles]
    matches = difflib.get_close_matches(short, shorts, n=1, cutoff=0.8)
    return known_titles[shorts.index(matches[0])] if matches else None


__all__ = [
    "build_rule_findings",
    "classify_not_verifiable",
    "suggest_similar_title",
]
