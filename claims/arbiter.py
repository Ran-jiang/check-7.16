"""
CCitecheck v0.2 Claim Arbiter — 唯一最终裁决器。

所有候选（规则 + LLM）必须经过 Arbiter 后才成为最终 Claim。
不设单独的 LLM Reviewer。

核心职责：
  1. 硬校验：schema / anchor 存在性 / anchor 连续性
  2. out-of-scope 过滤
  3. 从 anchors 重建 claim.text（"不改写原文"的结构性保证）
  4. 实体子串校验
  5. 同位置去重合并
  6. 完整性裁决（子集候选 → 保留更完整的）
  7. 排序、生成 claim_id
  8. 派生 block_ids

处理顺序和逻辑严格按照设计规范 §8 执行。
"""

from __future__ import annotations

import logging
import re

from parser.schema import Anchor, ParsedDocument

from .filters import is_out_of_scope_text
from .schema import (
    Claim,
    ClaimCandidate,
    ClaimDebug,
    ClaimType,
    ExtractionMethod,
    VerificationRoute,
)

logger = logging.getLogger(__name__)


# ============================================================
# 辅助函数
# ============================================================

def _parse_anchor_number(anchor_id: str) -> int:
    """从 anchor 编号提取数字部分，解析失败返回 -1"""
    match = re.search(r"(\d+)", anchor_id)
    if match:
        return int(match.group(1))
    return -1


def _check_anchor_continuity(anchor_ids: list[str]) -> bool:
    """
    检查 anchor 编号是否连续。

    连续 = 全局编号相邻（如 line00052、line00053）。
    允许跨 block。
    """
    if not anchor_ids:
        return False
    nums = [_parse_anchor_number(aid) for aid in anchor_ids]
    if any(n < 0 for n in nums):
        return False
    nums.sort()
    for i in range(1, len(nums)):
        if nums[i] != nums[i - 1] + 1:
            return False
    return True


def _rebuild_text(anchor_ids: list[str], anchor_map: dict[str, Anchor]) -> str:
    """
    从 anchors 重建 claim.text。

    按 anchor 编号排序后拼接 anchor.text。
    这是"不改写原文"的结构性保证——claim.text 永远等于原文锚点文本的精确拼接。

    Args:
        anchor_ids: anchor 编号列表
        anchor_map: anchor_id → Anchor 的映射

    Returns:
        按序拼接的文本
    """
    # 按编号排序
    sorted_ids = sorted(anchor_ids, key=_parse_anchor_number)
    parts = []
    for aid in sorted_ids:
        anchor = anchor_map.get(aid)
        if anchor:
            parts.append(anchor.text)
    return "".join(parts)


def _derive_block_ids(
    anchor_ids: list[str],
    anchor_map: dict[str, Anchor],
) -> list[str]:
    """
    从 anchor.block_id 派生 claim 的 block_ids。

    block_ids 仅作溯源，不参与 claim 判断（设计决策 2.6）。

    Args:
        anchor_ids: anchor 编号列表
        anchor_map: anchor_id → Anchor 的映射

    Returns:
        去重后的 block_id 列表
    """
    block_ids_set: set[str] = set()
    for aid in anchor_ids:
        anchor = anchor_map.get(aid)
        if anchor and anchor.block_id:
            block_ids_set.add(anchor.block_id)
    # 按 block_order 排序
    return sorted(block_ids_set)


def _derive_verification_route(
    claim_type: ClaimType,
    entities,
) -> VerificationRoute:
    """
    根据 claim_type 和 entities 推导核查路由（确定性规则）。

    规则：
      legal_source_claim / legal_source_paraphrase:
        所有 legal_sources 的 source_type 均为 judicial_interpretation
        → judicial_interpretation_database
        否则 → statute_database

      case_citation:
        任一 case_ref 为 with_case_number → case_database_exact
        全部为 without_case_number → case_database_search
        混合时取 case_database_exact（精确检索优先）

      case_holding_paraphrase:
        → case_database_fulltext

    Args:
        claim_type: 主张类型
        entities: 实体子模型

    Returns:
        核查路由
    """
    if claim_type in (ClaimType.LEGAL_SOURCE_CLAIM, ClaimType.LEGAL_SOURCE_PARAPHRASE):
        if hasattr(entities, "legal_sources") and entities.legal_sources:
            all_ji = all(
                ls.source_type == "judicial_interpretation"
                for ls in entities.legal_sources
            )
            if all_ji:
                return VerificationRoute.JUDICIAL_INTERPRETATION_DATABASE
        return VerificationRoute.STATUTE_DATABASE

    elif claim_type == ClaimType.CASE_CITATION:
        if hasattr(entities, "case_refs") and entities.case_refs:
            has_exact = any(
                cr.reference_type == "with_case_number"
                for cr in entities.case_refs
            )
            if has_exact:
                return VerificationRoute.CASE_DATABASE_EXACT
        return VerificationRoute.CASE_DATABASE_SEARCH

    elif claim_type == ClaimType.CASE_HOLDING_PARAPHRASE:
        return VerificationRoute.CASE_DATABASE_FULLTEXT

    # fallback
    return VerificationRoute.STATUTE_DATABASE


# ============================================================
# 实体选择
# ============================================================

def _count_legal_sources(entities) -> int:
    """返回 legal_sources 的数量（用于实体比较）"""
    if hasattr(entities, "legal_sources"):
        return len(entities.legal_sources)
    return 0


def _select_better_entities(entities_a, entities_b, method_a, method_b):
    """
    从两个同位置候选中选择信息更全的实体。

    规则：
      1. legal_sources 数量多者优先
      2. 数量相同则规则优先（rule > llm）
      3. 否则保留 a

    Args:
        entities_a: 候选A的实体
        entities_b: 候选B的实体
        method_a: 候选A的抽取方法
        method_b: 候选B的抽取方法

    Returns:
        选中的实体
    """
    count_a = _count_legal_sources(entities_a)
    count_b = _count_legal_sources(entities_b)

    if count_b > count_a:
        return entities_b
    if count_a > count_b:
        return entities_a

    # 数量相同，规则优先
    if method_b == ExtractionMethod.RULE and method_a != ExtractionMethod.RULE:
        return entities_b

    return entities_a


# ============================================================
# Claim Arbiter
# ============================================================

def arbitrate_claim_candidates(
    candidates: list[ClaimCandidate],
    parsed_doc: ParsedDocument,
) -> list[Claim]:
    """
    Claim Arbiter — 所有候选的最终裁决点。

    处理顺序（严格按设计规范 §8）：
      1. 硬校验：claim_type 合法、anchor_ids 非空、全部存在于 parsed_doc、编号连续
      2. out-of-scope 过滤
      3. 从 anchors 重建 claim.text
      4. 实体子串校验
      5. 去重合并：(claim_type, tuple(anchor_ids)) 相同 → 合并
      6. 完整性裁决：子集候选 → 保留更长的
      7. 不同位置不合并
      8. 排序与编号
      9. 派生 block_ids

    Args:
        candidates: 所有候选（规则 + LLM）
        parsed_doc: 已解析的文档

    Returns:
        最终 Claim 列表
    """
    # 构建索引
    anchor_map: dict[str, Anchor] = {a.anchor: a for a in parsed_doc.anchors}
    valid_anchor_ids = set(anchor_map.keys())

    # ---- 第1步：硬校验 ----
    passed: list[ClaimCandidate] = []
    for cand in candidates:
        # claim_type 合法
        if not isinstance(cand.claim_type, ClaimType):
            logger.warning("候选 claim_type 非法: %s，丢弃", cand.claim_type)
            continue

        # anchor_ids 非空
        if not cand.anchor_ids:
            logger.warning("候选 anchor_ids 为空，丢弃")
            continue

        # anchor 全部存在
        if not all(aid in valid_anchor_ids for aid in cand.anchor_ids):
            missing = [aid for aid in cand.anchor_ids if aid not in valid_anchor_ids]
            logger.warning("候选 anchor 不存在: %s，丢弃", missing)
            continue

        # anchor 编号连续
        if not _check_anchor_continuity(cand.anchor_ids):
            logger.warning("候选 anchor 编号不连续: %s，丢弃", cand.anchor_ids)
            continue

        passed.append(cand)

    if not passed:
        return []

    # ---- 第2步：out-of-scope 过滤 ----
    # 重建文本用于过滤判断
    filtered: list[ClaimCandidate] = []
    for cand in passed:
        text = _rebuild_text(cand.anchor_ids, anchor_map)
        # 含法源的候选绝不过滤（设计决策 2.3）
        has_legal_source = (
            hasattr(cand.entities, "legal_sources") and cand.entities.legal_sources
        )
        if has_legal_source:
            filtered.append(cand)
        elif not is_out_of_scope_text(text):
            filtered.append(cand)
        else:
            logger.debug("候选被 out-of-scope 过滤: %s", cand.anchor_ids)

    if not filtered:
        return []

    # ---- 第3步：重建 text 并记录 text_mismatch ----
    for cand in filtered:
        rebuilt_text = _rebuild_text(cand.anchor_ids, anchor_map)
        cand.llm_text = getattr(cand, 'llm_text', None)  # keep existing if set
        if cand.llm_text:
            # 忽略首尾空白比较
            if cand.llm_text.strip() != rebuilt_text.strip():
                # 记入 text_mismatch（稍后在创建 Claim 时写入 debug）
                cand._text_mismatch = True
            else:
                cand._text_mismatch = False
        else:
            cand._text_mismatch = False

    # ---- 第4步：实体子串校验 ----
    for cand in filtered:
        rebuilt_text = _rebuild_text(cand.anchor_ids, anchor_map)

        # 校验 paraphrase_text
        if hasattr(cand.entities, "paraphrase_text") and cand.entities.paraphrase_text:
            if cand.entities.paraphrase_text not in rebuilt_text:
                logger.warning(
                    "paraphrase_text 不是 claim.text 子串，置空。"
                    "paraphrase_text=%s, claim_text=%s",
                    cand.entities.paraphrase_text[:100],
                    rebuilt_text[:100],
                )
                cand.entities.paraphrase_text = ""

        # 校验 holding_text
        if hasattr(cand.entities, "holding_text") and cand.entities.holding_text:
            if cand.entities.holding_text not in rebuilt_text:
                logger.warning(
                    "holding_text 不是 claim.text 子串，置空。"
                    "holding_text=%s, claim_text=%s",
                    cand.entities.holding_text[:100],
                    rebuilt_text[:100],
                )
                cand.entities.holding_text = ""

    # ---- 第5步：去重合并 ----
    # key = (claim_type, tuple(anchor_ids))
    merged: dict[tuple, ClaimCandidate] = {}
    merged_methods: dict[tuple, list[ExtractionMethod]] = {}
    merged_count: dict[tuple, int] = {}

    for cand in filtered:
        key = (cand.claim_type, tuple(cand.anchor_ids))

        if key in merged:
            # 合并：实体取信息更全的一方
            existing = merged[key]
            merged[key].entities = _select_better_entities(
                existing.entities, cand.entities,
                existing.method, cand.method,
            )
            methods_set = set(merged_methods.get(key, []))
            methods_set.add(cand.method)
            merged_methods[key] = list(methods_set)
            merged_count[key] = merged_count.get(key, 1) + 1
            existing._text_mismatch = getattr(existing, '_text_mismatch', False) or getattr(cand, '_text_mismatch', False)
        else:
            merged[key] = cand
            merged_methods[key] = [cand.method]
            merged_count[key] = 1

    # 转回列表
    deduped = []
    for key, cand in merged.items():
        cand._methods = merged_methods[key]
        cand._candidate_count = merged_count[key]
        deduped.append(cand)

    # ---- 第6步：完整性裁决 ----
    # 若候选 A 的 anchor_ids 是候选 B 的 anchor_ids 的真子集，
    # 且二者 claim_type 相同、anchor 区间重叠，保留更长的 B
    # A 的 method 并入 B 的 debug

    # 按 anchor 数量降序排列（长的在前）
    deduped.sort(key=lambda c: len(c.anchor_ids), reverse=True)

    completeness_ruled: list[ClaimCandidate] = []
    consumed = set()

    for i, cand_a in enumerate(deduped):
        if i in consumed:
            continue

        set_a = set(cand_a.anchor_ids)
        nums_a = sorted([_parse_anchor_number(aid) for aid in cand_a.anchor_ids])

        is_superset = False

        # 检查是否被更长的候选包含
        for j, cand_b in enumerate(deduped):
            if j == i:
                continue
            if j in consumed:
                continue

            set_b = set(cand_b.anchor_ids)
            nums_b = sorted([_parse_anchor_number(aid) for aid in cand_b.anchor_ids])

            if cand_a.claim_type == cand_b.claim_type:
                # 区间重叠
                a_min, a_max = min(nums_a), max(nums_a) if nums_a else 0
                b_min, b_max = min(nums_b), max(nums_b) if nums_b else 0
                if a_min <= b_max and b_min <= a_max:
                    # A 是 B 的父集（A 更长）
                    if set_b.issubset(set_a) and set_b != set_a:
                        # B 被 A 吸收，方法并入 A
                        cand_a._methods = list(
                            set(getattr(cand_a, '_methods', [cand_a.method]))
                            | set(getattr(cand_b, '_methods', [cand_b.method]))
                        )
                        cand_a._candidate_count = getattr(cand_a, '_candidate_count', 1) + getattr(cand_b, '_candidate_count', 1)
                        consumed.add(j)

        completeness_ruled.append(cand_a)

    # ---- 第7步：不同位置不合并 ----
    # 已在去重步骤中通过 anchor_ids 精确匹配处理
    # 不同 anchor_ids 但相同 text 的 claim 各自保留
    # （不额外处理）

    # ---- 第8步：排序与编号 ----
    # 按（首 anchor 编号，claim_type）排序
    completeness_ruled.sort(
        key=lambda c: (
            min(_parse_anchor_number(aid) for aid in c.anchor_ids),
            c.claim_type.value,
        )
    )

    # 生成连续 claim_id
    claims: list[Claim] = []
    for idx, cand in enumerate(completeness_ruled):
        claim_id = f"cl_{idx + 1:05d}"

        # 重建 text
        text = _rebuild_text(cand.anchor_ids, anchor_map)

        # 派生 block_ids
        block_ids = _derive_block_ids(cand.anchor_ids, anchor_map)

        # 推导 verification_route
        verification_route = _derive_verification_route(cand.claim_type, cand.entities)

        # 构建 debug
        raw_methods = getattr(cand, '_methods', [cand.method])
        # 转换为字符串（ClaimDebug.methods 使用 list[str]）
        methods = [
            m.value if isinstance(m, ExtractionMethod) else str(m)
            for m in raw_methods
        ]
        candidate_count = getattr(cand, '_candidate_count', 1)
        text_mismatch = getattr(cand, '_text_mismatch', False)
        debug = ClaimDebug(
            methods=methods,
            candidate_count=candidate_count,
            text_mismatch=text_mismatch,
        )

        claim = Claim(
            claim_id=claim_id,
            claim_type=cand.claim_type,
            text=text,
            anchor_ids=cand.anchor_ids,
            block_ids=block_ids,
            verification_route=verification_route,
            entities=cand.entities,
            debug=debug,
        )
        claims.append(claim)

    return claims


# ============================================================
# 构建 ClaimDocument
# ============================================================

def build_claim_document(
    parsed_doc: ParsedDocument,
    claims: list[Claim],
    llm_used: bool = False,
    llm_chunk_failures: list[str] | None = None,
) -> "ClaimDocument":
    """
    构建最终的 ClaimDocument。

    Args:
        parsed_doc: 来源 ParsedDocument
        claims: 最终 Claim 列表
        llm_used: 是否使用了 LLM 抽取器
        llm_chunk_failures: LLM 调用失败的 chunk_id 列表

    Returns:
        ClaimDocument 对象
    """
    from .schema import ClaimDocument, ClaimMeta

    if llm_chunk_failures is None:
        llm_chunk_failures = []

    meta = ClaimMeta(
        schema_version="0.2",
        source_doc_id=parsed_doc.doc_meta.doc_id,
        source_doc_hash=parsed_doc.doc_meta.doc_hash,
        source_file=parsed_doc.doc_meta.source_file,
        extractor_version="0.2",
        llm_used=llm_used,
        llm_chunk_failures=llm_chunk_failures,
    )

    return ClaimDocument(
        claim_meta=meta,
        claims=claims,
    )
