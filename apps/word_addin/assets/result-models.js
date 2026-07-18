import { STATUTE_ERROR_LABELS } from "./statute-view-model.js"
import { CASE_ERROR_LABELS, CASE_STATUS_LABELS } from "./case-view-model.js"

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
    type: result.findings?.map(item => STATUTE_ERROR_LABELS[item.code] || item.code).join("；") || "法律引用无问题",
  }
}

export function normalizeCaseResult(result) {
  return {
    ...result,
    check_kind: "case",
    state: result.outcome,
    type: result.findings?.map(item => CASE_ERROR_LABELS[item.code] || item.code).join("；") || caseStatusLabel(result.lookup_status),
  }
}

export function findingLabel(finding, kind) {
  return (kind === "case" ? CASE_ERROR_LABELS : STATUTE_ERROR_LABELS)[finding.code] || finding.code
}

function caseStatusLabel(status) {
  return CASE_STATUS_LABELS[status] || status
}
