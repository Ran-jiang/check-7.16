import assert from "node:assert/strict"
import test from "node:test"

import { buildResultCards } from "../assets/result-models.js"


test("new statute results are grouped without reading legacy findings", () => {
  const cards = buildResultCards({
    statute_results: [{
      check_id: "vc_1",
      card_id: "card_1",
      claim_id: "claim_1",
      claim_text: "引用内容",
      law_title: "民法典",
      cited_locators: [{ article_no: "第二条", paragraph_no: "第三款" }],
      findings: [{ code: "citation_location_error", risk_level: "HIGH" }],
      outcome: "issue",
      source_locations: [],
    }],
    case_results: [],
  })

  assert.equal(cards[0].references[0].type, "条款编号或引用定位错误")
  assert.equal(cards[0].references[0].state, "issue")
  assert.deepEqual(cards[0].references[0].paragraphs, ["第三款"])
})


test("case results use case-specific error labels", () => {
  const cards = buildResultCards({
    statute_results: [],
    case_results: [{
      check_id: "cc_1",
      claim_text: "引用某案",
      lookup_status: "verified",
      findings: [{ code: "case_identity_error", risk_level: "HIGH" }],
      outcome: "issue",
    }],
  })

  assert.equal(cards[0].type, "案例引用信息错误")
  assert.equal(cards[0].state, "issue")
})


test("statute and case results from the same source range share one card", () => {
  const cards = buildResultCards({
    statute_results: [{
      check_id: "vc_1", card_id: "card_1", display_group_id: "dg_same",
      claim_id: "claim_statute", claim_text: "同一句引用", law_title: "民法典",
      cited_locators: [{ article_no: "第一条" }], findings: [], outcome: "pass",
      source_locations: [],
    }],
    case_results: [{
      check_id: "cc_1", display_group_id: "dg_same", claim_id: "claim_case",
      claim_text: "同一句引用", cited_case_number: "（2021）最高法民申1号",
      lookup_status: "verified", findings: [], outcome: "pass", source_locations: [],
    }],
  })

  assert.equal(cards.length, 1)
  assert.equal(cards[0].references.length, 2)
  assert.deepEqual(cards[0].references.map(item => item.check_kind), ["statute", "case"])
})


test("footnote results retain their body reference location for grouping and sorting", () => {
  const noteContext = {
    note_type: "footnote",
    note_id: "2",
    referenced_from: [{ block_id: "word:p:3", char_start: 12, char_end: 12 }],
  }
  const cards = buildResultCards({
    statute_results: [{
      check_id: "vc_note", card_id: "card_note", display_group_id: "dg_note",
      claim_id: "claim_note", claim_text: "脚注中的法条引用", law_title: "民法典",
      cited_locators: [{ article_no: "第五百零九条" }], findings: [], outcome: "pass",
      source_locations: [{ block_id: "word:footnote:2" }], note_context: noteContext,
    }],
    case_results: [],
  })

  assert.deepEqual(cards[0].note_context, noteContext)
  assert.equal(cards[0].sort_source_locations[0].block_id, "word:p:3")
  assert.equal(cards[0].source_locations[0].block_id, "word:p:3")
  assert.equal(cards[0].note_source_locations[0].block_id, "word:footnote:2")
})
