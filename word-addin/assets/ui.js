const screens = ["home-screen", "progress-screen", "results-screen"]

const DECISION_OPTIONS = [
  ["accepted", "接受"],
  ["ignored", "忽略"],
  ["escalated", "转人工"],
]

export class CheckUi {
  constructor() {
    this.messageTimer = null
    this.handlers = { onJump: null, onDecide: null }
    this.decisions = {}
  }

  setHandlers(handlers) {
    this.handlers = { ...this.handlers, ...handlers }
  }

  showScreen(id) {
    for (const screenId of screens) {
      document.getElementById(screenId).classList.toggle("is-hidden", screenId !== id)
    }
  }

  setDocument(name, status, ready) {
    document.getElementById("document-name").textContent = name
    document.getElementById("document-status").textContent = status
    document.getElementById("connection-dot").classList.toggle("is-ready", ready)
    document.getElementById("start-button").disabled = !ready
  }

  setStage(id, state, detail) {
    const stage = document.getElementById(id)
    stage.classList.remove("is-active", "is-complete")
    if (state) stage.classList.add(`is-${state}`)
    if (detail) stage.querySelector("small").textContent = detail
  }

  resetProgress() {
    this.setStage("stage-read", "active", "正在获取完整 DOCX")
    this.setStage("stage-submit", "", "等待执行")
    this.setStage("stage-check", "", "等待执行")
    this.setStage("stage-report", "", "等待执行")
  }

  renderHistory(history) {
    const section = document.getElementById("history-section")
    const list = document.getElementById("history-list")
    list.replaceChildren()
    section.classList.toggle("is-hidden", history.length === 0)
    for (const item of history) {
      const row = element("div", "history-item")
      const icon = element("div", "document-icon")
      icon.textContent = "§"
      const copy = element("div", "history-copy")
      copy.append(element("div", "history-name", item.fileName))
      const date = new Date(item.checkedAt).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })
      const outcome = item.issues ? `${item.issues} 项需核实` : `${item.total} 条已核查`
      copy.append(element("div", "history-meta", `${date} · ${outcome}`))
      row.append(icon, copy, element("span", "row-chevron"))
      list.append(row)
    }
  }

  renderResults(result, decisions = {}) {
    const { summary, verification } = result
    this.decisions = decisions
    document.getElementById("results-title").textContent = !result.semantic_check
      ? "法规溯源完成"
      : summary.issues
      ? `发现 ${summary.issues} 项需核实`
      : "未发现语义问题"
    document.getElementById("results-subtitle").textContent = `${result.file_name} · 共识别 ${summary.total} 处法律引用`
    this.renderSummary(summary)
    this.renderChecks(verification.legal_checks)
    this.showScreen("results-screen")
  }

  renderSummary(summary) {
    const grid = document.getElementById("summary-grid")
    grid.replaceChildren()
    const values = [
      [summary.total, "引用总数"],
      [summary.issues, "需核实"],
      [summary.passed, "语义通过"],
    ]
    for (const [value, label] of values) {
      const card = element("div", "summary-card")
      card.append(element("span", "summary-value", String(value)), element("span", "summary-label", label))
      grid.append(card)
    }
  }

  renderChecks(checks) {
    const list = document.getElementById("results-list")
    list.replaceChildren()
    if (!checks.length) {
      list.append(element("div", "empty-results", "未识别到明确的法规或条文引用。"))
      return
    }
    for (const check of checks) list.append(this.createResultCard(check))
  }

  createResultCard(check) {
    const verdict = check.semantic_comparison?.verdict
    const hasRuleFindings = (check.rule_findings || []).length > 0
    const state = hasRuleFindings || verdict === "issue"
      ? "issue" : verdict === "bug" ? "bug" : verdict === "pass" ? "pass" : "not-run"
    const card = element("article", `result-card is-${state}`)
    const top = element("div", "result-topline")
    top.append(
      element("div", "result-source", `《${check.law_title}》${check.article_no || ""}`),
      element("span", `status-pill is-${state}`, state === "issue" ? "需核实" : state === "bug" ? "无法判断" : state === "pass" ? "语义通过" : "未做语义核查")
    )
    card.append(top, element("blockquote", "claim-quote", check.claim_text))

    const lookupText = check.evidence?.article_text
      ? "法规溯源：已取得法条原文"
      : `法规溯源：${check.lookup_status || "未取得法条原文"}`
    card.append(element("div", "literal-status", lookupText))

    const relatedArticles = check.evidence?.related_articles || []
    if (relatedArticles.length) {
      card.append(element(
        "div",
        "retrieval-status",
        `已召回相关条款：${relatedArticles.map(item => item.article_no).join("、")}`
      ))
    }

    const findings = [...(check.rule_findings || []), ...(check.semantic_comparison?.issues || [])]
    for (const issue of findings) {
      const block = element("div", "issue-block")
      block.append(
        element("div", "issue-title", `${issue.risk_level} · ${issue.error_type}`),
        element("p", "issue-summary", issue.diff_summary),
        element("p", "issue-suggestion", issue.suggestion)
      )
      card.append(block)
    }
    if (verdict === "bug" && check.semantic_comparison.notes) {
      const block = element("div", "issue-block")
      block.append(element("div", "issue-title", "需要人工处理"), element("p", "issue-summary", check.semantic_comparison.notes))
      card.append(block)
    }
    if (check.evidence?.article_text) {
      card.append(this.createDetails(check))
    }
    card.append(this.createActionRow(check))
    return card
  }

  createActionRow(check) {
    const row = element("div", "action-row")
    const jump = element("button", "action-button jump-button", "定位原文")
    jump.type = "button"
    jump.addEventListener("click", () => this.handlers.onJump?.(check))
    row.append(jump)

    const group = element("div", "decision-group")
    for (const [value, label] of DECISION_OPTIONS) {
      const button = element("button", "action-button decision-button", label)
      button.type = "button"
      button.dataset.decision = value
      button.dataset.checkId = check.check_id
      if (this.decisions[check.check_id] === value) button.classList.add("is-active")
      button.addEventListener("click", () => {
        const current = this.decisions[check.check_id]
        const next = current === value ? null : value
        this.decisions = this.handlers.onDecide?.(check.check_id, next) || this.decisions
        for (const sibling of group.querySelectorAll(".decision-button")) {
          sibling.classList.toggle(
            "is-active",
            this.decisions[check.check_id] === sibling.dataset.decision
          )
        }
      })
      group.append(button)
    }
    row.append(group)
    return row
  }

  createDetails(check) {
    const details = element("details", "result-details")
    details.append(element(
      "summary",
      "",
      check.evidence?.related_articles?.length ? "查看召回的相关条款" : "查看法条原文"
    ))
    if (check.evidence?.article_text) {
      details.append(element("p", "statute-text", check.evidence.article_text))
    }
    return details
  }

  showMessage(message) {
    const node = document.getElementById("message")
    node.textContent = message
    node.classList.remove("is-hidden")
    clearTimeout(this.messageTimer)
    this.messageTimer = setTimeout(() => node.classList.add("is-hidden"), 5000)
  }
}

function element(tag, className = "", text = "") {
  const node = document.createElement(tag)
  if (className) node.className = className
  if (text) node.textContent = text
  return node
}
