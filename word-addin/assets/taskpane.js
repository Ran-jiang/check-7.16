import { checkDocument, checkHealth, checkSelection, exportReport } from "./api-client.js"
import { readDecisions, saveDecision } from "./history.js"
import {
  connectToWord,
  getDocumentBase64,
  getDocumentName,
  getSelectedText,
  jumpToText,
} from "./office-document.js"
import { CheckUi } from "./ui.js"

const ui = new CheckUi()
let documentName = "未命名文档.docx"
let lastResult = null
let lastCheckMode = "full"

document.getElementById("start-button").addEventListener("click", runCheck)
document.getElementById("selection-button").addEventListener("click", runSelectionCheck)
document.getElementById("rerun-button").addEventListener("click", rerunCurrentCheck)
document.getElementById("export-button").addEventListener("click", exportCurrentReport)
document.getElementById("brand-button").addEventListener("click", showHome)
document.getElementById("help-button").addEventListener("click", openHelp)

ui.setHandlers({
  onJump: check => {
    jumpToText(
      check.location_text || check.claim_text,
      check.location_occurrence || 0
    ).catch(error => ui.showMessage(error.message))
  },
  onDecide: (checkId, decision) => {
    if (!lastResult) return {}
    return saveDecision(lastResult.document_key, checkId, decision)
  },
})

initialize()

async function initialize() {
  await connect()
}

async function connect() {
  ui.setDocument("正在读取 Word 文档…", "正在连接插件", false)
  try {
    await Promise.all([connectToWord(), checkHealth()])
    documentName = await getDocumentName()
    ui.setDocument(documentName, "已连接 · 可核查全文或选中片段", true)
    document.getElementById("selection-button").disabled = false
  } catch (error) {
    ui.setDocument("无法开始核查", error.message, false)
    document.getElementById("selection-button").disabled = true
  }
}

function checkScope() {
  const scope = {
    include_statutes: document.getElementById("statute-toggle").checked,
    include_cases: document.getElementById("case-toggle").checked,
  }
  if (!scope.include_statutes && !scope.include_cases) {
    ui.showMessage("请至少选择一种核查范围（法律法规引用或司法案例引用）")
    return null
  }
  return scope
}

async function runCheck() {
  const scope = checkScope()
  if (!scope) return
  ui.resetProgress()
  ui.setBusy(true)
  ui.showScreen("progress-screen")
  try {
    const docxBase64 = await getDocumentBase64()
    ui.setStage("stage-read", "complete", "DOCX 读取完成")
    ui.setStage("stage-submit", "complete", "已安全提交至核验服务")
    ui.setStage("stage-check", "active", "正在查询法规库并执行语义核验")

    const result = await checkDocument({
      file_name: documentName,
      docx_base64: docxBase64,
      semantic_check: true,
      ...scope,
    })
    finishCheck(result, "full")
  } catch (error) {
    showHome()
    ui.showMessage(error.message || "核查失败")
  } finally {
    ui.setBusy(false)
  }
}

async function runSelectionCheck() {
  const scope = checkScope()
  if (!scope) return
  try {
    ui.setBusy(true)
    const selectedText = await getSelectedText()
    if (!selectedText) {
      ui.showMessage("请先在文档中选中要核查的内容")
      return
    }
    ui.resetProgress()
    ui.setStage("stage-read", "complete", "已读取选中片段")
    ui.showScreen("progress-screen")
    ui.setStage("stage-submit", "complete", "已安全提交至核验服务")
    ui.setStage("stage-check", "active", "正在查询法规库并执行语义核验")

    const result = await checkSelection({
      file_name: documentName,
      text: selectedText,
      semantic_check: true,
      ...scope,
    })
    finishCheck(result, "selection")
  } catch (error) {
    showHome()
    ui.showMessage(error.message || "选中内容核查失败")
  } finally {
    ui.setBusy(false)
  }
}

function finishCheck(result, mode) {
  lastResult = result
  lastCheckMode = mode
  document.getElementById("rerun-button").textContent = mode === "selection" ? "继续核查" : "重新核查"
  ui.setStage("stage-check", "complete", `已识别 ${result.summary.total} 处法律引用`)
  ui.setStage("stage-report", "complete", "核查完成，可导出报告")
  ui.renderResults(result, readDecisions(result.document_key))
}

function rerunCurrentCheck() {
  return lastCheckMode === "selection" ? runSelectionCheck() : runCheck()
}

async function exportCurrentReport() {
  if (!lastResult) {
    ui.showMessage("请先完成一次核查")
    return
  }
  try {
    ui.setBusy(true)
    const report = await exportReport({
      file_name: lastResult.file_name,
      semantic_check: lastResult.semantic_check,
      summary: lastResult.summary,
      verification: lastResult.verification,
      decisions: readDecisions(lastResult.document_key),
    })
    openExternal(`${window.location.origin}${report.url}`)
    ui.showMessage("核查报告已生成，可在浏览器中打印或另存为 PDF")
  } catch (error) {
    ui.showMessage(error.message || "报告导出失败")
  } finally {
    ui.setBusy(false)
  }
}

function showHome() {
  ui.showScreen("home-screen")
}

function openHelp() {
  openExternal(`${window.location.origin}/help.html`)
}

function openExternal(url) {
  if (Office.context.ui?.openBrowserWindow) {
    Office.context.ui.openBrowserWindow(url)
  } else {
    window.open(url, "_blank", "noopener,noreferrer")
  }
}
