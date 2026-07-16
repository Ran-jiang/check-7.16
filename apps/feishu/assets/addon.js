import { checkSnapshot } from "./api.js"
import { FeishuHost } from "./host.js"

const host = new FeishuHost()
const views = ["start-view", "progress-view", "results-view"]
let lastResult = null
let progressTimer = null

document.getElementById("start-button").addEventListener("click", runCheck)
document.getElementById("rerun-button").addEventListener("click", runCheck)
document.getElementById("background-button").addEventListener("click", () => showToast("核查将在后台继续"))
document.getElementById("minimize-button").addEventListener("click", () => showToast("可使用飞书小组件的最小化按钮收起面板"))

host.onDocumentChange(change => markAffectedChecksStale(change))

async function runCheck() {
  showView("progress-view")
  startProgress()
  try {
    const snapshot = await host.readSnapshot()
    lastResult = isDemo() ? await demoResult(snapshot) : await checkSnapshot(snapshot)
    finishProgress()
    renderResult(lastResult)
  } catch (error) {
    stopProgress()
    showView("start-view")
    showToast(error.message || "核查失败，请稍后重试")
  }
}

function renderResult(result) {
  const checks = normalizeChecks(result)
  const categories = groupChecks(checks)
  document.getElementById("issue-total").textContent = String(checks.filter(item => item.state !== "pass").length)
  const list = document.getElementById("category-list")
  list.replaceChildren()
  for (const [index, category] of categories.entries()) {
    const details = document.createElement("details")
    details.className = "category"
    details.open = index === 0 && category.key !== "passed"
    const summary = document.createElement("summary")
    summary.innerHTML = `<span class="category-icon">${category.icon}</span><b>${escapeHtml(category.label)}</b><em>${category.items.length}</em><i></i>`
    details.append(summary)
    const items = document.createElement("div")
    items.className = "category-items"
    for (const check of category.items) items.append(renderCheck(check))
    details.append(items)
    list.append(details)
  }
  showView("results-view")
}

function renderCheck(check) {
  const article = document.createElement("article")
  article.className = `finding finding-${check.state}`
  article.dataset.checkId = check.check_id
  const finding = check.finding
  article.innerHTML = `
    <div class="finding-head"><span>${escapeHtml(check.title)}</span><em>${check.state === "stale" ? "待重新核查" : check.risk}</em></div>
    <blockquote>${escapeHtml(check.claim_text)}</blockquote>
    ${finding ? `<p>${escapeHtml(finding.diff_summary || check.message || "建议人工复核")}</p>` : ""}
    ${finding?.suggestion ? `<div class="suggestion"><span>处理建议</span>${escapeHtml(finding.suggestion)}</div>` : ""}
    <div class="finding-actions">
      <button data-action="locate">定位</button>
      ${check.url ? `<a href="${escapeHtml(check.url)}" target="_blank" rel="noopener noreferrer">查看法条</a>` : ""}
      <button data-action="resolve">标记已处理</button>
      <button data-action="defer">暂不处理</button>
    </div>`
  article.querySelector('[data-action="locate"]').addEventListener("click", async () => {
    try { await host.scrollToLocation(check.location) } catch (error) { showToast(error.message) }
  })
  article.querySelector('[data-action="resolve"]').addEventListener("click", () => setDecision(article, "已处理"))
  article.querySelector('[data-action="defer"]').addEventListener("click", () => setDecision(article, "暂不处理"))
  return article
}

function normalizeChecks(result) {
  const legal = (result.verification?.legal_checks || []).map(check => {
    const findings = [...(check.rule_findings || []), ...(check.semantic_comparison?.issues || [])]
    const finding = findings[0]
    const state = finding ? "issue" : check.semantic_comparison?.verdict === "pass" ? "pass" : "review"
    return {
      ...check,
      state,
      finding,
      risk: finding?.risk_level === "HIGH" ? "高风险" : state === "pass" ? "已通过" : "建议复核",
      title: `${check.law_title || "法规引用"}${check.article_no || ""}`,
      url: sourceUrl(check.evidence?.data_source?.source_url),
      location: check.source_locations?.at(-1),
      category: categoryForLegal(check, finding, state),
    }
  })
  const cases = (result.verification?.case_checks || []).map(check => ({
    ...check,
    state: check.lookup_status === "verified" ? "pass" : "review",
    risk: check.lookup_status === "verified" ? "已通过" : "建议复核",
    title: check.cited_case_number || check.cited_case_name || "司法案例",
    category: check.lookup_status === "verified" ? "passed" : "case",
    finding: check.message ? { diff_summary: check.message } : null,
    url: sourceUrl(check.evidence?.url),
    location: check.source_locations?.at(-1),
  }))
  return [...legal, ...cases]
}

function groupChecks(checks) {
  const definitions = [
    ["source", "法规名称或条号问题", "§"],
    ["timeliness", "法规时效问题", "◷"],
    ["meaning", "内容一致性问题", "≠"],
    ["case", "司法案例问题", "⌘"],
    ["review", "建议人工复核", "?"],
    ["passed", "已通过", "✓"],
  ]
  return definitions.map(([key, label, icon]) => ({ key, label, icon, items: checks.filter(item => item.category === key) })).filter(group => group.items.length)
}

function categoryForLegal(check, finding, state) {
  if (state === "pass") return "passed"
  if (!finding) return "review"
  if (finding.error_type === "旧法旧规误用") return "timeliness"
  if (["法律渊源不存在", "条款编号或引用定位错误"].includes(finding.error_type)) return "source"
  return "meaning"
}

function setDecision(article, label) {
  article.classList.add("is-decided")
  article.querySelector(".finding-head em").textContent = label
}

function markAffectedChecksStale(change) {
  if (!lastResult || !change?.blockId) return
  for (const card of document.querySelectorAll(".finding")) {
    const check = normalizeChecks(lastResult).find(item => item.check_id === card.dataset.checkId)
    if (check?.location?.block_id === change.blockId) {
      card.classList.add("finding-stale")
      card.querySelector(".finding-head em").textContent = "待重新核查"
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
  stages.forEach((stage, index) => stage.className = value >= (index + 1) * 30 ? "is-complete" : value >= index * 30 ? "is-active" : "")
}

function showToast(message) {
  const toast = document.getElementById("toast")
  toast.textContent = message
  toast.classList.remove("is-hidden")
  setTimeout(() => toast.classList.add("is-hidden"), 2800)
}

function sourceUrl(raw = "") {
  const match = String(raw).match(/https?:\/\/[^)\s]+/)
  return match?.[0] || ""
}
function escapeHtml(value = "") { const node = document.createElement("span"); node.textContent = value; return node.innerHTML }
function isDemo() { return !host.isAvailable || new URLSearchParams(location.search).has("demo") }

async function demoResult(snapshot) {
  await new Promise(resolve => setTimeout(resolve, 1700))
  return {
    file_name: snapshot.title,
    summary: { total: 5, passed: 2, issues: 2, bugs: 1 },
    verification: {
      legal_checks: [
        demoLegal("vc_00001", "反不正当竞争法", "第二条", snapshot.blocks[1].text, "法律引用无问题", "pass", "blk-demo-2"),
        demoLegal("vc_00002", "网络数据安全管理条例", "第十八条", snapshot.blocks[2].text, "文中将“评估影响”扩张为独立的事前评估义务，并增加了本条没有直接规定的行政责任。", "issue", "blk-demo-3"),
      ],
      case_checks: [{ check_id: "cc_00001", cited_case_name: "某平台爬虫纠纷案", claim_text: "法院已处理大量爬虫纠纷。", lookup_status: "manual_review", message: "未附案号，相关案例无法唯一确认。", source_locations: [{ platform: "feishu", block_id: "blk-demo-2", char_start: 0, char_end: 12 }] }],
    },
  }
}

function demoLegal(id, law, article, text, summary, state, blockId) {
  return {
    check_id: id, law_title: law, article_no: article, claim_text: text, lookup_status: "article_found",
    rule_findings: state === "issue" ? [{ error_type: "曲解权威文本原意", risk_level: "HIGH", diff_summary: summary, suggestion: "删除扩张表述；如需保留行政责任结论，请补充对应责任条款。" }] : [],
    semantic_comparison: state === "pass" ? { verdict: "pass", issues: [] } : null,
    evidence: { data_source: { source_url: "https://www.pkulaw.com/chl/example.html" } },
    source_locations: [{ platform: "feishu", block_id: blockId, char_start: 0, char_end: text.length }],
  }
}
