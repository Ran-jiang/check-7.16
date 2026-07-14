export async function connectToWord() {
  if (!window.Office) {
    throw new Error("未加载 Office.js")
  }
  const info = await Office.onReady()
  if (info.host !== Office.HostType.Word) {
    throw new Error("请在 Microsoft Word 中打开此插件")
  }
}

export function getDocumentName() {
  const url = Office.context.document.url
  if (!url) return "当前文档.docx"
  const fileName = decodeURIComponent(url.split("/").pop() || "")
  return fileName || "当前文档.docx"
}

export async function getDocumentBase64() {
  const file = await getCompressedFile()
  try {
    const slices = []
    let totalLength = 0
    for (let index = 0; index < file.sliceCount; index += 1) {
      const slice = await getSlice(file, index)
      const bytes = Uint8Array.from(slice.data)
      slices.push(bytes)
      totalLength += bytes.length
    }
    const documentBytes = new Uint8Array(totalLength)
    let offset = 0
    for (const slice of slices) {
      documentBytes.set(slice, offset)
      offset += slice.length
    }
    return bytesToBase64(documentBytes)
  } finally {
    file.closeAsync()
  }
}

export function getSelectedText() {
  return new Promise((resolve, reject) => {
    Office.context.document.getSelectedDataAsync(
      Office.CoercionType.Text,
      result => result.status === Office.AsyncResultStatus.Succeeded
        ? resolve((result.value || "").trim())
        : reject(new Error(result.error.message))
    )
  })
}

// Word 的 search 对超长字符串会失败，截取片段前缀定位。
const JUMP_SNIPPET_LIMIT = 90

export async function jumpToText(snippet, occurrence = 0) {
  if (!window.Word?.run) {
    throw new Error("当前 Word 版本不支持文档内定位")
  }
  const needle = (snippet || "").trim().slice(0, JUMP_SNIPPET_LIMIT)
  if (!needle) throw new Error("没有可定位的文本")
  await Word.run(async context => {
    const results = context.document.body.search(needle, { matchCase: false })
    results.load("items")
    await context.sync()
    if (!results.items.length) {
      throw new Error("未在文档中找到该片段（可能已被修改）")
    }
    const target = results.items[Math.min(occurrence, results.items.length - 1)]
    target.select()
    await context.sync()
  })
}

function getCompressedFile() {
  return new Promise((resolve, reject) => {
    Office.context.document.getFileAsync(
      Office.FileType.Compressed,
      { sliceSize: 1024 * 1024 },
      result => result.status === Office.AsyncResultStatus.Succeeded
        ? resolve(result.value)
        : reject(new Error(result.error.message))
    )
  })
}

function getSlice(file, index) {
  return new Promise((resolve, reject) => {
    file.getSliceAsync(index, result => result.status === Office.AsyncResultStatus.Succeeded
      ? resolve(result.value)
      : reject(new Error(result.error.message)))
  })
}

function bytesToBase64(bytes) {
  const chunkSize = 32768
  let binary = ""
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize))
  }
  return btoa(binary)
}
