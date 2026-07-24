import test from 'node:test'
import assert from 'node:assert/strict'
import { createPdfProject } from '../src/api.js'
import { projectType, workspaceComponentFor } from '../src/projectTypes.js'

test('missing project type remains typst', () => {
  assert.equal(projectType({}), 'typst')
})

test('PDF project type stays PDF', () => {
  assert.equal(projectType({ type: 'pdf' }), 'pdf')
})

test('unknown or malformed project types safely remain typst', () => {
  assert.equal(projectType({ type: 'other' }), 'typst')
  assert.equal(projectType(null), 'typst')
  assert.equal(projectType('pdf'), 'typst')
})

test('workspace routing follows the safe project type', () => {
  assert.equal(workspaceComponentFor({ type: 'pdf' }), 'pdf')
  assert.equal(workspaceComponentFor({}), 'typst')
  assert.equal(workspaceComponentFor({ type: 'unknown' }), 'typst')
})

test('createPdfProject posts just name and file as FormData', async () => {
  const originalFetch = globalThis.fetch
  let request
  globalThis.fetch = async (url, options) => {
    request = { url, options }
    return { ok: true, json: async () => ({ id: 'pdf-project' }) }
  }
  try {
    const file = new Blob(['%PDF-1.4'], { type: 'application/pdf' })
    const result = await createPdfProject('Paper', file)

    assert.deepEqual(result, { id: 'pdf-project' })
    assert.equal(request.url, '/api/projects/pdf')
    assert.equal(request.options.method, 'POST')
    assert.equal(request.options.headers, undefined)
    assert.deepEqual([...request.options.body.keys()], ['name', 'file'])
    assert.equal(request.options.body.get('name'), 'Paper')
    assert.equal(await request.options.body.get('file').text(), await file.text())
  } finally {
    globalThis.fetch = originalFetch
  }
})
