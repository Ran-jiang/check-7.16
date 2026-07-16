export async function checkDocument(payload) {
  const response = await fetch("/api/checks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
  if (!response.ok) {
    const error = await response.json().catch(() => null)
    throw new Error(error?.detail || `核验服务返回 ${response.status}`)
  }
  return response.json()
}

export async function checkSelection(payload) {
  const response = await fetch("/api/checks/selection", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
  if (!response.ok) {
    const error = await response.json().catch(() => null)
    throw new Error(error?.detail || `核验服务返回 ${response.status}`)
  }
  return response.json()
}

export async function exportReport(payload) {
  const response = await fetch("/api/reports", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
  if (!response.ok) {
    const error = await response.json().catch(() => null)
    throw new Error(error?.detail || `报告生成失败（${response.status}）`)
  }
  return response.json()
}

export async function checkHealth() {
  const response = await fetch("/api/health")
  if (!response.ok) throw new Error("核验服务不可用")
}
