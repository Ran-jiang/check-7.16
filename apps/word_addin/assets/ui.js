const screens = ["home-screen", "progress-screen", "results-screen"]

const DECISION_OPTIONS = [
  ["ignored", "忽略"],
  ["accepted", "接受"],
]

const LOOKUP_STATUS_LABELS = {
  article_found: "已取得法条原文",
  relevant_articles_found: "已召回相关条款",
  law_found_article_missing: "法规存在，未找到该条",
  law_found_text_unavailable: "法规存在，条文全文不可用",
  law_not_found: "未检索到该法规",
  source_not_configured: "数据源未配置",
  source_error: "数据源调用失败",
  not_verifiable: "非法条类文件，不做条文核验",
}

const CASE_STATUS_LABELS = {
  verified: "案例已核验",
  not_found: "案例未命中",
  manual_review: "候选案例需人工确认",
  source_not_configured: "案例数据源未配置",
  source_error: "案例数据源调用失败",
}

export class CheckUi {
  constructor() {
    this.messageTimer = null
    this.handlers = { onJump: null, onDecide: null, onHistoryOpen: null }
    this.decisions = {}
    this.checks = []
    this.ready = false
    this.statusFilter = "all"
    this.typeFilter = ""
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
    this.ready = ready
    document.getElementById("document-name").textContent = name
    document.getElementById("document-status").textContent = status
    document.getElementById("connection-dot").classList.toggle("is-ready", ready)
    document.getElementById("start-button").disabled = !ready
    document.getElementById("selection-button").disabled = !ready
  }

  setBusy(busy) {
    for (const id of ["start-button", "selection-button"]) {
      const button = document.getElementById(id)
      if (button) button.disabled = busy || !this.ready
    }
    for (const id of ["rerun-button", "export-button"]) {
      const button = document.getElementById(id)
      if (button) button.disabled = busy
    }
    document.querySelector(".app-shell").setAttribute("aria-busy", String(busy))
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
    if (!section || !list) return
    section.classList.toggle("is-hidden", !history.length)
    list.replaceChildren(...history.map(entry => {
      const row = element("button", "history-row")
      row.type = "button"
      row.addEventListener("click", () => this.handlers.onHistoryOpen?.(entry))
      const copy = element("div", "history-copy")
      copy.append(
        element("div", "history-name", entry.fileName || "未命名文档"),
        element("div", "history-meta", `${formatCheckedAt(entry.checkedAt)} · 共 ${entry.total} 处引用`)
      )
      const badge = entry.issues > 0
        ? element("span", "history-badge is-issue", `${entry.issues} 处待核实`)
        : element("span", "history-badge is-pass", "全部通过")
      row.append(copy, badge)
      return row
    }))
  }

  renderResults(result, decisions = {}, options = {}) {
    const { summary, verification } = result
    this.decisions = decisions
    this.checks = [
      ...(verification.legal_checks || []).map(check => ({ ...check, check_kind: "statute" })),
      ...(verification.case_checks || []).map(check => ({ ...check, check_kind: "case" })),
    ].sort((left, right) => (
      stateOrder(checkState(left)) - stateOrder(checkState(right)) || compareCheckLocation(left, right)
    ))
    const title = document.getElementById("results-title")
    title.replaceChildren(
      element("span", "title-main", "核查完成！共发现法律引用 "),
      element("em", "title-count", String(summary.total)),
      element("span", "title-main", " 处， "),
      element("em", "title-count", String(summary.passed)),
      element("span", "title-main", " 处已通过， "),
      element("em", "title-count", String(summary.issues)),
      element("span", "title-main", " 处待核实， "),
      element("em", "title-count", String(summary.bugs)),
      element("span", "title-main", " 处无法判断")
    )
    document.getElementById("results-subtitle").textContent = options.snapshotAt
      ? `${result.file_name} · ${formatCheckedAt(options.snapshotAt)} 的核查快照`
      : result.file_name
    this.statusFilter = "all"
    this.typeFilter = ""
    this.renderStatusFilter(summary)
    this.renderTypeFilter()
    this.renderChecks()
    this.showScreen("results-screen")
  }

  renderStatusFilter(summary) {
    const container = document.getElementById("status-filter")
    const options = [
      ["all", "全部", summary.total],
      ["issue", "待核实", summary.issues],
      ["bug", "无法判断", summary.bugs],
      ["pass", "已通过", summary.passed],
    ]
    container.replaceChildren()
    for (const [value, label, count] of options) {
      const button = element("button", `status-tab${value === this.statusFilter ? " is-active" : ""}`)
      button.type = "button"
      button.role = "tab"
      button.setAttribute("aria-selected", String(value === this.statusFilter))
      button.append(label, element("em", "status-count", String(count)))
      button.addEventListener("click", () => {
        this.statusFilter = value
        this.typeFilter = ""
        this.renderStatusFilter(summary)
        this.renderTypeFilter()
        this.renderChecks()
      })
      container.append(button)
    }
  }

  renderTypeFilter() {
    const select = document.getElementById("type-filter")
    const types = new Set()
    for (const check of this.checks) {
      const findings = findingsOf(check)
      if (findings.length) {
        for (const finding of findings) types.add(finding.error_type)
      } else if (check.check_kind === "case") {
        types.add(caseTypeOf(check))
      }
    }
    const allOption = element("option", "", "全部类型")
    allOption.value = ""
    select.replaceChildren(allOption)
    for (const type of types) {
      const option = element("option", "", type)
      option.value = type
      select.append(option)
    }
    select.value = ""
    select.classList.toggle("is-hidden", !["all", "issue"].includes(this.statusFilter))
    select.onchange = () => {
      this.typeFilter = select.value
      this.renderChecks()
    }
  }

  renderChecks() {
    const list = document.getElementById("results-list")
    list.replaceChildren()
    const visible = this.checks.filter(check => {
      if (this.statusFilter !== "all" && checkState(check) !== this.statusFilter) return false
      if (!this.typeFilter) return true
      return findingsOf(check).some(f => f.error_type === this.typeFilter) ||
        (check.check_kind === "case" && caseTypeOf(check) === this.typeFilter)
    })
    if (!visible.length) {
      list.append(element("div", "empty-results", this.typeFilter ? "该类型下没有核查结果。" : "该状态下没有核查结果。"))
      return
    }
    for (const check of visible) list.append(this.createResultCard(check))
  }

  createResultCard(check) {
    if (check.check_kind === "case") return this.createCaseResultCard(check)
    const findings = findingsOf(check)
    const state = checkState(check)
    const pillText = state === "issue" ? "待核实" : state === "pass" ? "通过" : state === "bug" ? "无法判断" : "未核查"

    const card = element("article", `result-card is-${state}`)

    // 第一行：小字问题类型 + 右侧状态签
    const typeText = findings.length
      ? findings.map(f => f.error_type).join("；")
      : state === "pass" ? "法律引用无问题"
      : state === "bug" ? "数据源不足，需人工处理"
      : LOOKUP_STATUS_LABELS[check.lookup_status] || check.lookup_status
    const top = element("div", "result-topline")
    top.append(
      element("div", "card-type", typeText),
      element("span", `status-pill is-${state}`, pillText)
    )
    card.append(top)

    // 第二行：法律引用原文
    card.append(element("blockquote", "claim-quote", check.claim_text))

    // 第三行：风险分级与修改建议分行；建议直接给出修改内容，不带前缀
    if (findings.length) {
      const first = findings[0]
      const risk = first.risk_level === "HIGH" ? "高" : first.risk_level === "MEDIUM" ? "中" : first.risk_level
      card.append(element("div", "card-conf", `风险分级：${risk}`))
      card.append(element("p", "card-suggestion", first.suggestion))
    } else if (state === "bug" && check.semantic_comparison?.notes) {
      card.append(element("p", "card-suggestion", check.semantic_comparison.notes))
    }

    // 折叠：查看法条原文 + 原文链接
    if (check.evidence?.article_text || sourceUrlOf(check)) {
      card.append(this.createDetails(check))
    }

    card.append(this.createActionRow(check))
    return card
  }

  createCaseResultCard(check) {
    const state = checkState(check)
    const card = element("article", `result-card is-${state}`)
    const top = element("div", "result-topline")
    top.append(
      element("div", "card-type", caseTypeOf(check)),
      element("span", `status-pill is-${state}`,
        state === "pass" ? "通过" : state === "issue" ? "待核实" : "无法判断")
    )
    card.append(top, element("blockquote", "claim-quote", check.claim_text))
    const cited = check.cited_case_number || check.cited_case_name || "未命名案例线索"
    card.append(element("div", "card-conf", `引用线索：${cited}`))
    if (state === "issue") {
      card.append(element("p", "card-suggestion", check.message || "请核实案例名称或案号，并以权威案例库的检索结果为准。"))
    } else if (check.message) {
      card.append(element("p", "card-suggestion", check.message))
    }
    if (check.evidence) card.append(this.createCaseDetails(check))
    card.append(this.createActionRow(check))
    return card
  }

  createCaseDetails(check) {
    const details = element("details", "result-details")
    details.append(element("summary", "", "查看命中案例"))
    details.append(element("div", "statute-line",
      `${check.evidence.title || check.evidence.case_number}${check.evidence.court ? ` · ${check.evidence.court}` : ""}`))
    const url = sourceUrlOf(check)
    if (url) {
      const line = element("div", "statute-line", "原文链接：")
      const link = element("a", "statute-link", url)
      link.href = url
      link.target = "_blank"
      link.rel = "noopener noreferrer"
      line.append(link)
      details.append(line)
    }
    return details
  }

  createDetails(check) {
    const details = element("details", "result-details")
    details.append(element(
      "summary",
      "",
      check.evidence?.related_articles?.length ? "查看召回的相关条款" : "查看法条原文"
    ))
    const url = sourceUrlOf(check)
    if (url) {
      const linkLine = element("div", "statute-line")
      linkLine.append("原文链接：")
      const link = element("a", "statute-link", url)
      link.href = url
      link.target = "_blank"
      link.rel = "noopener noreferrer"
      linkLine.append(link)
      details.append(linkLine)
    }
    if (check.evidence?.article_text) {
      const textLine = element("div", "statute-line")
      textLine.append("原文内容：")
      const articleNo = check.evidence.article_no || check.article_no || ""
      const heading = `《${check.evidence.law_title || check.law_title}》${articleNo}`
      const articleText = stripRepeatedArticleHeading(check.evidence.article_text, articleNo)
      textLine.append(element("span", "statute-text-inline", `${heading}　${articleText}`))
      details.append(textLine)
    }
    return details
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

  showMessage(message) {
    const node = document.getElementById("message")
    node.textContent = message
    node.classList.remove("is-hidden")
    clearTimeout(this.messageTimer)
    this.messageTimer = setTimeout(() => node.classList.add("is-hidden"), 5000)
  }
}

function findingsOf(check) {
  return [...(check.rule_findings || []), ...(check.semantic_comparison?.issues || [])]
}

function checkState(check) {
  if (check.check_kind === "case") {
    return check.lookup_status === "verified" ? "pass" : check.lookup_status === "not_found" ? "issue" : "bug"
  }
  if (findingsOf(check).length) return "issue"
  if (check.semantic_comparison?.verdict === "pass") return "pass"
  if (!check.semantic_comparison && ["article_found", "relevant_articles_found"].includes(check.lookup_status)) {
    return "pass"
  }
  return "bug"
}

function stateOrder(state) {
  return { issue: 0, bug: 1, pass: 2 }[state] ?? 3
}

export function stripRepeatedArticleHeading(text, articleNo) {
  if (!articleNo) return String(text || "")
  return String(text || "").replace(
    /^\s*第[〇零一二三四五六七八九十百千万两0-9]+条(?:之[〇零一二三四五六七八九十百千万两0-9]+)?[\s　]*/,
    ""
  )
}

// 法宝部分接口返回 Markdown 形式链接（[文本](URL)），归一化为纯 URL
function sourceUrlOf(check) {
  const raw = check.evidence?.data_source?.source_url || check.evidence?.url || ""
  const match = String(raw).match(/\((https?:\/\/[^)]+)\)/)
  const url = match ? match[1] : String(raw).startsWith("http") ? String(raw) : ""
  try {
    return /(^|\.)pkulaw\.com$/i.test(new URL(url).hostname) ? url : ""
  } catch {
    return ""
  }
}

function caseTypeOf(check) {
  return `司法案例：${CASE_STATUS_LABELS[check.lookup_status] || check.lookup_status}`
}

function compareCheckLocation(left, right) {
  const anchor = check => Number(String(check.anchor_ids?.[0] || "").replace(/\D/g, "")) || Number.MAX_SAFE_INTEGER
  return anchor(left) - anchor(right) || left.check_id.localeCompare(right.check_id)
}

function element(tag, className = "", text = "") {
  const node = document.createElement(tag)
  if (className) node.className = className
  if (text) node.textContent = text
  return node
}

function formatCheckedAt(iso) {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ""
  const now = new Date()
  const time = `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`
  if (date.toDateString() === now.toDateString()) return `今天 ${time}`
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (date.toDateString() === yesterday.toDateString()) return `昨天 ${time}`
  return `${date.getMonth() + 1}月${date.getDate()}日 ${time}`
}
