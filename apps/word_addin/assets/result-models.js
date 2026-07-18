const STATUTE_LABELS = {
  source_not_found: "北大法宝未检索到所引法源",
  citation_location_error: "条款编号或引用定位错误",
  source_repealed: "法源已废止或失效",
  source_amended: "法源已修改",
  meaning_distorted: "曲解权威文本原意",
}

const CASE_LABELS = {
  case_not_found: "北大法宝未检索到引用案例",
  case_identity_error: "案例引用信息错误",
  holding_not_in_case: "所述观点非该案裁判观点",
}

export function buildResultCards(verification) {
  const cards = new Map()
  for (const result of verification.statute_results || []) {
    const normalized = normalizeStatuteResult(result)
    const card = cards.get(result.card_id) || {
      card_id: result.card_id,
      claim_id: result.claim_id,
      claim_text: result.claim_text,
      source_locations: result.source_locations,
      references: [],
      check_kind: "statute-group",
    }
    card.references.push(normalized)
    cards.set(result.card_id, card)
  }
  return [
    ...cards.values(),
    ...(verification.case_results || []).map(normalizeCaseResult),
  ]
}

export function normalizeStatuteResult(result) {
  const locator = result.cited_locators?.[0] || {}
  return {
    ...result,
    check_kind: "statute",
    state: result.outcome,
    article_no: locator.article_no,
    paragraphs: result.cited_locators?.map(item => item.paragraph_no).filter(Boolean) || [],
    items: result.cited_locators?.map(item => item.item_no).filter(Boolean) || [],
    type: result.findings?.map(item => STATUTE_LABELS[item.code] || item.code).join("；") || "法律引用无问题",
  }
}

export function normalizeCaseResult(result) {
  return {
    ...result,
    check_kind: "case",
    state: result.outcome,
    type: result.findings?.map(item => CASE_LABELS[item.code] || item.code).join("；") || caseStatusLabel(result.lookup_status),
  }
}

export function findingLabel(finding, kind) {
  return (kind === "case" ? CASE_LABELS : STATUTE_LABELS)[finding.code] || finding.code
}

function caseStatusLabel(status) {
  return {
    verified: "案例已验证",
    not_found: "北大法宝未检索到引用案例",
    manual_review: "候选案例待确认",
    source_not_configured: "案例库凭证未配置",
    source_error: "案例数据源调用失败",
  }[status] || status
}
