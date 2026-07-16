"""基于 anchor 内容和显式 Block 关系的确定性引用抽取。"""

from __future__ import annotations

from ..domain.citation import (
    CaseCitationEntities,
    CaseHoldingParaphraseEntities,
    ClaimCandidate,
    ClaimType,
    LegalSourceClaimEntities,
)
from ..domain.document import BlockRelationType, BlockType, ParsedDocument
from ..parsing.relations import build_block_relations
from .cases import extract_case_refs, find_holding_trigger_position, has_holding_trigger
from .statutes import extract_articles_only, extract_legal_sources, has_article_reference


_RELATION_PRIORITY = {
    BlockRelationType.TABLE_LEFT: 0,
    BlockRelationType.LIST_LEAD: 1,
    BlockRelationType.PREVIOUS_BLOCK: 2,
    BlockRelationType.TABLE_ABOVE: 3,
}


def extract_rule_candidates(
    parsed_doc: ParsedDocument,
    indexes: dict,
    include_statutes: bool = True,
    include_cases: bool = True,
) -> list[ClaimCandidate]:
    """逐句生成候选；裸条款只沿显式 Block 关系承前。"""
    build_block_relations(parsed_doc)
    candidates: list[ClaimCandidate] = []
    block_map = indexes.get("block_map", {})
    anchor_sources = {
        anchor.anchor: extract_legal_sources(anchor.text) if include_statutes else []
        for anchor in parsed_doc.anchors
    }
    records: dict[str, list[tuple[str, list]]] = {}
    for anchor in parsed_doc.anchors:
        if anchor_sources[anchor.anchor]:
            records.setdefault(anchor.block_id, []).append(
                (anchor.anchor, anchor_sources[anchor.anchor])
            )
    deferred_table_sources: dict[str, ClaimCandidate] = {}
    consumed_table_sources: set[str] = set()

    for anchor in parsed_doc.anchors:
        text = anchor.text
        current_block = block_map.get(anchor.block_id)
        legal_sources = anchor_sources[anchor.anchor]

        if legal_sources:
            candidate = _make_legal_candidate(anchor.anchor, legal_sources)
            if candidate:
                if (
                    current_block
                    and current_block.type == BlockType.TABLE_CELL
                    and all(not source.articles for source in legal_sources)
                ):
                    deferred_table_sources[anchor.anchor] = candidate
                else:
                    candidates.append(candidate)

        if not legal_sources and current_block and has_article_reference(text):
            articles = extract_articles_only(text)
            resolved = _resolve_block_source(current_block, block_map, records)
            if articles and resolved:
                source_anchor_id, source_block, sources = resolved
                inherited_sources = _build_inherited_sources(
                    sources, articles, source_anchor_id
                )
                candidate = _make_legal_candidate(anchor.anchor, inherited_sources)
                if candidate:
                    if source_block.type == BlockType.TABLE_CELL:
                        candidate.anchor_ids = [source_anchor_id, anchor.anchor]
                        consumed_table_sources.add(source_anchor_id)
                    candidates.append(candidate)

        case_refs = extract_case_refs(text) if include_cases else []
        if case_refs:
            if has_holding_trigger(text, case_refs):
                holding_pos = find_holding_trigger_position(text, case_refs)
                holding_text = (
                    text[holding_pos:]
                    if holding_pos is not None and holding_pos < len(text)
                    else ""
                )
                candidates.append(ClaimCandidate(
                    claim_type=ClaimType.CASE_HOLDING_PARAPHRASE,
                    anchor_ids=[anchor.anchor],
                    entities=CaseHoldingParaphraseEntities(
                        case_refs=case_refs, holding_text=holding_text
                    ),
                ))
            else:
                candidates.append(ClaimCandidate(
                    claim_type=ClaimType.CASE_CITATION,
                    anchor_ids=[anchor.anchor],
                    entities=CaseCitationEntities(case_refs=case_refs),
                ))

    candidates.extend(
        candidate for anchor_id, candidate in deferred_table_sources.items()
        if anchor_id not in consumed_table_sources
    )
    return candidates


def _resolve_block_source(block, block_map: dict, records: dict, visited=None):
    """沿 Block 关系解析唯一法源；说明块和歧义块终止链路。"""
    visited = set(visited or ())
    if block.block_id in visited:
        return None
    visited.add(block.block_id)
    relations = sorted(
        block.relations,
        key=lambda item: _RELATION_PRIORITY.get(item.relation_type, 99),
    )
    for relation in relations:
        target = block_map.get(relation.target_block_id)
        if not target:
            continue
        source_records = records.get(target.block_id, [])
        if source_records:
            flattened = [source for _, sources in source_records for source in sources]
            if len({source.title for source in flattened}) != 1:
                return None
            return source_records[0][0], target, [flattened[0]]
        # 只有纯条款 block 可继续承接；普通说明段立即截断。
        if has_article_reference(target.text):
            resolved = _resolve_block_source(target, block_map, records, visited)
            if resolved:
                return resolved
        if relation.relation_type != BlockRelationType.TABLE_ABOVE:
            return None
    return None


def _build_inherited_sources(
    source_list: list,
    current_articles: list,
    source_anchor_id: str,
) -> list:
    """使用关系目标的唯一法源和当前条款构建继承实体。"""
    from ..domain.citation import ArticleRef, LegalSource

    inherited = []
    for source in source_list:
        copied_articles = [
            ArticleRef(
                article=article.article,
                paragraphs=list(article.paragraphs),
                items=list(article.items),
            )
            for article in current_articles
        ]
        inherited.append(LegalSource(
            title=source.title,
            source_type=source.source_type,
            articles=copied_articles,
            resolution="inherited",
            inherited_from_anchor=source_anchor_id,
        ))
    return inherited


def _make_legal_candidate(
    anchor_id: str,
    legal_sources: list,
) -> ClaimCandidate | None:
    if not legal_sources:
        return None
    return ClaimCandidate(
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        anchor_ids=[anchor_id],
        entities=LegalSourceClaimEntities(legal_sources=legal_sources),
    )
