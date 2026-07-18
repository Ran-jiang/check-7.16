"""根据法规溯源证据生成确定性判定。"""

from __future__ import annotations

import difflib
import re

from ...domain.evidence import LookupStatus, SourceTier, SourceTrace
from ...domain.statute_results import (
    StatuteErrorCode,
    StatuteFinding,
    StatuteLocator,
    StatuteVersion,
)
from ...infrastructure.database import normalize_title, strip_version_annotation
from ...tracing.sources.base import LookupResult

_REPEALED_PATTERN = re.compile(r"废止|失效")
_GB_STANDARD_PATTERN = re.compile(r"GB\s*/?\s*[TZ]?\s*\d{3,6}")


def classify_not_verifiable(law_title: str) -> str | None:
    if "征求意见稿" in law_title:
        return "征求意见稿尚未生效，不属于可核验的现行法源，请人工确认引用意图"
    if _GB_STANDARD_PATTERN.search(law_title):
        return "国家/行业标准不在法规库核验范围内，请以标准全文出版物为准"
    if "专项通知" in law_title:
        return "专项通知不按法条编号核验，请以发布机关原文件及适用期限为准"
    return None


def assess_statute(
    law_title: str,
    article_no: str | None,
    result: LookupResult,
    attempts: list[SourceTrace],
    known_titles: list[str],
    historical_versions: list[StatuteVersion] | None = None,
) -> list[StatuteFinding]:
    """判定法源存在性、时效和条号定位；不执行语义判断。"""
    repealed = _is_repealed(result)
    if repealed:
        return [_repealed_finding(law_title)]

    if result.status == LookupStatus.LAW_FOUND_ARTICLE_MISSING and article_no:
        if historical_versions:
            version = historical_versions[0]
            return [StatuteFinding(
                code=StatuteErrorCode.SOURCE_AMENDED,
                risk_level="HIGH",
                summary=f"现行《{strip_version_annotation(law_title)}》不存在{article_no}，但历史版本中存在该条",
                suggestion="该引用对应历史版本，请核实适用时间，并改引现行规定。",
                cited_locator=StatuteLocator(article_no=article_no),
                historical_version=version,
            )]
        return [_missing_article_finding(law_title, article_no, result, attempts)]

    pkulaw = _completed_pkulaw_not_found(attempts)
    if result.status == LookupStatus.LAW_NOT_FOUND and pkulaw is not None:
        candidates = list(pkulaw.metadata.get("candidate_titles", []))
        suggested = suggest_similar_title(law_title, [*known_titles, *candidates])
        suggestion = (
            f"疑似应为《{suggested}》，请核实法规名称。"
            if suggested
            else "北大法宝未检索到该法规，请人工核对法规名称。"
        )
        return [StatuteFinding(
            code=StatuteErrorCode.SOURCE_NOT_FOUND,
            risk_level="HIGH",
            summary=f"北大法宝未检索到《{strip_version_annotation(law_title)}》",
            suggestion=suggestion,
        )]

    return []


def _is_repealed(result: LookupResult) -> bool:
    evidence = result.evidence
    if evidence is None:
        return False
    values = (
        evidence.version_status or "",
        evidence.version_label or "",
        str(evidence.source_metadata.get("timeliness", "")),
    )
    return any(_REPEALED_PATTERN.search(value) for value in values)


def _repealed_finding(law_title: str) -> StatuteFinding:
    title = strip_version_annotation(law_title)
    return StatuteFinding(
        code=StatuteErrorCode.SOURCE_REPEALED,
        risk_level="HIGH",
        summary=f"《{title}》的权威证据标记为已废止或失效",
        suggestion="该法律已经废止，若非适用行为时法，请核实并改引现行规定。",
    )


def _missing_article_finding(
    law_title: str,
    article_no: str,
    result: LookupResult,
    attempts: list[SourceTrace],
) -> StatuteFinding:
    local_count = next((
        int(trace.metadata.get("local_article_count", 0))
        for trace in attempts
        if trace.tier == SourceTier.LOCAL_SQLITE
    ), 0)
    if local_count:
        summary = f"《{strip_version_annotation(law_title)}》现行全文共{local_count}条，其中不存在{article_no}"
        risk = "HIGH"
        suggestion = "请核实条文编号；该条在现行有效版本中不存在。"
    else:
        summary = f"北大法宝已收录该法规，但未检索到{article_no}"
        risk = "MEDIUM"
        suggestion = "请人工核实该条文编号是否存在。"
    return StatuteFinding(
        code=StatuteErrorCode.CITATION_LOCATION_ERROR,
        risk_level=risk,
        summary=summary,
        suggestion=suggestion,
        cited_locator=StatuteLocator(article_no=article_no),
    )


def _completed_pkulaw_not_found(attempts: list[SourceTrace]) -> SourceTrace | None:
    return next((
        trace
        for trace in attempts
        if trace.tier == SourceTier.PKULAW_FALLBACK
        and trace.status == LookupStatus.LAW_NOT_FOUND
        and trace.metadata.get("search_completed") is True
    ), None)


def suggest_similar_title(law_title: str, known_titles: list[str]) -> str | None:
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


__all__ = ["assess_statute", "classify_not_verifiable", "suggest_similar_title"]
