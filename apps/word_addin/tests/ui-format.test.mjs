import assert from "node:assert/strict"
import test from "node:test"

import {
  CheckUi,
  formatReference,
  orderChecksByCitation,
  sourceUrlOf,
  stripRepeatedArticleHeading,
} from "../assets/ui.js"

test("single-reference cards preserve card_id for bookmark lookup", () => {
  const ui = new CheckUi()
  ui.createResultCard = value => value
  const flattened = ui.createStatuteGroup({
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

import { statuteViewOf } from "../assets/statute-view-model.js"
import { caseViewOf } from "../assets/case-view-model.js"

test("badge text follows the renamed three-state scheme", () => {
  const issue = statuteViewOf({ outcome: "issue", findings: [{ code: "meaning_distorted", risk_level: "HIGH", suggestion: "改。" }], law_title: "著作权法" })
  assert.equal(issue.state, "issue")
  assert.equal(issue.badge.text, "未通过")

  const bug = statuteViewOf({ outcome: "bug", law_title: "刑法", lookup_status: "law_found_text_unavailable" })
  assert.equal(bug.state, "bug")
  assert.equal(bug.badge.text, "待核实")

  const pass = statuteViewOf({ outcome: "pass", law_title: "民法典", lookup_status: "article_found", meaning_check: { verdict: "pass" } })
  assert.equal(pass.badge.text, "通过")
  assert.equal(pass.typeLabel, "法律引用无问题")

  const listing = statuteViewOf({
    outcome: "pass", law_title: "网络数据安全管理条例",
    lookup_status: "law_found_text_unavailable", cited_locators: [],
    evidence: { law_title: "网络数据安全管理条例", article_text: null, data_source: { source_url: "https://pkulaw.com/chl/example.html" } },
  })
  assert.equal(listing.typeLabel, "法源存在性核验通过")
  assert.equal(listing.evidence.articleText, "")
  assert.match(listing.evidence.summaryLabel, /^权威来源/)
})

test("out-of-scope statutes surface the boundary message", () => {
  const view = statuteViewOf({
    law_title: "知识产权法典",
    outcome: "bug",
    lookup_status: "out_of_scope",
    source_attempts: [{ status: "out_of_scope", message: "涉外法规（非中国/欧盟法域），超出本产品核查边界，请人工核验" }],
  })
  assert.equal(view.state, "bug")
  assert.equal(view.badge.text, "待核实")
  assert.equal(view.typeLabel, "超出核查边界")
  assert.match(view.verdict.suggestion, /超出本产品核查边界/)
})

test("EU statutes verified by EUR-Lex read as existence-only pass", () => {
  const view = statuteViewOf({
    law_title: "通用数据保护条例",
    jurisdiction: "EU",
    outcome: "pass",
    lookup_status: "relevant_articles_found",
    evidence: {
      law_title: "Regulation (EU) 2016/679",
      version_status: "现行有效",
      data_source: { source_url: "https://eur-lex.europa.eu/eli/reg/2016/679/oj" },
    },
  })
  assert.equal(view.state, "pass")
  assert.equal(view.typeLabel, "欧盟法规：已核验存在性")
  assert.equal(view.refLine.status, null)
  assert.equal(view.evidence.url, "https://eur-lex.europa.eu/eli/reg/2016/679/oj")
})

test("case checks normalize into the same shape as statutes", () => {
  const view = caseViewOf({
    check_kind: "case",
    outcome: "bug",
    check_id: "cc_00001",
    claim_text: "在某案中……",
    cited_case_name: "腾讯诉上海盈讯公司著作权侵权案",
    lookup_status: "manual_review",
    message: "北大法宝已返回相关候选，但无法确定唯一对应案例，请人工确认",
    evidence: { title: "某判决书", court: "深圳市南山区人民法院" },
  })
  assert.equal(view.kind, "case")
  assert.equal(view.state, "bug")
  assert.equal(view.badge.text, "待核实")
  assert.equal(view.typeLabel, "司法案例 · 候选案例需人工确认")
  assert.equal(view.refLine.label, "核查对象")
  assert.equal(view.evidence.summaryLabel, "命中案例")
  assert.match(view.evidence.articleText, /深圳市南山区人民法院/)
})

test("finding card renders the self-contained suggestion without audit summary", () => {
  const view = statuteViewOf({
    outcome: "issue",
    law_title: "著作权司法解释",
    findings: [{
      code: "meaning_distorted",
      risk_level: "HIGH",
      summary: "第二十条规定的是出版者责任。",
      suggestion: "建议改引第十五条。",
    }],
  })
  assert.equal(view.verdict.suggestion, "建议改引第十五条。")
  assert.equal(view.refLine.status, null)
})

test("case candidates come from the explicit domain field", () => {
  const candidates = [{ title: "候选案例", case_number: "（2024）示例1号" }]
  const view = caseViewOf({
    outcome: "bug",
    check_id: "cc_1",
    cited_case_name: "某案",
    lookup_status: "manual_review",
    candidate_cases: candidates,
  })
  assert.deepEqual(view.candidates, candidates)
})

test("compact sub-references drop the quote and jump affordance", () => {
  const view = statuteViewOf(
    { outcome: "pass", law_title: "刑法", article_no: "第二百九十一条", claim_text: "整段引文", lookup_status: "article_found", meaning_check: { verdict: "pass" } },
    { compact: true },
  )
  assert.equal(view.quote, null)
  assert.equal(view.actions.jump, false)
  assert.equal(view.actions.decide, true)
})

test("recalled related articles keep the evidence section even without full text", () => {
  const view = statuteViewOf({
    law_title: "著作权法",
    outcome: "pass",
    lookup_status: "relevant_articles_found",
    evidence: {
      law_title: "中华人民共和国著作权法",
      related_articles: [{ article_no: "第三条", article_text: "本法所称的作品……" }],
      data_source: {},
    },
  })
  assert.notEqual(view.evidence, null)
  assert.equal(view.evidence.related.length, 1)
  assert.match(view.evidence.summaryLabel, /召回的相关条款/)
})

test("EU evidence headings use the Article convention with a separator", () => {
  const view = statuteViewOf({
    law_title: "通用数据保护条例",
    article_no: "第十七条",
    jurisdiction: "EU",
    outcome: "pass",
    lookup_status: "article_found",
    meaning_check: { verdict: "pass" },
    evidence: {
      law_title: "General Data Protection Regulation 2016/679",
      article_no: "Article 17",
      article_text: "Right to erasure…",
      data_source: { source_url: "https://eur-lex.europa.eu/x" },
    },
  })
  assert.equal(view.evidence.summaryLabel, "权威原文 · General Data Protection Regulation 2016/679 · Article 17")
  assert.equal(view.typeLabel, "法律引用无问题")
})

test("structure citations label distinctly from nested references", () => {
  const structure = statuteViewOf({
    law_title: "中华人民共和国民法典",
    outcome: "pass",
    article_no: "第三编第四章",
    lookup_status: "relevant_articles_found",
    evidence: {
      law_title: "中华人民共和国民法典",
      structure_path: "第三编 合同 / 第一分编 通则 / 第四章 合同的履行",
      related_articles: [{ article_no: "第五百零九条", article_text: "……" }],
      data_source: {},
    },
  })
  assert.equal(structure.state, "pass")
  assert.equal(structure.typeLabel, "章节引用：已核验存在")
  assert.match(structure.evidence.structurePath, /合同的履行/)

  const ambiguous = statuteViewOf({
    law_title: "中华人民共和国民法典",
    outcome: "bug",
    article_no: "第四章",
    lookup_status: "relevant_articles_found",
    meaning_check: { execution_status: "skipped", skipped_reason: "structure_ambiguous" },
    evidence: { law_title: "中华人民共和国民法典", structure_path: "候选：……", data_source: {} },
  })
  assert.equal(ambiguous.state, "bug")
  assert.equal(ambiguous.typeLabel, "章节引用存在多个候选，请人工确认")

  const nested = statuteViewOf({
    outcome: "pass",
    law_title: "中华人民共和国刑法",
    article_no: "第二百九十一条",
    reference_role: "nested",
    lookup_status: "article_found",
  })
  assert.equal(nested.typeLabel, "内部转引：仅核验存在性")
})
