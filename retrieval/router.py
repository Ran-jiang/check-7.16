"""
法条检索路由器。

路由规则：
  1. local 命中法规 → 以本地结论为准（含 ARTICLE_NOT_FOUND 的引注错误信号），
     不再下探——本地库权威且完整
  2. local 未收录该法规 → 依次尝试 fallback 源（gov_search → pkulaw）
  3. 某个 fallback 抛错不中断路由，记入 result.errors 继续下一个
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol, runtime_checkable

from .local_source import LocalSource
from .schema import (
    ProvisionEvidence,
    ProvisionQuery,
    ProvisionResult,
    RetrievalStatus,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class FallbackSource(Protocol):
    """外部数据源协议（gov_search / pkulaw 实现）。"""

    name: str

    def fetch(self, query: ProvisionQuery) -> Optional[ProvisionEvidence]:
        """返回条文证据；未命中返回 None；基础设施故障可抛异常。"""
        ...


class ProvisionRouter:
    def __init__(
        self,
        local: LocalSource,
        fallbacks: Optional[list[FallbackSource]] = None,
    ):
        self.local = local
        self.fallbacks = fallbacks or []

    def resolve(self, query: ProvisionQuery) -> ProvisionResult:
        # ---- 第一层：本地库 ----
        local_result = self.local.lookup(query)
        if local_result is not None:
            return local_result

        # ---- 第二/三层：外部源 ----
        tried = ["local"]
        errors: list[str] = []
        for source in self.fallbacks:
            tried.append(source.name)
            try:
                evidence = source.fetch(query)
            except Exception as e:  # noqa: BLE001 — 数据源故障不中断路由
                logger.warning("source %s failed: %s", source.name, e)
                errors.append(f"{source.name}: {e}")
                continue
            if evidence is not None:
                status = (
                    RetrievalStatus.FOUND
                    if evidence.text
                    else RetrievalStatus.LAW_FOUND_NO_ARTICLE
                )
                return ProvisionResult(
                    query=query,
                    status=status,
                    evidence=evidence,
                    providers_tried=tried,
                    errors=errors,
                )

        return ProvisionResult(
            query=query,
            status=RetrievalStatus.NOT_FOUND,
            providers_tried=tried,
            errors=errors,
        )

    def resolve_all(self, queries: list[ProvisionQuery]) -> list[ProvisionResult]:
        return [self.resolve(q) for q in queries]


# ============================================================
# v0.2 ClaimDocument → ProvisionQuery 展开
# ============================================================

_LEGAL_CLAIM_TYPES = {"legal_source_claim", "legal_source_paraphrase"}


def queries_from_claims(claims_doc: dict) -> list[ProvisionQuery]:
    """
    从 v0.2 claims JSON（dict）展开法条检索请求。

    每个 legal claim 的每个 (法源 × 条) 生成一条查询；
    法源无条款号时生成一条法规级查询。
    案例类 claim 不在本层处理。
    """
    queries: list[ProvisionQuery] = []
    for claim in claims_doc.get("claims", []):
        if claim.get("claim_type") not in _LEGAL_CLAIM_TYPES:
            continue
        entities = claim.get("entities", {})
        quote_text = entities.get("paraphrase_text") or None
        for source in entities.get("legal_sources", []):
            title = source.get("title", "").strip()
            if not title:
                continue
            articles = source.get("articles", [])
            if not articles:
                queries.append(ProvisionQuery(
                    law_title=title,
                    source_type=source.get("source_type"),
                    quote_text=quote_text,
                    claim_id=claim.get("claim_id"),
                ))
                continue
            for art in articles:
                queries.append(ProvisionQuery(
                    law_title=title,
                    source_type=source.get("source_type"),
                    article_label=art.get("article"),
                    paragraph_labels=art.get("paragraphs", []),
                    item_labels=art.get("items", []),
                    quote_text=quote_text,
                    claim_id=claim.get("claim_id"),
                ))
    return queries
