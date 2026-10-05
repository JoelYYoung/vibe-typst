import assert from 'node:assert/strict'
import { test } from 'node:test'
import * as api from '../src/api.js'
import { recordingPageState, supportedRecordingType } from '../src/slideRecorder.js'

test('recording identity requires both page placement and exact slide content', () => {
  const take = { name: 'page-2.svg', token: 'content-a' }
  assert.equal(recordingPageState(take, 'page-2.svg', 'content-a'), 'recorded')
  assert.equal(recordingPageState(take, 'page-2.svg', 'content-b'), 'stale')
  assert.equal(recordingPageState(take, 'page-1.svg', 'content-a'), 'stale')
  assert.equal(recordingPageState(null, 'page-2.svg', 'content-a'), 'missing')
})

test('MP4-only browsers and unsupported browsers choose a valid recording path', () => {
  assert.equal(supportedRecordingType({ isTypeSupported: type => type === 'video/mp4' }), 'video/mp4')
  assert.equal(supportedRecordingType({ isTypeSupported: () => false }), null)
})

test('recording upload, preview, jobs and download stay in the addressed workspace and project', async () => {
  const previousFetch = globalThis.fetch
  const previousLocation = globalThis.location
  const requests = []
  globalThis.location = { search: '?workspace=0123456789abcdef01234567', href: 'http://localhost/', origin: 'http://localhost' }
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options })
    return new Response(JSON.stringify({}), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  api.setProjectScope('deck-a')
  try {
    await api.getRecording()
    await api.saveRecordingPage(2, new Blob(['video']), { duration: 1 })
    await api.startRecordingExport()
    await api.getRecordingExport('job')
    await api.prepareRecording()
    await api.clearRecordingPage(2)
    for (const { url } of requests) {
      assert.ok(url.startsWith('/project-workspaces/0123456789abcdef01234567/api/recording'))
      assert.ok(url.includes('project_id=deck-a'))
    }
    assert.ok(requests[1].options.body instanceof FormData)
    assert.equal(requests[1].options.method, 'PUT')
    assert.equal(requests.at(-1).options.method, 'DELETE')
    assert.ok(api.recordingPageUrl(2, 'take').includes('project_id=deck-a'))
    assert.ok(api.recordingExportUrl('job').includes('project_id=deck-a'))
  } finally {
    globalThis.fetch = previousFetch
    globalThis.location = previousLocation
    api.setProjectScope(null)
  }
})
