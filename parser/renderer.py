"""
CCitecheck v0.1 LLM Packet 渲染函数。

只渲染文本，不调用 LLM，不识别引用，不做法律判断。

渲染格式：
  # Section
  第四章 合同解除 > 4.2 单方解除

  [line00005] 根据《劳动合同法》第三十七条，劳动者可以解除劳动合同。
  [line00006] 公司应在员工离职后三十日内办理交接手续。

table cell 句子前加来源标注：
  [line00021] (表1 行2 列3) 单元格文本……
"""

from __future__ import annotations

from .schema import Anchor, Block, BlockType, Chunk, ParsedDocument


def render_chunk_for_llm(parsed_doc: ParsedDocument, chunk_id: str) -> str:
    """
    将指定 chunk 渲染为 LLM 输入文本。

    Args:
        parsed_doc: 已解析的文档
        chunk_id: 要渲染的 chunk ID（如 "c_00001"）

    Returns:
        渲染后的文本字符串

    Raises:
        ValueError: 如果 chunk_id 不存在
    """
    # 查找 chunk
    chunk: Chunk | None = None
    for c in parsed_doc.chunks:
        if c.chunk_id == chunk_id:
            chunk = c
            break
    if chunk is None:
        raise ValueError(f"Chunk not found: {chunk_id}")

    # 构建查找表
    anchor_map: dict[str, Anchor] = {a.anchor: a for a in parsed_doc.anchors}
    block_map: dict[str, Block] = {b.block_id: b for b in parsed_doc.blocks}

    lines: list[str] = []

    # ---- Section 头 ----
    if chunk.section_path:
        section_line = "# Section\n" + " > ".join(chunk.section_path)
        lines.append(section_line)
        lines.append("")  # 空行

    # ---- 渲染每个 anchor ----
    for anchor_id in chunk.anchor_ids:
        anchor = anchor_map.get(anchor_id)
        if anchor is None:
            lines.append(f"[{anchor_id}] (anchor not found)")
            continue

        block = block_map.get(anchor.block_id)
        if block is None:
            lines.append(f"[{anchor_id}] {anchor.text}")
            continue

        # 判断是否为 table_cell
        if block.type == BlockType.TABLE_CELL:
            # 面向用户使用 1-based 编号
            table_num = (block.table_index or 0) + 1
            row_num = (block.row_index or 0) + 1
            cell_num = (block.cell_index or 0) + 1
            source = f"(表{table_num} 行{row_num} 列{cell_num})"
            lines.append(f"[{anchor_id}] {source} {anchor.text}")
        else:
            lines.append(f"[{anchor_id}] {anchor.text}")

    return "\n".join(lines)


def render_all_chunks(parsed_doc: ParsedDocument) -> dict[str, str]:
    """
    渲染文档中所有 chunk，返回 chunk_id → 渲染文本 的映射。

    Args:
        parsed_doc: 已解析的文档

    Returns:
        字典，key 为 chunk_id，value 为渲染文本
    """
    result: dict[str, str] = {}
    for chunk in parsed_doc.chunks:
        result[chunk.chunk_id] = render_chunk_for_llm(parsed_doc, chunk.chunk_id)
    return result
