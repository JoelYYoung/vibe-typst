import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { execFileSync } from 'node:child_process'
import {
  createPdfPollController,
  editPdfTranscriptDraft,
  finishPdfTranscriptSave,
  nextPdfRenderState,
  pdfTerminalCdCommand,
  pdfTranscriptDirty,
  pdfVersions,
  reconcilePdfTranscriptDrafts,
  startPdfTranscriptSave,
  pdfWorkspacePanes,
} from '../src/pdfWorkspace.js'

const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

test('PDF workspace keeps terminal, preview, files, and presenter while omitting Typst-only panes', () => {
  assert.deepEqual(pdfWorkspacePanes, ['terminal', 'preview', 'files', 'presenter'])
  assert.equal(pdfWorkspacePanes.includes('editor'), false)
  assert.equal(pdfWorkspacePanes.includes('comments'), false)
})

test('PDF transcript draft is dirty only when it differs from saved page text', () => {
  assert.equal(pdfTranscriptDirty('', ''), false)
  assert.equal(pdfTranscriptDirty('Narration', 'Narration'), false)
  assert.equal(pdfTranscriptDirty('Changed narration', 'Narration'), true)
})

test('PDF render polling derives stable page tokens and clamps the active page after shrink', () => {
  const previous = { pages: ['page-1.png', 'page-2.png', 'page-3.png'], tokens: {}, page: 3 }
  const first = nextPdfRenderState(previous, { version: 7, pages: ['page-1.png', 'page-2.png'] })
  const second = nextPdfRenderState(first, { version: 7, pages: ['page-1.png', 'page-2.png'] })

  assert.equal(first.page, 2)
  assert.deepEqual(first.tokens, { 'page-1.png': 'pdf-7-page-1.png', 'page-2.png': 'pdf-7-page-2.png' })
  assert.deepEqual(second.tokens, first.tokens)
  assert.equal(nextPdfRenderState(first, { version: 6, pages: ['stale.png'] }), first)
})

test('PDF terminal cd command works with the deployed one-argument wrapper and quoted paths', () => {
  const command = pdfTerminalCdCommand("/workspace/Paper's draft")
  assert.equal(command.includes('cd --'), false)
  assert.equal(command, "cd '/workspace/Paper'\\''s draft'\n")
  const observed = execFileSync('bash', ['-c', `cd() { local t="${'${1:-/workspace}'}"; printf '%s' "$t"; }; ${command}`], { encoding: 'utf8' })
  assert.equal(observed, "/workspace/Paper's draft")
})

test('PDF poll controller stays single-flight, rejects older generations, and protects a saved map from stale responses', async () => {
  const renderOne = deferred()
  const mapOne = deferred()
  const renderTwo = deferred()
  const mapTwo = deferred()
  const renders = []
  const maps = []
  let renderCalls = 0
  let mapCalls = 0
  const controller = createPdfPollController({
    loadRender: () => [renderOne, renderTwo][renderCalls++].promise,
    loadMap: () => [mapOne, mapTwo][mapCalls++].promise,
    onRender: (value) => renders.push(value.version),
    onMap: (value) => maps.push(value),
    onError: () => assert.fail('unexpected poll error'),
  })

  const first = controller.poll()
  controller.poll()
  assert.equal(renderCalls, 1)
  renderOne.resolve({ version: 7, pages: ['page-1.png'] })
  await Promise.resolve()
  assert.equal(mapCalls, 1)
  const saved = { pages: [{ page: 1, note: 'saved' }], orphans: [] }
  controller.replaceMapAfterSave(saved)
  mapOne.resolve({ pages: [{ page: 1, note: 'stale' }], orphans: [] })
  await Promise.resolve()
  assert.equal(renderCalls, 2)
  renderTwo.resolve({ version: 6, pages: ['stale.png'] })
  await Promise.resolve()
  mapTwo.resolve({ pages: [{ page: 1, note: 'fresh' }], orphans: [] })
  await first

  assert.deepEqual(renders, [7])
  assert.deepEqual(maps, [saved, { pages: [{ page: 1, note: 'fresh' }], orphans: [] }])
  assert.equal(controller.maxConcurrent, 1)
})

test('PDF poll controller keeps the last good render and map through a transient failure, then recovers', async () => {
  const renderFailure = deferred()
  const maps = []
  const renders = []
  const errors = []
  let renderCalls = 0
  let mapCalls = 0
  const controller = createPdfPollController({
    loadRender: () => {
      renderCalls += 1
      if (renderCalls === 2) return renderFailure.promise
      return Promise.resolve({ version: renderCalls === 1 ? 4 : 5, pages: [`page-${renderCalls}.png`] })
    },
    loadMap: () => {
      mapCalls += 1
      return Promise.resolve({ pages: [{ page: 1, note: `map-${mapCalls}` }], orphans: [] })
    },
    onRender: (value) => renders.push(value),
    onMap: (value) => maps.push(value),
    onError: (error) => errors.push(error.message),
  })

  await controller.poll()
  const failed = controller.poll()
  controller.poll()
  renderFailure.reject(new Error('temporary render failure'))
  await failed

  assert.deepEqual(renders, [
    { version: 4, pages: ['page-1.png'] },
    { version: 5, pages: ['page-3.png'] },
  ])
  assert.deepEqual(maps, [
    { pages: [{ page: 1, note: 'map-1' }], orphans: [] },
    { pages: [{ page: 1, note: 'map-2' }], orphans: [] },
  ])
  assert.deepEqual(errors, ['temporary render failure'])
  assert.equal(mapCalls, 2)
  assert.equal(controller.maxConcurrent, 1)
})

test('PDF transcript drafts remain per-page when page one saves after page two is edited', () => {
  let drafts = reconcilePdfTranscriptDrafts({}, [
    { page: 1, note: 'one' }, { page: 2, note: 'two' },
  ], 2)
  drafts = editPdfTranscriptDraft(drafts, 1, 'one edited')
  const started = startPdfTranscriptSave(drafts, 1)
  drafts = started.drafts
  drafts = editPdfTranscriptDraft(drafts, 2, 'two edited')
  drafts = finishPdfTranscriptSave(drafts, started.request, true)

  assert.deepEqual(drafts[1], { draft: 'one edited', base: 'one edited', saving: false })
  assert.deepEqual(drafts[2], { draft: 'two edited', base: 'two', saving: false })
})

test('PDF transcript save request is captured synchronously and marks only that page saving', () => {
  const captured = { page: 1, text: 'page one' }
  const started = startPdfTranscriptSave({
    1: { draft: 'page one', base: 'old one', saving: false },
    2: { draft: 'page two', base: 'old two', saving: false },
  }, captured)
  assert.deepEqual(started.request, { page: 1, text: 'page one' })
  assert.equal(started.request, captured)
  assert.equal(started.drafts[1].saving, true)
  assert.equal(started.drafts[2].saving, false)
  const preview = fs.readFileSync(new URL('../src/PdfPreviewPane.jsx', import.meta.url), 'utf8')
  assert.match(preview, /if \(!dirty \|\| !page \|\| draftState\.saving\) return/)
})

test('PDF versions keep the API array response and the drawer exposes restore', () => {
  const versions = [{ tag: 'v2', message: 'revised', is_current: false }]
  assert.deepEqual(pdfVersions(versions), versions)
  assert.deepEqual(pdfVersions({ versions }), versions)
  const source = fs.readFileSync(new URL('../src/PdfWorkspace.jsx', import.meta.url), 'utf8')
  assert.match(source, /api\.gitRestore/)
})

test('PDF workspace boundary mounts only viewer-safe components and does not invoke Typst APIs', () => {
  const source = fs.readFileSync(new URL('../src/PdfWorkspace.jsx', import.meta.url), 'utf8')
  for (const required of ['TermPanel', 'PdfPreviewPane', 'PdfFilesDrawer', 'Presenter']) {
    assert.match(source, new RegExp(required))
  }
  for (const forbidden of [
    'TypstEditor', 'CommentCard', 'api.compile', 'api.resolve', 'api.locate',
    'api.getComments', 'api.getDocument', 'api.openFile', 'CRDT', 'resolver',
  ]) {
    assert.equal(source.includes(forbidden), false, `${forbidden} must not appear in PdfWorkspace`)
  }
})

test('PDF terminal receives the canonical project directory through TermPanel initialCwd', () => {
  const workspace = fs.readFileSync(new URL('../src/PdfWorkspace.jsx', import.meta.url), 'utf8')
  const terminal = fs.readFileSync(new URL('../src/TermPanel.jsx', import.meta.url), 'utf8')
  assert.match(workspace, /initialCwd=\{projectDir\}/)
  assert.match(terminal, /initialCwd/)
  assert.match(terminal, /ws\.onopen[\s\S]*initialCwd/)
})

test('PDF API helpers load and save one authoritative transcript page', async () => {
  const { getPdfTranscripts, savePdfTranscript } = await import('../src/api.js')
  const originalFetch = globalThis.fetch
  const requests = []
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, options })
    return { ok: true, json: async () => ({ ok: true }) }
  }
  try {
    await getPdfTranscripts()
    await savePdfTranscript(2, 'Page two')
    assert.equal(requests[0].url, '/api/pdf/transcripts')
    assert.equal(requests[1].url, '/api/pdf/transcripts/2')
    assert.equal(requests[1].options.method, 'PATCH')
    assert.deepEqual(JSON.parse(requests[1].options.body), { text: 'Page two' })
  } finally {
    globalThis.fetch = originalFetch
  }
})
