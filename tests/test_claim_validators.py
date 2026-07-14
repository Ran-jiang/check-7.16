"""
CCitecheck v0.2 Claim 校验器测试。

测试 validate_claim_document：
  19. 正常文档返回空列表；构造异常场景能检测
"""

from parser.schema import (
    Anchor, Block, BlockType, DocMeta, ParsedDocument,
)
from claims.arbiter import build_claim_document
from claims.schema import (
    Claim, ClaimDebug, ClaimType,
    ExtractionMethod, LegalSourceClaimEntities,
    LegalSource, LegalSourceType,
    VerificationRoute,
)
from claims.validators import validate_claim_document


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


def _make_valid_claim(anchor_ids: list[str], text: str, block_ids: list[str]) -> Claim:
    """构建合法的 Claim"""
    return Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=anchor_ids,
        block_ids=block_ids,
        verification_route=VerificationRoute.STATUTE_DATABASE,
        entities=LegalSourceClaimEntities(
            legal_sources=[
                LegalSource(
                    title="民法典",
                    source_type=LegalSourceType.LAW,
                    articles=[],
                )
            ]
        ),
        debug=ClaimDebug(
            methods=[ExtractionMethod.RULE],
            candidate_count=1,
            text_mismatch=False,
        ),
    )


# ============================================================
# Test 19a: 正常文档 → 返回空列表
# ============================================================

def test_valid_claim_document_passes():
    """正常文档校验通过，返回空列表"""
    text = "依据《民法典》第五百七十七条，被告应当承担违约责任。"
    doc = _make_parsed_doc([text])

    claim = Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=["line00001"],
        block_ids=["b_00001"],
        verification_route=VerificationRoute.STATUTE_DATABASE,
        entities=LegalSourceClaimEntities(
            legal_sources=[
                LegalSource(
                    title="民法典",
                    source_type=LegalSourceType.LAW,
                    articles=[],
                )
            ]
        ),
        debug=ClaimDebug(
            methods=[ExtractionMethod.RULE],
            candidate_count=1,
            text_mismatch=False,
        ),
    )

    from claims.arbiter import build_claim_document
    claim_doc = build_claim_document(doc, [claim])

    violations = validate_claim_document(doc, claim_doc)
    assert len(violations) == 0, f"Expected 0 violations, got: {violations}"


# ============================================================
# Test 19b: anchor 不存在 → 检测到
# ============================================================

def test_detect_nonexistent_anchor():
    """anchor 不存在 → 校验发现"""
    text = "测试文本。"
    doc = _make_parsed_doc([text])

    claim = Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=["line99999"],  # 不存在
        block_ids=["b_00001"],
        verification_route=VerificationRoute.STATUTE_DATABASE,
        entities=LegalSourceClaimEntities(legal_sources=[]),
        debug=ClaimDebug(),
    )

    claim_doc = build_claim_document(doc, [claim])
    violations = validate_claim_document(doc, claim_doc)
    assert len(violations) > 0
    assert any("不存在" in v for v in violations)


# ============================================================
# Test 19c: text 与拼接不符 → 检测到
# ============================================================

def test_detect_text_mismatch():
    """claim.text 与 anchor 拼接不一致 → 校验发现"""
    original_text = "原文内容。"
    doc = _make_parsed_doc([original_text])

    claim = Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text="被篡改的文本",  # 与原文不一致
        anchor_ids=["line00001"],
        block_ids=["b_00001"],
        verification_route=VerificationRoute.STATUTE_DATABASE,
        entities=LegalSourceClaimEntities(legal_sources=[]),
        debug=ClaimDebug(),
    )

    claim_doc = build_claim_document(doc, [claim])
    violations = validate_claim_document(doc, claim_doc)
    assert len(violations) > 0
    assert any("text" in v.lower() for v in violations)


# ============================================================
# Test 19d: anchor 不连续 → 检测到
# ============================================================

def test_detect_non_contiguous_anchors():
    """anchor_ids 不连续 → 校验发现"""
    texts = ["第一句。", "第二句。", "第三句。"]
    doc = _make_parsed_doc(texts)

    claim = Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=texts[0] + texts[2],  # 跳过了第二句
        anchor_ids=["line00001", "line00003"],  # 不连续
        block_ids=["b_00001", "b_00003"],
        verification_route=VerificationRoute.STATUTE_DATABASE,
        entities=LegalSourceClaimEntities(legal_sources=[]),
        debug=ClaimDebug(),
    )

    claim_doc = build_claim_document(doc, [claim])
    violations = validate_claim_document(doc, claim_doc)
    assert len(violations) > 0
    assert any("连续" in v or "不连续" in v for v in violations)


# ============================================================
# Test 19e: claim_id 重复 → 检测到
# ============================================================

def test_detect_duplicate_claim_id():
    """claim_id 重复 → 校验发现"""
    text = "测试文本。"
    doc = _make_parsed_doc([text])

    def make_claim(cid):
        return Claim(
            claim_id=cid,
            claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
            text=text,
            anchor_ids=["line00001"],
            block_ids=["b_00001"],
            verification_route=VerificationRoute.STATUTE_DATABASE,
            entities=LegalSourceClaimEntities(legal_sources=[]),
            debug=ClaimDebug(),
        )

    claim_doc = build_claim_document(doc, [
        make_claim("cl_00001"),
        make_claim("cl_00001"),  # 重复
    ])
    violations = validate_claim_document(doc, claim_doc)
    assert len(violations) > 0
    assert any("重复" in v for v in violations)


# ============================================================
# Test 19f: verification_route 不匹配 → 检测到
# ============================================================

def test_detect_wrong_verification_route():
    """verification_route 与对应表不符 → 校验发现"""
    text = "依据《民法典》第五百七十七条。"
    doc = _make_parsed_doc([text])

    claim = Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=["line00001"],
        block_ids=["b_00001"],
        verification_route=VerificationRoute.CASE_DATABASE_EXACT,  # 错误！
        entities=LegalSourceClaimEntities(
            legal_sources=[
                LegalSource(
                    title="民法典",
                    source_type=LegalSourceType.LAW,
                    articles=[],
                )
            ]
        ),
        debug=ClaimDebug(),
    )

    claim_doc = build_claim_document(doc, [claim])
    violations = validate_claim_document(doc, claim_doc)
    assert len(violations) > 0
    assert any("verification_route" in v.lower() for v in violations)


# ============================================================
# Test 19h: debug.methods 非法值 → 检测到
# ============================================================

def test_detect_invalid_debug_methods():
    """debug.methods 含非法值 → 校验发现"""
    text = "测试。"
    doc = _make_parsed_doc([text])

    claim = Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=["line00001"],
        block_ids=["b_00001"],
        verification_route=VerificationRoute.STATUTE_DATABASE,
        entities=LegalSourceClaimEntities(legal_sources=[]),
        debug=ClaimDebug(
            methods=["invalid_method"],  # 非法
            candidate_count=1,
            text_mismatch=False,
        ),
    )

    claim_doc = build_claim_document(doc, [claim])
    violations = validate_claim_document(doc, claim_doc)
    assert len(violations) > 0
    assert any("methods" in v.lower() or "debug" in v.lower() for v in violations)
