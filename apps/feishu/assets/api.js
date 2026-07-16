export async function checkSnapshot(snapshot, scope = {}) {
  const response = await fetch("/api/feishu/checks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      snapshot,
      semantic_check: true,
      include_statutes: true,
      include_cases: true,
      ...scope,
    }),
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(payload.detail || "核查服务暂时不可用")
  return payload
}
