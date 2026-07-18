/** 按 DOCX 解析器给出的真实阅读顺序排列核查卡片。 */
export function orderChecksByDocument(checks, previewBlocks = []) {
  const blockOrder = new Map(previewBlocks.map(block => [block.block_id, block.order]))
  return checks
    .map((check, originalIndex) => ({
      check,
      originalIndex,
      documentOrder: Math.min(
        ...(check.source_locations || [])
          .map(location => blockOrder.get(location.block_id))
          .filter(Number.isFinite),
        Number.MAX_SAFE_INTEGER,
      ),
    }))
    .sort((left, right) =>
      left.documentOrder - right.documentOrder || left.originalIndex - right.originalIndex
    )
    .map(item => item.check)
}
