import test from 'node:test'
import assert from 'node:assert/strict'
import {
  getConnectionSnapshot,
  reportConnectionLost,
  reportConnectionRestored,
  resetConnectionStatus,
  trackedFetch,
  connectionSourceFor,
  retryFailedReads,
} from '../src/connectionStatus.js'

test.afterEach(() => resetConnectionStatus())

test('connection issues remain until every source has recovered', () => {
  reportConnectionLost('server', 'server down')
  reportConnectionLost('editor', 'editor down')
  assert.equal(getConnectionSnapshot().disconnected, true)
  assert.deepEqual(getConnectionSnapshot().issues.map((issue) => issue.source), ['server', 'editor'])

  reportConnectionRestored('server')
  assert.equal(getConnectionSnapshot().disconnected, true)
  assert.equal(getConnectionSnapshot().issues[0].source, 'editor')

  reportConnectionRestored('editor')
  assert.deepEqual(getConnectionSnapshot(), { disconnected: false, issues: [] })
})

test('tracked requests report gateway failures and clear after a reachable response', async () => {
  const originalFetch = globalThis.fetch
  try {
    globalThis.fetch = async () => ({ status: 503 })
    await trackedFetch('/api/app/state')
    assert.equal(getConnectionSnapshot().disconnected, true)

    globalThis.fetch = async () => ({ status: 400 })
    await trackedFetch('/api/app/state')
    assert.equal(getConnectionSnapshot().disconnected, false)
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('tracked network errors report a lost server connection', async () => {
  const originalFetch = globalThis.fetch
  try {
    globalThis.fetch = async () => { throw new TypeError('fetch failed') }
    await assert.rejects(trackedFetch('/api/app/state'), /fetch failed/)
    assert.equal(getConnectionSnapshot().issues[0].source, connectionSourceFor('/api/app/state'))
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('account and heartbeat responses cannot clear a failing workspace endpoint', async () => {
  const originalFetch = globalThis.fetch
  try {
    globalThis.fetch = async (url) => ({ status: url === '/api/comments' ? 503 : 200 })
    await trackedFetch('/api/comments')
    await trackedFetch('/whoami')
    await trackedFetch('/api/app/state')
    assert.equal(getConnectionSnapshot().disconnected, true)
    assert.equal(getConnectionSnapshot().issues.length, 1)

    globalThis.fetch = async () => ({ status: 200 })
    await trackedFetch('/api/comments?status=pending')
    assert.equal(getConnectionSnapshot().disconnected, false)
  } finally { globalThis.fetch = originalFetch }
})

test('older requests cannot overwrite a newer recovery', async () => {
  const originalFetch = globalThis.fetch
  let finishOld
  try {
    globalThis.fetch = () => new Promise((resolve) => { finishOld = resolve })
    const old = trackedFetch('/api/comments')
    globalThis.fetch = async () => ({ status: 200 })
    await trackedFetch('/api/comments')
    finishOld({ status: 503 })
    await old
    assert.equal(getConnectionSnapshot().disconnected, false)
  } finally { globalThis.fetch = originalFetch }
})

test('retry probes failing reads without replaying mutations', async () => {
  const originalFetch = globalThis.fetch
  try {
    globalThis.fetch = async () => ({ status: 503 })
    await trackedFetch('/api/projects')
    await trackedFetch('/api/projects', { method: 'POST', body: '{}' })
    const calls = []
    globalThis.fetch = async (input, init) => { calls.push({ input, init }); return { status: 200 } }
    await retryFailedReads({ cache: 'no-store' })
    assert.deepEqual(calls, [{ input: '/api/projects', init: { cache: 'no-store' } }])
    assert.deepEqual(getConnectionSnapshot().issues.map((issue) => issue.source), ['server'])
  } finally { globalThis.fetch = originalFetch }
})
