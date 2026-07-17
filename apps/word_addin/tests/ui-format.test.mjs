import assert from "node:assert/strict"
import test from "node:test"

import {
  CheckUi,
  checkState,
  formatReference,
  orderChecksByCitation,
  sourceUrlOf,
  stripRepeatedArticleHeading,
} from "../assets/ui.js"

test("single-reference cards preserve card_id for bookmark lookup", () => {
  const ui = new CheckUi()
  ui.createResultCard = value => value
  const flattened = ui.createCitationCard({
    card_id: "card_00001",
    claim_text: "引用原文。",
    source_locations: [{ block_id: "word:p:0" }],
    references: [{ check_id: "vc_00001" }],
  })
  assert.equal(flattened.card_id, "card_00001")
  assert.equal(flattened.check_id, "vc_00001")
})

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

test("cards follow citation order instead of verification state", () => {
  const checks = [
    { check_id: "vc_3", anchor_ids: ["line00003"], status: "issue" },
    { check_id: "vc_1", anchor_ids: ["line00001"], status: "pass" },
    { check_id: "vc_2", anchor_ids: ["line00002"], status: "bug" },
  ]
  assert.deepEqual(
    orderChecksByCitation(checks).map((item) => item.check_id),
    ["vc_1", "vc_2", "vc_3"],
  )
})

test("hides obsolete lar links returned by the MCP law-list service", () => {
  const check = { evidence: { data_source: {
    source_url: "[北大法宝](https://www.pkulaw.com/lar/dead.html?way=mcp)",
  } } }
  assert.equal(sourceUrlOf(check), "")
})

test("keeps exact-article chl links", () => {
  const url = "https://pkulaw.com/chl/current.html"
  assert.equal(sourceUrlOf({ evidence: { data_source: { source_url: url } } }), url)
})

test("formats one article with multiple paragraphs as one reference", () => {
  assert.equal(formatReference({
    law_title: "中华人民共和国商标法",
    article_no: "第十三条",
    paragraphs: ["第一款", "第三款"],
  }), "《中华人民共和国商标法》第十三条第一款、第三款")
})

test("each reference keeps its own verification state", () => {
  assert.equal(checkState({ rule_findings: [], semantic_comparison: { verdict: "pass" } }), "pass")
  assert.equal(checkState({ rule_findings: [{ error_type: "条款编号或引用定位错误" }] }), "issue")
})
