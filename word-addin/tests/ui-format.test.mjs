import assert from "node:assert/strict"
import test from "node:test"

import { stripRepeatedArticleHeading } from "../assets/ui.js"

test("removes a repeated Chinese article heading when the card already has one", () => {
  const text = "第二百八十五条　违反国家规定，侵入计算机信息系统。\n第二款内容。"
  assert.equal(
    stripRepeatedArticleHeading(text, "第285条"),
    "违反国家规定，侵入计算机信息系统。\n第二款内容。"
  )
})

test("keeps article text untouched when no separate article number is shown", () => {
  const text = "第一条　第一条内容。\n第二条　第二条内容。"
  assert.equal(stripRepeatedArticleHeading(text, ""), text)
})
