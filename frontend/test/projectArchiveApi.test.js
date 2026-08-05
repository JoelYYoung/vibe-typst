import test from 'node:test'
import assert from 'node:assert/strict'
import {
  archiveProject,
  listProjects,
  restoreProject,
} from '../src/api.js'


test('project archive API separates active and archived lists and uses reversible actions', async () => {
  const requests = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, options })
    return new Response(JSON.stringify({ projects: [], ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }

  try {
    await listProjects()
    await listProjects(true)
    await archiveProject('deck/a')
    await restoreProject('deck/a')
  } finally {
    globalThis.fetch = originalFetch
  }

  assert.deepEqual(requests.map(({ url }) => url), [
    '/api/projects',
    '/api/projects?archived=true',
    '/api/projects/deck%2Fa/archive',
    '/api/projects/deck%2Fa/restore',
  ])
  assert.equal(requests[2].options.method, 'POST')
  assert.equal(requests[3].options.method, 'POST')
})
