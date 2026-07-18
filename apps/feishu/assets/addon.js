import { checkSnapshot } from "./api.js"
import { FeishuHost } from "./host.js"
import { buildResultCards } from "/assets/result-models.js"

function stripInternalMarkers(text) {
  return String(text || "").replace(/⟦[^⟧]*⟧|\[\[[^\[\]]{0,60}\]\]|【(?:锚点|anchor)[^】]*】|(?<![A-Za-z])line\d{4,6}(?!\d)/gi, "").trim()
}

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
  document.getElementById("results-subtitle").textContent = `${result.file_name} · 核查以法规现行有效版本为基准`
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
  if (check.references) return renderStatuteGroup(check)
  if (check.category === "case") return renderCaseCard(check)
  return renderStatuteCard(check, check.claim_text, check.location)
}

function renderStatuteCard(check, claimText, location, nested = false) {
  const article = document.createElement(nested ? "details" : "article")
  article.className = nested ? `reference-row is-${check.state}` : `result-card is-${check.state}`
  article.dataset.checkId = check.check_id
  const jurisdiction = check.jurisdiction && check.jurisdiction !== "CN" ? `<span class="jurisdiction-label">${escapeHtml(check.jurisdiction)}</span>` : ""
  const heading = `<span class="reference-source">${escapeHtml(check.title)}</span>${jurisdiction}<span class="status-pill is-${check.state}">${check.pill}</span>`
  const revision = check.finding?.revision
  const revisionButton = revision?.machine_applicable && revision.strategy === "replace_exact_text"
    ? `<div class="action-row"><button class="action-button decision-button" data-action="resolve">接受修订</button></div>`
    : ""
  article.innerHTML = `
    ${nested ? `<summary class="reference-row-summary">${heading}</summary>` : `${quoteZone(claimText)}<div class="zone-label-row"><span class="zone-label">核查对象</span></div><div class="reference-row-summary">${heading}</div>`}
    <div class="reference-body"><div class="reference-body-topline"><span class="card-type">${escapeHtml(check.type)}</span></div>
    ${check.finding ? `<div class="card-conf">风险分级：${escapeHtml(check.risk)}</div><p class="card-suggestion">${escapeHtml(findingText(check.finding))}</p>` : check.message && check.state === "bug" ? `<p class="card-suggestion">${escapeHtml(check.message)}</p>` : ""}
    ${renderStatuteDetails(check)}
    ${revisionButton}</div>`
  bindCardActions(article, location)
  return article
}

function renderStatuteGroup(card) {
  if (card.references.length === 1) return renderStatuteCard(card.references[0], card.claim_text, card.location)
  const article = document.createElement("article")
  article.className = "result-card statute-group is-multiple"
  article.dataset.cardId = card.card_id
  const counts = { issue: 0, bug: 0, pass: 0 }
  card.references.forEach(reference => { counts[reference.state] += 1 })
  article.innerHTML = `<div class="result-topline"><span class="card-type">本段共 ${card.references.length} 条引用</span><span class="multi-counts"><span class="count-issue">${counts.issue} 未通过</span><span class="count-bug">${counts.bug} 待核实</span><span class="count-pass">${counts.pass} 通过</span></span></div>${quoteZone(card.claim_text)}<div class="zone-label-row"><span class="zone-label">核查对象</span></div>`
  const references = createElement("div", "citation-references")
  card.references.forEach(reference => {
    const rendered = renderStatuteCard(reference, card.claim_text, card.location, true)
    references.append(rendered)
  })
  article.querySelector('[data-action="locate"]')?.addEventListener("click", () => locateSource(card.location))
  article.append(references)
  return article
}

function renderCaseCard(check) {
  const article = createElement("article", `result-card is-${check.state}`)
  article.dataset.checkId = check.check_id
  const candidates = caseCandidates(check)
  const candidateList = candidates.length ? `<details class="result-details" open><summary>参考案例（${candidates.length}）</summary>${candidates.map((candidate, index) => {
    const url = sourceUrl(candidate.url)
    const label = `${index + 1}. ${candidate.title || "未命名案例"}${candidate.case_number ? ` | ${candidate.case_number}` : ""}${candidate.court ? ` | ${candidate.court}` : ""}${candidate.last_instance_date ? ` | ${candidate.last_instance_date}` : ""}`
    return `<div class="statute-line">${escapeHtml(label)}${url ? ` | <a class="statute-link" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">查看原文</a>` : ""}</div>`
  }).join("")}</details>` : ""
  article.innerHTML = `${quoteZone(check.claim_text)}<div class="zone-label-row"><span class="zone-label">核查对象</span></div><details class="reference-row is-${check.state}"><summary class="reference-row-summary"><span class="reference-source">${escapeHtml(check.title)}</span>${check.jurisdiction && check.jurisdiction !== "CN" ? `<span class="jurisdiction-label">${escapeHtml(check.jurisdiction)}</span>` : ""}<span class="status-pill is-${check.state}">${check.pill}</span></summary><div class="reference-body"><div class="card-type">${escapeHtml(check.type)}</div>${check.message ? `<p class="card-suggestion">${escapeHtml(check.message)}</p>` : ""}${candidateList}</div></details>`
  bindCardActions(article, check.location)
  return article
}

function caseCandidates(check) {
  return (check.candidate_cases || []).slice(0, 10)
}

function renderStatuteDetails(check) {
  if (!check.evidence?.article_text && !check.url && !check.evidence?.structure_path) return ""
  const label = check.evidence?.article_text ? "权威原文" : "权威来源"
  return `<details class="result-details"><summary>${label}</summary>${check.evidence?.structure_path ? `<div class="statute-line">章节位置：${escapeHtml(check.evidence.structure_path)}</div>` : ""}${check.evidence?.article_text ? `<div class="authority-quote">${escapeHtml(check.evidence.article_text)}</div>` : ""}${check.url ? `<div class="statute-line">原文链接：<a class="statute-link" href="${escapeHtml(check.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(check.url)}</a></div>` : ""}</details>`
}

function quoteZone(text) {
  return `<div class="zone-label-row"><span class="zone-label">文书原文</span><button class="action-button jump-button" data-action="locate">定位原文</button></div><blockquote class="doc-quote">${escapeHtml(text)}</blockquote>`
}

function bindCardActions(article, location) {
  article.querySelector('[data-action="locate"]')?.addEventListener("click", () => locateSource(location))
  article.querySelector('[data-action="resolve"]')?.addEventListener("click", event => setDecision(article, event.currentTarget, "已接受"))
}

async function locateSource(location) {
  try { await host.scrollToLocation(location) } catch (error) { showToast(error.message) }
}

function normalizeChecks(result) {
  const newCards = buildResultCards(result.verification)
  return newCards.map(card => {
    if (card.check_kind === "case") {
      const finding = card.findings?.[0]
      return {
        ...card,
        pill: card.state === "pass" ? "通过" : card.state === "issue" ? "未通过" : "待核实",
        title: card.cited_case_number || card.cited_case_name || "司法案例",
        category: "case",
        finding,
        location: card.source_locations?.at(-1),
        message: finding ? findingText(finding) : card.message,
        url: sourceUrl(card.evidence?.url),
      }
    }
    const references = card.references.map(reference => {
      const finding = reference.findings?.[0]
      return {
        ...reference,
        pill: reference.state === "pass" ? "通过" : reference.state === "issue" ? "未通过" : "待核实",
        finding,
        risk: finding?.risk_level === "HIGH" ? "高" : finding?.risk_level === "MEDIUM" ? "中" : "",
        title: formatReference(reference),
        category: statuteCategory(finding?.code, reference.state),
        url: sourceUrl(reference.evidence?.data_source?.source_url),
      }
    })
    return {
      ...card,
      references,
      state: references.some(item => item.state === "issue") ? "issue" : references.some(item => item.state === "bug") ? "bug" : "pass",
      category: references.find(item => item.category !== "passed")?.category || "passed",
      location: card.source_locations?.at(-1),
    }
  })
}

function findingText(finding) {
  const summary = String(finding?.summary || "").trim().replace(/[。；]+$/, "")
  const suggestion = String(finding?.suggestion || "").trim()
  return suggestion || (summary ? `${summary}。` : "请人工复核该引用。")
}

function statuteCategory(code, state) {
  if (state === "pass") return "passed"
  if (["source_repealed", "source_amended"].includes(code)) return "timeliness"
  if (["source_not_found", "citation_location_error"].includes(code)) return "source"
  return code === "meaning_distorted" ? "meaning" : "review"
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
function escapeHtml(value = "") { const node = document.createElement("span"); node.textContent = stripInternalMarkers(value); return node.innerHTML }
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
      statute_results: [
        demoStatute("vc_00001", "card_00001", snapshot.blocks[1].text, "blk-demo-2", "反不正当竞争法", "第二条", "pass"),
        demoStatute("vc_00002", "card_00002", snapshot.blocks[2].text, "blk-demo-3", "网络数据安全管理条例", "第十八条", "issue"),
      ],
      case_results: [{ check_id: "cc_00001", claim_id: "claim_case_1", cited_case_name: "某平台爬虫纠纷案", claim_text: "法院已处理大量爬虫纠纷。", lookup_status: "manual_review", outcome: "bug", findings: [], message: "北大法宝返回了案例，但现有引用信息不足以证明是哪一份裁判文书。可参考案例如下。", source_locations: [{ platform: "feishu", block_id: "blk-demo-2", char_start: 0, char_end: 12 }] }],
    },
  }
}

function demoStatute(id, cardId, text, blockId, law, article, outcome) {
  return {
    check_id: id, card_id: cardId, claim_id: `claim_${id}`, claim_text: text,
    law_title: law, cited_locators: [{ article_no: article }], lookup_status: "article_found", outcome,
    findings: outcome === "issue" ? [{ code: "meaning_distorted", risk_level: "HIGH", diff_summary: "文中将“评估影响”扩张为独立的事前评估义务，并增加了本条没有直接规定的行政责任。", suggestion: "删除扩张表述；如需保留行政责任结论，请补充对应责任条款。" }] : [],
    evidence: { data_source: { source_url: "https://www.pkulaw.com/chl/example.html" } },
    source_locations: [{ platform: "feishu", block_id: blockId, char_start: 0, char_end: text.length }],
  }
}
