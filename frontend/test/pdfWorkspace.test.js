import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import {
  nextPdfRenderState,
  pdfTranscriptDirty,
  pdfVersions,
  pdfWorkspacePanes,
  shouldSyncPdfTranscriptDraft,
} from '../src/pdfWorkspace.js'

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

test('PDF transcript polling never overwrites a dirty draft or a just-saved draft with stale data', () => {
  assert.equal(shouldSyncPdfTranscriptDraft({ pageChanged: false, savedChanged: false, dirty: false }), false)
  assert.equal(shouldSyncPdfTranscriptDraft({ pageChanged: false, savedChanged: true, dirty: true }), false)
  assert.equal(shouldSyncPdfTranscriptDraft({ pageChanged: false, savedChanged: true, dirty: false }), true)
  assert.equal(shouldSyncPdfTranscriptDraft({ pageChanged: true, savedChanged: false, dirty: true }), true)
})

test('PDF render polling derives stable page tokens and clamps the active page after shrink', () => {
  const previous = { pages: ['page-1.png', 'page-2.png', 'page-3.png'], tokens: {}, page: 3 }
  const first = nextPdfRenderState(previous, { version: 7, pages: ['page-1.png', 'page-2.png'] })
  const second = nextPdfRenderState(first, { version: 7, pages: ['page-1.png', 'page-2.png'] })

  assert.equal(first.page, 2)
  assert.deepEqual(first.tokens, { 'page-1.png': 'pdf-7-page-1.png', 'page-2.png': 'pdf-7-page-2.png' })
  assert.deepEqual(second.tokens, first.tokens)
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
