"""把飞书文档块快照转换为平台无关文档模型。

飞书 SDK 调用由 ``apps/feishu`` 前端负责。本模块只接受带版本号的块快照，
不依赖飞书 SDK，也不执行引用识别、溯源或判定。
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from ..domain.document import (
    Anchor,
    Block,
    BlockType,
    DocMeta,
    HeadingSource,
    ParsedDocument,
)
from .chunks import build_chunks
from .relations import build_block_relations
from .sentences import split_sentences
from .utils import make_id_counter, normalize_whitespace


class FeishuBlockInput(BaseModel):
    """飞书前端提交的单个可核查文档块。"""

    block_id: str = Field(min_length=1)
    parent_id: Optional[str] = None
    block_type: Literal["heading", "paragraph", "list_item", "table_cell"]
    text: str
    heading_level: Optional[int] = Field(default=None, ge=1, le=9)
    table_index: Optional[int] = Field(default=None, ge=0)
    row_index: Optional[int] = Field(default=None, ge=0)
    cell_index: Optional[int] = Field(default=None, ge=0)
    row_start: Optional[int] = Field(default=None, ge=0)
    row_end: Optional[int] = Field(default=None, ge=0)
    col_start: Optional[int] = Field(default=None, ge=0)
    col_end: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_coordinates(self) -> "FeishuBlockInput":
        """保证表格坐标只出现在表格单元格上，并且三个坐标同时存在。"""
        coordinates = (self.table_index, self.row_index, self.cell_index)
        if self.block_type == "table_cell" and any(item is None for item in coordinates):
            raise ValueError("table_cell requires table_index, row_index and cell_index")
        if self.block_type != "table_cell" and any(item is not None for item in coordinates):
            raise ValueError("table coordinates are only valid for table_cell")
        spans = (self.row_start, self.row_end, self.col_start, self.col_end)
        if self.block_type != "table_cell" and any(item is not None for item in spans):
            raise ValueError("table spans are only valid for table_cell")
        if self.block_type == "table_cell":
            row_start = self.row_start if self.row_start is not None else self.row_index
            row_end = self.row_end if self.row_end is not None else self.row_index
            col_start = self.col_start if self.col_start is not None else self.cell_index
            col_end = self.col_end if self.col_end is not None else self.cell_index
            if row_start is None or row_end is None or col_start is None or col_end is None:
                raise ValueError("table_cell requires complete span coordinates")
            if row_end < row_start or col_end < col_start:
                raise ValueError("table span end must not precede its start")
        return self


class FeishuDocumentSnapshot(BaseModel):
    """飞书插件与后端之间的版本化文档快照契约。"""

    schema_version: Literal["1"] = "1"
    document_id: str = Field(min_length=1)
    title: str = Field(default="飞书文档", max_length=500)
    revision: Optional[str] = None
    blocks: list[FeishuBlockInput] = Field(default_factory=list, max_length=20_000)


def parse_feishu_snapshot(snapshot: FeishuDocumentSnapshot) -> ParsedDocument:
    """将飞书块顺序转换为与 DOCX 共用的块、锚点和语义块结构。"""
    next_block_id = make_id_counter("b_", 5)
    next_anchor_id = make_id_counter("line", 5)
    blocks: list[Block] = []
    anchors: list[Anchor] = []
    heading_stack: list[tuple[int, str]] = []

    for raw in snapshot.blocks:
        text = normalize_whitespace(raw.text)
        if not text:
            continue
        if raw.block_type == "heading":
            level = raw.heading_level or 1
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, text))

        block_id = next_block_id()
        block = Block(
            block_id=block_id,
            type=BlockType(raw.block_type),
            text=text,
            section_path=[item[1] for item in heading_stack],
            body_order=len(blocks),
            block_order=len(blocks),
            table_index=raw.table_index,
            row_index=raw.row_index,
            cell_index=raw.cell_index,
            row_start=raw.row_start if raw.row_start is not None else raw.row_index,
            row_end=raw.row_end if raw.row_end is not None else raw.row_index,
            col_start=raw.col_start if raw.col_start is not None else raw.cell_index,
            col_end=raw.col_end if raw.col_end is not None else raw.cell_index,
            row_span=(raw.row_end if raw.row_end is not None else raw.row_index or 0)
            - (raw.row_start if raw.row_start is not None else raw.row_index or 0) + 1,
            col_span=(raw.col_end if raw.col_end is not None else raw.cell_index or 0)
            - (raw.col_start if raw.col_start is not None else raw.cell_index or 0) + 1,
            is_list_item=raw.block_type == "list_item",
            is_article_start=bool(re.match(r"^第[一二三四五六七八九十百千零\d]+条", text)),
            heading_source=HeadingSource.STYLE if raw.block_type == "heading" else None,
            external_block_id=raw.block_id,
            external_parent_id=raw.parent_id,
        )
        for span in split_sentences(text):
            anchor_id = next_anchor_id()
            anchors.append(Anchor(
                anchor=anchor_id,
                text=span.text,
                block_id=block_id,
                char_start=span.char_start,
                char_end=span.char_end,
            ))
            block.sentence_anchors.append(anchor_id)
        if block.sentence_anchors:
            block.anchor_range = [block.sentence_anchors[0], block.sentence_anchors[-1]]
        blocks.append(block)

    digest_source = "\n".join(
        f"{item.block_id}\0{item.block_type}\0{item.text}" for item in snapshot.blocks
    )
    parsed = ParsedDocument(
        doc_meta=DocMeta(
            source_file=snapshot.title,
            doc_hash="sha256:" + hashlib.sha256(digest_source.encode("utf-8")).hexdigest(),
            source_platform="feishu",
            source_document_id=snapshot.document_id,
            source_revision=snapshot.revision,
        ),
        blocks=blocks,
        anchors=anchors,
    )
    return build_chunks(build_block_relations(parsed))


__all__ = ["FeishuBlockInput", "FeishuDocumentSnapshot", "parse_feishu_snapshot"]
