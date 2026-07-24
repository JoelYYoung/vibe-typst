import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { execFileSync } from 'node:child_process'
import {
  createPdfPollController,
  createPdfRestoreResetLatch,
  editPdfTranscriptDraft,
  finishPdfTranscriptSave,
  nextPdfRenderState,
  pdfTerminalCdCommand,
  pdfTranscriptExportText,
  pdfTranscriptDirty,
  pdfVersions,
  reconcilePdfTranscriptDrafts,
  resetPdfTranscriptDrafts,
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

test('PDF render polling uses an opaque generation for tokens and accepts a restarted lower version', () => {
  const previous = { pages: ['page-1.png', 'page-2.png', 'page-3.png'], tokens: {}, page: 3 }
  const first = nextPdfRenderState(previous, { version: 7, generation: 'generation-A', pages: ['page-1.png', 'page-2.png'] })
  const restarted = nextPdfRenderState(first, { version: 1, generation: 'generation-B', pages: ['page-1.png'] })

  assert.equal(first.page, 2)
  assert.deepEqual(first.tokens, { 'page-1.png': 'pdf-generation-A-page-1.png', 'page-2.png': 'pdf-generation-A-page-2.png' })
  assert.equal(restarted.version, 1)
  assert.equal(restarted.generation, 'generation-B')
  assert.deepEqual(restarted.tokens, { 'page-1.png': 'pdf-generation-B-page-1.png' })
})

test('PDF replacement updates Presenter pages and transcript rows from the matched parent map', async () => {
  let presenterState = {}
  const replacements = [
    {
      render: { version: 7, generation: 'generation-A', pages: ['old-page.png'] },
      map: { generation: 'generation-A', pages: [{ page: 1, note: 'old transcript' }], orphans: [] },
    },
    {
      render: { version: 1, generation: 'generation-B', pages: ['new-page-1.png', 'new-page-2.png'] },
      map: {
        generation: 'generation-B',
        pages: [{ page: 1, note: 'new first' }, { page: 2, note: 'new second' }],
        orphans: [{ page: 3, note: 'removed third' }],
      },
    },
  ]
  const controller = createPdfPollController({
    loadRender: () => replacements[0].render,
    loadMap: () => replacements.shift().map,
    onPair: (render, map) => {
      presenterState = nextPdfRenderState(presenterState, render, map)
    },
  })

  await controller.poll()
  await controller.poll()

  assert.deepEqual({
    generation: presenterState.generation,
    pages: presenterState.pages,
    transcriptRows: presenterState.slideMap,
  }, {
    generation: 'generation-B',
    pages: ['new-page-1.png', 'new-page-2.png'],
    transcriptRows: [{ page: 1, note: 'new first' }, { page: 2, note: 'new second' }],
  })
  const workspace = fs.readFileSync(new URL('../src/PdfWorkspace.jsx', import.meta.url), 'utf8')
  assert.match(workspace, /<Presenter[\s\S]*slideMap=\{render\.slideMap\}/)
  assert.match(workspace, /<Presenter[\s\S]*generation=\{render\.generation\}/)
})

test('PDF terminal cd command works with the deployed one-argument wrapper and quoted paths', () => {
  const command = pdfTerminalCdCommand("/workspace/Paper's draft")
  assert.equal(command.includes('cd --'), false)
  assert.equal(command, "cd '/workspace/Paper'\\''s draft'\n")
  const observed = execFileSync('bash', ['-c', `cd() { local t="${'${1:-/workspace}'}"; printf '%s' "$t"; }; ${command}`], { encoding: 'utf8' })
  assert.equal(observed, "/workspace/Paper's draft")
})

test('PDF poll controller commits only a matched render/map generation after a mismatch', async () => {
  const pairs = []
  let renderCalls = 0
  let mapCalls = 0
  const controller = createPdfPollController({
    loadRender: () => [
      { version: 7, generation: 'generation-A', pages: ['page-a.png'] },
      { version: 1, generation: 'generation-B', pages: ['page-b.png'] },
    ][renderCalls++],
    loadMap: () => [
      { generation: 'generation-B', pages: [{ page: 1, note: 'wrong pair' }], orphans: [] },
      { generation: 'generation-B', pages: [{ page: 1, note: 'matched pair' }], orphans: [] },
    ][mapCalls++],
    onPair: (render, map) => pairs.push({ generation: render.generation, note: map.pages[0].note }),
    onError: () => assert.fail('unexpected poll error'),
  })

  await controller.poll()

  assert.deepEqual(pairs, [{ generation: 'generation-B', note: 'matched pair' }])
  assert.equal(renderCalls, 2)
  assert.equal(mapCalls, 2)
  assert.equal(controller.maxConcurrent, 1)
})

test('PDF poll controller stops after one persistent generation mismatch and lets the scheduler retry', async () => {
  const pairs = []
  const errors = []
  let renderCalls = 0
  let mapCalls = 0
  const controller = createPdfPollController({
    loadRender: () => [
      { generation: 'generation-A', pages: [] },
      { generation: 'generation-A', pages: [] },
      { generation: 'generation-C', pages: [] },
    ][renderCalls++],
    loadMap: () => [
      { generation: 'generation-B', pages: [], orphans: [] },
      { generation: 'generation-B', pages: [], orphans: [] },
      { generation: 'generation-C', pages: [], orphans: [] },
    ][mapCalls++],
    onPair: (render) => pairs.push(render.generation),
    onError: (error) => errors.push(error.message),
  })

  assert.equal(await controller.poll(), false)
  assert.deepEqual(pairs, [])
  assert.equal(renderCalls, 2)
  assert.equal(mapCalls, 2)
  assert.deepEqual(errors, ['PDF render and transcript generations did not converge'])
})

test('PDF poll controller does not start a map request before its render request settles', async () => {
  const render = deferred()
  let mapCalls = 0
  const controller = createPdfPollController({
    loadRender: () => render.promise,
    loadMap: () => {
      mapCalls += 1
      return { generation: 'generation-A', pages: [], orphans: [] }
    },
  })
  const poll = controller.poll()
  assert.equal(mapCalls, 0)
  render.resolve({ generation: 'generation-A', pages: [] })
  await poll
  assert.equal(mapCalls, 1)
})

test('PDF poll controller keeps the last good render and map through a transient failure, then recovers', async () => {
  const renderFailure = deferred()
  const pairs = []
  const errors = []
  let renderCalls = 0
  let mapCalls = 0
  const controller = createPdfPollController({
    loadRender: () => {
      renderCalls += 1
      if (renderCalls === 2) return renderFailure.promise
      return Promise.resolve({ version: renderCalls === 1 ? 4 : 5, generation: 'generation-A', pages: [`page-${renderCalls}.png`] })
    },
    loadMap: () => {
      mapCalls += 1
      return Promise.resolve({ generation: 'generation-A', pages: [{ page: 1, note: `map-${mapCalls}` }], orphans: [] })
    },
    onPair: (render, map) => pairs.push({ render, map }),
    onError: (error) => errors.push(error.message),
  })

  await controller.poll()
  const failed = controller.poll()
  controller.poll()
  renderFailure.reject(new Error('temporary render failure'))
  await failed

  assert.deepEqual(pairs, [
    { render: { version: 4, generation: 'generation-A', pages: ['page-1.png'] }, map: { generation: 'generation-A', pages: [{ page: 1, note: 'map-1' }], orphans: [] } },
    { render: { version: 5, generation: 'generation-A', pages: ['page-3.png'] }, map: { generation: 'generation-A', pages: [{ page: 1, note: 'map-2' }], orphans: [] } },
  ])
  assert.deepEqual(errors, ['temporary render failure'])
  assert.equal(mapCalls, 2)
  assert.equal(controller.maxConcurrent, 1)
})

test('PDF poll controller invalidation drops a stale matched pair before retrying', async () => {
  const renderOne = deferred()
  const mapOne = deferred()
  const pairs = []
  let calls = 0
  const controller = createPdfPollController({
    loadRender: () => calls++ === 0 ? renderOne.promise : { generation: 'generation-A', pages: ['page.png'] },
    loadMap: () => calls++ === 1 ? mapOne.promise : { generation: 'generation-A', pages: [{ page: 1, note: 'fresh' }], orphans: [] },
    onPair: (render, map) => pairs.push({ render, map }),
  })

  const first = controller.poll()
  const refreshed = controller.invalidateMapAfterSave()
  renderOne.resolve({ generation: 'generation-A', pages: ['page.png'] })
  mapOne.resolve({ generation: 'generation-A', pages: [{ page: 1, note: 'stale' }], orphans: [] })
  await first
  assert.equal(await refreshed, true)

  assert.deepEqual(pairs, [{
    render: { generation: 'generation-A', pages: ['page.png'] },
    map: { generation: 'generation-A', pages: [{ page: 1, note: 'fresh' }], orphans: [] },
  }])
})

test('PDF poll invalidation reports failure without pretending a stale map was refreshed', async () => {
  const controller = createPdfPollController({
    loadRender: () => Promise.reject(new Error('render unavailable')),
    loadMap: () => ({ generation: 'generation-A', pages: [], orphans: [] }),
  })
  assert.equal(await controller.invalidateMapAfterSave(), false)
})

test('PDF poll invalidation does not report a prior mutation when its queued refresh fails', async () => {
  let controller
  let renders = 0
  controller = createPdfPollController({
    loadRender: () => renders++ === 0
      ? { generation: 'generation-A', pages: [] }
      : Promise.reject(new Error('refresh failed')),
    loadMap: () => ({ generation: 'generation-A', pages: [], orphans: [] }),
    onPair: () => controller.invalidateMapAfterSave(),
  })
  assert.equal(await controller.poll(), false)
})

test('PDF restore reset stays pending through a failed refresh and consumes once after periodic recovery', async () => {
  const resets = []
  const latch = createPdfRestoreResetLatch(() => resets.push('reset'))
  let renders = 0
  const controller = createPdfPollController({
    loadRender: () => renders++ === 0
      ? Promise.reject(new Error('temporary refresh failure'))
      : { generation: 'generation-B', pages: ['restored.png'] },
    loadMap: () => ({ generation: 'generation-B', pages: [{ page: 1, note: 'restored' }], orphans: [] }),
    onPair: () => latch.consume(),
  })

  latch.markPending()
  assert.equal(await controller.invalidateMapAfterSave(), false)
  assert.deepEqual(resets, [])
  assert.equal(latch.pending, true)

  assert.equal(await controller.poll(), true)
  assert.deepEqual(resets, ['reset'])
  assert.equal(latch.pending, false)
  await controller.poll()
  assert.deepEqual(resets, ['reset'])
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

test('PDF transcript export uses the last saved per-page base and restore reset discards drafts', () => {
  const rows = [{ page: 1, note: 'server fresh one' }, { page: 2, note: 'server two' }]
  const drafts = {
    1: { draft: 'dirty one', base: 'saved one', saving: false },
    2: { draft: 'saved two', base: 'saved two', saving: false },
  }
  assert.equal(pdfTranscriptExportText(['page-1.png', 'page-2.png'], rows, drafts), 'Page 1\nserver fresh one\n\nPage 2\nsaved two\n')
  assert.deepEqual(resetPdfTranscriptDrafts(drafts, [{ page: 1, note: 'restored one' }], 1), {
    1: { draft: 'restored one', base: 'restored one', saving: false },
  })
  const workspace = fs.readFileSync(new URL('../src/PdfWorkspace.jsx', import.meta.url), 'utf8')
  assert.match(workspace, /Unsaved transcript drafts will be discarded/)
  assert.match(workspace, /await onRestored\?\.\(\)\n        await reload\(\)/)
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
