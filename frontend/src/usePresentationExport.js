import { useEffect, useRef, useState } from 'react'
import * as api from './api.js'

export const exportRunning = (job) => ['running', 'cancelling'].includes(job?.status)

// Owned by the workspace, outside the presenter. HTTP reads never change the
// authoritative job state, and closing a view never sends a cancellation.
export default function usePresentationExport(projectId) {
  const [job, setJob] = useState(null)
  const [connectionError, setConnectionError] = useState('')
  const [dismissed, setDismissed] = useState(null)
  const [dock, setDock] = useState(null)
  const jobRef = useRef(null)
  jobRef.current = job
  useEffect(() => {
    jobRef.current = null
    setJob(null)
    setConnectionError('')
    setDismissed(null)
  }, [projectId])
  useEffect(() => {
    let disposed = false
    let timer
    async function poll() {
      try {
        const current = jobRef.current
        const next = exportRunning(current)
          ? await api.getRecordingExport(current.id)
          : (await api.getRecording()).export
        if (disposed) return
        setJob(next)
        setConnectionError('')
      } catch (error) {
        if (!disposed) setConnectionError(error.message)
      }
      if (!disposed) timer = setTimeout(poll, exportRunning(jobRef.current) ? 1000 : 5000)
    }
    poll()
    const resume = () => { clearTimeout(timer); poll() }
    window.addEventListener('online', resume)
    return () => { disposed = true; clearTimeout(timer); window.removeEventListener('online', resume) }
  }, [projectId, job?.id])

  async function cancel() {
    if (job?.status !== 'running') return
    try {
      setJob(await api.cancelRecordingExport(job.id))
      setConnectionError('')
    } catch (error) { setConnectionError(error.message) }
  }

  return { job, setJob, cancel, connectionError, dock, setDock, visible: job && job.id !== dismissed,
    dismiss: () => { if (!exportRunning(job)) setDismissed(job.id) } }
}
