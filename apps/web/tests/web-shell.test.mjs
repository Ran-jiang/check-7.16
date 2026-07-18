import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import test from "node:test"

import { orderChecksByDocument } from "../assets/web-order.js"

const html = readFileSync(new URL("../index.html", import.meta.url), "utf8")
const script = readFileSync(new URL("../assets/web.js", import.meta.url), "utf8")

test("web shell exposes upload, paste, preview and result surfaces", () => {
  for (const id of ["docx-file", "source-text", "document-preview", "web-results", "download-button"]) {
    assert.match(html, new RegExp(`id=["']${id}["']`))
  }
  assert.match(html, /引用句已标亮/)
  assert.doesNotMatch(html, /全部引用/)
})

test("web client uses the dedicated session revision protocol", () => {
  assert.match(script, /\/api\/web\/checks/)
  assert.match(script, /accepted_check_ids/)
  assert.match(script, /method:\s*"DELETE"/)
  assert.match(script, /\/document`/)
  assert.match(script, /centerInPane/)
  assert.match(script, /原文链接：/)
})

test("result cards follow parsed document order across paragraphs and tables", () => {
  const previewBlocks = [
    { block_id: "word:p:12", order: 0 },
    { block_id: "word:t:0:0:0", order: 1 },
    { block_id: "word:p:13", order: 2 },
  ]
  const checks = [
    { check_id: "third", source_locations: [{ block_id: "word:p:13" }] },
    { check_id: "second", source_locations: [{ block_id: "word:t:0:0:0" }] },
    { check_id: "first", source_locations: [{ block_id: "word:p:12" }] },
  ]

  assert.deepEqual(
    orderChecksByDocument(checks, previewBlocks).map(item => item.check_id),
    ["first", "second", "third"],
  )
})

test("references in the same sentence preserve their original order", () => {
  const previewBlocks = [{ block_id: "word:p:2", order: 4 }]
  const checks = [
    { check_id: "statute-a", source_locations: [{ block_id: "word:p:2" }] },
    { check_id: "statute-b", source_locations: [{ block_id: "word:p:2" }] },
  ]

  assert.deepEqual(
    orderChecksByDocument(checks, previewBlocks).map(item => item.check_id),
    ["statute-a", "statute-b"],
  )
})
