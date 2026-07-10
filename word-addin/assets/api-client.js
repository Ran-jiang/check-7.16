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

export async function checkHealth() {
  const response = await fetch("/api/health")
  if (!response.ok) throw new Error("核验服务不可用")
}
