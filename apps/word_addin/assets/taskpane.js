import { captureDebugEvent, checkDocument, checkHealth, checkSelection, exportReport } from "./api-client.js"
import { readDecisions, readHistory, readResultSnapshot, recordHistory, saveDecision } from "./history.js"
import {
  connectToWord,
  getDocumentBase64,
  getDocumentName,
  getSelectedContent,
} from "./office-document.js"
import { clearSourceBookmarks, jumpToSource, seedSourceBookmarks } from "./word-bookmarks.js"
import { CheckUi } from "./ui.js"

const ui = new CheckUi()
let documentName = "未命名文档.docx"
let lastResult = null
let lastCheckMode = "full"

document.getElementById("start-button").addEventListener("click", runCheck)
document.getElementById("selection-button").addEventListener("click", runSelectionCheck)
document.getElementById("rerun-button").addEventListener("click", rerunCurrentCheck)
document.getElementById("export-button").addEventListener("click", exportCurrentReport)
document.getElementById("clear-bookmarks-button").addEventListener("click", clearBookmarkMarkers)
document.getElementById("brand-button").addEventListener("click", showHome)
document.getElementById("help-button").addEventListener("click", openHelp)

ui.setHandlers({
  onJump: async check => {
    try {
      const details = await jumpToSource(check, lastResult.document_key)
      await saveWordDebug("locate_success", check, details)
      if (details.warning) ui.showMessage(details.warning)
    } catch (error) {
      await saveWordDebug("locate_error", check, null, error)
      ui.showMessage(error.message)
    }
  },
  onDecide: (checkId, decision) => {
    if (!lastResult) return {}
    return saveDecision(lastResult.document_key, checkId, decision)
  },
  onHistoryOpen: openHistorySnapshot,
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
    await finishCheck(result, "full")
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
    const selection = await getSelectedContent()
    if (!selection.text) {
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
      text: selection.text,
      source_blocks: selection.source_blocks,
      semantic_check: true,
      ...scope,
    })
    await finishCheck(result, "selection")
  } catch (error) {
    showHome()
    ui.showMessage(error.message || "选中内容核查失败")
  } finally {
    ui.setBusy(false)
  }
}

async function saveWordDebug(event, check, details, error = null) {
  if (!lastResult?.debug_run_id) return
  const officeError = error ? {
    name: error.name,
    message: error.message,
    code: error.code,
    stack: error.stack,
    debugInfo: error.debugInfo,
    innerError: error.innerError,
  } : null
  try {
    await captureDebugEvent({
      run_id: lastResult.debug_run_id,
      event,
      payload: { document_name: documentName, check, details, error: officeError },
    })
  } catch (_) {
    // 调试留档不能影响正常定位操作。
  }
}

async function finishCheck(result, mode) {
  lastResult = result
  lastCheckMode = mode
  if (mode === "full") {
    recordHistory(result)
    ui.renderHistory(readHistory())
  }
  document.getElementById("rerun-button").textContent = mode === "selection" ? "继续核查" : "重新核查"
  ui.setStage("stage-check", "complete", `已识别 ${result.summary.card_total} 个引用句、${result.summary.reference_total} 条引用`)
  const debugLabel = result.debug_run_id ? ` · 调试 ${result.debug_run_id}` : ""
  ui.setStage("stage-report", "complete", `核查完成，可导出报告${debugLabel}`)
  let bookmarkError = null
  let anchors = null
  try {
    anchors = await seedSourceBookmarks(result)
    await saveWordDebug("bookmark_seeded", null, anchors)
  } catch (error) {
    bookmarkError = error
    await saveWordDebug("bookmark_seed_error", null, null, error)
  }
  ui.renderResults(result, readDecisions(result.document_key))
  ui.setLocateFailures(anchors?.failed || [])
  if (bookmarkError) ui.showMessage(bookmarkError.message || "原文定位标记创建失败")
  else if (anchors?.failed?.length) ui.showMessage(`${anchors.failed.length} 处原文未能创建定位标记`)
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

function openHistorySnapshot(entry) {
  const snapshot = readResultSnapshot(entry.documentKey)
  if (!snapshot) {
    ui.showMessage("这条记录没有保存结果快照，请重新核查该文档")
    return
  }
  lastResult = snapshot
  lastCheckMode = "full"
  document.getElementById("rerun-button").textContent = "重新核查"
  ui.renderResults(snapshot, readDecisions(snapshot.document_key), { snapshotAt: entry.checkedAt })
}

async function clearBookmarkMarkers() {
  try {
    ui.setBusy(true)
    const count = await clearSourceBookmarks()
    ui.showMessage(count ? `已清除 ${count} 个定位标记` : "没有需要清除的定位标记")
  } catch (error) {
    ui.showMessage(error.message || "清除定位标记失败")
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
