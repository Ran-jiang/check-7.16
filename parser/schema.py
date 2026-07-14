"""
CCitecheck v0.1 数据结构定义。

本模块使用 Pydantic v2 定义 DOCX 解析产物的所有数据结构。
三层架构：
  - Anchor Layer：句级锚点层（细粒度）
  - Block Layer：文档结构块层（中粒度）
  - Chunk Layer：LLM Packet 层（粗粒度）
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


# ---- 辅助类型 ----

class BlockType(str, Enum):
    """文档结构块类型"""
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE_CELL = "table_cell"
    FOOTNOTE = "footnote"
    ENDNOTE = "endnote"


class HeadingSource(str, Enum):
    """标题识别来源"""
    STYLE = "style"
    PATTERN = "pattern"


# ---- Anchor Layer ----

class Anchor(BaseModel):
    """
    句级锚点。

    每个句子生成一个 anchor。anchor 是逻辑坐标，
    通过 block_id + char_start + char_end 定位到 block.text 中的具体文本。
    """
    anchor: str = Field(description="锚点编号，格式 line00001")
    text: str = Field(description="句子文本，从所属 block.text 派生")
    block_id: str = Field(description="所属 block 的 ID")
    # para_index 仅对段落类 block（heading/paragraph/list_item）有意义
    para_index: Optional[int] = Field(default=None, description="原始段落序号，从0开始")
    note_type: Optional[str] = Field(default=None, description="footnote/endnote；正文为 null")
    note_id: Optional[str] = Field(default=None, description="Word 注释 ID；正文为 null")
    char_start: int = Field(description="在 block.text 中的起始偏移（左闭）", ge=0)
    char_end: int = Field(description="在 block.text 中的结束偏移（右开）", ge=0)


# ---- Block Layer ----

class Block(BaseModel):
    """
    文档结构块。

    block 是文档的结构单元。block.text 是解析后、归一化后的规范文本来源。
    所有偏移量都基于 block.text 计算。
    """
    block_id: str = Field(description="block 唯一 ID，格式 b_00001")
    type: BlockType = Field(description="block 类型")
    text: str = Field(description="归一化后的规范文本")
    style: Optional[str] = Field(default=None, description="DOCX 段落样式名")
    # ---- 结构路径 ----
    section_path: list[str] = Field(default_factory=list, description="从一级标题到当前标题的路径")
    # ---- 顺序索引 ----
    body_order: int = Field(description="DOCX body 顶层元素顺序，从0开始")
    block_order: int = Field(description="所有 block 的全局阅读顺序，从0开始")
    para_index: Optional[int] = Field(default=None, description="原始段落序号，table_cell 时为 null")
    note_type: Optional[str] = Field(default=None, description="footnote/endnote；正文为 null")
    note_id: Optional[str] = Field(default=None, description="Word 注释 ID；正文为 null")
    # ---- 表格定位 ----
    table_index: Optional[int] = Field(default=None, description="表格编号，从0开始")
    row_index: Optional[int] = Field(default=None, description="行编号，从0开始")
    cell_index: Optional[int] = Field(default=None, description="单元格编号，从0开始")
    # ---- 自动编号 ----
    has_numbering: bool = Field(default=False, description="段落是否携带 Word 自动编号")
    numbering_text: Optional[str] = Field(default=None, description="从 numbering.xml 还原的编号文本")
    numbering_unresolved: bool = Field(default=False, description="无法可靠还原编号文本")
    # ---- 列举项 ----
    is_list_item: bool = Field(default=False, description="是否为列举项")
    list_group_id: Optional[str] = Field(default=None, description="列举组 ID，格式 lg_00001")
    # ---- 法条标志 ----
    is_article_start: bool = Field(default=False, description="是否为'第X条'起始")
    heading_source: Optional[HeadingSource] = Field(default=None, description="标题识别来源")
    # ---- 锚点关联 ----
    anchor_range: list[str] = Field(default_factory=list, description="[第一个anchor, 最后一个anchor]")
    sentence_anchors: list[str] = Field(default_factory=list, description="该 block 内所有 anchor 编号列表")


# ---- Chunk Layer ----

class Chunk(BaseModel):
    """
    LLM 输入打包单元。

    chunk 不重复存正文文本。调用 LLM 时通过 anchor_ids 动态渲染。
    """
    chunk_id: str = Field(description="chunk 唯一 ID，格式 c_00001")
    section_path: list[str] = Field(default_factory=list, description="chunk 所属的标题路径")
    block_ids: list[str] = Field(default_factory=list, description="chunk 覆盖的 block ID 列表")
    anchor_ids: list[str] = Field(default_factory=list, description="chunk 实际包含的 anchor 编号列表")
    anchor_range: list[str] = Field(default_factory=list, description="[第一个anchor, 最后一个anchor]")
    estimated_tokens: int = Field(default=0, description="粗略 token 估算")
    overlap_anchor_ids: list[str] = Field(default_factory=list, description="重叠 anchor 列表（仅超长 block 拆分时）")


# ---- 文档元信息 ----

class DocMeta(BaseModel):
    """文档级元信息"""
    schema_version: str = Field(default="0.1", description="schema 版本号")
    doc_id: str = Field(default_factory=lambda: str(uuid4()), description="文档唯一 ID（uuid4）")
    source_file: str = Field(default="", description="原始 DOCX 文件名")
    doc_hash: str = Field(default="", description="原始文件 SHA-256 摘要")
    parsed_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="解析时间（ISO-8601）"
    )


# ---- 顶层文档 ----

class ParsedDocument(BaseModel):
    """
    CCitecheck 解析产出的顶层结构。

    每次解析视为一次文档快照（snapshot）。
    """
    doc_meta: DocMeta = Field(default_factory=DocMeta)
    blocks: list[Block] = Field(default_factory=list)
    anchors: list[Anchor] = Field(default_factory=list)
    chunks: list[Chunk] = Field(default_factory=list)
