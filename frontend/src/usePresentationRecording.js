import { useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import { recordingPageState, startSlideRecording } from './slideRecorder.js'

export default function usePresentationRecording({ enabled, page, pages, tokens, renderVersion }) {
  const [takes, setTakes] = useState({})
  const [status, setStatus] = useState('idle')
  const [elapsed, setElapsed] = useState(0)
  const [audioLevel, setAudioLevel] = useState(null)
  const [error, setError] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [snapshot, setSnapshot] = useState(null)
  const [exportAvailable, setExportAvailable] = useState(false)
  const [job, setJob] = useState(null)
  const [preview, setPreview] = useState(false)
  const [pending, setPending] = useState(null)
  const session = useRef(null)
  const phase = useRef('idle')
  const mounted = useRef(true)
  const request = useRef(null)
  const activePage = useRef(null)
  const activeSlide = useRef(null)
  const stopRef = useRef(null)
  const loadSequence = useRef(0)
  const deckKey = JSON.stringify(pages.map((name) => [name, tokens[name]]))
  const changeStatus = (value) => { phase.current = value; if (mounted.current) setStatus(value) }
  const busy = ['starting', 'recording', 'saving', 'clearing'].includes(status)

  async function load() {
    const sequence = ++loadSequence.current
    setError('')
    try {
      const result = await api.prepareRecording()
      if (!mounted.current || sequence !== loadSequence.current) return
      setTakes(Object.fromEntries(result.takes.map((take) => [take.page, take])))
      setExportAvailable(result.export_available)
      setJob(result.export)
      setSnapshot({ key: deckKey, slides: result.slides })
      setLoaded(true)
    } catch (error) { if (mounted.current && sequence === loadSequence.current) setError(error.message) }
  }

  useEffect(() => { if (enabled) load() }, [enabled, deckKey, renderVersion])
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      request.current?.abort()
      session.current?.stop().catch(() => {})
    }
  }, [])
  useEffect(() => {
    if (status !== 'recording') { setAudioLevel(null); return }
    const interval = setInterval(() => {
      setElapsed(session.current?.elapsed() || 0)
      setAudioLevel(session.current?.audioLevel() ?? null)
    }, 100)
    return () => clearInterval(interval)
  }, [status])
  useEffect(() => {
    if (!busy && !pending) return
    const beforeUnload = (event) => { event.preventDefault(); event.returnValue = '' }
    const hidden = () => {
      if (document.hidden && phase.current === 'recording') {
        setError('Recording stopped because this tab was hidden. Review the take before exporting.')
        stopRef.current?.()
      }
    }
    window.addEventListener('beforeunload', beforeUnload)
    document.addEventListener('visibilitychange', hidden)
    return () => {
      window.removeEventListener('beforeunload', beforeUnload)
      document.removeEventListener('visibilitychange', hidden)
    }
  }, [busy, pending])
  useEffect(() => {
    setPreview(false)
    if (phase.current === 'recording' && (page !== activePage.current
      || pages[page - 1] !== activeSlide.current?.name || tokens[pages[page - 1]] !== activeSlide.current?.previewToken)) {
      setError('The slide changed during recording. This take was stopped; re-record the updated page.')
      stopRef.current?.()
    }
  }, [page, pages[page - 1], tokens[pages[page - 1]]])
  useEffect(() => {
    if (phase.current === 'recording' && snapshot?.slides[page - 1]?.recording_id !== activeSlide.current?.recording_id) {
      setError('This slide moved during recording. Review the take before saving it to the current page.')
      stopRef.current?.()
    }
  }, [snapshot])
  useEffect(() => {
    if (job?.status !== 'running') return
    let cancelled = false
    let timeout
    async function poll() {
      try {
        const next = await api.getRecordingExport(job.id)
        if (cancelled) return
        setJob(next)
        if (next.status === 'running') timeout = setTimeout(poll, 1000)
      } catch (error) {
        if (cancelled) return
        setJob((current) => ({ ...current, status: 'failed', error: error.message }))
      }
    }
    timeout = setTimeout(poll, 1000)
    return () => { cancelled = true; clearTimeout(timeout) }
  }, [job?.id, job?.status])

  async function start() {
    if (phase.current !== 'idle' || pending) return
    changeStatus('starting')
    setPreview(false)
    setError('')
    setElapsed(0)
    request.current = new AbortController()
    activePage.current = page
    activeSlide.current = { name: pages[page - 1], token: snapshot?.slides[page - 1]?.token,
      recording_id: snapshot?.slides[page - 1]?.recording_id,
      previewToken: tokens[pages[page - 1]] }
    try {
      const take = await startSlideRecording({
        ...activeSlide.current, url: api.renderUrl(activeSlide.current.name, activeSlide.current.previewToken),
        signal: request.current.signal,
        onInterrupted: (message) => {
          if (!mounted.current) return
          setError(message)
          stopRef.current?.()
        },
      })
      if (!mounted.current) { take.stop().catch(() => {}); return }
      session.current = take
      changeStatus('recording')
    } catch (error) {
      if (mounted.current) { setError(error.message); changeStatus('idle') }
    }
  }

  async function upload(take) {
    changeStatus('saving')
    try {
      const saved = await api.saveRecordingPage(take.page, take.video, take.metadata, request.current?.signal)
      if (!mounted.current) return
      setTakes((previous) => ({ ...previous, [take.page]: saved }))
      setPending(null)
      // A previous export no longer describes all of the current takes.
      setJob((previous) => previous?.status === 'running' ? previous : null)
    } catch (error) {
      if (mounted.current) { setPending(take); setError(`${error.message} Your take is kept here; retry saving or download a backup.`) }
    } finally { if (mounted.current) changeStatus('idle') }
  }

  async function stop() {
    if (phase.current !== 'recording') return
    changeStatus('saving')
    try {
      const take = await session.current.stop()
      session.current = null
      if (mounted.current) await upload({ ...take, page: activePage.current })
    } catch (error) { if (mounted.current) { setError(error.message); changeStatus('idle') } }
  }
  stopRef.current = stop

  async function clearPage() {
    if (phase.current !== 'idle' || pending) return
    changeStatus('clearing')
    setError('')
    const clearedPage = page
    try {
      await api.clearRecordingPage(clearedPage, takes[clearedPage]?.take)
      if (!mounted.current) return
      setPreview(false)
      setTakes((previous) => {
        const next = { ...previous }
        delete next[clearedPage]
        return next
      })
      setJob((previous) => previous?.status === 'running' ? previous : null)
    } catch (error) { if (mounted.current) setError(error.message) }
    finally { if (mounted.current) changeStatus('idle') }
  }

  async function exportVideo() {
    if (phase.current !== 'idle' || pending || job?.status === 'running') return
    changeStatus('exporting')
    setError('')
    setPreview(false)
    try {
      const next = await api.startRecordingExport()
      if (mounted.current) setJob(next)
    } catch (error) { if (mounted.current) setError(error.message) }
    finally { if (mounted.current) changeStatus('idle') }
  }

  // PDF preview cache tokens identify an entire render generation, rather than page bytes.
  // Recording uses independently hashed pages returned by the server for both deck types.
  const pageStates = pages.map((name, index) => recordingPageState(takes[index + 1], name,
    snapshot?.key === deckKey ? snapshot.slides[index]?.token : undefined))
  const completed = pageStates.filter((value) => value === 'recorded').length
  const exportMatches = completed === pages.length && job?.takes?.length === pages.length
    && job.takes.every((take, index) => takes[index + 1]?.take === take)
  return {
    takes, status, elapsed, audioLevel, error, loaded: loaded && snapshot?.key === deckKey, exportAvailable, job, preview, pending, busy,
    locked: busy || !!pending,
    pageStates, completed,
    start, stop, clearPage, exportVideo, load,
    setPreview, setError,
    retry: () => { setError(''); upload(pending) },
    discard: () => { setPending(null); setError('') },
    movePointer: (point) => session.current?.setPointer(point),
    previewUrl: takes[page] ? api.recordingPageUrl(page, takes[page].take) : null,
    exportUrl: job?.status === 'complete' && exportMatches ? api.recordingExportUrl(job.id) : null,
  }
}
