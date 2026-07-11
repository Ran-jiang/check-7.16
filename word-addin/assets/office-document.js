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
