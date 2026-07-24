export function pdfFileFromSelection(files) {
  if (!files || files.length !== 1) return null
  const file = files[0]
  return file && typeof file.name === 'string' && file.name.toLowerCase().endsWith('.pdf') ? file : null
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
