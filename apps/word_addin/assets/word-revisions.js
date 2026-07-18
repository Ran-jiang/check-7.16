export function revisionFor(check) {
  const proposal = (check.findings || []).map(item => item.revision).find(item =>
    item?.machine_applicable && item.strategy === "replace_exact_text" && item.original_text && item.revised_text
  )
  if (!proposal) return null
  return { original: proposal.original_text, revised: proposal.revised_text }
}

export async function applyTrackedRevision(check) {
  if (!window.Word?.run || !Office.context.requirements.isSetSupported("WordApi", "1.4")) {
    throw new Error("当前 Word 版本不支持自动写入修订，请手动修改")
  }
  const revision = revisionFor(check)
  if (!revision) throw new Error("该问题没有可安全写入的修订文本")
  return Word.run(async context => {
    const document = context.document
    document.load("changeTrackingMode")
    const matches = document.body.search(revision.original, {
      matchCase: true, matchWholeWord: false, matchWildcards: false,
      ignoreSpace: false, ignorePunct: false,
    })
    matches.load("items")
    await context.sync()
    if (matches.items.length !== 1) {
      throw new Error(matches.items.length ? "原文存在多处相同内容，请手动修改" : "原文已变化，请重新核查后再写入修订")
    }
    const previousMode = document.changeTrackingMode
    try {
      document.changeTrackingMode = "TrackAll"
      matches.items[0].insertText(revision.revised, "Replace")
      await context.sync()
    } finally {
      document.changeTrackingMode = previousMode
      await context.sync()
    }
    return { method: "unique_text", revised_text: revision.revised }
  })
}
