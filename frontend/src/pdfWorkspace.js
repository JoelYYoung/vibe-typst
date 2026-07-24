const pageNames = (value) => Array.isArray(value) ? value.filter((name) => typeof name === 'string') : []

export const pdfWorkspacePanes = ['terminal', 'preview', 'files', 'presenter']

export function pdfVersions(response) {
  if (Array.isArray(response)) return response
  return response && Array.isArray(response.versions) ? response.versions : []
}

export function pdfTranscriptDirty(draft, saved) {
  return draft !== saved
}

export function shouldSyncPdfTranscriptDraft({ pageChanged, savedChanged, dirty }) {
  return pageChanged || (savedChanged && !dirty)
}

export function nextPdfRenderState(previous = {}, response = {}) {
  const pages = pageNames(response.pages)
  const version = response.version ?? previous.version ?? 0
  const serverTokens = response.tokens && typeof response.tokens === 'object' ? response.tokens : {}
  const tokens = Object.fromEntries(pages.map((name) => [name, serverTokens[name] ?? `pdf-${version}-${name}`]))
  const total = pages.length
  const requested = Number.isInteger(previous.page) ? previous.page : 1
  const page = total ? Math.max(1, Math.min(requested, total)) : 1
  return { pages, tokens, version, page }
}
