"""
CCitecheck v0.2 核心入口 — 可验证主张抽取编排。

extract_claims 是 v0.2 的主入口函数。
编排流程：
  1. 构建索引（anchor_id → Anchor, chunk_id → anchors 等）
  2. 规则抽取器 → rule candidates
  3. Claim Arbiter → 最终 claims
  4. 构建 ClaimDocument
  5. 校验 → 失败则抛异常

注意：v0.2 当前仅使用规则抽取器。
LLM 抽取器留待 v0.3+ 语义比对阶段引入。
"""

from __future__ import annotations

import logging

from parser.schema import Anchor, Chunk, ParsedDocument

from .arbiter import arbitrate_claim_candidates, build_claim_document
from .rule_engine import extract_rule_candidates
from .schema import ClaimDocument
from .validators import validate_claim_document

logger = logging.getLogger(__name__)


def build_indexes(parsed_doc: ParsedDocument) -> dict:
    """
    构建抽取流程所需的全部索引。

    索引内容：
      - anchor_map: anchor_id → Anchor
      - anchor_order: anchor_id → 全局序号（从1开始）
      - block_map: block_id → Block
      - chunk_map: chunk_id → Chunk
      - chunk_anchors: chunk_id → anchor_ids 列表

    Args:
        parsed_doc: 已解析的文档

    Returns:
        索引字典
    """
    anchor_map: dict[str, Anchor] = {a.anchor: a for a in parsed_doc.anchors}
    anchor_order: dict[str, int] = {}
    for i, a in enumerate(parsed_doc.anchors):
        anchor_order[a.anchor] = i + 1

    block_map = {b.block_id: b for b in parsed_doc.blocks}
    chunk_map: dict[str, Chunk] = {c.chunk_id: c for c in parsed_doc.chunks}
    chunk_anchors: dict[str, list[str]] = {}
    for c in parsed_doc.chunks:
        chunk_anchors[c.chunk_id] = list(c.anchor_ids)

    return {
        "anchor_map": anchor_map,
        "anchor_order": anchor_order,
        "block_map": block_map,
        "chunk_map": chunk_map,
        "chunk_anchors": chunk_anchors,
    }


def extract_claims(parsed_doc: ParsedDocument) -> ClaimDocument:
    """
    CCitecheck v0.2 主入口 — 从 ParsedDocument 抽取可验证主张。

    流程：
      1. 构建索引
      2. 规则抽取器
      3. Claim Arbiter 裁决
      4. 构建 ClaimDocument
      5. 校验（失败则抛 ValueError）

    Args:
        parsed_doc: v0.1 解析产物

    Returns:
        ClaimDocument 对象

    Raises:
        ValueError: 校验失败时抛出，包含所有违反项
    """
    # ---- 1. 构建索引 ----
    indexes = build_indexes(parsed_doc)

    # ---- 2. 规则抽取 ----
    logger.info("开始规则抽取……")
    rule_candidates = extract_rule_candidates(parsed_doc, indexes)
    logger.info("规则抽取完成，候选数: %d", len(rule_candidates))

    # ---- 3. Claim Arbiter ----
    logger.info("Claim Arbiter 开始裁决，总候选数: %d", len(rule_candidates))
    final_claims = arbitrate_claim_candidates(rule_candidates, parsed_doc)
    logger.info("Arbiter 完成，最终 claim 数: %d", len(final_claims))

    # ---- 4. 构建 ClaimDocument ----
    claim_doc = build_claim_document(parsed_doc, final_claims)

    # ---- 5. 校验 ----
    violations = validate_claim_document(parsed_doc, claim_doc)
    if violations:
        logger.error("Claim 校验失败! 违反项数: %d", len(violations))
        for v in violations:
            logger.error("  - %s", v)
        raise ValueError(
            f"Claim 校验失败，共 {len(violations)} 项违反:\n"
            + "\n".join(f"  {i+1}. {v}" for i, v in enumerate(violations))
        )

    return claim_doc
