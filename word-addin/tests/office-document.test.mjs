import assert from "node:assert/strict"
import test from "node:test"

import { getDocumentName, jumpToText } from "../assets/office-document.js"


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


test("jumpToText selects the requested repeated occurrence", async () => {
  const selected = []
  const items = [0, 1, 2].map(index => ({
    select() { selected.push(index) },
  }))
  const Word = {
    async run(callback) {
      await callback({
        document: {
          body: {
            search(needle) {
              assert.equal(needle, "重复引用")
              return { items, load() {} }
            },
          },
        },
        async sync() {},
      })
    },
  }
  globalThis.Word = Word
  globalThis.window = { Word }

  await jumpToText("重复引用", 1)
  assert.deepEqual(selected, [1])
})


test("jumpToText reports a stale or missing source snippet", async () => {
  const Word = {
    async run(callback) {
      await callback({
        document: {
          body: {
            search() { return { items: [], load() {} } },
          },
        },
        async sync() {},
      })
    },
  }
  globalThis.Word = Word
  globalThis.window = { Word }

  await assert.rejects(() => jumpToText("已删除文本"), /未在文档中找到/)
})
