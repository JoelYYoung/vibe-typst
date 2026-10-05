const issues = new Map()
const listeners = new Set()
const failedReads = new Map()
const completedRequests = new Map()
let requestSequence = 0

let snapshot = { disconnected: false, issues: [] }

function publish() {
  snapshot = {
    disconnected: issues.size > 0,
    issues: [...issues.entries()].map(([source, message]) => ({ source, message })),
  }
  listeners.forEach((listener) => listener())
}

export function reportConnectionLost(source, message) {
  if (!source || issues.get(source) === message) return
  issues.set(source, message || 'The remote server is unavailable.')
  publish()
}

export function reportConnectionRestored(source) {
  if (!issues.delete(source)) return
  publish()
}

export function subscribeConnectionStatus(listener) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function getConnectionSnapshot() {
  return snapshot
}

export function connectionSourceFor(input) {
  const url = new URL(typeof input === 'string' || input instanceof URL ? String(input) : input.url,
    typeof location === 'undefined' ? 'http://localhost' : location.href)
  return `http:${url.origin}${url.pathname}`
}

// A successful account or heartbeat request cannot establish that a stalled workspace
// endpoint has recovered. Track reads independently, retaining safe retry targets.
export async function retryFailedReads(init, excludeInput) {
  const excluded = excludeInput ? connectionSourceFor(excludeInput) : null
  await Promise.allSettled([...failedReads.entries()]
    .filter(([source]) => source !== excluded)
    .map(([, input]) => trackedFetch(input, init)))
}

export async function trackedFetch(input, init) {
  const isRead = (init?.method || input?.method || 'GET').toUpperCase() === 'GET'
  const source = isRead ? connectionSourceFor(input) : 'server'
  const sequence = ++requestSequence
  const record = (failed) => {
    if (sequence < (completedRequests.get(source) || 0)) return
    completedRequests.set(source, sequence)
    if (failed) {
      if (isRead) failedReads.set(source, input)
      reportConnectionLost(source, 'The remote server is unavailable.')
    } else {
      failedReads.delete(source)
      reportConnectionRestored(source)
    }
  }
  try {
    const response = await globalThis.fetch(input, init)
    if ([502, 503, 504].includes(response.status)) {
      record(true)
    } else {
      record(false)
    }
    return response
  } catch (error) {
    // An AbortController is also used for normal component cleanup. Only a real request
    // failure should change the application-wide connection state.
    if (!(error?.name === 'AbortError' && (init?.signal || input?.signal)?.aborted)) {
      record(true)
    }
    throw error
  }
}

// Test-only reset kept explicit so connection-state tests cannot leak module state.
export function resetConnectionStatus() {
  failedReads.clear()
  completedRequests.clear()
  requestSequence = 0
  if (!issues.size) return
  issues.clear()
  publish()
}
