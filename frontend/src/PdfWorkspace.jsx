import React, { useCallback, useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import TermPanel from './TermPanel.jsx'
import Presenter from './Presenter.jsx'
import PdfPreviewPane from './PdfPreviewPane.jsx'
import { createPdfPollController, createPdfRestoreResetLatch, nextPdfRenderState, pdfVersions } from './pdfWorkspace.js'
import { toast } from './Toaster.jsx'

function PdfFilesDrawer({ onClose, onRestored }) {
  const [files, setFiles] = useState([])
  const [versions, setVersions] = useState([])
  const [busy, setBusy] = useState(false)
  const reload = useCallback(async () => {
    const [fileResult, versionResult] = await Promise.all([
      api.listProjectFiles().catch(() => ({ items: [] })),
      api.gitVersions().catch(() => []),
    ])
    setFiles(fileResult.items || [])
    setVersions(pdfVersions(versionResult))
  }, [])
  useEffect(() => { reload() }, [reload])
  async function restore(version) {
    if (!window.confirm(`Restore “${version.message}”? This replaces the active PDF with that saved version. Unsaved transcript drafts will be discarded.`)) return
    setBusy(true)
    try {
      const result = await api.gitRestore(version.tag)
      if (result && result.ok) {
        await onRestored?.()
        await reload()
      } else {
        toast.error((result && result.error) || 'Could not restore version')
      }
    } catch (error) {
      toast.error(error.message || 'Could not restore version')
    } finally {
      setBusy(false)
    }
  }
  return (
    <aside className="pdf-drawer" aria-label="PDF files and versions">
      <div className="pdf-drawer-head"><strong>Files & versions</strong><button onClick={onClose}>✕</button></div>
      <div className="pdf-drawer-section"><span>FILES</span>
        {files.length ? files.map((item) => <div className="pdf-drawer-row" key={item.path}>{item.path}</div>) : <div className="empty">No project files.</div>}
      </div>
      <div className="pdf-drawer-section"><span>VERSIONS</span>
        {versions.length ? versions.map((version) => <div className="pdf-drawer-row" key={version.tag}>
          <span>{version.tag} · {version.message}</span>
          {version.is_current ? <em>current</em> : <button disabled={busy} onClick={() => restore(version)}>Restore</button>}
        </div>) : <div className="empty">No saved versions.</div>}
      </div>
    </aside>
  )
}

export default function PdfWorkspace({ project, onBack }) {
  const [render, setRender] = useState({ pages: [], tokens: {}, version: 0, generation: '', page: 1 })
  const [projectDir, setProjectDir] = useState(project?.path || '')
  const [slideMap, setSlideMap] = useState([])
  const [orphans, setOrphans] = useState([])
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [presenting, setPresenting] = useState(false)
  const [presentationLive, setPresentationLive] = useState(false)
  const [transcriptResetEpoch, setTranscriptResetEpoch] = useState(0)
  const channelRef = useRef(null)
  const presentationStateRef = useRef({ page: 1, pages: [], tokens: {}, pointer: null })
  const pointerRef = useRef(null)
  const lastPongRef = useRef(0)
  const initialLoadRef = useRef(true)
  const restoreResetLatchRef = useRef(null)
  if (!restoreResetLatchRef.current) {
    restoreResetLatchRef.current = createPdfRestoreResetLatch(
      () => setTranscriptResetEpoch((epoch) => epoch + 1)
    )
  }
  const pollerRef = useRef(null)
  if (!pollerRef.current) {
    pollerRef.current = createPdfPollController({
      loadRender: async () => {
        const result = initialLoadRef.current ? await api.getState() : await api.renderVersion()
        initialLoadRef.current = false
        if (result.project) setProjectDir(result.project)
        return result
      },
      loadMap: api.getSlideMap,
      onPair: (renderResult, mapResult) => {
        setRender((previous) => nextPdfRenderState(previous, renderResult))
        setSlideMap(mapResult.pages || [])
        setOrphans(mapResult.orphans || [])
        restoreResetLatchRef.current.consume()
      },
      // Keep the last successful transcript map and render visible during a replacement retry.
      onError: () => {},
    })
  }
  const poller = pollerRef.current
  const refreshAfterTranscriptSave = useCallback(() => poller.invalidateMapAfterSave(), [poller])
  const refreshAfterRestore = useCallback(async () => {
    restoreResetLatchRef.current.markPending()
    const refreshed = await poller.invalidateMapAfterSave()
    if (!refreshed) throw new Error('Could not refresh the restored PDF')
  }, [poller])

  useEffect(() => {
    let cancelled = false
    let timer = null
    const tick = async () => {
      await poller.poll()
      if (!cancelled) timer = setTimeout(tick, 5000)
    }
    tick()
    return () => { cancelled = true; if (timer) clearTimeout(timer) }
  }, [poller])

  useEffect(() => {
    presentationStateRef.current = { page: render.page, pages: render.pages, tokens: render.tokens, pointer: pointerRef.current }
  }, [render.page, render.pages, render.tokens])
  useEffect(() => {
    const channel = new BroadcastChannel('tcb-present')
    channelRef.current = channel
    channel.onmessage = (event) => {
      const message = event.data || {}
      if (message.hello) channel.postMessage(presentationStateRef.current)
      if (message.pong) lastPongRef.current = Date.now()
    }
    const heartbeat = setInterval(() => {
      channel.postMessage({ ping: true })
      setPresentationLive(Date.now() - lastPongRef.current < 4000)
    }, 1500)
    return () => { clearInterval(heartbeat); channel.close(); channelRef.current = null }
  }, [])
  useEffect(() => { channelRef.current?.postMessage(presentationStateRef.current) }, [render.page, render.pages, render.tokens])

  const setPage = useCallback((next) => {
    setRender((previous) => {
      const wanted = typeof next === 'function' ? next(previous.page) : next
      const total = previous.pages.length
      return { ...previous, page: total ? Math.max(1, Math.min(Number(wanted) || 1, total)) : 1 }
    })
  }, [])
  const sendPointer = useCallback((pointer) => {
    pointerRef.current = pointer || null
    presentationStateRef.current = { ...presentationStateRef.current, pointer: pointerRef.current }
    channelRef.current?.postMessage({ pointer: pointerRef.current })
  }, [])

  return (
    <div className="pdf-workspace">
      <header className="bar">
        <button className="back-btn" onClick={onBack}>← Projects</button>
        <strong className="project-name-chip">{project?.name || 'PDF project'}</strong>
        <span className="grow" />
        {presentationLive && <span className="status-chip live on"><span className="status-dot" />Projection live</span>}
        <button className="openbtn" onClick={() => setDrawerOpen(true)}>Files & versions</button>
        <button className="openbtn present" onClick={() => setPresenting(true)} disabled={!render.pages.length}>Present</button>
      </header>
      <main className="pdf-workspace-main">
        <section className="pdf-terminal-pane"><div className="pdf-terminal-head">Terminal</div><TermPanel initialCwd={projectDir} /></section>
        <PdfPreviewPane pages={render.pages} tokens={render.tokens} page={render.page} setPage={setPage} resetEpoch={transcriptResetEpoch}
          slideMap={slideMap} orphans={orphans} onTranscriptSaved={refreshAfterTranscriptSave} />
      </main>
      {drawerOpen && <PdfFilesDrawer onClose={() => setDrawerOpen(false)} onRestored={refreshAfterRestore} />}
      {presenting && <Presenter onClose={() => { setPresenting(false); poller.poll() }} onSaved={refreshAfterTranscriptSave}
        onPointer={sendPointer} page={render.page} setPage={setPage} pages={render.pages} tokens={render.tokens} />}
    </div>
  )
}
