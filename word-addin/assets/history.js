const STORAGE_KEY = "ccitecheck.recentChecks"
const HISTORY_LIMIT = 3

export function readHistory() {
  const value = localStorage.getItem(STORAGE_KEY)
  if (!value) return []
  try {
    const history = JSON.parse(value)
    return Array.isArray(history) ? history.slice(0, HISTORY_LIMIT) : []
  } catch {
    localStorage.removeItem(STORAGE_KEY)
    return []
  }
}

export function recordHistory(result) {
  const entry = {
    fileName: result.file_name,
    documentKey: result.document_key,
    checkedAt: new Date().toISOString(),
    total: result.summary.total,
    issues: result.summary.issues,
  }
  const history = readHistory().filter(item => item.documentKey !== entry.documentKey)
  localStorage.setItem(STORAGE_KEY, JSON.stringify([entry, ...history].slice(0, HISTORY_LIMIT)))
}

// ---- 每条核查项的人工处理标记（接受/忽略），按文档内容哈希持久化 ----

const DECISIONS_KEY = "ccitecheck.decisions"

export function readDecisions(documentKey) {
  try {
    const store = JSON.parse(localStorage.getItem(DECISIONS_KEY) || "{}")
    return store[documentKey] || {}
  } catch {
    return {}
  }
}

export function saveDecision(documentKey, checkId, decision) {
  let store = {}
  try {
    store = JSON.parse(localStorage.getItem(DECISIONS_KEY) || "{}")
  } catch {
    store = {}
  }
  const decisions = store[documentKey] || {}
  if (decision) {
    decisions[checkId] = decision
  } else {
    delete decisions[checkId]
  }
  store[documentKey] = decisions
  localStorage.setItem(DECISIONS_KEY, JSON.stringify(store))
  return decisions
}
