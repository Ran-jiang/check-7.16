import assert from "node:assert/strict"
import test from "node:test"

import { snapshotFromRoot } from "../assets/host.js"

test("snapshotFromRoot preserves headings, paragraphs and table coordinates", () => {
  const root = {
    id: 1, type: "page", data: { plain_text: "标题" }, childSnapshots: [
      { id: 2, type: "heading1", data: { plain_text: "审查意见" }, childSnapshots: [] },
      { id: 3, type: "text", data: { plain_text: "依据《民法典》第五百七十七条。" }, childSnapshots: [] },
      { id: 4, type: "table", data: { property: { column_size: 2 } }, childSnapshots: [
        { id: 5, type: "table_cell", data: {}, childSnapshots: [{ id: 6, type: "text", data: { plain_text: "名称" }, childSnapshots: [] }] },
        { id: 7, type: "table_cell", data: {}, childSnapshots: [{ id: 8, type: "text", data: { plain_text: "内容" }, childSnapshots: [] }] },
      ] },
    ],
  }

  const snapshot = snapshotFromRoot(root, { documentId: "doccn_1", title: "合同" })
  assert.equal(snapshot.document_id, "doccn_1")
  assert.deepEqual(snapshot.blocks.map(item => item.block_type), ["heading", "paragraph", "table_cell", "table_cell"])
  assert.equal(snapshot.blocks[0].heading_level, 1)
  assert.deepEqual(snapshot.blocks.slice(2).map(item => [item.row_index, item.cell_index, item.text]), [
    [0, 0, "名称"],
    [0, 1, "内容"],
  ])
})
