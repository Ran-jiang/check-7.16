"""
CCitecheck v0.2 Claim Arbiter 测试。

测试：
  15. 同位置 rule+llm 候选 → 合并，methods=["rule","llm"]
  16. 子集候选 → 保留完整主张
  17. 不同 anchor、相同文本 → 不合并
"""

from parser.schema import (
    Anchor, Block, BlockType, DocMeta, ParsedDocument,
)
from claims.arbiter import (
    arbitrate_claim_candidates,
    build_claim_document,
    _rebuild_text,
    _check_anchor_continuity,
    _derive_verification_route,
)
from claims.schema import (
    Claim, ClaimCandidate, ClaimDebug, ClaimType,
    ExtractionMethod, LegalSourceClaimEntities,
    CaseCitationEntities, CaseRef, CaseReferenceType,
    VerificationRoute, LegalSource, LegalSourceType,
)


# ============================================================
# 辅助函数
# ============================================================

def _make_parsed_doc(texts: list[str]) -> ParsedDocument:
    """用文本列表构建 ParsedDocument"""
    blocks = []
    anchors = []
    for i, text in enumerate(texts):
        block_id = f"b_{i+1:05d}"
        anchor_id = f"line{i+1:05d}"

        block = Block(
            block_id=block_id,
            type=BlockType.PARAGRAPH,
            text=text,
            style=None,
            section_path=[],
            body_order=i,
            block_order=i,
            para_index=i,
            table_index=None,
            row_index=None,
            cell_index=None,
            has_numbering=False,
            numbering_text=None,
            numbering_unresolved=False,
            is_list_item=False,
            list_group_id=None,
            is_article_start=False,
            heading_source=None,
            anchor_range=[anchor_id, anchor_id],
            sentence_anchors=[anchor_id],
        )
        blocks.append(block)

        anchor = Anchor(
            anchor=anchor_id,
            text=text,
            block_id=block_id,
            para_index=i,
            char_start=0,
            char_end=len(text),
        )
        anchors.append(anchor)

    return ParsedDocument(
        doc_meta=DocMeta(
            schema_version="0.1",
            source_file="test.docx",
            doc_hash="sha256:test",
        ),
        blocks=blocks,
        anchors=anchors,
        chunks=[],
    )


def _make_rule_candidate(
    claim_type: ClaimType,
    anchor_ids: list[str],
    entities=None,
) -> ClaimCandidate:
    """快捷构建规则候选"""
    if entities is None:
        entities = LegalSourceClaimEntities()
    return ClaimCandidate(
        claim_type=claim_type,
        anchor_ids=anchor_ids,
        entities=entities,
        method=ExtractionMethod.RULE,
    )


def _make_llm_candidate(
    claim_type: ClaimType,
    anchor_ids: list[str],
    entities=None,
    llm_text: str | None = None,
) -> ClaimCandidate:
    """快捷构建 LLM 候选"""
    if entities is None:
        entities = LegalSourceClaimEntities()
    return ClaimCandidate(
        claim_type=claim_type,
        anchor_ids=anchor_ids,
        entities=entities,
        method=ExtractionMethod.LLM,
        llm_text=llm_text,
    )


# ============================================================
# Test 15: 同位置 rule+llm → 合并
# ============================================================

def test_merge_same_position():
    """同位置 rule+llm 候选 → 合并，methods=["rule","llm"]"""
    text = "依据《民法典》第五百七十七条，被告应当承担违约责任。"
    doc = _make_parsed_doc([text])

    entities_rule = LegalSourceClaimEntities(
        legal_sources=[
            LegalSource(
                title="民法典",
                source_type=LegalSourceType.LAW,
                articles=[],
            )
        ]
    )
    entities_llm = LegalSourceClaimEntities(
        legal_sources=[
            LegalSource(
                title="中华人民共和国民法典",
                source_type=LegalSourceType.LAW,
                articles=[],
            )
        ]
    )

    candidates = [
        _make_rule_candidate(ClaimType.LEGAL_SOURCE_CLAIM, ["line00001"], entities_rule),
        _make_llm_candidate(ClaimType.LEGAL_SOURCE_CLAIM, ["line00001"], entities_llm),
    ]

    claims = arbitrate_claim_candidates(candidates, doc)

    assert len(claims) == 1
    claim = claims[0]
    # 方法应合并
    assert ExtractionMethod.RULE in claim.debug.methods
    assert ExtractionMethod.LLM in claim.debug.methods
    assert claim.debug.candidate_count == 2
    # text 应从 anchors 重建
    assert claim.text == text


# ============================================================
# Test 16: 子集候选 → 保留完整主张
# ============================================================

def test_completeness_ruling():
    """子集候选（单句 vs 完整多句）→ 保留更长的完整主张"""
    texts = [
        "依据《民法典》第五百七十七条，",
        "被告应当承担违约责任。",
    ]
    doc = _make_parsed_doc(texts)

    # 候选A：只有法条号句（子集）
    cand_a = _make_rule_candidate(
        ClaimType.LEGAL_SOURCE_CLAIM,
        ["line00001"],
        LegalSourceClaimEntities(
            legal_sources=[
                LegalSource(
                    title="民法典",
                    source_type=LegalSourceType.LAW,
                    articles=[],
                )
            ]
        ),
    )

    # 候选B：完整两句（包含法条号句和法律判断句）
    cand_b = _make_rule_candidate(
        ClaimType.LEGAL_SOURCE_CLAIM,
        ["line00001", "line00002"],
        LegalSourceClaimEntities(
            legal_sources=[
                LegalSource(
                    title="民法典",
                    source_type=LegalSourceType.LAW,
                    articles=[],
                )
            ]
        ),
    )

    claims = arbitrate_claim_candidates([cand_a, cand_b], doc)

    # 应保留更完整的 B
    assert len(claims) == 1
    claim = claims[0]
    assert claim.anchor_ids == ["line00001", "line00002"]
    assert claim.text == texts[0] + texts[1]


# ============================================================
# Test 17: 不同 anchor、相同文本 → 不合并
# ============================================================

def test_no_merge_different_position():
    """不同 anchor、相同文本 → 不合并，各自保留"""
    texts = [
        "依据《民法典》第五百七十七条，被告应当承担违约责任。",
        "依据《民法典》第五百七十七条，被告应当承担违约责任。",
    ]
    doc = _make_parsed_doc(texts)

    entities = LegalSourceClaimEntities(
        legal_sources=[
            LegalSource(
                title="民法典",
                source_type=LegalSourceType.LAW,
                articles=[],
            )
        ]
    )
    candidates = [
        _make_rule_candidate(ClaimType.LEGAL_SOURCE_CLAIM, ["line00001"], entities),
        _make_rule_candidate(ClaimType.LEGAL_SOURCE_CLAIM, ["line00002"], entities),
    ]

    claims = arbitrate_claim_candidates(candidates, doc)

    # 不同位置 → 各自保留
    assert len(claims) == 2
    anchor_sets = [set(c.anchor_ids) for c in claims]
    assert {"line00001"} in anchor_sets
    assert {"line00002"} in anchor_sets


# ============================================================
# Test: 重建 text
# ============================================================

def test_rebuild_text():
    """验证 text 重建正确"""
    texts = [
        "第一句。",
        "第二句；",
        "第三句。",
    ]
    doc = _make_parsed_doc(texts)
    anchor_map = {a.anchor: a for a in doc.anchors}

    rebuilt = _rebuild_text(["line00001", "line00002", "line00003"], anchor_map)
    assert rebuilt == "第一句。第二句；第三句。"


# ============================================================
# Test: anchor 连续性
# ============================================================

def test_anchor_continuity():
    """验证 anchor 连续性检测"""
    assert _check_anchor_continuity(["line00001", "line00002", "line00003"])
    assert not _check_anchor_continuity(["line00001", "line00003"])
    assert not _check_anchor_continuity([])
    assert _check_anchor_continuity(["line00099", "line00100", "line00101"])


# ============================================================
# Test: verification_route 推导
# ============================================================

def test_derive_verification_route_legal_source():
    """法源类 claim 的 verification_route"""
    # 全部为 judicial_interpretation → judicial_interpretation_database
    entities = LegalSourceClaimEntities(
        legal_sources=[
            LegalSource(
                title="最高人民法院关于适用民法典的解释",
                source_type=LegalSourceType.JUDICIAL_INTERPRETATION,
                articles=[],
            )
        ]
    )
    route = _derive_verification_route(ClaimType.LEGAL_SOURCE_CLAIM, entities)
    assert route == VerificationRoute.JUDICIAL_INTERPRETATION_DATABASE

    # 混合法源 → statute_database
    entities2 = LegalSourceClaimEntities(
        legal_sources=[
            LegalSource(
                title="民法典",
                source_type=LegalSourceType.LAW,
                articles=[],
            )
        ]
    )
    route2 = _derive_verification_route(ClaimType.LEGAL_SOURCE_CLAIM, entities2)
    assert route2 == VerificationRoute.STATUTE_DATABASE


def test_derive_verification_route_case():
    """案例类 claim 的 verification_route"""
    # with_case_number → case_database_exact
    entities = CaseCitationEntities(
        case_refs=[
            CaseRef(
                reference_type=CaseReferenceType.WITH_CASE_NUMBER,
                case_number="（2021）最高法民申1234号",
            )
        ]
    )
    route = _derive_verification_route(ClaimType.CASE_CITATION, entities)
    assert route == VerificationRoute.CASE_DATABASE_EXACT

    # without_case_number → case_database_search
    entities2 = CaseCitationEntities(
        case_refs=[
            CaseRef(
                reference_type=CaseReferenceType.WITHOUT_CASE_NUMBER,
                case_name="指导案例第24号",
            )
        ]
    )
    route2 = _derive_verification_route(ClaimType.CASE_CITATION, entities2)
    assert route2 == VerificationRoute.CASE_DATABASE_SEARCH


# ============================================================
# Test: text_mismatch 标记
# ============================================================

def test_text_mismatch_flag():
    """LLM text 与重建文本不一致时标记 text_mismatch"""
    text = "依据《民法典》，被告应当承担违约责任。"
    doc = _make_parsed_doc([text])

    llm_candidate = _make_llm_candidate(
        ClaimType.LEGAL_SOURCE_CLAIM,
        ["line00001"],
        LegalSourceClaimEntities(
            legal_sources=[
                LegalSource(
                    title="民法典",
                    source_type=LegalSourceType.LAW,
                    articles=[],
                )
            ]
        ),
        llm_text="LLM 改写了这句话",  # 与原文不一致
    )

    claims = arbitrate_claim_candidates([llm_candidate], doc)

    assert len(claims) == 1
    claim = claims[0]
    assert claim.text == text  # 最终 text 是重建文本
    assert claim.debug.text_mismatch is True


# ============================================================
# Test: build_claim_document
# ============================================================

def test_build_claim_document():
    """测试 ClaimDocument 构建"""
    doc = _make_parsed_doc([
        "依据《民法典》第五百七十七条，被告应当承担违约责任。"
    ])

    claim = Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text="依据《民法典》第五百七十七条，被告应当承担违约责任。",
        anchor_ids=["line00001"],
        block_ids=["b_00001"],
        verification_route=VerificationRoute.STATUTE_DATABASE,
        entities=LegalSourceClaimEntities(),
        debug=ClaimDebug(
            methods=[ExtractionMethod.RULE],
            candidate_count=1,
            text_mismatch=False,
        ),
    )

    claim_doc = build_claim_document(
        doc, [claim],
        llm_used=True,
        llm_chunk_failures=["c_00002"],
    )

    assert claim_doc.claim_meta.schema_version == "0.2"
    assert claim_doc.claim_meta.llm_used is True
    assert claim_doc.claim_meta.llm_chunk_failures == ["c_00002"]
    assert len(claim_doc.claims) == 1
    assert claim_doc.claims[0].claim_id == "cl_00001"
