import { normalizeCaseResult, normalizeStatuteResult, findingLabel } from "/assets/result-models.js"
import { caseViewOf } from "/assets/case-view-model.js"
import { statuteViewOf } from "/assets/statute-view-model.js"
import { orderChecksByDocument } from "./web-order.js"

const state = { entry: "file", file: null, result: null, status: "all", accepted: new Set(), selected: null }
const $ = id => document.getElementById(id)

document.querySelectorAll(".entry-tab").forEach(button => button.addEventListener("click", () => switchEntry(button.dataset.entry)))
$("docx-file").addEventListener("change", event => selectFile(event.target.files[0]))
$("drop-zone").addEventListener("dragover", event => { event.preventDefault(); event.currentTarget.classList.add("is-dragging") })
$("drop-zone").addEventListener("dragleave", event => event.currentTarget.classList.remove("is-dragging"))
$("drop-zone").addEventListener("drop", event => { event.preventDefault(); event.currentTarget.classList.remove("is-dragging"); selectFile(event.dataTransfer.files[0]) })
$("check-button").addEventListener("click", runCheck)
$("home-button").addEventListener("click", showLanding)
$("new-check").addEventListener("click", showLanding)

function switchEntry(entry) {
  state.entry = entry
  document.querySelectorAll(".entry-tab").forEach(button => {
    const active = button.dataset.entry === entry
    button.classList.toggle("is-active", active)
    button.setAttribute("aria-selected", String(active))
  })
  $("file-entry").classList.toggle("is-hidden", entry !== "file")
  $("text-entry").classList.toggle("is-hidden", entry !== "text")
}

function selectFile(file) {
  if (!file) return
  if (!file.name.toLowerCase().endsWith(".docx")) return toast("请选择 DOCX 格式文书")
  if (file.size > 25 * 1024 * 1024) return toast("文档超过 25 MB 限制")
  state.file = file
  $("file-label").textContent = file.name
  $("drop-zone").classList.add("has-file")
}

async function loadModels() {
  const select = $("web-model")
  if (!select) return
  try {
    const response = await fetch("/api/models")
    if (!response.ok) throw new Error()
    const { models, default: fallback } = await response.json()
    select.innerHTML = ""
    for (const model of models) {
      const option = document.createElement("option")
      option.value = model.key
      option.textContent = model.configured ? model.label : `${model.label}（未配置密钥）`
      option.disabled = !model.configured
      select.append(option)
    }
    // 必须在全部插入后再设默认值：对未入 DOM 的 option 设 selected 会错乱
    if (fallback) select.value = fallback
    if (!select.value || select.selectedIndex < 0) {
      const first = [...select.options].find(o => !o.disabled)
      if (first) select.value = first.value
    }
  } catch {
    select.innerHTML = '<option value="">默认模型</option>'
  }
}
loadModels()

async function runCheck() {
  const selectedModel = $("web-model") && $("web-model").value
  const scope = { include_statutes: $("web-statutes").checked, include_cases: $("web-cases").checked, semantic_check: true, ...(selectedModel ? { model: selectedModel } : {}) }
  if (!scope.include_statutes && !scope.include_cases) return toast("请至少选择一种核查范围")
  let path, payload
  if (state.entry === "file") {
    if (!state.file) return toast("请先选择 DOCX 文书")
    path = "/api/web/checks"
    payload = { file_name: state.file.name, docx_base64: await fileBase64(state.file), ...scope }
  } else {
    const text = $("source-text").value.trim()
    if (!text) return toast("请先粘贴待核查文本")
    path = "/api/web/checks/text"
    payload = { file_name: "粘贴文本.docx", text, ...scope }
  }
  showOnly("progress")
  try {
    const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || `核查服务返回 ${response.status}`)
    state.result = data
    state.status = "all"
    state.accepted = new Set()
    state.selected = null
    renderWorkspace()
  } catch (error) {
    showLanding()
    toast(error.message || "核查失败")
  }
}

function renderWorkspace() {
  const { result } = state
  $("preview-title").textContent = `核查文件：${result.file_name}`
  const s = result.summary
  $("summary-line").textContent = `核查完成。共 ${s.reference_total} 条引用 · ${s.passed} 处通过 · ${s.issues} 处未通过 · ${s.bugs} 处待核实`
  $("download-button").href = `/api/web/sessions/${result.session_id}/document`
  $("download-button").classList.remove("is-disabled")
  renderStatusFilters()
  renderPreview()
  renderResults()
  showOnly("workspace")
}

function checks() {
  if (!state.result) return []
  const items = [
    ...state.result.verification.statute_results.map(item => ({ ...normalizeStatuteResult(item), check_kind: "statute" })),
    ...state.result.verification.case_results.map(item => ({ ...normalizeCaseResult(item), check_kind: "case" })),
  ]
  return orderChecksByDocument(items, state.result.preview_blocks)
}

function renderStatusFilters() {
  const counts = { all: checks().length, issue: 0, bug: 0, pass: 0 }
  checks().forEach(check => counts[check.outcome] = (counts[check.outcome] || 0) + 1)
  $("status-filters").replaceChildren(...[["all", "全部"], ["issue", "未通过"], ["bug", "待核实"], ["pass", "已通过"]].map(([value, label]) => {
    const button = el("button", `filter-button${state.status === value ? " is-active" : ""}`)
    button.type = "button"
    button.setAttribute("aria-pressed", String(state.status === value))
    button.append(label, el("em", "", String(counts[value] || 0)))
    button.addEventListener("click", () => { state.status = value; renderStatusFilters(); renderResults() })
    return button
  }))
}

function renderResults() {
  const visible = checks().filter(check => state.status === "all" || check.outcome === state.status)
  $("web-results").replaceChildren(...visible.map(createResultCard))
  if (!visible.length) $("web-results").append(el("div", "empty-state", "当前筛选条件下没有核查结果。"))
}

function createResultCard(check) {
  const view = check.check_kind === "case" ? caseViewOf(check, { compact: true }) : statuteViewOf(check, { compact: true })
  const card = el("article", `web-result-card is-${check.outcome}${state.selected === check.check_id ? " is-selected" : ""}`)
  card.dataset.checkId = check.check_id
  const top = el("button", "result-card-top")
  top.type = "button"
  top.append(el("h3", "result-reference", view.refLine.text || check.claim_text), el("span", `result-status is-${check.outcome}`, view.badge.text))
  top.addEventListener("click", () => selectCheck(check))
  card.append(top)
  const tags = el("div", "result-tags")
  ;(check.findings || []).forEach(finding => tags.append(el("span", "", findingLabel(finding, check.check_kind))))
  if (tags.childNodes.length) card.append(tags)
  if (view.verdict?.suggestion) card.append(el("p", "result-suggestion", view.verdict.suggestion))
  if (view.evidence) card.append(createEvidence(view.evidence))
  const proposal = revisionOf(check)
  if (proposal) {
    const accepted = state.accepted.has(check.check_id)
    const button = el("button", `revision-button${accepted ? " is-accepted" : ""}`, accepted ? "撤销修订" : "接受修订")
    button.type = "button"
    button.addEventListener("click", () => toggleRevision(check, accepted))
    card.append(button)
  }
  return card
}

function createEvidence(evidence) {
  const section = el("section", "web-evidence")
  section.append(el("div", "evidence-title", evidence.summaryLabel))
  if (evidence.structurePath) section.append(el("p", "evidence-path", `章节位置：${evidence.structurePath}`))
  if (evidence.articleText) section.append(el("blockquote", "", `${evidence.articleHeading ? `${evidence.articleHeading}　` : ""}${evidence.articleText}`))
  if (evidence.url) {
    const linkLine = el("p", "source-line")
    linkLine.append("原文链接：")
    const link = el("a", "source-link", evidence.url)
    link.href = evidence.url; link.target = "_blank"; link.rel = "noopener noreferrer"
    linkLine.append(link)
    section.append(linkLine)
  }
  return section
}

async function toggleRevision(check, accepted) {
  const url = `/api/web/sessions/${state.result.session_id}/revisions${accepted ? `/${check.check_id}` : ""}`
  const response = await fetch(url, accepted ? { method: "DELETE" } : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ check_id: check.check_id }) })
  const data = await response.json()
  if (!response.ok) return toast(data.detail || "修订操作失败")
  state.accepted = new Set(data.accepted_check_ids)
  renderPreview()
  renderResults()
  toast(accepted ? "已撤销该项修订" : "已加入修订版，可随时撤销")
}

function renderPreview() {
  const blocks = state.result.preview_blocks || []
  $("document-preview").replaceChildren(...blocks.map(block => {
    const tag = block.block_type === "heading" ? "h3" : "p"
    const node = el(tag, `preview-block is-${block.block_type}`)
    node.dataset.blockId = block.block_id
    let text = revisedBlockText(block.text)
    const related = checks().filter(check => (check.source_locations || []).some(location => location.block_id === block.block_id))
    if (!related.length) node.textContent = text
    else appendHighlightedText(node, text, related)
    node.addEventListener("click", () => related.length && selectCheck(related[0]))
    return node
  }))
}

function appendHighlightedText(node, text, related) {
  const ranges = related.map(check => {
    const needle = revisedClaim(check)
    const start = text.indexOf(needle)
    return start >= 0 ? { start, end: start + needle.length, check } : null
  }).filter(Boolean).sort((a, b) => a.start - b.start)
  if (!ranges.length) { node.textContent = text; return }
  let cursor = 0
  for (const range of ranges) {
    if (range.start < cursor) continue
    node.append(text.slice(cursor, range.start))
    const mark = el("mark", `citation-mark is-${range.check.outcome}${state.selected === range.check.check_id ? " is-selected" : ""}`, text.slice(range.start, range.end))
    mark.dataset.checkId = range.check.check_id
    node.append(mark)
    cursor = range.end
  }
  node.append(text.slice(cursor))
}

function selectCheck(check) {
  state.selected = check.check_id
  renderPreview(); renderResults()
  const preview = document.querySelector(`mark[data-check-id="${CSS.escape(check.check_id)}"]`) || document.querySelector(`[data-block-id="${CSS.escape(check.source_locations?.[0]?.block_id || "")}"]`)
  const card = document.querySelector(`.web-result-card[data-check-id="${CSS.escape(check.check_id)}"]`)
  centerInPane(preview, document.querySelector(".document-pane"))
  centerInPane(card, document.querySelector(".results-pane"))
}

function centerInPane(element, pane) {
  if (!element || !pane) return
  if (pane.scrollHeight > pane.clientHeight + 1) {
    const paneBox = pane.getBoundingClientRect()
    const elementBox = element.getBoundingClientRect()
    pane.scrollTo({
      top: pane.scrollTop + elementBox.top - paneBox.top - (pane.clientHeight - elementBox.height) / 2,
      behavior: "smooth",
    })
    return
  }
  element.scrollIntoView({ behavior: "smooth", block: "center" })
}

function revisedBlockText(text) { for (const check of checks()) if (state.accepted.has(check.check_id)) { const r = revisionOf(check); if (r && text.includes(r.original_text)) text = text.replace(r.original_text, r.revised_text) } return text }
function revisedClaim(check) { const r = revisionOf(check); return state.accepted.has(check.check_id) && r ? r.revised_text : check.claim_text }
function revisionOf(check) { const revisions = (check.findings || []).map(item => item.revision).filter(item => item?.machine_applicable && item.revised_text); return revisions.length === 1 ? revisions[0] : null }
function showLanding() { showOnly("landing") }
function showOnly(id) { document.body.dataset.screen = id; for (const section of ["landing", "progress", "workspace"]) $(section).classList.toggle("is-hidden", section !== id); window.scrollTo({ top: 0, behavior: "smooth" }) }
function fileBase64(file) { return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(",")[1]); reader.onerror = () => reject(new Error("文件读取失败")); reader.readAsDataURL(file) }) }
function toast(message) { $("toast").textContent = message; $("toast").classList.remove("is-hidden"); clearTimeout(toast.timer); toast.timer = setTimeout(() => $("toast").classList.add("is-hidden"), 5000) }
function el(tag, className = "", text = "") { const node = document.createElement(tag); if (className) node.className = className; if (text) node.textContent = text; return node }
