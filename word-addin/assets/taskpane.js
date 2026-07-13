import { checkDocument, checkHealth, checkSelection, exportReport } from "./api-client.js"
import { readDecisions, readHistory, recordHistory, saveDecision } from "./history.js"
import {
  connectToWord,
  getDocumentBase64,
  getDocumentName,
  getSelectedText,
  jumpToText,
} from "./office-document.js"
import { CheckUi } from "./ui.js"

const ui = new CheckUi()
let documentName = "当前文档.docx"
let lastResult = null

document.getElementById("start-button").addEventListener("click", runCheck)
document.getElementById("selection-button").addEventListener("click", runSelectionCheck)
document.getElementById("rerun-button").addEventListener("click", runCheck)
document.getElementById("export-button").addEventListener("click", exportCurrentReport)
document.getElementById("attach-button").addEventListener("click", connect)
document.getElementById("brand-button").addEventListener("click", showHome)
document.getElementById("help-button").addEventListener("click", openHelp)

ui.setHandlers({
  onJump: check => {
    jumpToText(check.claim_text).catch(error => ui.showMessage(error.message))
  },
  onDecide: (checkId, decision) => {
    if (!lastResult) return {}
    return saveDecision(lastResult.file_name, checkId, decision)
  },
})

initialize()

async function initialize() {
  ui.renderHistory(readHistory())
  await connect()
}

async function connect() {
  ui.setDocument("正在读取 Word 文档…", "正在连接插件", false)
  try {
    await Promise.all([connectToWord(), checkHealth()])
    documentName = getDocumentName()
    ui.setDocument(documentName, "已连接 · 可核查全文或选中片段", true)
    document.getElementById("selection-button").disabled = false
  } catch (error) {
    ui.setDocument("无法开始核查", error.message, false)
    document.getElementById("selection-button").disabled = true
  }
}

async function runCheck() {
  ui.resetProgress()
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
    })
    finishCheck(result)
  } catch (error) {
    showHome()
    ui.showMessage(error.message || "核查失败")
  }
}

async function runSelectionCheck() {
  try {
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
    })
    finishCheck(result)
  } catch (error) {
    showHome()
    ui.showMessage(error.message || "选中内容核查失败")
  }
}

function finishCheck(result) {
  lastResult = result
  ui.setStage("stage-check", "complete", `已识别 ${result.summary.total} 处法律引用`)
  ui.setStage("stage-report", "complete", "核查完成，可导出报告")
  recordHistory(result)
  ui.renderHistory(readHistory())
  ui.renderResults(result, readDecisions(result.file_name))
}

async function exportCurrentReport() {
  if (!lastResult) {
    ui.showMessage("请先完成一次核查")
    return
  }
  try {
    const report = await exportReport({
      file_name: lastResult.file_name,
      semantic_check: lastResult.semantic_check,
      summary: lastResult.summary,
      verification: lastResult.verification,
      decisions: readDecisions(lastResult.file_name),
    })
    openExternal(`${window.location.origin}${report.url}`)
    ui.showMessage("核查报告已生成，可在浏览器中打印或另存为 PDF")
  } catch (error) {
    ui.showMessage(error.message || "报告导出失败")
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
