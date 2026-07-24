const pageNames = (value) => Array.isArray(value) ? value.filter((name) => typeof name === 'string') : []
const quoteShell = (value) => `'${String(value).replace(/'/g, `'\\''`)}'`

export const pdfWorkspacePanes = ['terminal', 'preview', 'files', 'presenter']

export function pdfVersions(response) {
  if (Array.isArray(response)) return response
  return response && Array.isArray(response.versions) ? response.versions : []
}

export function pdfTranscriptDirty(draft, saved) {
  return draft !== saved
}

export function nextPdfRenderState(previous = {}, response = {}) {
  const pages = pageNames(response.pages)
  const version = response.version ?? previous.version ?? 0
  const generation = typeof response.generation === 'string' && response.generation
    ? response.generation
    : `legacy-${version}`
  const serverTokens = response.tokens && typeof response.tokens === 'object' ? response.tokens : {}
  const tokens = Object.fromEntries(pages.map((name) => [name, serverTokens[name] ?? `pdf-${generation}-${name}`]))
  const total = pages.length
  const requested = Number.isInteger(previous.page) ? previous.page : 1
  const page = total ? Math.max(1, Math.min(requested, total)) : 1
  return { pages, tokens, version, generation, page }
}

export function pdfTerminalCdCommand(path) {
  return `cd ${quoteShell(path)}\n`
}

export function createPdfPollController({ loadRender, loadMap, onPair = () => {}, onError = () => {} }) {
  let inFlight = null
  let queued = false
  let mapMutation = 0
  let concurrent = 0
  let maxConcurrent = 0

  const run = async () => {
    let committedMutation = -1
    do {
      queued = false
      const expectedMapMutation = mapMutation
      concurrent += 1
      maxConcurrent = Math.max(maxConcurrent, concurrent)
      try {
        const render = await loadRender()
        const map = await loadMap()
        if (!render?.generation || render.generation !== map?.generation) {
          queued = true
          continue
        }
        if (expectedMapMutation === mapMutation) {
          onPair(render, map)
          committedMutation = expectedMapMutation
        }
      } catch (error) {
        onError(error)
      } finally {
        concurrent -= 1
      }
    } while (queued)
    return committedMutation === mapMutation
  }

  const poll = () => {
    if (inFlight) {
      queued = true
      return inFlight
    }
    inFlight = run().finally(() => { inFlight = null })
    return inFlight
  }

  return {
    poll,
    invalidateMapAfterSave() {
      mapMutation += 1
      return poll()
    },
    get maxConcurrent() { return maxConcurrent },
  }
}

const pageNote = (rows, page) => {
  const row = Array.isArray(rows) ? rows.find((candidate) => candidate && candidate.page === page) : null
  return row && typeof row.note === 'string' ? row.note : ''
}

export function reconcilePdfTranscriptDrafts(previous = {}, rows, total) {
  const next = { ...previous }
  for (let page = 1; page <= total; page += 1) {
    const saved = pageNote(rows, page)
    const current = previous[page]
    if (!current || !pdfTranscriptDirty(current.draft, current.base)) {
      next[page] = { draft: saved, base: saved, saving: current?.saving || false }
    }
  }
  return next
}

export function resetPdfTranscriptDrafts(_previous = {}, rows, total) {
  return Object.fromEntries(Array.from({ length: total }, (_, index) => {
    const page = index + 1
    const saved = pageNote(rows, page)
    return [page, { draft: saved, base: saved, saving: false }]
  }))
}

export function pdfTranscriptExportText(pages, rows, drafts = {}) {
  return pageNames(pages).map((_, index) => {
    const page = index + 1
    const base = drafts[page]?.base
    const text = typeof base === 'string' ? base : pageNote(rows, page)
    return `Page ${page}\n${text}`
  }).join('\n\n') + '\n'
}

export function editPdfTranscriptDraft(drafts, page, draft) {
  const current = drafts[page] || { draft: '', base: '', saving: false }
  return { ...drafts, [page]: { ...current, draft } }
}

export function startPdfTranscriptSave(drafts, pageOrRequest) {
  const request = typeof pageOrRequest === 'object'
    ? pageOrRequest
    : { page: pageOrRequest, text: (drafts[pageOrRequest] || { draft: '' }).draft }
  const current = drafts[request.page] || { draft: '', base: '', saving: false }
  return { request, drafts: { ...drafts, [request.page]: { ...current, saving: true } } }
}

export function finishPdfTranscriptSave(drafts, request, ok) {
  const current = drafts[request.page]
  if (!current) return drafts
  if (!ok) return { ...drafts, [request.page]: { ...current, saving: false } }
  const unchangedSinceRequest = current.draft === request.text
  return {
    ...drafts,
    [request.page]: {
      draft: unchangedSinceRequest ? request.text : current.draft,
      base: request.text,
      saving: false,
    },
  }
}
