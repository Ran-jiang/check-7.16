import { checkDocument, checkHealth } from "./api-client.js"
import { readHistory, recordHistory } from "./history.js"
import { connectToWord, getDocumentBase64, getDocumentName } from "./office-document.js"
import { CheckUi } from "./ui.js"

const ui = new CheckUi()
let documentName = "当前文档.docx"

document.getElementById("start-button").addEventListener("click", runCheck)
document.getElementById("rerun-button").addEventListener("click", runCheck)
document.getElementById("attach-button").addEventListener("click", connect)
document.getElementById("brand-button").addEventListener("click", showHome)
document.getElementById("settings-button").addEventListener("click", () => toggleSettings(true))
document.getElementById("settings-close").addEventListener("click", () => toggleSettings(false))
document.getElementById("help-button").addEventListener("click", openHelp)

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
    ui.setDocument(documentName, "已连接 · 将核查完整文档", true)
  } catch (error) {
    ui.setDocument("无法开始核查", error.message, false)
  }
}

async function runCheck() {
  toggleSettings(false)
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
      semantic_check: document.getElementById("semantic-toggle").checked,
    })

    ui.setStage("stage-check", "complete", `已识别 ${result.summary.total} 处法律引用`)
    ui.setStage("stage-report", "complete", "核查报告已生成")
    recordHistory(result)
    ui.renderHistory(readHistory())
    ui.renderResults(result)
  } catch (error) {
    showHome()
    ui.showMessage(error.message || "核查失败")
  }
}

function showHome() {
  toggleSettings(false)
  ui.showScreen("home-screen")
}

function toggleSettings(open) {
  document.getElementById("settings-panel").classList.toggle("is-hidden", !open)
}

function openHelp() {
  const url = `${window.location.origin}/help.html`
  if (Office.context.ui?.openBrowserWindow) {
    Office.context.ui.openBrowserWindow(url)
  } else {
    window.open(url, "_blank", "noopener,noreferrer")
  }
}
