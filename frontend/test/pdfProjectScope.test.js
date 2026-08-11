import test from 'node:test'
import assert from 'node:assert/strict'
import { projectionProjectId, projectionUrl } from '../src/workspaceRouting.js'

// One workspace backend serves several open projects but treats exactly one as "active".
// A presenter that asked for "the active PDF" started reading another tab's project the
// moment that tab opened one — its page images and transcript saves followed the wrong deck.
// Every PDF request now names its own project, and the projection window (a separate tab,
// with no access to the presenter's in-memory scope) carries it in the URL.

test('a projection carries the project it was opened for, alongside its workspace', () => {
  assert.equal(
    projectionUrl('?workspace=' + 'a'.repeat(24), 'project-1'),
    `/?workspace=${'a'.repeat(24)}&project=&pid=project-1`,
  )
  assert.equal(projectionUrl('', 'project-1'), '/?project=&pid=project-1')
  // `project` (empty) stays the flag that selects the audience view; `pid` names the deck.
  assert.equal(projectionUrl('', null), '/?project=')
})

test('a projection recovers its project from its own URL', () => {
  assert.equal(projectionProjectId('?project=&pid=project-1'), 'project-1')
  assert.equal(projectionProjectId('?project='), null)
  assert.equal(projectionProjectId('?project=&pid=   '), null)
  assert.equal(projectionProjectId(''), null)
})

test('PDF requests name their project while Typst requests keep the active document', async () => {
  const api = await import('../src/api.js')

  api.setPdfProjectScope(null)
  assert.equal(api.pdfProjectScope(), null)
  // Unscoped: exactly the URLs the Typst editor has always sent.
  assert.equal(api.renderUrl('page-1.svg', 'tok'), '/api/render/page-1.svg?v=tok')

  api.setPdfProjectScope('project-1')
  assert.equal(api.pdfProjectScope(), 'project-1')
  // The render URL already carries a cache-busting token, so the scope appends with `&`.
  assert.equal(
    api.renderUrl('page-1.png', 'tok'),
    '/api/render/page-1.png?v=tok&project_id=project-1',
  )

  api.setPdfProjectScope('a/b')
  assert.equal(
    api.renderUrl('page-1.png', 'tok'),
    '/api/render/page-1.png?v=tok&project_id=a%2Fb',
  )
  api.setPdfProjectScope(null)
})
