const BOOKMARK_PREFIX = "_cc"
const SENTENCE_ENDINGS = ["。", "！", "？"]

let repairEnabled = true

export async function seedSourceBookmarks(result) {
  assertWordApi()
  repairEnabled = true
  const targets = bookmarkTargets(result)
  const includeNotes = supportsWordApi15()

  return Word.run(async context => {
    const inventory = await buildRangeInventory(context, includeNotes)
    await loadSentenceRanges(context, inventory, targets)
    const bookmarkNames = await getBookmarkNames(context, includeNotes)

    for (const name of bookmarkNames) {
      if (name.toLowerCase().startsWith(BOOKMARK_PREFIX)) {
        context.document.deleteBookmark(name)
      }
    }
    await context.sync()

    const failed = []
    let seeded = 0
    for (const target of targets) {
      if (!includeNotes && isNoteBlock(target.location.block_id)) {
        failed.push(failureOf(target, "WordApi 1.4 不支持脚注或尾注定位"))
        continue
      }
      const range = findAnchorRange(inventory, target.location)
      if (!range) {
        failed.push(failureOf(target, "未找到 Anchor 原文"))
        continue
      }
      range.insertBookmark(target.bookmarkName)
      seeded += 1
    }
    await context.sync()
    return { requested: targets.length, seeded, failed }
  })
}

export async function clearSourceBookmarks() {
  assertWordApi()
  return Word.run(async context => {
    const bookmarkNames = await getBookmarkNames(context, supportsWordApi15())
    const names = bookmarkNames.filter(name => name.toLowerCase().startsWith(BOOKMARK_PREFIX))
    for (const name of names) context.document.deleteBookmark(name)
    await context.sync()
    repairEnabled = false
    return names.length
  })
}

export async function jumpToSource(check, documentKey) {
  assertWordApi()
  const target = primaryTarget(check, documentKey)
  const includeNotes = supportsWordApi15()
  if (!includeNotes && isNoteBlock(target.location.block_id)) {
    throw new Error("WordApi 1.4 不支持定位脚注或尾注原文")
  }

  return Word.run(async context => {
    const bookmark = context.document.getBookmarkRangeOrNullObject(target.bookmarkName)
    bookmark.load("isNullObject")
    await context.sync()
    if (!bookmark.isNullObject) {
      bookmark.select()
      await context.sync()
      return { location: target.location, method: "bookmark" }
    }
    if (!repairEnabled) throw new Error("定位标记已清除，请重新核查")

    const inventory = await buildRangeInventory(context, includeNotes)
    await loadSentenceRanges(context, inventory, [target])
    const repaired = findAnchorRange(inventory, target.location)
    if (repaired) {
      repaired.insertBookmark(target.bookmarkName)
      repaired.select()
      await context.sync()
      return { location: target.location, method: "text_repair" }
    }

    const block = inventory.find(item => item.blockId === target.location.block_id)
    if (!block) throw new Error("原文已删除，请重新核查")
    block.range.select()
    await context.sync()
    return {
      location: target.location,
      method: "block_fallback",
      warning: "原句可能已被修改，已定位到所在段落",
    }
  })
}

function assertWordApi() {
  if (!window.Word?.run) throw new Error("当前 Word 版本不支持文档内定位")
}

function supportsWordApi15() {
  return Office.context.requirements.isSetSupported("WordApi", "1.5")
}

function bookmarkTargets(result) {
  const verification = result.verification
  const checks = [
    ...(verification.citation_cards || []),
    ...(verification.case_checks || []),
  ]
  return checks.flatMap(check => locationsForCheck(check, result.document_key))
}

function locationsForCheck(check, documentKey) {
  const checkId = check.card_id || check.check_id
  return (check.source_locations || [])
    .map((location, index) => ({
      checkId,
      index,
      location,
      bookmarkName: bookmarkName(documentKey, checkId, index),
    }))
    .filter(target => target.location.platform === "docx" && target.location.anchor_text)
}

function primaryTarget(check, documentKey) {
  const targets = locationsForCheck(check, documentKey)
  if (!targets.length) throw new Error("该结果缺少 Word 块定位信息")
  const first = targets[0]
  const sameTableRow = targets.length > 1
    && targets.every(target => target.location.table_index === first.location.table_index
      && target.location.row_index === first.location.row_index
      && target.location.table_index !== null)
  return sameTableRow ? targets.at(-1) : first
}

function bookmarkName(documentKey, checkId, index) {
  return `${BOOKMARK_PREFIX}${hashText(documentKey)}_${checkId.toLowerCase()}_${index}`
}

function hashText(value) {
  let hash = 2166136261
  for (const character of String(value)) {
    hash ^= character.codePointAt(0)
    hash = Math.imul(hash, 16777619)
  }
  return (hash >>> 0).toString(36)
}

function failureOf(target, reason) {
  return { check_id: target.checkId, location_index: target.index, reason }
}

function isNoteBlock(blockId) {
  return /^word:(footnote|endnote):/.test(blockId)
}

function canonicalText(text) {
  return String(text)
    .replace(/\u0002/g, "")
    .replace(/[\t\n\r\u0007\u00a0\u3000]/g, " ")
    .replace(/ +/g, " ")
    .trim()
}

function findAnchorRange(inventory, location) {
  const anchor = canonicalText(location.anchor_text)
  const candidates = inventory
    .filter(item => item.sentences && item.normalizedText.includes(anchor))
    .sort((left, right) => Number(right.blockId === location.block_id)
      - Number(left.blockId === location.block_id))

  for (const item of candidates) {
    const pieces = item.sentences.items
    const normalizedPieces = normalizePieces(pieces)
    const prefix = [""]
    for (const piece of normalizedPieces) prefix.push(prefix.at(-1) + piece)
    let best = null
    for (let start = 0; start < pieces.length; start += 1) {
      for (let end = start; end < pieces.length; end += 1) {
        const combined = prefix[end + 1].slice(prefix[start].length)
        if (!combined.includes(anchor)) continue
        if (!best || end - start < best.end - best.start) best = { start, end }
        break
      }
    }
    if (best) {
      return best.start === best.end
        ? pieces[best.start]
        : pieces[best.start].expandTo(pieces[best.end])
    }
  }
  return null
}

function normalizePieces(pieces) {
  let previousEndedWithSpace = false
  return pieces.map((piece, index) => {
    const text = String(piece.text).replace(/\u0002/g, "")
    const beginsWithSpace = /^[\t\n\r\u0007\u00a0\u3000 ]/.test(text)
    const prefix = index && (previousEndedWithSpace || beginsWithSpace) ? " " : ""
    previousEndedWithSpace = /[\t\n\r\u0007\u00a0\u3000 ]$/.test(text)
    return prefix + canonicalText(text)
  })
}

async function getBookmarkNames(context, includeNotes) {
  const scopes = [context.document.body.getRange()]
  if (includeNotes) {
    const collections = [context.document.body.footnotes, context.document.body.endnotes]
    for (const collection of collections) collection.load("items")
    await context.sync()
    for (const collection of collections) {
      for (const note of collection.items) scopes.push(note.body.getRange())
    }
  }
  const results = scopes.map(scope => scope.getBookmarks(true, false))
  await context.sync()
  return [...new Set(results.flatMap(result => result.value))]
}

async function loadSentenceRanges(context, inventory, targets) {
  const anchors = targets.map(target => canonicalText(target.location.anchor_text))
  const candidates = inventory.filter(item => anchors.some(anchor => item.normalizedText.includes(anchor)))
  for (const item of candidates) {
    if (!SENTENCE_ENDINGS.some(ending => item.normalizedText.includes(ending))) {
      item.sentences = { items: [item.range] }
      continue
    }
    try {
      item.sentences = item.range.getTextRanges(SENTENCE_ENDINGS, false)
      item.sentences.load("items/text")
      await context.sync()
    } catch {
      item.sentences = null
    }
  }
}

async function buildRangeInventory(context, includeNotes) {
  const paragraphs = context.document.body.paragraphs
  const tables = context.document.body.tables
  paragraphs.load("items/tableNestingLevel")
  tables.load("items/rowCount,items/columnCount")
  const noteCollections = includeNotes
    ? [["footnote", context.document.body.footnotes], ["endnote", context.document.body.endnotes]]
    : []
  for (const [, collection] of noteCollections) collection.load("items")
  await context.sync()

  const inventory = []
  let paragraphIndex = 0
  for (const paragraph of paragraphs.items) {
    if (paragraph.tableNestingLevel !== 0) continue
    inventory.push({ blockId: `word:p:${paragraphIndex}`, range: paragraph.getRange() })
    paragraphIndex += 1
  }
  tables.items.forEach((table, tableIndex) => {
    for (let row = 0; row < table.rowCount; row += 1) {
      for (let cell = 0; cell < table.columnCount; cell += 1) {
        inventory.push({
          blockId: `word:t:${tableIndex}:${row}:${cell}`,
          range: table.getCell(row, cell).body.getRange(),
        })
      }
    }
  })
  for (const [type, collection] of noteCollections) {
    collection.items.forEach((note, noteIndex) => {
      note.body.paragraphs.load("items")
      inventory.push({ type, note, noteIndex })
    })
  }
  await context.sync()

  for (const pending of inventory.filter(item => item.note)) {
    const index = inventory.indexOf(pending)
    const ranges = pending.note.body.paragraphs.items.map((paragraph, paragraphIndex) => ({
      blockId: `word:${pending.type}:${pending.noteIndex}:${paragraphIndex}`,
      range: paragraph.getRange(),
    }))
    inventory.splice(index, 1, ...ranges)
  }
  for (const item of inventory) item.range.load("text")
  await context.sync()
  for (const item of inventory) item.normalizedText = canonicalText(item.range.text)
  return inventory
}
