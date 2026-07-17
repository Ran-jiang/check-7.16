"""
CCiteheck 规则识别测试。

测试规则抽取器的核心功能：
  1. 单法源完整主张 → legal_source_claim
  2. 多法源一句 → 一个 claim，legal_sources 含两个法源
  3. 法条转述并入 legal_source_claim（不再区分）
  4. 法条+法律判断 → 仍为 legal_source_claim
  5. 三种括号案号 → with_case_number
  6. 指导案例 → without_case_number
  7. 案号+"认为" → case_holding_paraphrase
  8. 仅"本院认为"无 case_ref → 不抽取
  9. "本案""该案" → 不抽取
  10. 非法律规范文件 → 不识别为法源
  11. source_type 推断
"""

import pytest
from ccitecheck.domain.document import (
    Anchor, Block, BlockType, DocMeta, ParsedDocument,
)
from ccitecheck.recognition.rules import extract_rule_candidates
from ccitecheck.domain.citation import (
    ClaimType, LegalSourceType,
    CaseReferenceType,
)
from ccitecheck.recognition.service import build_indexes
from ccitecheck.recognition.statutes import (
    extract_legal_sources, infer_source_type,
)


# ============================================================
# 辅助函数：构建测试用的 ParsedDocument
# ============================================================

def _make_parsed_doc(texts: list[str]) -> ParsedDocument:
    """用文本列表构建 ParsedDocument（每个文本一个 anchor/block）"""
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


def _make_indexes(parsed_doc: ParsedDocument) -> dict:
    """构建索引"""
    return build_indexes(parsed_doc)


# ============================================================
# Test 1: 单法源完整主张 → legal_source_claim
# ============================================================

def test_single_legal_source_claim():
    """单法源完整主张 → legal_source_claim，claim.text 为整句"""
    text = "依据《中华人民共和国劳动合同法》第三十七条，劳动者可以解除劳动合同。"
    doc = _make_parsed_doc([text])
    indexes = _make_indexes(doc)

    candidates = extract_rule_candidates(doc, indexes)

    assert len(candidates) == 1
    c = candidates[0]
    assert c.claim_type == ClaimType.LEGAL_SOURCE_CLAIM
    assert c.anchor_ids == ["line00001"]
    # 验证法源内容
    legal_sources = c.entities.legal_sources
    assert len(legal_sources) == 1
    assert legal_sources[0].title == "中华人民共和国劳动合同法"
    assert legal_sources[0].source_type == LegalSourceType.LAW
    assert len(legal_sources[0].articles) == 1
    assert legal_sources[0].articles[0].article == "第三十七条"


# ============================================================
# Test 2: 多法源一句 → 一个 claim
# ============================================================

def test_multiple_legal_sources():
    """多法源一句话 → 一个 claim，legal_sources 含两个法源"""
    text = "依据《民法典》第五百七十七条、《民事诉讼法》第一百四十七条及相关司法解释，被告应当承担违约责任。"
    doc = _make_parsed_doc([text])
    indexes = _make_indexes(doc)

    candidates = extract_rule_candidates(doc, indexes)

    assert len(candidates) == 1
    c = candidates[0]
    assert c.claim_type == ClaimType.LEGAL_SOURCE_CLAIM
    legal_sources = c.entities.legal_sources
    assert len(legal_sources) == 2
    # 验证两个法源都存在
    titles = [ls.title for ls in legal_sources]
    assert "民法典" in titles
    assert "民事诉讼法" in titles


def test_multiple_articles_keep_law_ownership_and_paragraph_grouping():
    text = (
        "综上所述，依照《中华人民共和国商标法》第十三条第一款、第三款，"
        "《最高人民法院关于审理涉及驰名商标保护的民事纠纷案件应用法律若干问题的解释》"
        "第九条、第十条，《最高人民法院关于审理商标民事纠纷案件适用法律若干问题的解释》"
        "第八条规定，判决如下："
    )
    sources = extract_legal_sources(text)
    assert [source.title for source in sources] == [
        "中华人民共和国商标法",
        "最高人民法院关于审理涉及驰名商标保护的民事纠纷案件应用法律若干问题的解释",
        "最高人民法院关于审理商标民事纠纷案件适用法律若干问题的解释",
    ]
    assert [(article.article, article.paragraphs) for article in sources[0].articles] == [
        ("第十三条", ["第一款", "第三款"]),
    ]
    assert [article.article for article in sources[1].articles] == ["第九条", "第十条"]
    assert [article.article for article in sources[2].articles] == ["第八条"]


def test_nested_bare_law_does_not_attach_to_explicit_source():
    text = (
        "《最高人民法院关于审理涉及驰名商标保护的民事纠纷案件应用法律若干问题的解释》"
        "第九条规定，属于商标法第十三条第二款规定的容易导致混淆。"
    )
    sources = extract_legal_sources(text)
    assert [(source.title, [article.article for article in source.articles]) for source in sources] == [
        ("最高人民法院关于审理涉及驰名商标保护的民事纠纷案件应用法律若干问题的解释", ["第九条"]),
        ("商标法", ["第十三条"]),
    ]
    assert sources[1].articles[0].paragraphs == ["第二款"]


# ============================================================
# Test 3: 法条转述并入 legal_source_claim（不再区分逐字/转述）
# ============================================================

def test_paraphrase_style_citation_is_legal_source_claim():
    """法条号后跟'规定'的转述式引用，同样识别为 legal_source_claim"""
    doc = _make_parsed_doc(["《商标法》第四十八条规定，商标的使用是指将商标用于商品。"])
    candidates = extract_rule_candidates(doc, _make_indexes(doc))
    assert len(candidates) == 1
    c = candidates[0]
    assert c.claim_type == ClaimType.LEGAL_SOURCE_CLAIM
    assert c.entities.legal_sources[0].title == "商标法"


# ============================================================
# Test 4: 法条+法律判断 → 仍为 legal_source_claim
# ============================================================

def test_legal_source_with_judgment():
    """"依据《X法》第X条，被告应当承担违约责任。" → legal_source_claim"""
    text = "依据《民法典》第五百七十七条，被告应当承担违约责任。"
    doc = _make_parsed_doc([text])
    indexes = _make_indexes(doc)

    candidates = extract_rule_candidates(doc, indexes)

    assert len(candidates) == 1
    c = candidates[0]
    # 虽然句末是法律判断，但有明确法条引用，抽取为 legal_source_claim
    assert c.claim_type == ClaimType.LEGAL_SOURCE_CLAIM
    assert c.entities.legal_sources[0].title == "民法典"


# ============================================================
# Test 5: 三种括号案号 → with_case_number
# ============================================================

@pytest.mark.parametrize("case_text", [
    "（2021）最高法民申1234号",
    "(2021)京73民终123号",
    "〔2019〕粤民再56号",
])
def test_case_number_variants(case_text):
    """三种括号形态的案号 → with_case_number"""
    text = f"在{case_text}案中，法院作出了重要裁判。"
    doc = _make_parsed_doc([text])
    indexes = _make_indexes(doc)

    candidates = extract_rule_candidates(doc, indexes)

    assert len(candidates) == 1
    c = candidates[0]
    assert c.claim_type == ClaimType.CASE_CITATION
    case_refs = c.entities.case_refs
    assert len(case_refs) >= 1
    # 至少有一个 with_case_number
    has_exact = any(
        cr.reference_type == CaseReferenceType.WITH_CASE_NUMBER
        for cr in case_refs
    )
    assert has_exact


# ============================================================
# Test 6: 指导案例 → without_case_number
# ============================================================

def test_guiding_case():
    """"指导案例第24号……" → case_citation (without_case_number) 或 case_holding_paraphrase"""
    text = "指导案例第24号明确了类似案件的裁判规则。"
    doc = _make_parsed_doc([text])
    indexes = _make_indexes(doc)

    candidates = extract_rule_candidates(doc, indexes)

    # 指导案例可能被识别为 case_citation 或 case_holding_paraphrase
    # 因为"裁判规则"也是观点触发词
    case_candidates = [
        c for c in candidates
        if c.claim_type in (ClaimType.CASE_CITATION, ClaimType.CASE_HOLDING_PARAPHRASE)
    ]
    assert len(case_candidates) >= 1
    c = case_candidates[0]
    # 验证至少有一个 without_case_number 的 case_ref
    case_refs = c.entities.case_refs if hasattr(c.entities, "case_refs") else []
    has_wo_num = any(
        cr.reference_type == CaseReferenceType.WITHOUT_CASE_NUMBER
        for cr in case_refs
    )
    assert has_wo_num, f"Expected without_case_number in case_refs={case_refs}"


# ============================================================
# Test 7: 案号 + "认为" → case_holding_paraphrase
# ============================================================

def test_case_holding_paraphrase():
    """案号 + "认为" → case_holding_paraphrase"""
    text = "（2021）最高法民申1234号案中，法院认为，类似情形下应当优先保护善意第三人。"
    doc = _make_parsed_doc([text])
    indexes = _make_indexes(doc)

    candidates = extract_rule_candidates(doc, indexes)

    # 应抽取为 case_holding_paraphrase
    holding_candidates = [
        c for c in candidates
        if c.claim_type == ClaimType.CASE_HOLDING_PARAPHRASE
    ]
    assert len(holding_candidates) == 1
    c = holding_candidates[0]
    assert hasattr(c.entities, "holding_text")
    assert "优先保护" in c.entities.holding_text or len(c.entities.holding_text) > 0


# ============================================================
# Test 8: 仅"本院认为"无 case_ref → 不抽取
# ============================================================

def test_no_holding_without_case_ref():
    """仅"本院认为"无 case_ref → 不抽取 case_holding_paraphrase"""
    text = "本院认为，原告的诉讼请求缺乏事实依据，应予驳回。"
    doc = _make_parsed_doc([text])
    indexes = _make_indexes(doc)

    candidates = extract_rule_candidates(doc, indexes)

    # 不应抽取 case_holding_paraphrase
    holding = [
        c for c in candidates
        if c.claim_type == ClaimType.CASE_HOLDING_PARAPHRASE
    ]
    assert len(holding) == 0


# ============================================================
# Test 9: "本案""该案" → 不抽取 case_citation
# ============================================================

@pytest.mark.parametrize("pronoun_text", [
    "本案的事实经过如下……",
    "该案的争议焦点在于……",
    "此案的处理结果对类似案件有参考意义。",
    "上述案件已由上级法院提审。",
    "前案判决确立了新的裁判规则。",
    "原案经过再审程序已改判。",
])
def test_pronoun_not_case_citation(pronoun_text):
    """指代词 → 不抽取 case_citation"""
    doc = _make_parsed_doc([pronoun_text])
    indexes = _make_indexes(doc)

    candidates = extract_rule_candidates(doc, indexes)

    # 不应抽取 case_citation
    case_cands = [
        c for c in candidates
        if c.claim_type in (ClaimType.CASE_CITATION, ClaimType.CASE_HOLDING_PARAPHRASE)
    ]
    assert len(case_cands) == 0, f"指代词文本不应产生案例候选: {candidates}"


# ============================================================
# Test 10: 非法律规范文件 → 不识别为法源
# ============================================================

@pytest.mark.parametrize("non_legal_text", [
    "双方于2021年签订了《技术开发合同》和《保密协议》。",
    "原告提交了《授权确认函》和《公证书》作为证据。",
])
def test_non_legal_source_excluded(non_legal_text):
    """《XX合同》《授权确认函》→ 不识别为法源"""
    doc = _make_parsed_doc([non_legal_text])
    indexes = _make_indexes(doc)

    candidates = extract_rule_candidates(doc, indexes)

    # 不应抽取法律类 claim
    legal_cands = [
        c for c in candidates
        if c.claim_type == ClaimType.LEGAL_SOURCE_CLAIM
    ]
    assert len(legal_cands) == 0, f"合同/授权书不应产生法源候选: {candidates}"


# ============================================================
# Test 11: source_type 推断
# ============================================================

def test_source_type_inference():
    """source_type 推断规则"""
    # 司法解释
    assert infer_source_type("最高人民法院关于适用〈民法典〉若干问题的解释") == LegalSourceType.JUDICIAL_INTERPRETATION
    assert infer_source_type("最高人民法院关于审理商标案件的批复") == LegalSourceType.JUDICIAL_INTERPRETATION

    # 法律
    assert infer_source_type("中华人民共和国民法典") == LegalSourceType.LAW
    assert infer_source_type("中华人民共和国商标法") == LegalSourceType.LAW
    assert infer_source_type("中华人民共和国劳动合同法") == LegalSourceType.LAW

    # 行政法规/规章/规范性文件 → 统一归入 other_normative_document
    assert infer_source_type("商标法实施条例") == LegalSourceType.OTHER_NORMATIVE_DOCUMENT
    assert infer_source_type("卫星导航地图管理办法") == LegalSourceType.OTHER_NORMATIVE_DOCUMENT
    assert infer_source_type("互联网信息服务管理办法") == LegalSourceType.OTHER_NORMATIVE_DOCUMENT
    assert infer_source_type("商标评审规则") == LegalSourceType.OTHER_NORMATIVE_DOCUMENT
    assert infer_source_type("关于加强知识产权保护的决定") == LegalSourceType.OTHER_NORMATIVE_DOCUMENT
    assert infer_source_type("关于审理商标案件的指导意见") == LegalSourceType.OTHER_NORMATIVE_DOCUMENT
    assert infer_source_type("企业信息公示暂行条例") == LegalSourceType.OTHER_NORMATIVE_DOCUMENT
    assert infer_source_type("不动产登记暂行条例实施办法") == LegalSourceType.OTHER_NORMATIVE_DOCUMENT

    # 无后缀 → 不会进入此函数（由 _is_legal_source 在调用前过滤）
    # 此测试只验证 infer_source_type 对白名单内标题的分类

    # 验证白名单过滤：无后缀标题不会被识别为法源
    from ccitecheck.recognition.statutes import _is_legal_source
    assert not _is_legal_source("某公司内部管理制度汇编")
    assert not _is_legal_source("※※收藏")
    assert not _is_legal_source("x环线·地图")


# ============================================================
# 法源前向继承（承前省略法源名的指代消解）
# ============================================================

def test_carry_forward_entity_only_not_anchor_chain():
    """
    手册场景：一个《反不正当竞争法》声明后跟 N 个"第X条"段落。

    关键断言：
      1. 每个"第X条"句产生独立 claim（anchor_ids 只有当前句）
      2. 每个 claim 正确继承反不正当竞争法的法源名
      3. 没有任何 claim 的 anchor_ids 跨多个句子
    """
    texts = [
        "《中华人民共和国反不正当竞争法》是维护市场竞争秩序的基本法律。",
        "第2条规定，经营者遵循自愿、平等、公平、诚信原则。",
        "第10条规定，经营者不得实施侵犯商业秘密的行为。",
        "第13条规定，经营者不得以不正当方式获取、使用他人合法持有的数据。",
    ]
    doc = _make_parsed_doc(texts)
    indexes = _make_indexes(doc)

    candidates = extract_rule_candidates(doc, indexes)

    # 第一个 anchor 有《》→ explicit claim
    claim1 = [c for c in candidates if c.anchor_ids == ["line00001"]]
    assert len(claim1) == 1

    # 后三个是继承 claim，各自独立
    claim2 = [c for c in candidates if c.anchor_ids == ["line00002"]]
    claim3 = [c for c in candidates if c.anchor_ids == ["line00003"]]
    claim4 = [c for c in candidates if c.anchor_ids == ["line00004"]]
    assert len(claim2) == 1
    assert len(claim3) == 1
    assert len(claim4) == 1

    # 每个继承 claim 的法源标记为 inherited
    for c in [claim2[0], claim3[0], claim4[0]]:
        assert c.claim_type == ClaimType.LEGAL_SOURCE_CLAIM
        for ls in c.entities.legal_sources:
            assert ls.title == "中华人民共和国反不正当竞争法"
            assert ls.resolution == "inherited"
            assert ls.inherited_from_anchor == "line00001"

    # 没有任何 claim 跨 2 个以上 anchor
    for c in candidates:
        assert len(c.anchor_ids) == 1, f"claim {c.anchor_ids} should be single-anchor"


def test_carry_forward_resets_on_section_change():
    """
    跨 section 场景：标题切换后出现裸"第X条" → 不继承，不产出。
    """
    from ccitecheck.domain.document import Block, BlockType
    # 构建带不同 section_path 的 anchor
    texts = [
        ("2.1 商标侵权", "《中华人民共和国商标法》第五十七条规定了侵权情形。"),
        ("2.2 赔偿计算", "第六十三条规定了赔偿数额的计算方式。"),  # 不同 section，不应继承
    ]
    blocks = []
    anchors = []
    for i, (section, text) in enumerate(texts):
        block_id = f"b_{i+1:05d}"
        anchor_id = f"line{i+1:05d}"
        block = Block(
            block_id=block_id, type=BlockType.PARAGRAPH, text=text,
            style=None, section_path=[section], body_order=i, block_order=i,
            para_index=i, table_index=None, row_index=None, cell_index=None,
            has_numbering=False, numbering_text=None, numbering_unresolved=False,
            is_list_item=False, list_group_id=None, is_article_start=False,
            heading_source=None, anchor_range=[anchor_id, anchor_id],
            sentence_anchors=[anchor_id],
        )
        blocks.append(block)
        anchor = Anchor(anchor=anchor_id, text=text, block_id=block_id,
                       para_index=i, char_start=0, char_end=len(text))
        anchors.append(anchor)

    from ccitecheck.domain.document import DocMeta
    doc = ParsedDocument(doc_meta=DocMeta(schema_version="0.1", source_file="test", doc_hash="test"),
                         blocks=blocks, anchors=anchors, chunks=[])
    indexes = _make_indexes(doc)
    candidates = extract_rule_candidates(doc, indexes)

    # line00001 有《》→ 应产出
    c1 = [c for c in candidates if "line00001" in c.anchor_ids]
    assert len(c1) == 1

    # line00002 在不同 section 下，只有裸"第六十三条"，不应产出
    c2 = [c for c in candidates if "line00002" in c.anchor_ids]
    assert len(c2) == 0, "跨 section 的裸条款号不应继承，应留给 LLM"


def test_carry_forward_stops_at_empty_paragraph_gap():
    doc = _make_parsed_doc([
        "《中华人民共和国民法典》是基础性法律。",
        "第一条规定了立法目的。",
    ])
    # 模拟两个非空 block 之间有一个被解析器过滤的空段。
    doc.blocks[1].body_order = 2
    doc.blocks[1].para_index = 2
    candidates = extract_rule_candidates(doc, _make_indexes(doc))
    assert not any(c.anchor_ids == ["line00002"] for c in candidates)


def test_carry_forward_stops_at_explanatory_paragraph():
    doc = _make_parsed_doc([
        "《中华人民共和国民法典》是基础性法律。",
        "下文仅对合同履行的一般背景作说明。",
        "第一条规定了立法目的。",
    ])
    candidates = extract_rule_candidates(doc, _make_indexes(doc))
    assert not any(c.anchor_ids == ["line00003"] for c in candidates)


def test_carry_forward_rejects_multiple_source_ambiguity():
    doc = _make_parsed_doc([
        "《中华人民共和国民法典》与《中华人民共和国公司法》均为相关依据。",
        "第一条规定了立法目的。",
    ])
    candidates = extract_rule_candidates(doc, _make_indexes(doc))
    assert not any(c.anchor_ids == ["line00002"] for c in candidates)


# ============================================================
# Test: extract_legal_sources 完整性
# ============================================================

def test_extract_legal_sources_with_articles():
    """测试条款号提取"""
    text = "根据《中华人民共和国商标法》第四十八条第一款第（一）项的规定……"
    sources = extract_legal_sources(text)

    assert len(sources) == 1
    s = sources[0]
    assert s.title == "中华人民共和国商标法"
    assert len(s.articles) == 1
    assert s.articles[0].article == "第四十八条"


def test_extract_legal_sources_without_articles():
    """"依据《民法典》及相关规定" → 法源存在但 articles 为空"""
    text = "依据《民法典》及相关规定，本案应适用普通诉讼时效。"
    sources = extract_legal_sources(text)

    # "民法典"是法源（以"法"结尾）
    assert any(ls.title == "民法典" for ls in sources)
    ce_source = next(ls for ls in sources if ls.title == "民法典")
    # 无条款号时 articles 为空
    assert ce_source.articles == []
