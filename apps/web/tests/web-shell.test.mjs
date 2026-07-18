import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import test from "node:test"

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
