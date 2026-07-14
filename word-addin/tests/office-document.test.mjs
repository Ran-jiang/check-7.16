import assert from "node:assert/strict"
import test from "node:test"

import { jumpToText } from "../assets/office-document.js"


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
