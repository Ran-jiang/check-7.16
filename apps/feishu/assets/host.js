/** 飞书专属操作统一封装在此适配器中，UI 与核查内核不直接导入 SDK。 */
export class FeishuHost {
  constructor(api = globalThis.DocMiniApp) {
    this.api = api
  }

  get isAvailable() { return Boolean(this.api) }

  async readSnapshot() {
    if (!this.api) return demoSnapshot()
    const docRef = await this.api.getActiveDocumentRef()
    const [root, title] = await Promise.all([
      this.api.Document.getRootBlock(docRef),
      this.api.Document.getTitle(docRef),
    ])
    this.docRef = docRef
    return snapshotFromRoot(root, { documentId: docRef.docToken, title })
  }

  async scrollToLocation(location) {
    if (!location?.block_id) throw new Error("该结果缺少飞书块定位信息")
    if (!this.api) return
    const docRef = this.docRef || await this.api.getActiveDocumentRef()
    const blockRef = this.api.getBlockRefById(docRef, Number(location.block_id))
    await this.api.Viewport.scrollToBlock(blockRef, true)
  }

  onDocumentChange(handler) {
    if (!this.api?.Events?.onDocumentChange) return () => {}
    let active = true
    let docRef
    const bridge = event => {
      for (const change of event?.changes || []) {
        handler({ ...change, blockId: String(change.blockId || change.parentBlockId || "") })
      }
    }
    this.api.getActiveDocumentRef().then(ref => {
      if (!active) return
      docRef = ref
      this.docRef = ref
      return this.api.Events.onDocumentChange(ref, bridge)
    }).catch(() => {})
    return () => {
      active = false
      if (docRef) this.api.Events.offDocumentChange(docRef, bridge).catch(() => {})
    }
  }
}

const HEADING = /^heading([1-9])$/
const TEXTUAL = new Set(["text", "bullet", "ordered", "todo", "quote", "code"])

/** 把飞书官方根块快照转换为 CCiteheck 输入契约。 */
export function snapshotFromRoot(root, { documentId, title }) {
  const blocks = []
  let tableIndex = 0

  function visit(block, parentId = null) {
    if (!block) return
    const type = String(block.type || "")
    const heading = type.match(HEADING)
    if (heading) pushTextBlock(block, "heading", parentId, { heading_level: Number(heading[1]) })
    else if (TEXTUAL.has(type)) {
      const blockType = ["bullet", "ordered", "todo"].includes(type) ? "list_item" : "paragraph"
      pushTextBlock(block, blockType, parentId)
    } else if (type === "table") {
      pushTable(block, tableIndex++)
      return
    }
    for (const child of block.childSnapshots || []) visit(child, String(block.id))
  }

  function pushTextBlock(block, blockType, parentId, extra = {}) {
    const text = plainText(block)
    if (!text.trim()) return
    blocks.push({ block_id: String(block.id), parent_id: parentId, block_type: blockType, text, ...extra })
  }

  function pushTable(table, index) {
    const cells = table.childSnapshots || []
    const columns = Math.max(1, Number(table.data?.property?.column_size || cells.length))
    cells.forEach((cell, position) => {
      const text = collectPlainText(cell)
      if (!text.trim()) return
      blocks.push({
        block_id: String(cell.id),
        parent_id: String(table.id),
        block_type: "table_cell",
        text,
        table_index: index,
        row_index: Math.floor(position / columns),
        cell_index: position % columns,
        row_start: Math.floor(position / columns),
        row_end: Math.floor(position / columns),
        col_start: position % columns,
        col_end: position % columns,
      })
    })
  }

  visit(root)
  return {
    schema_version: "1",
    document_id: String(documentId),
    title: title || "飞书文档",
    revision: null,
    blocks,
  }
}

function plainText(block) {
  return String(block?.data?.plain_text || "")
}

function collectPlainText(block) {
  const parts = [plainText(block)]
  for (const child of block?.childSnapshots || []) parts.push(collectPlainText(child))
  return parts.filter(Boolean).join("\n")
}

function demoSnapshot() {
  return {
    schema_version: "1",
    document_id: "demo-feishu-document",
    title: "网络数据合规研究（交互预览）",
    revision: "demo-r1",
    blocks: [
      { block_id: "blk-demo-1", block_type: "heading", heading_level: 1, text: "网络爬虫的法律边界" },
      { block_id: "blk-demo-2", block_type: "paragraph", text: "在此之前，法院已通过反法第2条和原第12条处理了大量爬虫纠纷。" },
      { block_id: "blk-demo-3", block_type: "paragraph", text: "《网络数据安全管理条例》第18条确立了使用自动化工具访问、收集网络数据的事前评估义务，并明确违反的承担行政责任。" }
    ]
  }
}
