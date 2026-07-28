import test from 'node:test'
import assert from 'node:assert/strict'
import {
  canSubmitProjectCreation,
  formatFileSize,
  pdfFileFromSelection,
  resetProjectCreation,
  selectPdfFile,
  switchProjectCreationType,
} from '../src/projectCreation.js'
import {
  canonicalProjectFromOpen,
  clearRequestedProject,
  requestedProjectId,
  workspaceViewFor,
} from '../src/projectRouting.js'

test('the canonical open result, rather than a stale card, selects PdfWorkspace', () => {
  const staleCard = { id: 'project-1', type: 'typst' }
  const response = { project: { id: 'project-1', type: 'pdf', name: 'Paper' } }
  const canonical = canonicalProjectFromOpen(response)

  assert.notEqual(canonical, staleCard)
  assert.equal(canonical, response.project)
  assert.equal(workspaceViewFor(canonical), 'PdfWorkspace')
  assert.notEqual(workspaceViewFor(canonical), 'App')
})

test('openProject is distinct from the projection query', () => {
  assert.equal(requestedProjectId('?openProject=abc%20123'), 'abc 123')
  assert.equal(requestedProjectId('?project'), null)
  assert.equal(
    clearRequestedProject('?openProject=p1&theme=dark'),
    '?theme=dark',
  )
  assert.equal(clearRequestedProject('?openProject=p1'), '')
})

test('PDF creation only accepts one PDF file and switching away clears it', () => {
  const pdf = { name: 'paper.PDF' }
  const text = { name: 'notes.txt' }

  assert.equal(pdfFileFromSelection([]), null)
  assert.equal(pdfFileFromSelection([text]), null)
  assert.equal(pdfFileFromSelection([pdf, text]), null)
  assert.equal(pdfFileFromSelection([pdf]), pdf)
  assert.deepEqual(
    switchProjectCreationType({ name: 'Paper', type: 'pdf', file: pdf }, 'typst'),
    { name: 'Paper', type: 'typst', file: null },
  )
})

test('PDF selection accepts exactly one PDF and reports its file', () => {
  const file = { name: 'deck.pdf', size: 2048, type: 'application/pdf' }

  assert.deepEqual(selectPdfFile([file], null), { file, error: null })
})

test('an invalid PDF drop preserves the current valid selection', () => {
  const current = { name: 'deck.pdf', size: 2048, type: 'application/pdf' }
  const result = selectPdfFile(
    [{ name: 'notes.txt', size: 10, type: 'text/plain' }],
    current,
  )

  assert.equal(result.file, current)
  assert.match(result.error, /PDF/)
})

test('a multiple-file PDF drop preserves the current valid selection', () => {
  const current = { name: 'deck.pdf', size: 2048, type: 'application/pdf' }
  const result = selectPdfFile(
    [
      { name: 'a.pdf', size: 20, type: 'application/pdf' },
      { name: 'b.pdf', size: 30, type: 'application/pdf' },
    ],
    current,
  )

  assert.equal(result.file, current)
  assert.match(result.error, /one PDF/)
})

test('PDF file sizes use compact binary units', () => {
  assert.equal(formatFileSize(512), '512 B')
  assert.equal(formatFileSize(2048), '2 KB')
  assert.equal(formatFileSize(1572864), '1.5 MB')
})

test('PDF creation readiness includes name, selected file, and busy state, then resets', () => {
  const pdf = { name: 'paper.pdf' }
  assert.equal(canSubmitProjectCreation({ name: 'Paper', type: 'pdf', file: pdf, busy: false }), true)
  assert.equal(canSubmitProjectCreation({ name: 'Paper', type: 'pdf', file: null, busy: false }), false)
  assert.equal(canSubmitProjectCreation({ name: 'Paper', type: 'pdf', file: pdf, busy: true }), false)
  assert.deepEqual(resetProjectCreation(), { name: '', type: 'typst', file: null })
})
