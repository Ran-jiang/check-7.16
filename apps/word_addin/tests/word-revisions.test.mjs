import assert from "node:assert/strict"
import test from "node:test"
import { revisionFor } from "../assets/word-revisions.js"

test("revisionFor selects a machine-applicable issue", () => {
  assert.deepEqual(revisionFor({
    findings: [{ revision: {
      strategy: "replace_exact_text",
      original_text: "依照第十条处理",
      revised_text: "依照第十一条处理",
      machine_applicable: true,
    } }],
  }), { original: "依照第十条处理", revised: "依照第十一条处理" })
})

test("revisionFor rejects missing replacement", () => {
  assert.equal(revisionFor({ document_quote: "原文", findings: [] }), null)
})
