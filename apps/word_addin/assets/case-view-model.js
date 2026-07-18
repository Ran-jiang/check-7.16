import { BADGE_TEXT, sourceUrlOf } from "./view-model.js"

export const CASE_STATUS_LABELS = {
  verified: "案例已核验", not_found: "案例未命中", manual_review: "候选案例需人工确认",
  source_not_configured: "案例数据源未配置", source_error: "案例数据源调用失败", out_of_scope: "超出核查边界",
}

export const CASE_ERROR_LABELS = {
  case_not_found: "北大法宝未检索到引用案例", case_identity_error: "案例引用信息错误",
  holding_not_in_case: "所述观点非该案裁判观点",
}

export function caseTypeOf(check) {
  return `司法案例 · ${CASE_STATUS_LABELS[check.lookup_status] || check.lookup_status}`
}

export function caseViewOf(check, options = {}) {
  const state = check.outcome || "bug"
  const findings = check.findings || []
  const first = findings[0]
  const verdict = first
    ? { riskText: first.risk_level === "HIGH" ? "高" : "中", suggestion: first.suggestion }
    : check.message ? { riskText: null, suggestion: check.message } : null
  const line = check.evidence
    ? `${check.evidence.title || check.evidence.case_number || ""}${check.evidence.court ? ` · ${check.evidence.court}` : ""}`
    : ""
  return {
    kind: "case", checkId: check.check_id, cardId: null, state,
    badge: { state, text: BADGE_TEXT[state] || "未核查" }, typeLabel: caseTypeOf(check),
    quote: options.compact ? null : check.claim_text || "",
    refLine: { label: "核查对象", text: check.cited_case_number || check.cited_case_name || "未命名案例线索", status: null },
    verdict,
    evidence: check.evidence ? { summaryLabel: "命中案例", articleHeading: "", articleText: line, related: [], url: sourceUrlOf(check) } : null,
    typeTags: findings.map(finding => CASE_ERROR_LABELS[finding.code] || finding.code),
    actions: { jump: !options.compact, decide: true }, raw: check,
  }
}
