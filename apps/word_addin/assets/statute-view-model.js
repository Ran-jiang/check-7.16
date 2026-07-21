import { BADGE_TEXT, sourceUrlOf, stripRepeatedArticleHeading } from "./view-model.js"

export const LOOKUP_STATUS_LABELS = {
  article_found: "已取得法条原文", relevant_articles_found: "已召回相关条款",
  law_found_article_missing: "法规存在，未找到该条", law_found_text_unavailable: "法规存在，条文全文不可用",
  law_not_found: "未检索到该法规", source_not_configured: "数据源未配置",
  source_error: "数据源调用失败", not_verifiable: "非法条类文件，不做条文核验",
  out_of_scope: "超出核查边界",
}

export const STATUTE_ERROR_LABELS = {
  source_not_found: "北大法宝未检索到所引法源", citation_location_error: "条款编号或引用定位错误",
  source_name_ambiguous: "法规名称无法确定",
  source_repealed: "法源已废止或失效", source_amended: "法源已修改", meaning_distorted: "曲解权威文本原意",
}

export function formatReference(check) {
  if (check.source_resolution === "bare_unresolved") {
    return `${check.law_title || "未确定法名"}${check.article_no || ""}${(check.paragraphs || []).join("、")}${(check.items || []).join("、")}`
  }
  return `《${check.law_title}》${check.article_no || ""}${(check.paragraphs || []).join("、")}${(check.items || []).join("、")}`
}

export function statuteViewOf(check, options = {}) {
  const state = check.outcome || "bug"
  const findings = check.findings || []
  return {
    kind: "statute", checkId: check.check_id, cardId: check.card_id || null, state,
    badge: { state, text: BADGE_TEXT[state] || "未核查" },
    typeLabel: statuteTypeLabel(check, state, findings),
    quote: options.compact ? null : check.claim_text || "",
    refLine: { label: "核查对象", text: formatReference(check), status: null },
    verdict: statuteVerdict(check, state, findings), evidence: statuteEvidence(check),
    typeTags: findings.map(finding => STATUTE_ERROR_LABELS[finding.code] || finding.code),
    actions: { jump: !options.compact, decide: true }, raw: check,
  }
}

function statuteTypeLabel(check, state, findings) {
  if (findings.length) return findings.map(f => STATUTE_ERROR_LABELS[f.code] || f.code).join("；")
  if (check.lookup_status === "out_of_scope") return "超出核查边界"
  if (state === "pass") {
    if (check.lookup_status === "law_found_text_unavailable" && !(check.cited_locators || []).length) return "法源存在性核验通过"
    if (check.jurisdiction === "EU" && !check.meaning_check) return "欧盟法规：已核验存在性"
    if (/[编章节]$/.test(check.article_no || "")) return "章节引用：已核验存在"
    if (check.reference_role === "nested") return "内部转引：仅核验存在性"
    return "法律引用无问题"
  }
  if (check.meaning_check?.skipped_reason === "structure_ambiguous") return "章节引用存在多个候选，请人工确认"
  if (check.meaning_check?.execution_status === "llm_error") return "语义核查服务失败，可重试"
  if (check.meaning_check?.verdict === "insufficient_input") return "输入不足，需人工处理"
  return LOOKUP_STATUS_LABELS[check.lookup_status] || "未完成核查，需人工处理"
}

function statuteVerdict(check, state, findings) {
  if (findings.length) {
    const first = findings[0]
    return { riskText: first.risk_level === "HIGH" ? "高" : "中", suggestion: findingText(first) }
  }
  if (check.lookup_status === "out_of_scope") {
    const message = (check.source_attempts || []).find(item => item.status === "out_of_scope")?.message
    return message ? { riskText: null, suggestion: message } : null
  }
  return state === "bug" && check.meaning_check?.notes ? { riskText: null, suggestion: check.meaning_check.notes } : null
}

function statuteEvidence(check) {
  const evidence = check.evidence
  const url = sourceUrlOf(check)
  const articleText = stripRepeatedArticleHeading(evidence?.article_text, check.article_no)
  const related = (evidence?.related_articles || []).map(item => ({ heading: item.article_no || "", text: item.article_text || "" }))
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
