import test from 'node:test'
import assert from 'node:assert/strict'
import {
  canSubmitProjectCreation,
  pdfFileFromSelection,
  resetProjectCreation,
  switchProjectCreationType,
} from '../src/projectCreation.js'
import { canonicalProjectFromOpen, workspaceViewFor } from '../src/projectRouting.js'

test('the canonical open result, rather than a stale card, selects PdfWorkspace', () => {
  const staleCard = { id: 'project-1', type: 'typst' }
  const response = { project: { id: 'project-1', type: 'pdf', name: 'Paper' } }
  const canonical = canonicalProjectFromOpen(response)

  assert.notEqual(canonical, staleCard)
  assert.equal(canonical, response.project)
  assert.equal(workspaceViewFor(canonical), 'PdfWorkspace')
  assert.notEqual(workspaceViewFor(canonical), 'App')
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

test('PDF creation readiness includes name, selected file, and busy state, then resets', () => {
  const pdf = { name: 'paper.pdf' }
  assert.equal(canSubmitProjectCreation({ name: 'Paper', type: 'pdf', file: pdf, busy: false }), true)
  assert.equal(canSubmitProjectCreation({ name: 'Paper', type: 'pdf', file: null, busy: false }), false)
  assert.equal(canSubmitProjectCreation({ name: 'Paper', type: 'pdf', file: pdf, busy: true }), false)
  assert.deepEqual(resetProjectCreation(), { name: '', type: 'typst', file: null })
})
