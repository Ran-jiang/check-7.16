"""
CCitecheck v0.2 规则抽取器。

基于 anchors 逐句扫描，只识别格式明确、低误报的形态。
候选的 anchor_ids 为单句——规则引擎不做跨句，跨句召回交给 LLM。

分工边界：
  - 规则引擎：单句、高精度、基于正则模式
  - LLM 抽取器：跨句、复杂表述、补召回
  - 所有候选必须经过 Claim Arbiter 裁决

规则抽取流程：
  1. 遍历每个 anchor
  2. 检测法源引用 → legal_source_claim 或 legal_source_paraphrase
  3. 检测案例引用 → case_citation 或 case_holding_paraphrase
  4. 同一句只产出一个候选（多法源合并为一个候选）
"""

from __future__ import annotations

import logging

from parser.schema import Anchor, ParsedDocument

from .case_citation import (
    extract_case_refs,
    find_holding_trigger_position,
    has_holding_trigger,
)
from .legal_citation import (
    extract_legal_sources,
    find_paraphrase_trigger_position,
    has_article_reference,
    extract_articles_only,
)
from .schema import (
    CaseCitationEntities,
    CaseHoldingParaphraseEntities,
    ClaimCandidate,
    ClaimType,
    ExtractionMethod,
    LegalSourceClaimEntities,
    LegalSourceParaphraseEntities,
)

logger = logging.getLogger(__name__)


def extract_rule_candidates(
    parsed_doc: ParsedDocument,
    indexes: dict,
) -> list[ClaimCandidate]:
    """
    基于 anchors 逐句扫描，生成规则候选列表。

    规则引擎只做单句（跨句是 LLM 的职责）。
    每个 anchor 独立分析，同一句只产出一个候选。

    法源前向继承（承前省略法源名的指代消解）：
      判决书写作规范中，"第X条"承前省略法源名是标准文体。
      若当前 anchor 有条款号但无《》法源名，向上查找最近的法源 anchor，
      将其 title 填入当前 claim 的 entities.legal_sources，标记 resolution="inherited"。

      关键约束：
        - 只继承实体（法源名），不连接 anchor_ids
        - claim.anchor_ids 永远只是当前句，不包含法源来源句
        - section_path 变化时重置继承状态（跨标题不继承）

    Args:
        parsed_doc: 已解析的文档
        indexes: 预构建的索引字典

    Returns:
        ClaimCandidate 列表
    """
    anchor_map: dict[str, Anchor] = indexes["anchor_map"]
    candidates: list[ClaimCandidate] = []

    # ---- 法源前向继承状态 ----
    last_legal_sources: list | None = None
    last_source_anchor_id: str | None = None
    last_section_path: tuple | None = None  # 用于检测标题切换

    for anchor in parsed_doc.anchors:
        text = anchor.text
        current_section = tuple(anchor_map[anchor.anchor].block_id if anchor.anchor in anchor_map else ())

        # ---- section_path 变化 → 重置继承 ----
        # 跨标题不继承：新标题下出现裸"第X条"不回溯旧标题的法源
        section_path = _get_section_path(anchor, parsed_doc)
        if last_section_path is not None and section_path != last_section_path:
            last_legal_sources = None
            last_source_anchor_id = None
        last_section_path = section_path

        # 1. 检测法源引用（含《》书名号）
        legal_sources = extract_legal_sources(text)

        if legal_sources:
            # 更新继承状态（供后续 anchor 使用）
            last_legal_sources = legal_sources
            last_source_anchor_id = anchor.anchor

            candidate = _make_legal_candidate(
                anchor.anchor, text, legal_sources
            )
            if candidate:
                candidates.append(candidate)
                continue

        # 1b. 法源前向继承：当前句有条款号但无《》法源名
        if not legal_sources and last_legal_sources and has_article_reference(text):
            articles = extract_articles_only(text)
            if articles:
                # 只继承实体（法源名），不连接 anchor
                # 每个"第X条"句是一个独立 claim
                inherited_sources = _build_inherited_sources(
                    last_legal_sources, articles,
                    last_source_anchor_id or "",
                )
                candidate = _make_legal_candidate(
                    anchor.anchor, text, inherited_sources
                )
                if candidate:
                    # anchor_ids 永远只是当前句，不包含法源来源句
                    candidate.anchor_ids = [anchor.anchor]
                    candidates.append(candidate)
                    continue

        # 2. 检测案例引用
        case_refs = extract_case_refs(text)

        if case_refs:
            # 检测是否有观点触发词 → case_holding_paraphrase
            if has_holding_trigger(text, case_refs):
                holding_pos = find_holding_trigger_position(text, case_refs)
                holding_text = text[holding_pos:] if holding_pos is not None and holding_pos < len(text) else ""

                candidate = ClaimCandidate(
                    claim_type=ClaimType.CASE_HOLDING_PARAPHRASE,
                    anchor_ids=[anchor.anchor],
                    entities=CaseHoldingParaphraseEntities(
                        case_refs=case_refs,
                        holding_text=holding_text,
                    ),
                    method=ExtractionMethod.RULE,
                )
                candidates.append(candidate)
            else:
                # 纯案例引用
                candidate = ClaimCandidate(
                    claim_type=ClaimType.CASE_CITATION,
                    anchor_ids=[anchor.anchor],
                    entities=CaseCitationEntities(
                        case_refs=case_refs,
                    ),
                    method=ExtractionMethod.RULE,
                )
                candidates.append(candidate)

    return candidates


def _get_section_path(anchor, parsed_doc: ParsedDocument) -> tuple:
    """
    获取 anchor 所属 block 的 section_path（用于检测标题切换）。

    Args:
        anchor: Anchor 对象
        parsed_doc: 已解析的文档

    Returns:
        section_path 元组（可哈希，用于比较）
    """
    for block in parsed_doc.blocks:
        if block.block_id == anchor.block_id:
            return tuple(block.section_path)
    return ()


def _build_inherited_sources(
    last_sources: list,
    current_articles: list,
    source_anchor_id: str,
) -> list:
    """
    从上一个 anchor 的法源列表构建继承法源。

    只继承法源名和 source_type，条款号使用当前句的。
    标记 resolution="inherited" + inherited_from_anchor 供溯源。

    Args:
        last_sources: 上一个 anchor 的 LegalSource 列表
        current_articles: 当前句提取的 ArticleRef 列表
        source_anchor_id: 法源名所在的 anchor 编号

    Returns:
        新的 LegalSource 列表
    """
    from .schema import LegalSource, ArticleRef
    inherited = []
    for ls in last_sources:
        copied_articles = [
            ArticleRef(article=a.article, paragraphs=list(a.paragraphs), items=list(a.items))
            for a in current_articles
        ]
        inherited.append(LegalSource(
            title=ls.title,
            source_type=ls.source_type,
            articles=copied_articles,
            resolution="inherited",
            inherited_from_anchor=source_anchor_id,
        ))
    return inherited


def _make_legal_candidate(
    anchor_id: str,
    text: str,
    legal_sources: list,
) -> ClaimCandidate | None:
    """
    根据法源引用生成候选。

    类型判定（tie-break）：
      1. 若某法源的条款号之后紧跟转述触发词（规定/明确/指出/载明）
         且触发词后仍有实体内容 → legal_source_paraphrase
      2. 否则 → legal_source_claim
         （包括含"依据/根据/依照"引导的句子，以及无引导词但含法源的句子）

    Args:
        anchor_id: 当前 anchor 编号
        text: anchor 文本
        legal_sources: 已提取的法源列表

    Returns:
        ClaimCandidate 或 None
    """
    if not legal_sources:
        return None

    # 检查是否有转述触发
    paraphrase_pos = find_paraphrase_trigger_position(text, legal_sources)
    if paraphrase_pos is not None:
        paraphrase_text = text[paraphrase_pos:]
        return ClaimCandidate(
            claim_type=ClaimType.LEGAL_SOURCE_PARAPHRASE,
            anchor_ids=[anchor_id],
            entities=LegalSourceParaphraseEntities(
                legal_sources=legal_sources,
                paraphrase_text=paraphrase_text,
            ),
            method=ExtractionMethod.RULE,
        )

    # 默认 → legal_source_claim
    return ClaimCandidate(
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        anchor_ids=[anchor_id],
        entities=LegalSourceClaimEntities(
            legal_sources=legal_sources,
        ),
        method=ExtractionMethod.RULE,
    )
