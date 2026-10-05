import React, { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import {
  getConnectionSnapshot,
  reportConnectionLost,
  reportConnectionRestored,
  subscribeConnectionStatus,
  trackedFetch,
  connectionSourceFor,
  retryFailedReads,
} from './connectionStatus.js'
import { workspacePath } from './workspaceRouting.js'

const HEARTBEAT_MS = 10_000
const HEARTBEAT_TIMEOUT_MS = 4_000
const RESTORED_NOTICE_MS = 3_000

export default function ConnectionMonitor() {
  const connection = useSyncExternalStore(
    subscribeConnectionStatus,
    getConnectionSnapshot,
    getConnectionSnapshot,
  )
  const [checking, setChecking] = useState(false)
  const [showRestored, setShowRestored] = useState(false)
  const wasDisconnected = useRef(connection.disconnected)
  const inFlight = useRef(false)

  const checkNow = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    setChecking(true)
    const heartbeatUrl = workspacePath('/api/app/state')
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), HEARTBEAT_TIMEOUT_MS)
    try {
      const init = {
        cache: 'no-store',
        signal: controller.signal,
      }
      const [response] = await Promise.all([
        trackedFetch(heartbeatUrl, init),
        retryFailedReads(init, heartbeatUrl),
      ])
      if (![502, 503, 504].includes(response.status)) reportConnectionRestored('server')
    } catch (error) {
      if (error?.name === 'AbortError') {
        reportConnectionLost(connectionSourceFor(heartbeatUrl), 'The remote server is not responding.')
      }
    } finally {
      clearTimeout(timeout)
      inFlight.current = false
      setChecking(false)
    }
  }, [])

  useEffect(() => {
    let stopped = false
    let timer = null
    const tick = async () => {
      await checkNow()
      if (!stopped) timer = setTimeout(tick, HEARTBEAT_MS)
    }
    tick()
    const onOffline = () => reportConnectionLost('browser', 'This device is offline.')
    const onOnline = () => {
      reportConnectionRestored('browser')
      checkNow()
    }
    const onVisibility = () => { if (document.visibilityState === 'visible') checkNow() }
    if (navigator.onLine === false) onOffline()
    window.addEventListener('offline', onOffline)
    window.addEventListener('online', onOnline)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      stopped = true
      if (timer) clearTimeout(timer)
      window.removeEventListener('offline', onOffline)
      window.removeEventListener('online', onOnline)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [checkNow])

  useEffect(() => {
    let timer = null
    if (wasDisconnected.current && !connection.disconnected) {
      window.dispatchEvent(new Event('connection-restored'))
      setShowRestored(true)
      timer = setTimeout(() => setShowRestored(false), RESTORED_NOTICE_MS)
    } else if (connection.disconnected) {
      setShowRestored(false)
    }
    wasDisconnected.current = connection.disconnected
    return () => { if (timer) clearTimeout(timer) }
  }, [connection.disconnected])

  if (connection.disconnected) {
    const message = connection.issues[0]?.message || 'The remote server is unavailable.'
    return (
      <div className="connection-banner lost" role="alert" aria-live="assertive">
        <span className="connection-indicator" aria-hidden="true" />
        <span><strong>Connection lost.</strong> {message} Changes may not sync until it returns.</span>
        <button type="button" onClick={checkNow} disabled={checking}>
          {checking ? 'Checking…' : 'Retry now'}
        </button>
      </div>
    )
  }

  if (showRestored) {
    return (
      <div className="connection-banner restored" role="status" aria-live="polite">
        <span className="connection-indicator" aria-hidden="true" />
        <span><strong>Reconnected.</strong> The server connection is available again.</span>
      </div>
    )
  }

  return null
}
