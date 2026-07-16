"""法规溯源链的应用编排。

本模块负责三件事：创建默认数据源链、按顺序执行回退查询、并发执行一批
互不依赖的查询。它只返回证据和每次查询轨迹，不解释证据，也不产生
“通过、问题、无法判断”等判定结论。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Mapping

from ..domain.evidence import LookupStatus, SourceTrace
from .sources.base import LookupRequest, LookupResult, StatuteSource
from .sources.local_laws import LocalSQLiteSource
from .sources.pkulaw.statutes import PkulawFallbackSource

DEFAULT_LOOKUP_WORKERS = 6


def build_default_sources(db_path: str | Path) -> list[StatuteSource]:
    """创建默认法规溯源链：先查本地库，再回退到北大法宝。"""
    return [LocalSQLiteSource(db_path), PkulawFallbackSource()]


def lookup_with_chain(
    sources: list[StatuteSource],
    request: LookupRequest,
) -> tuple[LookupResult, list[SourceTrace]]:
    """按顺序执行数据源链，并保留所有查询轨迹。

    取得条文或相关条款时立即返回；只取得法规元数据时暂存为部分结果，
    继续尝试后续数据源。所有数据源均失败时返回最后一次结果。
    """
    attempts: list[SourceTrace] = []
    last_result: LookupResult | None = None
    best_partial: LookupResult | None = None

    for source in sources:
        result = source.lookup(request)
        attempts.append(result.trace)
        last_result = result
        if result.status in (
            LookupStatus.ARTICLE_FOUND,
            LookupStatus.RELEVANT_ARTICLES_FOUND,
        ):
            return result, attempts
        if result.status in (
            LookupStatus.LAW_FOUND_ARTICLE_MISSING,
            LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE,
        ):
            best_partial = result

    if last_result is None:
        raise ValueError("No statute sources configured")
    if best_partial is not None:
        return best_partial, attempts
    return last_result, attempts


def run_lookup_batch(
    sources: list[StatuteSource],
    requests: Mapping[tuple, LookupRequest],
    workers: int = DEFAULT_LOOKUP_WORKERS,
) -> dict[tuple, tuple[LookupResult, list[SourceTrace]]]:
    """并发执行已去重的法规查询，并保持输入键与结果的对应关系。"""
    if not requests:
        return {}
    keys = list(requests)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(
            pool.map(lambda key: lookup_with_chain(sources, requests[key]), keys)
        )
    return dict(zip(keys, outcomes))


__all__ = ["build_default_sources", "lookup_with_chain", "run_lookup_batch"]
