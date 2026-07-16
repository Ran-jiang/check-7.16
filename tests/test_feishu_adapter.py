"""飞书块快照适配器和平台定位坐标回归测试。"""

from ccitecheck.application import validate_shared_document
from ccitecheck.parsing.feishu import (
    FeishuBlockInput,
    FeishuDocumentSnapshot,
    parse_feishu_snapshot,
)
from ccitecheck.recognition import extract_claims


def _snapshot(*blocks: FeishuBlockInput) -> FeishuDocumentSnapshot:
    return FeishuDocumentSnapshot(
        document_id="doccn_demo",
        title="法律意见书",
        revision="42",
        blocks=list(blocks),
    )


def test_feishu_snapshot_enters_shared_ir_with_stable_locations():
    parsed = validate_shared_document(parse_feishu_snapshot(_snapshot(
        FeishuBlockInput(
            block_id="heading-1",
            block_type="heading",
            heading_level=1,
            text="审查意见",
        ),
        FeishuBlockInput(
            block_id="paragraph-1",
            parent_id="heading-1",
            block_type="paragraph",
            text="依据《中华人民共和国民法典》第五百七十七条，应承担违约责任。",
        ),
    )))

    assert parsed.doc_meta.source_platform == "feishu"
    assert parsed.doc_meta.source_document_id == "doccn_demo"
    assert parsed.doc_meta.source_revision == "42"
    assert parsed.blocks[1].section_path == ["审查意见"]
    assert parsed.blocks[1].external_block_id == "paragraph-1"

    claims = extract_claims(parsed, include_statutes=True, include_cases=False)
    assert len(claims.claims) == 1
    location = claims.claims[0].source_locations[0]
    assert location.platform == "feishu"
    assert location.document_id == "doccn_demo"
    assert location.revision == "42"
    assert location.block_id == "paragraph-1"


def test_feishu_two_column_table_preserves_source_coordinates():
    parsed = validate_shared_document(parse_feishu_snapshot(_snapshot(
        FeishuBlockInput(
            block_id="law-name",
            block_type="table_cell",
            text="《中华人民共和国民法典》",
            table_index=0,
            row_index=0,
            cell_index=0,
        ),
        FeishuBlockInput(
            block_id="law-content",
            block_type="table_cell",
            text="第五百七十七条规定，当事人一方不履行合同义务的，应当承担违约责任。",
            table_index=0,
            row_index=0,
            cell_index=1,
        ),
    )))

    claims = extract_claims(parsed, include_statutes=True, include_cases=False)
    assert len(claims.claims) == 1
    claim = claims.claims[0]
    assert claim.entities.legal_sources[0].title == "中华人民共和国民法典"
    assert [item.block_id for item in claim.source_locations] == [
        "law-name",
        "law-content",
    ]
    assert claim.source_locations[-1].cell_index == 1


def test_vertical_merged_law_cell_is_inherited_by_each_covered_row():
    parsed = parse_feishu_snapshot(_snapshot(
        FeishuBlockInput(
            block_id="law-name",
            block_type="table_cell",
            text="《中华人民共和国民法典》",
            table_index=0,
            row_index=0,
            cell_index=0,
            row_start=0,
            row_end=1,
            col_start=0,
            col_end=0,
        ),
        FeishuBlockInput(
            block_id="article-1",
            block_type="table_cell",
            text="第一条规定了立法目的。",
            table_index=0,
            row_index=0,
            cell_index=1,
        ),
        FeishuBlockInput(
            block_id="article-2",
            block_type="table_cell",
            text="第二条规定了适用范围。",
            table_index=0,
            row_index=1,
            cell_index=1,
        ),
    ))

    claims = extract_claims(parsed, include_statutes=True, include_cases=False).claims
    assert len(claims) == 2
    assert [claim.entities.legal_sources[0].title for claim in claims] == [
        "中华人民共和国民法典",
        "中华人民共和国民法典",
    ]
    assert claims[1].source_locations[0].row_end == 1
