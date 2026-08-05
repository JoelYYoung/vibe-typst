import React, { useCallback, useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import TermPanel from './TermPanel.jsx'
import Presenter from './Presenter.jsx'
import PdfPreviewPane from './PdfPreviewPane.jsx'
import {
  clampPdfPage,
  clampPdfTerminalWidth,
  createPdfPollController,
  nextPdfRenderState,
  reconcilePdfPageCursors,
  startPdfPresentationPage,
} from './pdfWorkspace.js'
import { shortPath, TerminalIcon } from './terminalUi.jsx'
import { workspaceChannelName } from './workspaceRouting.js'

export default function PdfWorkspace({ project, onBack }) {
  const [render, setRender] = useState({
    pages: [], tokens: {}, version: 0, generation: '', slideMap: [], orphans: [],
  })
  const [previewPage, setPreviewPage] = useState(1)
  const [presentPage, setPresentPage] = useState(1)
  const [projectDir, setProjectDir] = useState(project?.path || '')
  const [presenting, setPresenting] = useState(false)
  const [presentationLive, setPresentationLive] = useState(false)
  const [terminalWidth, setTerminalWidth] = useState(null)
  const mainRef = useRef(null)
  const dividerCleanupRef = useRef(null)
  const channelRef = useRef(null)
  const presentationStateRef = useRef({ page: 1, pages: [], tokens: {}, pointer: null })
  const pointerRef = useRef(null)
  const lastPongRef = useRef(0)
  const initialLoadRef = useRef(true)
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
        setRender((previous) => nextPdfRenderState(previous, renderResult, mapResult))
      },
      // Keep the last successful transcript map and render visible during a replacement retry.
      onError: () => {},
    })
  }
  const poller = pollerRef.current
  const refreshAfterTranscriptSave = useCallback(() => poller.invalidateMapAfterSave(), [poller])

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
    presentationStateRef.current = {
      page: presentPage,
      pages: render.pages,
      tokens: render.tokens,
      pointer: pointerRef.current,
    }
  }, [presentPage, render.pages, render.tokens])
  useEffect(() => {
    const channel = new BroadcastChannel(workspaceChannelName('tcb-present'))
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
  useEffect(() => {
    channelRef.current?.postMessage(presentationStateRef.current)
  }, [presentPage, render.pages, render.tokens])

  useEffect(() => {
    const next = reconcilePdfPageCursors(
      { previewPage, presentPage },
      render.pages.length,
    )
    if (next.previewPage !== previewPage) setPreviewPage(next.previewPage)
    if (next.presentPage !== presentPage) setPresentPage(next.presentPage)
  }, [previewPage, presentPage, render.pages.length])

  useEffect(() => () => dividerCleanupRef.current?.(), [])

  const setPreview = useCallback((next) => {
    setPreviewPage((previous) => {
      const wanted = typeof next === 'function' ? next(previous) : next
      return clampPdfPage(Number(wanted), render.pages.length)
    })
  }, [render.pages.length])

  const setPresentation = useCallback((next) => {
    setPresentPage((previous) => {
      const wanted = typeof next === 'function' ? next(previous) : next
      return clampPdfPage(Number(wanted), render.pages.length)
    })
  }, [render.pages.length])

  const presentationActive = presenting || presentationLive

  const openPresenter = useCallback(() => {
    setPresentPage((current) => startPdfPresentationPage(
      previewPage,
      current,
      presentationActive,
      render.pages.length,
    ))
    setPresenting(true)
  }, [presentationActive, previewPage, render.pages.length])

  const startDividerDrag = useCallback((event) => {
    if (!mainRef.current) return
    event.preventDefault()
    dividerCleanupRef.current?.()
    const bounds = mainRef.current.getBoundingClientRect()
    const move = (moveEvent) => {
      setTerminalWidth(clampPdfTerminalWidth(
        moveEvent.clientX,
        bounds.left,
        bounds.width,
      ))
    }
    const stop = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', stop)
      window.removeEventListener('pointercancel', stop)
      dividerCleanupRef.current = null
    }
    dividerCleanupRef.current = stop
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', stop)
    window.addEventListener('pointercancel', stop)
  }, [])

  const sendPointer = useCallback((pointer) => {
    pointerRef.current = pointer || null
    presentationStateRef.current = { ...presentationStateRef.current, pointer: pointerRef.current }
    channelRef.current?.postMessage({ pointer: pointerRef.current })
  }, [])

  return (
    <div className="pdf-workspace">
      <header className="bar">
        <button className="back-btn" onClick={onBack} title="Back to projects">← Projects</button>
        <div className="bar-title" title={project?.name || 'PDF project'}>
          {project?.name || 'PDF project'}
        </div>
        <button className="openbtn present" onClick={openPresenter} disabled={!render.pages.length}
          title="presenter view (current + next page, transcript, dual-screen)">▶ Present</button>
        <div className="actions">
          <span className={'status-chip live' + (presentationActive ? ' on' : '')}
            title={presentationActive
              ? 'a projection / presentation is open and live'
              : 'open a projection from Present to control it from here'}>
            <span className="status-dot" />
            {presentationActive ? `live · ${presentPage}` : 'no presentation'}
          </span>
        </div>
      </header>
      <main className="pdf-workspace-main" ref={mainRef}>
        <section
          className="pdf-terminal-pane"
          style={{ width: terminalWidth == null ? '38%' : terminalWidth }}
        >
          <div className="term-head">
            <span className="termpath" title={projectDir}>
              <TerminalIcon size={13} />
              {shortPath(projectDir) || '~'}
            </span>
          </div>
          <TermPanel />
        </section>
        <div
          className="pdf-workspace-divider"
          role="separator"
          aria-label="Resize terminal and PDF preview"
          aria-orientation="vertical"
          title="drag to resize terminal"
          onPointerDown={startDividerDrag}
        />
        <PdfPreviewPane
          pages={render.pages}
          tokens={render.tokens}
          page={previewPage}
          setPage={setPreview}
          slideMap={render.slideMap}
          orphans={render.orphans}
          presentPage={presentPage}
          presentationActive={presentationActive}
          onFollowPresentation={() => setPreview(presentPage)}
          onSendPreview={() => setPresentation(previewPage)}
          onTranscriptSaved={refreshAfterTranscriptSave}
        />
      </main>
      {presenting && <Presenter onClose={() => { setPresenting(false); poller.poll() }} onSaved={refreshAfterTranscriptSave}
        onPointer={sendPointer} page={presentPage} setPage={setPresentation} pages={render.pages} tokens={render.tokens}
        slideMap={render.slideMap} generation={render.generation} />}
    </div>
  )
}
