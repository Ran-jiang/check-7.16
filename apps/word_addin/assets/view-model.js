// 结果页统一视图模型：把法规引用与司法案例两类核查结果归一成同一形状，
// 渲染层（ui.js）只消费 ViewCheck，不再各写各的状态判定与文案映射。
// 本文件只做纯数据变换，不触碰 DOM，可在 node --test 下直接运行。

export const LOOKUP_STATUS_LABELS = {
  article_found: "已取得法条原文",
  relevant_articles_found: "已召回相关条款",
  law_found_article_missing: "法规存在，未找到该条",
  law_found_text_unavailable: "法规存在，条文全文不可用",
  law_not_found: "未检索到该法规",
  source_not_configured: "数据源未配置",
  source_error: "数据源调用失败",
  not_verifiable: "非法条类文件，不做条文核验",
  out_of_scope: "超出核查边界",
}

export const CASE_STATUS_LABELS = {
  verified: "案例已核验",
  not_found: "案例未命中",
  manual_review: "候选案例需人工确认",
  source_not_configured: "案例数据源未配置",
  source_error: "案例数据源调用失败",
  out_of_scope: "超出核查边界",
}

export const BADGE_TEXT = { pass: "通过", issue: "未通过", bug: "待核实" }

export function findingsOf(check) {
  return [...(check.rule_findings || []), ...(check.semantic_comparison?.issues || [])]
}

export function formatReference(check) {
  const paragraphs = (check.paragraphs || []).join("、")
  const items = (check.items || []).join("、")
  return `《${check.law_title}》${check.article_no || ""}${paragraphs}${items}`
}

export function checkState(check) {
  if (check.check_kind === "case") {
    return check.lookup_status === "verified" ? "pass" : check.lookup_status === "not_found" ? "issue" : "bug"
  }
  if (findingsOf(check).length) return "issue"
  // 章节引用多候选：存在性虽确认，但归属未定，转人工（与后端 bugs 口径一致）
  if (check.semantic_comparison?.skipped_reason === "structure_ambiguous") return "bug"
  if (check.verification_scope === "existence_only" && ["article_found", "relevant_articles_found"].includes(check.lookup_status)) return "pass"
  if (check.semantic_comparison?.verdict === "pass") return "pass"
  if (!check.semantic_comparison && ["article_found", "relevant_articles_found"].includes(check.lookup_status)) {
    return "pass"
  }
  return "bug"
}

export function stripRepeatedArticleHeading(text, articleNo) {
  if (!articleNo) return String(text || "")
  return String(text || "").replace(
    /^\s*第[〇零一二三四五六七八九十百千万两0-9]+条(?:之[〇零一二三四五六七八九十百千万两0-9]+)?[\s　]*/,
    ""
  )
}

// 法宝部分接口返回 Markdown 形式链接（[文本](URL)），归一化为纯 URL
export function sourceUrlOf(check) {
  const raw = check.evidence?.data_source?.source_url || check.evidence?.url || ""
  const match = String(raw).match(/\((https?:\/\/[^)]+)\)/)
  const url = match ? match[1] : String(raw).startsWith("http") ? String(raw) : ""
  try {
    const parsed = new URL(url)
    const legacyMcpUrl = parsed.pathname.startsWith("/lar/") && parsed.searchParams.get("way") === "mcp"
    const trusted = /(^|\.)pkulaw\.com$/i.test(parsed.hostname) || /(^|\.)europa\.eu$/i.test(parsed.hostname)
    return trusted && !legacyMcpUrl ? url : ""
  } catch {
    return ""
  }
}

export function caseTypeOf(check) {
  return `司法案例 · ${CASE_STATUS_LABELS[check.lookup_status] || check.lookup_status}`
}

function compareCheckLocation(left, right) {
  const anchor = check => Number(String(check.anchor_ids?.[0] || "").replace(/\D/g, "")) || Number.MAX_SAFE_INTEGER
  const leftId = left.card_id || left.check_id
  const rightId = right.card_id || right.check_id
  return anchor(left) - anchor(right) || leftId.localeCompare(rightId)
}

export function orderChecksByCitation(checks) {
  return [...checks].sort(compareCheckLocation)
}

function statuteTypeLabel(check, state, findings) {
  if (findings.length) return findings.map(f => f.error_type).join("；")
  if (check.lookup_status === "out_of_scope") return "超出核查边界"
  if (state === "pass") {
    if (check.jurisdiction === "EU" && check.verification_scope === "existence_only") return "欧盟法规：已核验存在性"
    if (check.verification_scope === "existence_only") {
      // 章节引用（第X编/章/节，无条号）与内部转引共用存在性核验，但语义不同
      if (/[编章节]$/.test(check.article_no || "")) return "章节引用：已核验存在"
      return "内部转引：仅核验存在性"
    }
    return "法律引用无问题"
  }
  if (check.semantic_comparison?.skipped_reason === "structure_ambiguous") return "章节引用存在多个候选，请人工确认"
  if (check.semantic_comparison?.execution_status === "llm_error") return "语义核查服务失败，可重试"
  if (check.semantic_comparison?.verdict === "insufficient_input") return "输入不足，需人工处理"
  if (state === "bug") return LOOKUP_STATUS_LABELS[check.lookup_status] || "未完成核查，需人工处理"
  return LOOKUP_STATUS_LABELS[check.lookup_status] || check.lookup_status
}

function outOfScopeMessage(check) {
  const trace = (check.source_attempts || []).find(item => item.status === "out_of_scope")
  return trace?.message || ""
}

function statuteVerdict(check, state, findings) {
  if (findings.length) {
    const first = findings[0]
    const risk = first.risk_level === "HIGH" ? "高" : first.risk_level === "MEDIUM" ? "中" : first.risk_level
    return { riskText: risk, suggestion: first.suggestion }
  }
  if (check.lookup_status === "out_of_scope") {
    const message = outOfScopeMessage(check)
    return message ? { riskText: null, suggestion: message } : null
  }
  if (state === "bug" && check.semantic_comparison?.notes) {
    return { riskText: null, suggestion: check.semantic_comparison.notes }
  }
  return null
}

function caseVerdict(check, state) {
  if (state === "issue") {
    return { riskText: null, suggestion: check.message || "请核实案例名称或案号，并以权威案例库的检索结果为准。" }
  }
  if (check.message) return { riskText: null, suggestion: check.message }
  return null
}

function joinLawHeading(lawTitle, articleNo) {
  if (!articleNo) return lawTitle
  // 中文条号（第X条）直接连写；欧盟体例（Article N）用间隔点衔接英文法名
  return /^第/.test(articleNo) ? `${lawTitle}${articleNo}` : `${lawTitle} · ${articleNo}`
}

function statuteEvidence(check) {
  const evidence = check.evidence
  const url = sourceUrlOf(check)
  const articleText = stripRepeatedArticleHeading(evidence?.article_text, check.article_no)
  const related = (evidence?.related_articles || []).map(item => ({
    heading: item.article_no || "",
    text: item.article_text || "",
  }))
  const structurePath = evidence?.structure_path || ""
  if (!articleText && !related.length && !url && !structurePath) return null
  const lawTitle = evidence?.law_title || check.law_title
  const articleNo = evidence?.article_no || check.article_no || ""
  const heading = joinLawHeading(lawTitle, articleNo)
  return {
    summaryLabel: related.length && !articleText
      ? `权威原文 · 召回的相关条款`
      : `权威原文 · ${heading}`,
    articleHeading: articleText ? heading : "",
    articleText,
    related,
    url,
    structurePath,
  }
}

function caseEvidence(check) {
  if (!check.evidence) return null
  const line = `${check.evidence.title || check.evidence.case_number || ""}${check.evidence.court ? ` · ${check.evidence.court}` : ""}`
  return {
    summaryLabel: "命中案例",
    articleHeading: "",
    articleText: line,
    related: [],
    url: sourceUrlOf(check),
  }
}

function versionStatusOf(check) {
  const evidence = check.evidence
  if (!evidence) return null
  let text = evidence.version_label || evidence.version_status || ""
  if (/^effective$/i.test(text)) text = "现行有效"
  // has_articles 等库内部状态码不面向用户展示
  if (!text || /^[a-z_]+$/i.test(text)) return null
  return { text, effective: /现行有效/.test(text) }
}

export function normalizeCheck(check, options = {}) {
  const compact = Boolean(options.compact)
  const kind = check.check_kind === "case" ? "case" : "statute"
  const state = checkState(check)
  const findings = findingsOf(check)
  const isCase = kind === "case"
  return {
    kind,
    checkId: check.check_id,
    cardId: check.card_id || null,
    state,
    badge: { state, text: BADGE_TEXT[state] || "未核查" },
    typeLabel: isCase ? caseTypeOf(check) : statuteTypeLabel(check, state, findings),
    quote: compact ? null : check.claim_text || "",
    refLine: isCase
      ? { label: "核查对象", text: check.cited_case_number || check.cited_case_name || "未命名案例线索", status: null }
      : { label: "核查对象", text: formatReference(check), status: versionStatusOf(check) },
    verdict: isCase ? caseVerdict(check, state) : statuteVerdict(check, state, findings),
    evidence: isCase ? caseEvidence(check) : statuteEvidence(check),
    typeTags: isCase ? [caseTypeOf(check)] : findings.map(f => f.error_type),
    actions: { jump: !compact, decide: true },
    raw: check,
  }
}
