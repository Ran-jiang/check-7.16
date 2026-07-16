import assert from "node:assert/strict"
import test from "node:test"

import { getDocumentName, getSelectedContent, jumpToSource } from "../assets/office-document.js"


test("getDocumentName reads the real filename from the Word URL", async () => {
  globalThis.Office = {
    context: { document: { url: "https://example.com/contracts/%E5%90%88%E5%90%8C%E5%AE%A1%E6%9F%A5.docx?web=1" } },
  }
  assert.equal(await getDocumentName(), "合同审查.docx")
})


test("getDocumentName falls back to Word file properties", async () => {
  globalThis.Office = {
    AsyncResultStatus: { Succeeded: "succeeded" },
    context: { document: {
      url: "",
      getFilePropertiesAsync(callback) {
        callback({ status: "succeeded", value: { url: "C:\\Legal\\法律意见书.docx" } })
      },
    } },
  }
  assert.equal(await getDocumentName(), "法律意见书.docx")
})

test("getSelectedContent keeps the original block and in-block offset", async () => {
  const firstRange = { start: 0, end: 3, text: "标题\r", load() {} }
  const sourceRange = { start: 100, end: 107, text: "前文依据法条。", load() {} }
  const first = { tableNestingLevel: 0, getRange() { return firstRange } }
  const source = { tableNestingLevel: 0, getRange() { return sourceRange } }
  const selection = {
    start: 102,
    end: 106,
    paragraphs: { items: [source], load() {} },
    load() {},
  }
  const Word = { async run(callback) { return callback({
    document: {
      getSelection() { return selection },
      body: {
        paragraphs: { items: [first, source], load() {} },
        tables: { items: [], load() {} },
        footnotes: { items: [], load() {} },
        endnotes: { items: [], load() {} },
      },
    },
    async sync() {},
  }) } }
  globalThis.Word = Word
  globalThis.window = { Word }
  assert.deepEqual(await getSelectedContent(), {
    text: "依据法条",
    source_blocks: [{ block_id: "word:p:1", char_start: 2 }],
  })
})


test("jumpToSource selects anchor character offsets inside a shared paragraph block", async () => {
  const calls = []
  const blockRange = {
    text: "  重复  引用。\r", start: 100, end: 110, load() {},
    select() { calls.push("block") },
  }
  const paragraphs = [
    { tableNestingLevel: 0 },
    { tableNestingLevel: 1 },
    { tableNestingLevel: 0 },
    { tableNestingLevel: 0, getRange() { return blockRange } },
  ]
  const selection = {
    setRange(start, end) { calls.push([start, end]) },
    select() { calls.push("select") },
  }
  const Word = {
    async run(callback) {
      await callback({
        document: {
          body: {
            paragraphs: { items: paragraphs, load(value) {
              assert.equal(value, "items/tableNestingLevel")
            } },
          },
          getSelection() { return selection },
        },
        async sync() {},
      })
    },
  }
  globalThis.Word = Word
  globalThis.window = { Word }

  await jumpToSource([{ platform: "docx", block_id: "word:p:2", char_start: 2, char_end: 4 }])
  assert.deepEqual(calls, ["block", [104, 107], "select"])
})

test("jumpToSource uses the content cell for a paired table citation", async () => {
  const selectedCells = []
  const range = { text: "第127条。", start: 20, end: 27, load() {}, select() {} }
  const Word = { async run(callback) { await callback({
    document: {
      body: { tables: { getItemAt() { return { getCell(row, cell) {
        selectedCells.push([row, cell])
        return { body: { getRange() { return range } } }
      } } } } },
      getSelection() { return { setRange() {}, select() {} } },
    },
    async sync() {},
  }) } }
  globalThis.Word = Word
  globalThis.window = { Word }
  await jumpToSource([
    { platform: "docx", block_id: "word:t:0:1:0", table_index: 0, row_index: 1, char_start: 0, char_end: 2 },
    { platform: "docx", block_id: "word:t:0:1:1", table_index: 0, row_index: 1, char_start: 0, char_end: 6 },
  ])
  assert.deepEqual(selectedCells, [[1, 1]])
})


test("jumpToSource requires structured Word coordinates", async () => {
  globalThis.Word = { async run() {} }
  globalThis.window = { Word: globalThis.Word }
  await assert.rejects(() => jumpToSource([]), /缺少 Word 块定位信息/)
})
