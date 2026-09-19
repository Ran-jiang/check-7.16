import assert from "node:assert/strict"
import test from "node:test"
import { applyTrackedRevision, revisionFor, undoTrackedRevision } from "../assets/word-revisions.js"

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
  assert.equal(revisionFor({ claim_text: "原文", findings: [] }), null)
})

test("applyTrackedRevision replaces one exact occurrence with change tracking", async () => {
  const calls = []
  const match = {
    insertText(text, mode) { calls.push(["replace", text, mode]) },
  }
  const document = {
    changeTrackingMode: "Off",
    load(field) { assert.equal(field, "changeTrackingMode") },
    body: {
      search(text, options) {
        calls.push(["search", text, options])
        return { items: [match], load(field) { assert.equal(field, "items") } }
      },
    },
  }
  globalThis.Office = { context: { requirements: { isSetSupported: () => true } } }
  globalThis.window = { Word: { run: async callback => callback({ document, async sync() {} }) } }
  globalThis.Word = globalThis.window.Word
  const check = { findings: [{ revision: {
    strategy: "replace_exact_text",
    original_text: "依据《民法典》第五百零九条第九款处理。",
    revised_text: "依据《民法典》第五百零九条第一款处理。",
    machine_applicable: true,
  } }] }
  const result = await applyTrackedRevision(check)
  assert.deepEqual(result, {
    method: "unique_text",
    revised_text: "依据《民法典》第五百零九条第一款处理。",
  })
  assert.deepEqual(calls.at(-1), ["replace", "依据《民法典》第五百零九条第一款处理。", "Replace"])
  assert.equal(document.changeTrackingMode, "Off")
})

test("applyTrackedRevision refuses ambiguous document text", async () => {
  const document = {
    changeTrackingMode: "Off", load() {},
    body: { search() { return { items: [{}, {}], load() {} } } },
  }
  globalThis.Office = { context: { requirements: { isSetSupported: () => true } } }
  globalThis.window = { Word: { run: async callback => callback({ document, async sync() {} }) } }
  globalThis.Word = globalThis.window.Word
  await assert.rejects(() => applyTrackedRevision({ findings: [{ revision: {
    strategy: "replace_exact_text", original_text: "重复原文", revised_text: "修订原文", machine_applicable: true,
  } }] }), /原文存在多处相同内容/)
})

test("undoTrackedRevision restores the original text", async () => {
  const calls = []
  const document = {
    changeTrackingMode: "Off", load() {},
    body: { search(text) {
      calls.push(["search", text])
      return { items: [{ insertText(value) { calls.push(["replace", value]) } }], load() {} }
    } },
  }
  globalThis.Office = { context: { requirements: { isSetSupported: () => true } } }
  globalThis.window = { Word: { run: async callback => callback({ document, async sync() {} }) } }
  globalThis.Word = globalThis.window.Word
  const result = await undoTrackedRevision({ findings: [{ revision: {
    strategy: "replace_exact_text", original_text: "原文", revised_text: "修订文", machine_applicable: true,
  } }] })
  assert.deepEqual(calls, [["search", "修订文"], ["replace", "原文"]])
  assert.deepEqual(result, { method: "unique_text", restored_text: "原文" })
})
