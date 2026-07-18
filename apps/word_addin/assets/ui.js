import {
  BADGE_TEXT,
  orderChecksByCitation,
  sourceUrlOf,
  stripRepeatedArticleHeading,
} from "./view-model.js"
import { caseTypeOf, caseViewOf, CASE_STATUS_LABELS } from "./case-view-model.js"
import { formatReference, statuteViewOf, LOOKUP_STATUS_LABELS } from "./statute-view-model.js"
import { buildResultCards } from "./result-models.js"

export {
  BADGE_TEXT,
  CASE_STATUS_LABELS,
  LOOKUP_STATUS_LABELS,
  caseTypeOf,
  formatReference,
  orderChecksByCitation,
  sourceUrlOf,
  stripRepeatedArticleHeading,
}

const screens = ["home-screen", "progress-screen", "results-screen", "help-screen"]

const REFERENCE_ROLE_LABELS = { nested: "内部转引", inherited: "承前引用" }

function viewOf(check, options = {}) {
  return check.check_kind === "case" ? caseViewOf(check, options) : statuteViewOf(check, options)
}

export class CheckUi {
  constructor() {
    this.messageTimer = null
    this.handlers = { onJump: null, onDecide: null, onHistoryOpen: null }
    this.decisions = {}
    this.checks = []
    this.cards = []
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

  setLocateFailures(failures) {
    const failedIds = new Set(failures.map(item => item.check_id))
    for (const button of document.querySelectorAll(".jump-button[data-locate-id]")) {
      const failed = failedIds.has(button.dataset.locateId)
      button.classList.toggle("has-warning", failed)
      button.title = failed ? "定位标记未创建，点击时将重新匹配原文" : ""
    }
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
        ? element("span", "history-badge is-issue", `${entry.issues} 处未通过`)
        : element("span", "history-badge is-pass", "全部通过")
      row.append(copy, badge)
      return row
    }))
  }

  renderResults(result, decisions = {}, options = {}) {
    const { summary, verification } = result
    this.decisions = decisions
    this.cards = orderChecksByCitation(buildResultCards(verification))
    this.checks = [
      ...this.cards.flatMap(item => item.check_kind === "statute-group"
        ? item.references.map(reference => ({ ...reference, check_kind: "statute" }))
        : [item]),
    ]
    const title = document.getElementById("results-title")
    title.replaceChildren(
      element("span", "title-main", "核查完成！发现引用句"),
      element("em", "title-count", String(summary.card_total)),
      element("span", "title-main", "处，共核查法律引用"),
      element("em", "title-count", String(summary.reference_total)),
      element("span", "title-main", "条；"),
      element("em", "title-count", String(summary.passed)),
      element("span", "title-main", "处已通过，"),
      element("em", "title-count", String(summary.issues)),
      element("span", "title-main", "处未通过，"),
      element("em", "title-count", String(summary.bugs)),
      element("span", "title-main", "处待核实")
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
      ["issue", "未通过", summary.issues],
      ["bug", "待核实", summary.bugs],
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
      const findings = check.findings || []
      if (findings.length) {
        for (const type of viewOf(check).typeTags) types.add(type)
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
    const visible = this.cards.filter(item => {
      const checks = item.check_kind === "statute-group" ? item.references : [item]
      if (this.statusFilter !== "all" && !checks.some(check => check.outcome === this.statusFilter)) return false
      if (!this.typeFilter) return true
      return checks.some(check => viewOf(check).typeTags.includes(this.typeFilter) ||
        (check.check_kind === "case" && caseTypeOf(check) === this.typeFilter))
    })
    if (!visible.length) {
      list.append(element("div", "empty-results", this.typeFilter ? "该类型下没有核查结果。" : "该状态下没有核查结果。"))
      return
    }
    for (const item of visible) {
      list.append(item.check_kind === "statute-group" ? this.createStatuteGroup(item) : this.createResultCard(item))
    }
  }

  createStatuteGroup(card) {
    if (card.references.length === 1) {
      return this.createResultCard({
        ...card.references[0],
        card_id: card.card_id,
        claim_text: card.claim_text,
        source_locations: card.source_locations,
      })
    }
    return this.createMultiReferenceCard(card)
  }

  // 统一卡片解剖（单条/多条/案例同构）：
  // ①汇总行（仅多条）②文书原文（含定位）③「核查对象」分区标签 ④收起的核查行×N
  createResultCard(check) {
    const view = viewOf(check, { compact: true })
    const card = element("article", `result-card is-${view.state}`)
    card.append(...this.createQuoteZone(check.claim_text, check, check.card_id || check.check_id))
    card.append(this.createSectionLabel(view.refLine.label))
    const rows = element("div", "citation-references")
    rows.append(this.createReferenceRow(view))
    card.append(rows)
    return card
  }

  createMultiReferenceCard(card) {
    const views = card.references.map(reference =>
      statuteViewOf(reference, { compact: true })
    )
    const container = element("article", "result-card statute-group is-multiple")

    const top = element("div", "result-topline")
    top.append(element("div", "card-type", `本段共 ${views.length} 条引用`))
    const counts = element("div", "multi-counts")
    const issueCount = views.filter(view => view.state === "issue").length
    const bugCount = views.filter(view => view.state === "bug").length
    const passCount = views.filter(view => view.state === "pass").length
    if (issueCount) counts.append(element("span", "count-issue", `${issueCount} 未通过`))
    if (bugCount) counts.append(element("span", "count-bug", `${bugCount} 待核实`))
    if (passCount) counts.append(element("span", "count-pass", `${passCount} 通过`))
    top.append(counts)
    container.append(top)

    container.append(...this.createQuoteZone(card.claim_text, card, card.card_id))
    container.append(this.createSectionLabel("核查对象"))

    const references = element("div", "citation-references")
    for (const view of views) references.append(this.createReferenceRow(view))
    container.append(references)
    return container
  }

  createSectionLabel(text) {
    const row = element("div", "zone-label-row")
    row.append(element("span", "zone-label", text))
    return row
  }

  // 核查行：默认收起为一行（条目 + 徽章），展开显示结论、建议、权威原文与决策
  createReferenceRow(view) {
    const row = element("details", `reference-row is-${view.state}`)
    const summary = element("summary", "reference-row-summary")
    summary.append(element("span", "reference-source", view.refLine.text))
    const role = REFERENCE_ROLE_LABELS[view.raw.reference_role]
    if (role) summary.append(element("span", "reference-role", role))
    summary.append(element("span", `status-pill is-${view.state}`, view.badge.text))
    row.append(summary)

    const body = element("div", "reference-row-body")
    const bodyTop = element("div", "reference-body-topline")
    bodyTop.append(element("div", "card-type", view.typeLabel))
    body.append(bodyTop)
    this.appendVerdict(body, view)
    if (view.candidates?.length) body.append(this.createCaseCandidates(view.candidates))
    if (view.evidence) body.append(this.createEvidenceDetails(view.evidence))
    body.append(this.createDecisionRow(view))
    row.append(body)
    return row
  }

  // ②区：区块标签行（文书原文 + 定位原文）+ 灰色引文块
  // 同一问题聚合多处出现时，按出现位置逐个给出定位按钮
  createQuoteZone(quoteText, jumpTarget, locateId) {
    const labelRow = element("div", "zone-label-row")
    labelRow.append(element("span", "zone-label", "文书原文"))
    if (jumpTarget) {
      const locationCount = (jumpTarget.source_locations || []).length
      const buttons = locationCount > 1
        ? Array.from({ length: locationCount }, (_, index) => ({ text: `定位 ${index + 1}`, index }))
        : [{ text: "定位原文", index: 0 }]
      for (const { text, index } of buttons) {
        const jump = element("button", "action-button jump-button", text)
        jump.type = "button"
        jump.dataset.locateId = locateId
        jump.addEventListener("click", () => this.handlers.onJump?.(jumpTarget, index))
        labelRow.append(jump)
      }
    }
    return [labelRow, element("blockquote", "doc-quote", quoteText || "")]
  }

  // 展开态：风险分级 + 建议
  appendVerdict(container, view) {
    if (!view.verdict) return
    if (view.verdict.riskText) {
      container.append(element("div", "card-conf", `风险分级：${view.verdict.riskText}`))
    }
    if (view.verdict.suggestion) {
      container.append(element("p", "card-suggestion", view.verdict.suggestion))
    }
  }

  // ⑤区：权威原文折叠（蓝色左条），内容在前、链接在后
  createEvidenceDetails(evidence) {
    const details = element("details", "result-details")
    details.append(element("summary", "", evidence.summaryLabel))
    if (evidence.structurePath) {
      details.append(element("div", "statute-line", `章节位置：${evidence.structurePath}`))
    }
    if (evidence.articleText) {
      const block = element("div", "authority-quote")
      if (evidence.articleHeading) {
        block.append(element("strong", "", evidence.articleHeading), "　")
      }
      block.append(evidence.articleText)
      details.append(block)
    }
    for (const item of evidence.related) {
      const block = element("div", "authority-quote")
      if (item.heading) block.append(element("strong", "", item.heading), "　")
      block.append(item.text)
      details.append(block)
    }
    if (evidence.url) {
      const linkLine = element("div", "statute-line")
      linkLine.append("原文链接：")
      const link = element("a", "statute-link", evidence.url)
      link.href = evidence.url
      link.target = "_blank"
      link.rel = "noopener noreferrer"
      linkLine.append(link)
      details.append(linkLine)
    }
    return details
  }

  createCaseCandidates(candidates) {
    const details = element("details", "result-details")
    details.open = true
    details.append(element("summary", "", `参考案例（${candidates.length}）`))
    candidates.forEach((candidate, index) => {
      const line = element("div", "statute-line")
      line.append(`${index + 1}. ${candidate.title || "未命名案例"}`)
      const metadata = [candidate.case_number, candidate.court, candidate.last_instance_date].filter(Boolean).join("｜")
      if (metadata) line.append(element("div", "", metadata))
      if (candidate.url) {
        line.append(" ")
        const link = element("a", "statute-link", "查看原文")
        link.href = candidate.url
        link.target = "_blank"
        link.rel = "noopener noreferrer"
        line.append(link)
      }
      details.append(line)
    })
    return details
  }

  // ⑥区：单个决策按钮，接受修订 ⇄ 取消修订
  createDecisionRow(view) {
    const row = element("div", "action-row")
    const applicable = (view.raw.findings || []).some(finding => finding.revision?.machine_applicable)
    if (!applicable) return row
    const button = element("button", "action-button decision-button")
    button.type = "button"
    button.dataset.decision = "accepted"
    button.dataset.checkId = view.checkId
    const sync = () => {
      const accepted = this.decisions[view.checkId] === "accepted"
      button.textContent = accepted ? "修订已写入" : "接受修订"
      button.classList.toggle("is-active", accepted)
      button.disabled = accepted
    }
    sync()
    button.addEventListener("click", async () => {
      if (this.decisions[view.checkId] === "accepted") return
      await this.handlers.onApplyFix?.(view.raw)
      sync()
    })
    row.append(button)
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
