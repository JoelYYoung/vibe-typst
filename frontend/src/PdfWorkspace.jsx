import React, { useCallback, useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import TermPanel from './TermPanel.jsx'
import Presenter from './Presenter.jsx'
import PdfPreviewPane from './PdfPreviewPane.jsx'
import { nextPdfRenderState, pdfVersions } from './pdfWorkspace.js'

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
    if (!window.confirm(`Restore “${version.message}”? This replaces the active PDF with that saved version.`)) return
    setBusy(true)
    try {
      const result = await api.gitRestore(version.tag)
      if (result && result.ok) {
        await reload()
        onRestored && onRestored()
      }
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
  const [render, setRender] = useState({ pages: [], tokens: {}, version: 0, page: 1 })
  const [projectDir, setProjectDir] = useState(project?.path || '')
  const [slideMap, setSlideMap] = useState([])
  const [orphans, setOrphans] = useState([])
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [presenting, setPresenting] = useState(false)
  const [presentationLive, setPresentationLive] = useState(false)
  const channelRef = useRef(null)
  const presentationStateRef = useRef({ page: 1, pages: [], tokens: {}, pointer: null })
  const pointerRef = useRef(null)
  const lastPongRef = useRef(0)

  const loadSlideMap = useCallback(async () => {
    try {
      const result = await api.getSlideMap()
      setSlideMap(result.pages || [])
      setOrphans(result.orphans || [])
    } catch {
      setSlideMap([])
      setOrphans([])
    }
  }, [])

  const syncRender = useCallback(async (initial = false) => {
    try {
      const result = initial ? await api.getState() : await api.renderVersion()
      if (initial && result.project) setProjectDir(result.project)
      setRender((previous) => {
        return nextPdfRenderState(previous, result)
      })
      // Transcript edits and PDF replacement can both happen outside this component. Fetch the
      // authoritative map on each render poll; PdfPreviewPane preserves a genuinely dirty draft.
      loadSlideMap()
    } catch {
      // A replacement can briefly leave the render unavailable; the next poll retries.
    }
  }, [loadSlideMap])

  useEffect(() => {
    syncRender(true)
    const timer = setInterval(() => syncRender(false), 1500)
    return () => clearInterval(timer)
  }, [syncRender])

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
        <PdfPreviewPane pages={render.pages} tokens={render.tokens} page={render.page} setPage={setPage}
          slideMap={slideMap} orphans={orphans} onTranscriptSaved={loadSlideMap} />
      </main>
      {drawerOpen && <PdfFilesDrawer onClose={() => setDrawerOpen(false)} onRestored={() => syncRender(true)} />}
      {presenting && <Presenter onClose={() => { setPresenting(false); loadSlideMap() }} onSaved={loadSlideMap}
        onPointer={sendPointer} page={render.page} setPage={setPage} pages={render.pages} tokens={render.tokens} />}
    </div>
  )
}
