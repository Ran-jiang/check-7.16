"""
Test 8：Renderer 渲染测试。

测试覆盖：
  - 普通 anchor 渲染格式
  - table cell anchor 渲染带来源标注
  - section_path 为空时省略 Section 头
  - chunk 不存在时报错
"""

import pytest

from parser.schema import (
    ParsedDocument,
    DocMeta,
    Block,
    Anchor,
    Chunk,
    BlockType,
)
from parser.renderer import render_chunk_for_llm
from parser.utils import make_id_counter


def _make_parsed_doc_with_table() -> ParsedDocument:
    """构造包含 table cell 的 parsed_doc 用于渲染测试。"""
    next_anchor = make_id_counter("line", 5)
    next_block = make_id_counter("b_", 5)
    next_chunk = make_id_counter("c_", 5)

    # Block 1: 普通段落
    b1 = Block(
        block_id=next_block(),
        type=BlockType.PARAGRAPH,
        text="第一条 合同双方确认本协议内容。",
        section_path=["第一章 总则"],
        body_order=0,
        block_order=0,
        para_index=0,
        anchor_range=["line00001", "line00001"],
        sentence_anchors=["line00001"],
    )

    # Block 2: 表格单元格
    b2 = Block(
        block_id=next_block(),
        type=BlockType.TABLE_CELL,
        text="甲方：张三",
        section_path=["第一章 总则"],
        body_order=1,
        block_order=1,
        para_index=None,
        table_index=0,
        row_index=0,
        cell_index=0,
        anchor_range=["line00002", "line00002"],
        sentence_anchors=["line00002"],
    )

    # Anchors
    a1 = Anchor(
        anchor="line00001",
        text="第一条 合同双方确认本协议内容。",
        block_id=b1.block_id,
        para_index=0,
        char_start=0,
        char_end=len(b1.text),
    )
    a2 = Anchor(
        anchor="line00002",
        text="甲方：张三",
        block_id=b2.block_id,
        para_index=None,
        char_start=0,
        char_end=len(b2.text),
    )

    # Chunk
    c1 = Chunk(
        chunk_id=next_chunk(),
        section_path=["第一章 总则"],
        block_ids=[b1.block_id, b2.block_id],
        anchor_ids=["line00001", "line00002"],
        anchor_range=["line00001", "line00002"],
        estimated_tokens=20,
    )

    return ParsedDocument(
        doc_meta=DocMeta(source_file="test.docx", doc_hash="test"),
        blocks=[b1, b2],
        anchors=[a1, a2],
        chunks=[c1],
    )


def _make_parsed_doc_no_section() -> ParsedDocument:
    """构造无 section_path 的 parsed_doc。"""
    next_anchor = make_id_counter("line", 5)
    next_block = make_id_counter("b_", 5)
    next_chunk = make_id_counter("c_", 5)

    b1 = Block(
        block_id=next_block(),
        type=BlockType.PARAGRAPH,
        text="简单文本。",
        section_path=[],
        body_order=0,
        block_order=0,
        para_index=0,
        anchor_range=["line00001", "line00001"],
        sentence_anchors=["line00001"],
    )

    a1 = Anchor(
        anchor="line00001",
        text="简单文本。",
        block_id=b1.block_id,
        para_index=0,
        char_start=0,
        char_end=len(b1.text),
    )

    c1 = Chunk(
        chunk_id=next_chunk(),
        section_path=[],
        block_ids=[b1.block_id],
        anchor_ids=["line00001"],
        anchor_range=["line00001", "line00001"],
        estimated_tokens=5,
    )

    return ParsedDocument(
        doc_meta=DocMeta(source_file="test.docx", doc_hash="test"),
        blocks=[b1],
        anchors=[a1],
        chunks=[c1],
    )


class TestRenderer:
    """Test 8：渲染测试"""

    def test_normal_anchor_rendering(self):
        """普通 anchor 渲染为 [line00001] 文本。"""
        parsed = _make_parsed_doc_with_table()
        rendered = render_chunk_for_llm(parsed, parsed.chunks[0].chunk_id)

        assert "[line00001]" in rendered
        assert "第一条 合同双方确认本协议内容。" in rendered

    def test_table_cell_anchor_rendering(self):
        """table cell anchor 渲染带来源标注。"""
        parsed = _make_parsed_doc_with_table()
        rendered = render_chunk_for_llm(parsed, parsed.chunks[0].chunk_id)

        # 表格编号（1-based）
        assert "(表1 行1 列1)" in rendered
        assert "[line00002]" in rendered
        assert "甲方：张三" in rendered

    def test_section_header_rendered(self):
        """有 section_path 时渲染 Section 头。"""
        parsed = _make_parsed_doc_with_table()
        rendered = render_chunk_for_llm(parsed, parsed.chunks[0].chunk_id)

        assert "# Section" in rendered
        assert "第一章 总则" in rendered

    def test_no_section_header_when_empty(self):
        """section_path 为空时省略 Section 头。"""
        parsed = _make_parsed_doc_no_section()
        rendered = render_chunk_for_llm(parsed, parsed.chunks[0].chunk_id)

        assert "# Section" not in rendered
        assert "[line00001]" in rendered

    def test_chunk_not_found(self):
        """chunk 不存在时抛出 ValueError。"""
        parsed = _make_parsed_doc_no_section()
        with pytest.raises(ValueError, match="Chunk not found"):
            render_chunk_for_llm(parsed, "c_nonexistent")
