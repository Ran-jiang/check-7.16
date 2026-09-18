import { BADGE_TEXT, sourceUrlOf, stripRepeatedArticleHeading } from "./view-model.js"

export const STATUTE_ERROR_LABELS = {
  source_not_found: "MCP未检索到所引法源",
  law_name_error: "法规名称错误",
  article_not_found: "所引条文待核实",
  format_error: "引用编号格式错误",
  article_number_error: "条文序号错误",
  citation_hierarchy_error: "条款项层级错误",
  source_repealed: "法源版本或效力错误",
  source_amended: "法源版本或效力错误",
}

export const APPLICATION_ERROR_LABELS = {
  rule_fact_mismatch: "法条与事实关联性弱",
  meaning_distorted: "引文不忠实于权威原文",
  legal_alias_inconsistent: "全文法规引用简称不一致",
  format_error: "格式错误",
}

export function formatReference(check) {
  if (check.law_identity_resolved === false) {
    return `${check.law_title || "未确定法名"}${check.article_no || ""}${(check.paragraphs || []).join("、")}${(check.items || []).join("、")}`
  }
  return `《${check.law_title}》${check.article_no || ""}${(check.paragraphs || []).join("、")}${(check.items || []).join("、")}`
}

export function statuteViewOf(check, options = {}) {
  const state = check.outcome || "bug"
  const findings = check.findings || []
  const applicationReviews = check.application_check?.reviews || []
  return {
    kind: "statute", checkId: check.check_id, cardId: check.card_id || null, state,
    badge: { state, text: BADGE_TEXT[state] || "未核查" },
    typeLabel: statuteTypeLabel(check, state, findings, applicationReviews),
    quote: options.compact ? null : check.claim_text || "",
    refLine: { label: "核查对象", text: formatReference(check), status: null },
    verdict: statuteVerdict(check, state, findings, applicationReviews), evidence: statuteEvidence(check),
    typeTags: [
      ...findings.map(finding => statuteErrorLabel(finding.code, check)),
      ...applicationReviews.map(review => APPLICATION_ERROR_LABELS[review.error_type] || review.error_type),
    ],
    actions: { jump: !options.compact, decide: true }, raw: check,
  }
}

function statuteTypeLabel(check, state, findings, applicationReviews) {
  const labels = findings.map(finding => statuteErrorLabel(finding.code, check))
  labels.push(...applicationReviews.map(review => APPLICATION_ERROR_LABELS[review.error_type] || review.error_type))
  if (labels.length) return labels.join("；")
  if (check.lookup_status === "out_of_scope") return "超出核查边界"
  if (state === "pass") {
    if (check.lookup_status === "law_found_text_unavailable" && !(check.cited_locators || []).length) return "法源存在性核验通过"
    if (check.jurisdiction === "EU" && !check.application_check) return "欧盟法规：已核验存在性"
    if (/[编章节]$/.test(check.article_no || "")) return "章节引用：已核验存在"
    if (check.reference_role === "nested") {
      if (check.relation_status === "resolved") return "内部转引：已确认关系并更正引用位置"
      return "内部转引：与主法条援引的规则一致"
    }
    return "法律引用无问题"
  }
  if (state === "review" && check.location_resolution) return "引文对应位置待核查"
  if (check.application_check?.execution_status === "llm_error") return "模型服务不可用"
  return sourceUnavailableLabel(check)
}

function statuteVerdict(check, state, findings, applicationReviews) {
  if (findings.length) {
    const first = findings[0]
    const applicationText = applicationReviewText(applicationReviews)
    return {
      riskText: first.risk_level === "HIGH" ? "高" : "中",
      suggestion: [findingText(first), applicationText].filter(Boolean).join("\n"),
    }
  }
  if (applicationReviews.length) {
    return {
      riskText: "待核实",
      suggestion: applicationReviewText(applicationReviews),
    }
  }
  if (check.lookup_status === "out_of_scope") {
    const message = (check.source_attempts || []).find(item => item.status === "out_of_scope")?.message
    return message ? { riskText: null, suggestion: message } : null
  }
  if (check.reference_role === "nested" && check.relation_message) {
    return { riskText: null, suggestion: check.relation_message }
  }
  if ((state === "bug" || state === "review") && check.message) {
    return { riskText: null, suggestion: check.message }
  }
  return null
}

function applicationReviewText(reviews) {
  const text = reviews
    .map(review => review.suggestion || review.summary)
    .filter(Boolean)
    .map(value => String(value).trim().replace(/[。；]+$/, ""))
    .filter(Boolean)
    .join("；")
  return text ? `${text}。` : ""
}

export function statuteErrorLabel(code, check = {}) {
  if (code === "article_not_found" && check.findings?.some(f => f.code === code && f.risk_level === "HIGH")) return "该版本中不存在所引条号"
  const label = STATUTE_ERROR_LABELS[code] || code
  return code === "source_not_found" ? `${sourceNameOf(check)}未检索到所引法源` : label
}

function sourceUnavailableLabel(check) {
  return `${sourceNameOf(check, ["source_error", "source_not_configured"])}不可用`
}

function sourceNameOf(check, statuses = ["law_not_found"]) {
  const attempts = check.source_attempts || []
  const trace = [...attempts].reverse().find(item => statuses.includes(item.status))
    || check.evidence?.data_source
  const fallback = check.jurisdiction === "EU"
    ? "EUR-Lex MCP"
    : check.jurisdiction === "CN" ? "北大法宝 MCP" : "Ansvar Gateway"
  const name = String(trace?.source_name || fallback).trim()
  return /\bMCP$/i.test(name) ? name : `${name} MCP`
}

function statuteEvidence(check) {
  const evidence = check.evidence
  const url = sourceUrlOf(check)
  const related = (evidence?.related_articles || []).map(item => ({ heading: item.locator || item.article_no || "", text: item.article_text || "" }))
  if (check.correction_evidence?.article_text) {
    related.push({ heading: `纠正候选 · ${check.correction_evidence.article_no || ""}`, text: check.correction_evidence.article_text })
    if (evidence?.article_text && !evidence?.related_articles?.length) {
      related.unshift({ heading: `原引用 · ${evidence.article_no || ""}`, text: evidence.article_text })
    }
  }
  // related_articles 是 article_text 聚合内容的逐条结构化版本；两者只能展示
  // 一种，否则同一色块会先显示整组法条，再逐条重复一次。
  const articleText = related.length ? "" : stripRepeatedArticleHeading(evidence?.article_text, check.article_no)
  const structurePath = evidence?.structure_path || ""
  if (!articleText && !related.length && !url && !structurePath) return null
  const lawTitle = evidence?.law_title || check.law_title
  const articleNo = evidence?.article_no || check.article_no || ""
  const heading = !articleNo ? lawTitle : /^第/.test(articleNo) ? `${lawTitle}${articleNo}` : `${lawTitle} · ${articleNo}`
  const summaryLabel = !articleText && !related.length
    ? `权威来源 · ${lawTitle}`
    : related.length && !articleText
      ? "权威原文 · 召回的相关条款"
      : `权威原文 · ${heading}`
  return { summaryLabel, articleHeading: articleText ? heading : "", articleText, related, url, structurePath }
}

function findingText(finding) {
  const summary = String(finding.summary || "").trim().replace(/[。；]+$/, "")
  const suggestion = String(finding.suggestion || "").trim()
  return suggestion || (summary ? `${summary}。` : "")
}
