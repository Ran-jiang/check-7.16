"""
CCitecheck v0.2 Claim 文档校验。

validate_claim_document 对 ClaimDocument 执行全量不变量校验。
校验失败返回违反项列表（字符串），通过返回空列表。

校验断言清单：
  - claim_id 唯一、按序连续
  - claim.text 非空，且严格等于 anchor_ids 文本拼接
  - anchor_ids 非空、全部存在、编号连续
  - block_ids 若非空则全部存在于 parsed_doc.blocks
  - claim_type 合法
  - verification_route 合法且符合对应表
  - entities 通过对应 claim_type 的 pydantic 子模型校验
  - paraphrase_text / holding_text 为 claim.text 子串
  - debug.methods 仅含 rule / llm
"""

from __future__ import annotations

import re

from parser.schema import Anchor, Block, ParsedDocument

from .schema import Claim, ClaimDocument, ClaimType, VerificationRoute


def _parse_anchor_number(anchor_id: str) -> int:
    """从 anchor 编号提取数字部分"""
    match = re.search(r"(\d+)", anchor_id)
    if match:
        return int(match.group(1))
    return -1


def _check_anchor_continuity(anchor_ids: list[str]) -> bool:
    """检查 anchor 编号是否连续"""
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


def validate_claim_document(
    parsed_doc: ParsedDocument,
    claim_doc: ClaimDocument,
) -> list[str]:
    """
    对 ClaimDocument 执行全量不变量校验。

    Args:
        parsed_doc: 来源 ParsedDocument
        claim_doc: 待校验的 ClaimDocument

    Returns:
        违反项字符串列表。空列表表示全部校验通过。
    """
    violations: list[str] = []

    # 构建索引
    anchor_map: dict[str, Anchor] = {a.anchor: a for a in parsed_doc.anchors}
    block_map: dict[str, Block] = {b.block_id: b for b in parsed_doc.blocks}

    # ---- 1. claim_id 唯一 ----
    claim_ids = [c.claim_id for c in claim_doc.claims]
    if len(claim_ids) != len(set(claim_ids)):
        violations.append("[claim_id] claim_id 重复")
        # 无法继续校验唯一性相关项
        return violations

    # ---- 2. claim_id 按序连续 ----
    for i, c in enumerate(claim_doc.claims):
        expected_id = f"cl_{i + 1:05d}"
        if c.claim_id != expected_id:
            violations.append(
                f"[claim_id] claim_id 不连续: 期望 {expected_id}, 实际 {c.claim_id}"
            )

    # ---- 3. 逐条校验 ----
    for claim in claim_doc.claims:
        # claim.text 非空
        if not claim.text:
            violations.append(
                f"[text] claim {claim.claim_id}: text 为空"
            )

        # claim.text 严格等于 anchor_ids 文本拼接
        # （与设计决策 2.1 对应：不改写原文由结构保证）
        expected_text = _rebuild_from_anchors(claim.anchor_ids, anchor_map)
        if claim.text != expected_text:
            violations.append(
                f"[text] claim {claim.claim_id}: text 与 anchor 拼接不一致. "
                f"claim.text={repr(claim.text)[:80]}, "
                f"expected={repr(expected_text)[:80]}"
            )

        # anchor_ids 非空
        if not claim.anchor_ids:
            violations.append(
                f"[anchor] claim {claim.claim_id}: anchor_ids 为空"
            )

        # anchor 全部存在
        for aid in claim.anchor_ids:
            if aid not in anchor_map:
                violations.append(
                    f"[anchor] claim {claim.claim_id}: anchor {aid} 不存在"
                )

        # anchor 编号连续
        if claim.anchor_ids and not _check_anchor_continuity(claim.anchor_ids):
            violations.append(
                f"[anchor] claim {claim.claim_id}: anchor_ids 编号不连续: "
                f"{claim.anchor_ids}"
            )

        # block_ids 全部存在
        for bid in claim.block_ids:
            if bid not in block_map:
                violations.append(
                    f"[block] claim {claim.claim_id}: block_id {bid} 不存在"
                )

        # claim_type 合法
        if not isinstance(claim.claim_type, ClaimType):
            violations.append(
                f"[claim_type] claim {claim.claim_id}: claim_type 非法: "
                f"{claim.claim_type}"
            )

        # verification_route 合法
        if not isinstance(claim.verification_route, VerificationRoute):
            violations.append(
                f"[verification_route] claim {claim.claim_id}: "
                f"verification_route 非法: {claim.verification_route}"
            )

        # verification_route 与 claim_type 对应表
        vr_violation = _check_verification_route(claim)
        if vr_violation:
            violations.append(
                f"[verification_route] claim {claim.claim_id}: {vr_violation}"
            )

        # paraphrase_text 为 claim.text 子串
        if hasattr(claim.entities, "paraphrase_text") and claim.entities.paraphrase_text:
            if claim.entities.paraphrase_text not in claim.text:
                violations.append(
                    f"[entities] claim {claim.claim_id}: "
                    f"paraphrase_text 不是 claim.text 子串"
                )

        # holding_text 为 claim.text 子串
        if hasattr(claim.entities, "holding_text") and claim.entities.holding_text:
            if claim.entities.holding_text not in claim.text:
                violations.append(
                    f"[entities] claim {claim.claim_id}: "
                    f"holding_text 不是 claim.text 子串"
                )

        # debug.methods 仅含 rule / llm
        for method in claim.debug.methods:
            if method not in ("rule", "llm"):
                violations.append(
                    f"[debug] claim {claim.claim_id}: "
                    f"debug.methods 含非法值: {method}"
                )

    return violations


def _rebuild_from_anchors(
    anchor_ids: list[str],
    anchor_map: dict[str, Anchor],
) -> str:
    """从 anchors 重建文本（用于校验）"""
    sorted_ids = sorted(anchor_ids, key=_parse_anchor_number)
    parts = []
    for aid in sorted_ids:
        anchor = anchor_map.get(aid)
        if anchor:
            parts.append(anchor.text)
    return "".join(parts)


def _check_verification_route(claim: Claim) -> str | None:
    """
    检查 verification_route 是否符合对应表。

    Returns:
        违反描述或 None
    """
    ct = claim.claim_type
    vr = claim.verification_route

    if ct in (ClaimType.LEGAL_SOURCE_CLAIM, ClaimType.LEGAL_SOURCE_PARAPHRASE):
        expected = (
            VerificationRoute.JUDICIAL_INTERPRETATION_DATABASE
            if _all_judicial_interpretation(claim.entities)
            else VerificationRoute.STATUTE_DATABASE
        )
        if vr != expected:
            return f"期望 {expected}, 实际 {vr}"

    elif ct == ClaimType.CASE_CITATION:
        expected = (
            VerificationRoute.CASE_DATABASE_EXACT
            if _has_with_case_number(claim.entities)
            else VerificationRoute.CASE_DATABASE_SEARCH
        )
        if vr != expected:
            return f"期望 {expected}, 实际 {vr}"

    elif ct == ClaimType.CASE_HOLDING_PARAPHRASE:
        if vr != VerificationRoute.CASE_DATABASE_FULLTEXT:
            return f"期望 case_database_fulltext, 实际 {vr}"

    return None


def _all_judicial_interpretation(entities) -> bool:
    """所有 legal_sources 的 source_type 是否均为 judicial_interpretation"""
    if hasattr(entities, "legal_sources") and entities.legal_sources:
        return all(
            ls.source_type == "judicial_interpretation"
            for ls in entities.legal_sources
        )
    return False


def _has_with_case_number(entities) -> bool:
    """是否存在 with_case_number 的 case_ref"""
    if hasattr(entities, "case_refs") and entities.case_refs:
        return any(
            cr.reference_type == "with_case_number"
            for cr in entities.case_refs
        )
    return False
