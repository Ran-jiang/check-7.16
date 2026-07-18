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
