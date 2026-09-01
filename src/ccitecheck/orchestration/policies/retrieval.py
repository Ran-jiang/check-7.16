"""Scheduler 的数据源回退策略。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Mapping

from ...domain.evidence import LookupStatus, SourceTrace
from ...retrieval.service import DEFAULT_LOOKUP_WORKERS, execute_source
from ...retrieval.sources.base import LookupRequest, LookupResult, StatuteSource
from ...retrieval.sources.pkulaw.urls import is_legacy_mcp_url


def lookup_with_chain(
    sources: list[StatuteSource], request: LookupRequest
) -> tuple[LookupResult, list[SourceTrace]]:
    attempts: list[SourceTrace] = []
    last_result: LookupResult | None = None
    best_partial: LookupResult | None = None
    local_complete_with_legacy_url: LookupResult | None = None
    for source in sources:
        result = execute_source(source, request)
        attempts.append(result.trace)
        last_result = result
        if result.status in {LookupStatus.ARTICLE_FOUND, LookupStatus.RELEVANT_ARTICLES_FOUND}:
            if is_legacy_mcp_url(result.trace.source_url):
                discarded = result.trace.source_url
                result.trace.source_url = None
                result.trace.metadata["discarded_legacy_source_url"] = discarded
                if result.evidence is not None:
                    result.evidence.data_source.source_url = None
                local_complete_with_legacy_url = result
                continue
            return result, attempts
        if result.status in {
            LookupStatus.LAW_FOUND_ARTICLE_MISSING,
            LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE,
        }:
            best_partial = result
    if last_result is None:
        raise ValueError("No statute sources configured")
    return local_complete_with_legacy_url or best_partial or last_result, attempts


def run_lookup_batch(
    sources: list[StatuteSource],
    requests: Mapping[tuple, LookupRequest],
    workers: int = DEFAULT_LOOKUP_WORKERS,
) -> dict[tuple, tuple[LookupResult, list[SourceTrace]]]:
    if not requests:
        return {}
    keys = list(requests)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(
            lambda key: lookup_with_chain(sources, requests[key]), keys
        ))
    return dict(zip(keys, outcomes))


__all__ = ["lookup_with_chain", "run_lookup_batch"]
