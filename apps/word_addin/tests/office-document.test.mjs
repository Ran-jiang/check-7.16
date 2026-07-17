import assert from "node:assert/strict"
import test from "node:test"

import {
  getDocumentName,
  getSelectedContent,
} from "../assets/office-document.js"
import { clearSourceBookmarks, jumpToSource, seedSourceBookmarks } from "../assets/word-bookmarks.js"


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
  globalThis.Office = { context: { requirements: { isSetSupported() { return false } } } }
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


test("seedSourceBookmarks replaces old markers and jumpToSource selects the bookmark", async () => {
  globalThis.Office = { context: { requirements: { isSetSupported() { return false } } } }
  const calls = []
  const sentence = {
    text: "裁判理由完整一句。",
    insertBookmark(name) { calls.push(["insert", name]) },
  }
  const paragraphRange = {
    text: "裁判理由完整一句。\r",
    load() {},
    getTextRanges(endings, trimSpacing) {
      assert.deepEqual(endings, ["。", "！", "？"])
      assert.equal(trimSpacing, false)
      return { items: [sentence], load() {} }
    },
  }
  const body = {
    paragraphs: {
      items: [{ tableNestingLevel: 0, getRange() { return paragraphRange } }],
      load() {},
    },
    tables: { items: [], load() {} },
    getRange() { return { getBookmarks() { return { value: ["_CCOLD_0", "_Toc42"] } } } },
  }
  const bookmark = {
    isNullObject: false,
    load() {},
    select() { calls.push(["select", "bookmark"]) },
  }
  const Word = { async run(callback) { return callback({
    document: {
      body,
      deleteBookmark(name) { calls.push(["delete", name]) },
      getBookmarkRangeOrNullObject(name) {
        assert.equal(name, calls.find(call => call[0] === "insert")[1])
        return bookmark
      },
    },
    async sync() {},
  }) } }
  globalThis.Word = Word
  globalThis.window = { Word }
  const location = {
    platform: "docx", block_id: "word:p:0", char_start: 0, char_end: 9,
    anchor_text: "裁判理由完整一句。",
  }
  const details = await seedSourceBookmarks({
    document_key: "sha256:test",
    verification: {
      citation_cards: [{ card_id: "card_1", source_locations: [location] }],
      case_checks: [],
    },
  })
  assert.deepEqual(details, { requested: 1, seeded: 1, failed: [] })
  const insertedName = calls.find(call => call[0] === "insert")[1]
  assert.match(insertedName, /^_cc[a-z0-9]+_[a-z0-9_]+_0$/)
  assert.deepEqual(calls, [["delete", "_CCOLD_0"], ["insert", insertedName]])
  const jump = await jumpToSource(
    { card_id: "card_1", source_locations: [location] },
    "sha256:test",
  )
  assert.equal(jump.method, "bookmark")
  assert.deepEqual(calls.at(-1), ["select", "bookmark"])
})

test("seedSourceBookmarks matches nested quoted sentences and removes footnote marks", async () => {
  globalThis.Office = { context: { requirements: { isSetSupported() { return false } } } }
  const calls = []
  const pieces = [
    { text: "前文\u0002他说：“第一句。", expandTo(last) {
      calls.push(["expand", last.text])
      return bookmarkRange
    } },
    { text: "第二句。" },
    { text: "”随后离开。" },
  ]
  const bookmarkRange = {
    insertBookmark(name) { calls.push(["insert", name]) },
  }
  const paragraphRange = {
    text: pieces.map(piece => piece.text).join("") + "\r",
    load() {},
    getTextRanges() { return { items: pieces, load() {} } },
  }
  const body = {
    paragraphs: {
      items: [{ tableNestingLevel: 0, getRange() { return paragraphRange } }],
      load() {},
    },
    tables: { items: [], load() {} },
    getRange() { return { getBookmarks() { return { value: [] } } } },
  }
  const Word = { async run(callback) { return callback({
    document: { body, deleteBookmark() {} },
    async sync() {},
  }) } }
  globalThis.Word = Word
  globalThis.window = { Word }
  const details = await seedSourceBookmarks({
    document_key: "sha256:quoted",
    verification: {
      citation_cards: [{
        card_id: "card_quoted",
        source_locations: [{
          platform: "docx",
          block_id: "word:p:0",
          anchor_text: "他说：“第一句。第二句。”",
        }],
      }],
      case_checks: [],
    },
  })
  assert.equal(details.seeded, 1)
  assert.deepEqual(details.failed, [])
  assert.deepEqual(calls[0], ["expand", "”随后离开。"])
})

test("clearSourceBookmarks is case-insensitive and disables automatic repair", async () => {
  globalThis.Office = { context: { requirements: { isSetSupported() { return true } } } }
  const deleted = []
  const footnoteRange = {
    getBookmarks() { return { value: ["_ccfootnote_0"] } },
  }
  const Word = { async run(callback) { return callback({
    document: {
      body: {
        getRange() { return { getBookmarks() { return { value: ["_CCMARK_0"] } } } },
        footnotes: {
          items: [{ body: { getRange() { return footnoteRange } } }],
          load() {},
        },
        endnotes: { items: [], load() {} },
      },
      deleteBookmark(name) { deleted.push(name) },
      getBookmarkRangeOrNullObject() { return { isNullObject: true, load() {} } },
    },
    async sync() {},
  }) } }
  globalThis.Word = Word
  globalThis.window = { Word }
  assert.equal(await clearSourceBookmarks(), 2)
  assert.deepEqual(deleted, ["_CCMARK_0", "_ccfootnote_0"])
  await assert.rejects(
    () => jumpToSource({
      card_id: "card_1",
      source_locations: [{
        platform: "docx", block_id: "word:p:0", anchor_text: "原文。",
      }],
    }, "sha256:test"),
    /定位标记已清除/,
  )
})

test("jumpToSource requires structured Word coordinates", async () => {
  globalThis.Office = { context: { requirements: { isSetSupported() { return false } } } }
  globalThis.Word = { async run() {} }
  globalThis.window = { Word: globalThis.Word }
  await assert.rejects(
    () => jumpToSource({ card_id: "card_1", source_locations: [] }, "sha256:test"),
    /缺少 Word 块定位信息/,
  )
})
