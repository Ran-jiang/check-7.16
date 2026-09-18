import { APPLICATION_ERROR_LABELS, STATUTE_ERROR_LABELS, statuteErrorLabel } from "./statute-view-model.js"
import { CASE_ERROR_LABELS, CASE_STATUS_LABELS } from "./case-view-model.js"

export function buildResultCards(verification) {
  const cards = new Map()
  for (const result of verification.statute_results || []) {
    if (!result.display_group_id) throw new Error("法规核验结果缺少 display_group_id")
    const normalized = normalizeStatuteResult(result)
    const groupId = result.display_group_id
    const card = cards.get(groupId) || {
      card_id: result.card_id,
      display_group_id: groupId,
      claim_id: result.claim_id,
      claim_text: result.claim_text,
      source_locations: result.note_context?.referenced_from || result.source_locations,
      note_source_locations: result.note_context ? result.source_locations : [],
      sort_source_locations: result.note_context?.referenced_from || result.source_locations,
      note_context: result.note_context || null,
      references: [],
      check_kind: "statute-group",
    }
    if (result.note_context) {
      card.note_context = result.note_context
      card.note_source_locations = result.source_locations
      card.sort_source_locations = result.note_context.referenced_from || card.sort_source_locations
    }
    card.references.push(normalized)
    cards.set(groupId, card)
  }
  for (const result of verification.case_results || []) {
    if (!result.display_group_id) throw new Error("案例核验结果缺少 display_group_id")
    const normalized = normalizeCaseResult(result)
    const groupId = result.display_group_id
    const card = cards.get(groupId) || {
      card_id: result.check_id,
      display_group_id: groupId,
      claim_id: result.claim_id,
      claim_text: result.claim_text,
      source_locations: result.note_context?.referenced_from || result.source_locations,
      note_source_locations: result.note_context ? result.source_locations : [],
      sort_source_locations: result.note_context?.referenced_from || result.source_locations,
      note_context: result.note_context || null,
      references: [],
      check_kind: "statute-group",
    }
    if (result.note_context) {
      card.note_context = result.note_context
      card.note_source_locations = result.source_locations
      card.sort_source_locations = result.note_context.referenced_from || card.sort_source_locations
    }
    card.references.push(normalized)
    cards.set(groupId, card)
  }
  return [...cards.values()].map(card =>
    card.references.length === 1 && card.references[0].check_kind === "case"
      ? card.references[0]
      : card
  )
}

export function normalizeStatuteResult(result) {
  const locator = result.cited_locators?.[0] || {}
  const citationTypes = result.findings?.map(item => statuteErrorLabel(item.code, result)) || []
  const applicationTypes = result.application_check?.reviews?.map(
    item => APPLICATION_ERROR_LABELS[item.error_type] || item.error_type
  ) || []
  return {
    ...result,
    check_kind: "statute",
    state: result.outcome,
    article_no: locator.article_no,
    paragraphs: result.cited_locators?.map(item => item.paragraph_no).filter(Boolean) || [],
    items: result.cited_locators?.map(item => item.item_no).filter(Boolean) || [],
    type: [...citationTypes, ...applicationTypes].join("；") || "法律引用无问题",
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
  return kind === "case"
    ? CASE_ERROR_LABELS[finding.code] || finding.code
    : STATUTE_ERROR_LABELS[finding.code] || finding.code
}

function caseStatusLabel(status) {
  return CASE_STATUS_LABELS[status] || status
}
