export function selectPdfFile(files, currentFile = null) {
  const candidates = Array.from(files || [])
  if (candidates.length !== 1) {
    return { file: currentFile, error: 'Choose exactly one PDF file.' }
  }
  const candidate = candidates[0]
  const isPdf = candidate?.type === 'application/pdf'
    || candidate?.name?.toLowerCase().endsWith('.pdf')
  if (!isPdf) {
    return { file: currentFile, error: 'Only PDF files are supported.' }
  }
  return { file: candidate, error: null }
}

export function pdfFileFromSelection(files) {
  return selectPdfFile(files).file
}

const compactNumber = (value) => (
  value.toFixed(value >= 10 || Number.isInteger(value) ? 0 : 1)
)

export function formatFileSize(bytes) {
  const size = Number.isFinite(bytes) ? Math.max(0, bytes) : 0
  if (size < 1024) return `${Math.round(size)} B`
  if (size < 1024 * 1024) return `${compactNumber(size / 1024)} KB`
  return `${compactNumber(size / (1024 * 1024))} MB`
}

export function switchProjectCreationType(state, type) {
  return { ...state, type, file: type === 'pdf' ? state.file : null }
}

export function resetProjectCreation() {
  return { name: '', type: 'typst', file: null }
}

export function canSubmitProjectCreation({ name, type, file, busy }) {
  return !busy && Boolean(name && name.trim()) && (type !== 'pdf' || Boolean(file))
}
