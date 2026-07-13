const screens = ["home-screen", "progress-screen", "results-screen"]

const DECISION_OPTIONS = [
  ["ignored", "忽略"],
  ["accepted", "接受"],
]

const CONFIDENCE_LABELS = { high: "高", medium: "中", low: "低" }

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

export class CheckUi {
  constructor() {
    this.messageTimer = null
    this.handlers = { onJump: null, onDecide: null }
    this.decisions = {}
    this.checks = []
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
      const outcome = item.issues ? `${item.issues} 项待核实` : `${item.total} 条已核查`
      copy.append(element("div", "history-meta", `${date} · ${outcome}`))
      row.append(icon, copy, element("span", "row-chevron"))
      list.append(row)
    }
  }

  renderResults(result, decisions = {}) {
    const { summary, verification } = result
    this.decisions = decisions
    this.checks = verification.legal_checks
    const title = document.getElementById("results-title")
    title.replaceChildren(
      element("span", "title-main", "共发现法律引用"),
      element("em", "title-count", String(summary.total)),
      element("span", "title-main", "处"),
      element("span", "title-sub",
        `${summary.passed} 处已通过 · ${summary.issues} 处待核实` +
        (summary.bugs ? ` · ${summary.bugs} 处无法判断` : ""))
    )
    document.getElementById("results-subtitle").textContent = result.file_name
    this.renderTypeFilter()
    this.renderChecks()
    this.showScreen("results-screen")
  }

  renderTypeFilter() {
    const select = document.getElementById("type-filter")
    const types = new Set()
    for (const check of this.checks) {
      for (const finding of findingsOf(check)) types.add(finding.error_type)
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
    select.onchange = () => this.renderChecks(select.value)
  }

  renderChecks(typeFilter = "") {
    const list = document.getElementById("results-list")
    list.replaceChildren()
    const visible = typeFilter
      ? this.checks.filter(check => findingsOf(check).some(f => f.error_type === typeFilter))
      : this.checks
    if (!visible.length) {
      list.append(element("div", "empty-results", typeFilter ? "该类型下没有核查结果。" : "未识别到明确的法规或条文引用。"))
      return
    }
    for (const check of visible) list.append(this.createResultCard(check))
  }

  createResultCard(check) {
    const findings = findingsOf(check)
    const verdict = check.semantic_comparison?.verdict
    const state = findings.length ? "issue" : verdict === "pass" ? "pass" : verdict === "bug" ? "bug" : "not-run"
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

    // 第三行：置信度与修改建议分行；建议直接给出修改内容，不带前缀
    if (findings.length) {
      const first = findings[0]
      const isRule = (check.rule_findings || []).includes(first)
      const confidence = isRule
        ? "确定"
        : CONFIDENCE_LABELS[check.semantic_comparison?.confidence] || "—"
      card.append(element("div", "card-conf", `置信度：${confidence}`))
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

  createDetails(check) {
    const details = element("details", "result-details")
    details.append(element(
      "summary",
      "",
      check.evidence?.related_articles?.length ? "查看召回的相关条款" : "查看法条原文"
    ))
    const url = sourceUrlOf(check)
    const linkLine = element("div", "statute-line")
    linkLine.append("原文链接：")
    if (url) {
      const link = element("a", "statute-link", url)
      link.href = url
      link.target = "_blank"
      link.rel = "noopener noreferrer"
      linkLine.append(link)
    } else {
      linkLine.append("本地法规库（无外部链接）")
    }
    details.append(linkLine)
    if (check.evidence?.article_text) {
      const textLine = element("div", "statute-line")
      textLine.append("原文内容：")
      textLine.append(element("span", "statute-text-inline", check.evidence.article_text))
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

// 法宝部分接口返回 Markdown 形式链接（[文本](URL)），归一化为纯 URL
function sourceUrlOf(check) {
  const raw = check.evidence?.data_source?.source_url || ""
  const match = String(raw).match(/\((https?:\/\/[^)]+)\)/)
  if (match) return match[1]
  return String(raw).startsWith("http") ? raw : ""
}

function element(tag, className = "", text = "") {
  const node = document.createElement(tag)
  if (className) node.className = className
  if (text) node.textContent = text
  return node
}
