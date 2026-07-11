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
    checkedAt: new Date().toISOString(),
    total: result.summary.total,
    issues: result.summary.issues,
  }
  const history = readHistory().filter(item => item.fileName !== entry.fileName)
  localStorage.setItem(STORAGE_KEY, JSON.stringify([entry, ...history].slice(0, HISTORY_LIMIT)))
}
