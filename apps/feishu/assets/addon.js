import { checkSnapshot } from "./api.js"
import { FeishuHost } from "./host.js"

const host = new FeishuHost()
const views = ["start-view", "progress-view", "results-view"]
let lastResult = null
let progressTimer = null
let renderedChecks = []
let activeStatus = "all"
let activeType = ""

document.getElementById("start-button").addEventListener("click", runCheck)
document.getElementById("rerun-button").addEventListener("click", runCheck)
document.getElementById("brand-button").addEventListener("click", () => showView("start-view"))
document.getElementById("background-button").addEventListener("click", () => showToast("核查将在后台继续"))
document.getElementById("minimize-button").addEventListener("click", () => showToast("可使用飞书小组件的最小化按钮收起面板"))
document.getElementById("type-filter").addEventListener("change", event => {
  activeType = event.target.value
  renderVisibleChecks()
})

host.onDocumentChange(change => markAffectedChecksStale(change))

async function runCheck() {
  const scope = {
    include_statutes: document.getElementById("statute-toggle").checked,
    include_cases: document.getElementById("case-toggle").checked,
  }
  if (!scope.include_statutes && !scope.include_cases) {
    showToast("请至少选择一种核查范围（法律法规引用或司法案例引用）")
    return
  }
  showView("progress-view")
  startProgress()
  try {
    const snapshot = await host.readSnapshot()
    document.getElementById("document-name").textContent = snapshot.title || "当前飞书文档"
    lastResult = isDemo() ? await demoResult(snapshot) : await checkSnapshot(snapshot, scope)
    finishProgress()
    renderResult(lastResult)
  } catch (error) {
    stopProgress()
    showView("start-view")
    showToast(error.message || "核查失败，请稍后重试")
  }
}

function renderResult(result) {
  renderedChecks = normalizeChecks(result)
  activeStatus = "all"
  activeType = ""
  const summary = result.summary
  document.getElementById("results-title").innerHTML = `核查完成！发现引用句<em class="title-count">${summary.card_total}</em>处，共核查法律引用<em class="title-count">${summary.reference_total}</em>条；<em class="title-count">${summary.passed}</em>处已通过，<em class="title-count">${summary.issues}</em>处待核实，<em class="title-count">${summary.bugs}</em>处无法判断`
  document.getElementById("results-subtitle").textContent = result.file_name
  renderStatusFilter()
  renderTypeFilter()
  renderVisibleChecks()
  showView("results-view")
}

function renderStatusFilter() {
  const entries = renderedChecks.flatMap(item => item.references || [item])
  const counts = { all: entries.length, issue: entries.filter(item => item.state === "issue").length, bug: entries.filter(item => item.state === "bug").length, pass: entries.filter(item => item.state === "pass").length }
  const filter = document.getElementById("status-filter")
  filter.replaceChildren()
  for (const [value, label] of [["all", "全部"], ["issue", "待核实"], ["bug", "无法判断"], ["pass", "已通过"]]) {
    const button = document.createElement("button")
    button.type = "button"
    button.className = `status-tab${activeStatus === value ? " is-active" : ""}`
    button.setAttribute("role", "tab")
    button.innerHTML = `${label}<em class="status-count">${counts[value]}</em>`
    button.addEventListener("click", () => {
      activeStatus = value
      renderStatusFilter()
      renderVisibleChecks()
    })
    filter.append(button)
  }
}

function renderTypeFilter() {
  const select = document.getElementById("type-filter")
  const options = [...new Set(renderedChecks.flatMap(item => item.references || [item]).map(item => item.type).filter(Boolean))]
  select.replaceChildren(new Option("全部类型", ""))
  for (const value of options) select.append(new Option(value, value))
  select.value = activeType
}

function renderVisibleChecks() {
  const list = document.getElementById("results-list")
  list.replaceChildren()
  const visible = renderedChecks.filter(item => {
    const entries = item.references || [item]
    const statusMatch = activeStatus === "all" || entries.some(entry => entry.state === activeStatus)
    const typeMatch = !activeType || entries.some(entry => entry.type === activeType)
    return statusMatch && typeMatch
  })
  if (!visible.length) {
    const empty = document.createElement("div")
    empty.className = "empty-results"
    empty.textContent = "当前筛选条件下没有核查结果。"
    list.append(empty)
    return
  }
  for (const check of visible) list.append(renderCheck(check))
}

function renderCheck(check) {
  if (check.references) return renderCitationCard(check)
  if (check.category === "case") return renderCaseCard(check)
  return renderStatuteCard(check, check.claim_text, check.location)
}

function renderStatuteCard(check, claimText, location, nested = false) {
  const article = document.createElement("article")
  article.className = nested ? `citation-reference is-${check.state}` : `result-card is-${check.state}`
  article.dataset.checkId = check.check_id
  article.innerHTML = `
    <div class="result-topline"><span class="status-pill is-${check.state}">${check.pill}</span><span class="card-type">${escapeHtml(check.type)}</span></div>
    ${nested ? "" : `<blockquote class="claim-quote">${escapeHtml(claimText)}</blockquote>`}
    <div class="reference-source">${escapeHtml(check.title)}</div>
    ${check.finding ? `<div class="card-conf">风险分级：${escapeHtml(check.risk)}</div><p class="card-suggestion">${escapeHtml(check.finding.suggestion || "请人工复核该引用。")}</p>` : check.message && check.state === "bug" ? `<p class="card-suggestion">${escapeHtml(check.message)}</p>` : ""}
    ${renderStatuteDetails(check)}
    <div class="action-row">
      ${nested ? "" : `<button class="action-button jump-button" data-action="locate">定位原文</button>`}
      <div class="decision-group"><button class="action-button decision-button" data-action="defer">忽略</button><button class="action-button decision-button" data-action="resolve">接受</button></div>
    </div>`
  bindCardActions(article, location)
  return article
}

function renderCitationCard(card) {
  if (card.references.length === 1) return renderStatuteCard(card.references[0], card.claim_text, card.location)
  const article = document.createElement("article")
  article.className = "result-card citation-card is-multiple"
  article.dataset.cardId = card.card_id
  article.append(createElement("blockquote", "claim-quote", card.claim_text))
  const references = createElement("div", "citation-references")
  card.references.forEach((reference, index) => {
    const rendered = renderStatuteCard(reference, card.claim_text, card.location, true)
    rendered.prepend(createElement("div", "reference-label", `引用 ${index + 1}${reference.reference_role === "nested" ? " · 内部转引" : reference.reference_role === "inherited" ? " · 承前引用" : ""}`))
    references.append(rendered)
  })
  const locate = createElement("button", "action-button jump-button", "定位原文")
  locate.type = "button"
  locate.addEventListener("click", () => locateSource(card.location))
  const row = createElement("div", "action-row card-action-row")
  row.append(locate)
  article.append(references, row)
  return article
}

function renderCaseCard(check) {
  const article = createElement("article", `result-card is-${check.state}`)
  article.dataset.checkId = check.check_id
  article.innerHTML = `<div class="result-topline"><div class="card-type">${escapeHtml(check.type)}</div><span class="status-pill is-${check.state}">${check.pill}</span></div><blockquote class="claim-quote">${escapeHtml(check.claim_text)}</blockquote><div class="card-conf">引用线索：${escapeHtml(check.title)}</div>${check.message ? `<p class="card-suggestion">${escapeHtml(check.message)}</p>` : ""}<div class="action-row"><button class="action-button jump-button" data-action="locate">定位原文</button><div class="decision-group"><button class="action-button decision-button" data-action="defer">忽略</button><button class="action-button decision-button" data-action="resolve">接受</button></div></div>`
  bindCardActions(article, check.location)
  return article
}

function renderStatuteDetails(check) {
  if (!check.evidence?.article_text && !check.url) return ""
  return `<details class="result-details"><summary>查看法条原文</summary>${check.url ? `<div class="statute-line">原文链接：<a class="statute-link" href="${escapeHtml(check.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(check.url)}</a></div>` : ""}${check.evidence?.article_text ? `<div class="statute-line">原文内容：<span class="statute-text-inline">${escapeHtml(check.evidence.article_text)}</span></div>` : ""}</details>`
}

function bindCardActions(article, location) {
  article.querySelector('[data-action="locate"]')?.addEventListener("click", () => locateSource(location))
  article.querySelector('[data-action="resolve"]').addEventListener("click", event => setDecision(article, event.currentTarget, "已接受"))
  article.querySelector('[data-action="defer"]').addEventListener("click", event => setDecision(article, event.currentTarget, "已忽略"))
}

async function locateSource(location) {
  try { await host.scrollToLocation(location) } catch (error) { showToast(error.message) }
}

function normalizeChecks(result) {
  const legal = result.verification.citation_cards.map(card => {
    const references = card.references.map(check => {
    const findings = [...(check.rule_findings || []), ...(check.semantic_comparison?.issues || [])]
    const finding = findings[0]
    const existenceOnly = check.verification_scope === "existence_only" && ["article_found", "relevant_articles_found"].includes(check.lookup_status)
    const state = finding ? "issue" : (check.semantic_comparison?.verdict === "pass" || existenceOnly) ? "pass" : "bug"
    const type = finding?.error_type || (existenceOnly ? "内部转引：仅核验存在性" : state === "pass" ? "法律引用无问题" : "未完成核查，需人工处理")
      return {
      ...check,
      state,
      pill: state === "issue" ? "待核实" : state === "pass" ? "通过" : "无法判断",
      type,
      finding,
      risk: finding?.risk_level === "HIGH" ? "高" : finding?.risk_level === "MEDIUM" ? "中" : finding?.risk_level || "",
      message: check.semantic_comparison?.notes || check.message || "",
      title: formatReference(check),
      url: sourceUrl(check.evidence?.data_source?.source_url),
      category: categoryForLegal(check, finding, state),
      }
    })
    const state = references.some(item => item.state === "issue") ? "issue"
      : references.some(item => item.state !== "pass") ? "bug" : "pass"
    const category = references.find(item => item.category !== "passed")?.category || "passed"
    return { ...card, references, state, category, location: card.source_locations?.at(-1) }
  })
  const cases = (result.verification?.case_checks || []).map(check => ({
    ...check,
    state: check.lookup_status === "verified" ? "pass" : check.lookup_status === "not_found" ? "issue" : "bug",
    pill: check.lookup_status === "verified" ? "通过" : check.lookup_status === "not_found" ? "待核实" : "无法判断",
    type: ({ verified: "案例已验证", not_found: "案例未命中", manual_review: "候选案例需人工确认", source_not_configured: "案例源未配置", source_error: "案例检索失败" })[check.lookup_status] || "司法案例",
    title: check.cited_case_number || check.cited_case_name || "司法案例",
    category: check.lookup_status === "verified" ? "passed" : "case",
    finding: check.message ? { diff_summary: check.message } : null,
    url: sourceUrl(check.evidence?.url),
    location: check.source_locations?.at(-1),
  }))
  return [...legal, ...cases]
}

function categoryLabel(value) {
  return {
    source: "法规名称或条号问题",
    timeliness: "法规时效问题",
    meaning: "内容一致性问题",
    case: "司法案例问题",
    review: "建议人工复核",
    passed: "已通过",
  }[value] || value
}

function categoryForLegal(check, finding, state) {
  if (state === "pass") return "passed"
  if (!finding) return "review"
  if (finding.error_type === "法源已废止或失效") return "timeliness"
  if (["法律渊源不存在", "条款编号或引用定位错误"].includes(finding.error_type)) return "source"
  return "meaning"
}

function setDecision(article, button, label) {
  article.classList.add("is-decided")
  for (const candidate of article.querySelectorAll(".decision-button")) candidate.classList.toggle("is-active", candidate === button)
  const pill = article.querySelector(".status-pill")
  if (pill) pill.textContent = label
}

function markAffectedChecksStale(change) {
  if (!lastResult || !change?.blockId) return
  for (const card of document.querySelectorAll(".result-card")) {
    const check = normalizeChecks(lastResult).find(item => item.check_id === card.dataset.checkId || item.card_id === card.dataset.cardId)
    if (check?.location?.block_id === change.blockId) {
      card.classList.add("finding-stale")
      const pill = card.querySelector(".status-pill")
      if (pill) pill.textContent = "待重新核查"
    }
  }
}

function showView(id) {
  for (const view of views) document.getElementById(view).classList.toggle("is-hidden", view !== id)
}

function startProgress() {
  let value = 0
  stopProgress()
  progressTimer = setInterval(() => {
    value = Math.min(value + Math.ceil((90 - value) / 8), 90)
    updateProgress(value)
  }, 240)
}

function finishProgress() { stopProgress(); updateProgress(100) }
function stopProgress() { if (progressTimer) clearInterval(progressTimer); progressTimer = null }
function updateProgress(value) {
  document.getElementById("progress-bar").style.transform = `scaleX(${value / 100})`
  document.getElementById("progress-count").textContent = `${Math.round(value * 24 / 100)} / 24`
  const stages = [...document.querySelectorAll(".stages li")]
  const step = 100 / stages.length
  stages.forEach((stage, index) => stage.className = value >= (index + 1) * step ? "is-complete" : value >= index * step ? "is-active" : "")
}

function showToast(message) {
  const toast = document.getElementById("toast")
  toast.textContent = message
  toast.classList.remove("is-hidden")
  setTimeout(() => toast.classList.add("is-hidden"), 2800)
}

function sourceUrl(raw = "") {
  const match = String(raw).match(/https?:\/\/[^)\s]+/)
  if (!match) return ""
  try {
    const parsed = new URL(match[0])
    return parsed.pathname.startsWith("/lar/") && parsed.searchParams.get("way") === "mcp" ? "" : match[0]
  } catch { return "" }
}
function formatReference(check) {
  return `《${check.law_title}》${check.article_no || ""}${(check.paragraphs || []).join("、")}${(check.items || []).join("、")}`
}
function escapeHtml(value = "") { const node = document.createElement("span"); node.textContent = value; return node.innerHTML }
function createElement(tag, className = "", text = "") {
  const node = document.createElement(tag)
  if (className) node.className = className
  if (text) node.textContent = text
  return node
}
function isDemo() { return !host.isAvailable || new URLSearchParams(location.search).has("demo") }

async function demoResult(snapshot) {
  await new Promise(resolve => setTimeout(resolve, 1700))
  return {
    file_name: snapshot.title,
    summary: { total: 3, card_total: 2, reference_total: 3, passed: 1, issues: 1, bugs: 1 },
    verification: {
      citation_cards: [
        demoCard("card_00001", snapshot.blocks[1].text, "blk-demo-2", [demoLegal("vc_00001", "反不正当竞争法", "第二条", "法律引用无问题", "pass")]),
        demoCard("card_00002", snapshot.blocks[2].text, "blk-demo-3", [demoLegal("vc_00002", "网络数据安全管理条例", "第十八条", "文中将“评估影响”扩张为独立的事前评估义务，并增加了本条没有直接规定的行政责任。", "issue")]),
      ],
      case_checks: [{ check_id: "cc_00001", cited_case_name: "某平台爬虫纠纷案", claim_text: "法院已处理大量爬虫纠纷。", lookup_status: "manual_review", message: "未附案号，相关案例无法唯一确认。", source_locations: [{ platform: "feishu", block_id: "blk-demo-2", char_start: 0, char_end: 12 }] }],
    },
  }
}

function demoCard(id, text, blockId, references) {
  return { card_id: id, claim_text: text, references, source_locations: [{ platform: "feishu", block_id: blockId, char_start: 0, char_end: text.length }] }
}

function demoLegal(id, law, article, summary, state) {
  return {
    check_id: id, law_title: law, article_no: article, lookup_status: "article_found",
    rule_findings: state === "issue" ? [{ error_type: "曲解权威文本原意", risk_level: "HIGH", diff_summary: summary, suggestion: "删除扩张表述；如需保留行政责任结论，请补充对应责任条款。" }] : [],
    semantic_comparison: state === "pass" ? { verdict: "pass", issues: [] } : null,
    evidence: { data_source: { source_url: "https://www.pkulaw.com/chl/example.html" } },
  }
}
