import React, { useCallback, useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import TypstEditor from './TypstEditor.jsx'
import PreviewPane from './PreviewPane.jsx'
import CommentCard from './CommentCard.jsx'
import Presenter from './Presenter.jsx'
import FileManager from './FileManager.jsx'
import FilePicker from './FilePicker.jsx'
import TermPanel from './TermPanel.jsx'

const FILTERS = ['pending', 'done', 'all']
const selKey = (s) => (s.kind === 'page' ? `p${s.page_no}` : `${s.page}:${s.text}`)
const CLAUDE_MSG = "I've added new comments. Please fetch them via MCP and revise the deck according to the comments."

// keep full path if short, else "…/last/three/levels"
function shortPath(p, threshold = 5, keep = 3) {
  if (!p) return ''
  const segs = p.split('/').filter(Boolean)
  if (segs.length <= threshold) return p
  return '…/' + segs.slice(-keep).join('/')
}
const samePath = (a, b) => (a || '').replace(/\/+$/, '') === (b || '').replace(/\/+$/, '')

// clean chevron icons for collapse/expand (replace the chunky ◀ ▶ triangles)
const Chevron = ({ dir = 'right', size = 15 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" style={{ display: 'block' }}>
    <path d={dir === 'left' ? 'M15 6l-6 6 6 6' : 'M9 6l6 6-6 6'} />
  </svg>
)

// one shared terminal icon (toggle + terminal header), crisp at any size
const TerminalIcon = ({ size = 16 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ display: 'block' }}>
    <rect x="2" y="4" width="20" height="16" rx="2" />
    <path d="M6 9l3 3-3 3" />
    <path d="M13 15h4" />
  </svg>
)

// `rv` is a monotonic "the deck changed" tick — it drives non-image refreshes (reload the
// slide map, re-broadcast to the projection). It is NO LONGER the SVG cache-buster: each
// page's <img> URL now carries its own CONTENT token (a hash of that page's bytes, from the
// backend), so an unchanged page keeps the same URL (browser cache hit, no refetch) and only
// a page that actually changed is re-fetched. Content tokens also can't collide across
// projects/reloads — which is what the old per-project version counter did (the "stale
// a → aa → aaa" bug). Seeded from Date.now() purely so the tick is unique across remounts.
let _rvSeq = Date.now()
const nextRv = () => (_rvSeq += 1)

export default function App({ onBackToProjects }) {
  const [meta, setMeta] = useState({ project: '', project_name: '', mode: 'local', file: '', main: '', room: '', store: '' })
  const [pages, setPages] = useState([])
  const [tokens, setTokens] = useState({}) // per-page content token {name: hash} → SVG URL cache-buster
  const [ppi, setPpi] = useState(120)
  const [rv, setRv] = useState(nextRv) // monotonic "deck changed" tick (drives non-image refreshes)
  const [editorSyncSeq, setEditorSyncSeq] = useState(0) // remount editor on room rotation (fresh Yjs doc)
  const [compileError, setCompileError] = useState(null) // array of compile-error strings, or null
  const [locateMark, setLocateMark] = useState(null) // {page,x,y,key} reverse-locate marker
  const [pdfBusy, setPdfBusy] = useState(false)
  const [notesOn, setNotesOn] = useState(false) // show speaker notes beside each slide
  const [slideMap, setSlideMap] = useState([])  // per-page {section, note, note_raw, ...}
  const [noteOrphans, setNoteOrphans] = useState([])  // transcripts that render on no slide
  const [presenting, setPresenting] = useState(false)
  const [presentPage, setPresentPage] = useState(1) // current slide for presenter/projection (persists)
  const [presentationLive, setPresentationLive] = useState(false) // a projection window is open + answering
  // A single "edit session" for ONE comment card: its body text AND its anchors are edited
  // together. While a card is being edited, slide/editor "+ add" routes INTO it (not the
  // new-comment composer) — edit is edit, new is new.
  const [editingCardId, setEditingCardId] = useState(null)
  const [editBody, setEditBody] = useState('')
  const [editSels, setEditSels] = useState([])
  const presentChRef = useRef(null)
  const presentStateRef = useRef({ page: 1, pages: [], tokens: {}, pointer: null })
  const presentPointerRef = useRef(null)
  const lastPongRef = useRef(0)
  const [comments, setComments] = useState([])
  const [filter, setFilter] = useState('pending')

  const [selections, setSelections] = useState([])
  const [editorSel, setEditorSel] = useState(null)
  const [body, setBody] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [leftW, setLeftW] = useState(460)
  const [rightW, setRightW] = useState(380)
  const [leftCollapsed, setLeftCollapsed] = useState(false)
  const [rightCollapsed, setRightCollapsed] = useState(false)
  const [fileMgrOpen, setFileMgrOpen] = useState(false)
  const [termH, setTermH] = useState(220)
  const [termOpen, setTermOpen] = useState(false)
  // mount the terminal once (first open) and keep it alive thereafter. Closing only HIDES
  // it (display:none) so the shell session, scrollback, and any running command survive a
  // hide/show cycle. Remounting would fork a fresh shell and wipe the session.
  const [termMounted, setTermMounted] = useState(false)
  const [termInfo, setTermInfo] = useState({ cwd: null, claude: false, codex: false, agent: false })
  const editorRef = useRef(null)
  const termRef = useRef(null)
  const projectDir = meta.project || ''

  function beep() {
    try {
      const a = new (window.AudioContext || window.webkitAudioContext)()
      const o = a.createOscillator(); const g = a.createGain()
      o.connect(g); g.connect(a.destination)
      o.frequency.value = 660; g.gain.value = 0.05
      o.start(); o.stop(a.currentTime + 0.12)
    } catch {}
  }

  async function onOpened(r) {
    setFileMgrOpen(false)
    if (!r || !r.file) return
    setMeta((m) => ({ ...m, file: r.file, project: r.project, project_name: r.project_name || m.project_name, mode: r.mode || m.mode, main: r.main, room: r.room, store: r.store }))
    setPages(r.pages || []); setTokens(r.tokens || {}); setRv(nextRv())
    setSelections([]); setEditorSel(null)
    setMsg('✓ ' + (r.main || 'opened'))
    loadComments()
    // auto-setup workdir silently — both local & server, so agent CLIs in the project dir
    // always find the vibe-typst MCP
    if (!r.workdir_ready) {
      try { await api.setupWorkdir() } catch {}
    }
  }

  function cdTerminal() {
    if (termRef.current && projectDir) termRef.current.runCommand(`cd ${JSON.stringify(projectDir)}`)
  }
  function tellAgent() {
    // type the prompt AND submit it (real Enter), so the active agent starts on it immediately
    if (termRef.current) termRef.current.runInAgent(CLAUDE_MSG)
  }

  // poll the live terminal state (cwd + whether an agent is running) while it's open
  useEffect(() => {
    if (!termOpen || leftCollapsed) return
    // panel was just un-hidden (terminal opened, OR the left pane expanded): xterm was 0x0
    // while display:none, so re-measure it. The terminal stays MOUNTED across collapse, so
    // this only re-fits — it never reconnects the shell.
    const r = requestAnimationFrame(() => termRef.current && termRef.current.refit && termRef.current.refit())
    let on = true
    const tick = async () => { try { const i = await api.terminalInfo(); if (on) setTermInfo(i) } catch {} }
    tick()
    const t = setInterval(tick, 1500)
    return () => { on = false; clearInterval(t); cancelAnimationFrame(r) }
  }, [termOpen, leftCollapsed])

  function startTermDrag(e) {
    e.preventDefault()
    const onMove = (ev) => setTermH(Math.max(80, Math.min(window.innerHeight - ev.clientY - 40, window.innerHeight - 180)))
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    document.body.style.cursor = 'row-resize'
  }

  function startDrag(e) {
    e.preventDefault()
    const onMove = (ev) => {
      // stop before squeezing the preview (<380) or pushing the right pane off-screen
      const max = window.innerWidth - rightW - 380 - 14
      setLeftW(Math.max(220, Math.min(ev.clientX, max)))
    }
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    document.body.style.cursor = 'col-resize'
  }

  function startRightDrag(e) {
    e.preventDefault()
    const onMove = (ev) => {
      // keep the middle (slide preview) at least ~380px wide
      const maxRight = window.innerWidth - (leftCollapsed ? 26 : leftW) - 380 - 14
      setRightW(Math.max(280, Math.min(window.innerWidth - ev.clientX, maxRight)))
    }
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    document.body.style.cursor = 'col-resize'
  }

  const loadState = useCallback(async () => {
    const s = await api.getState()
    setMeta({ project: s.project, project_name: s.project_name || '', mode: s.mode || 'local', file: s.file, main: s.main, room: s.room, store: s.store })
    const externalEditSeq = Number.isFinite(s.external_edit_seq) ? s.external_edit_seq : 0
    lastRenderRef.current = { ...lastRenderRef.current, room: s.room || null, externalEditSeq }
    setPages(s.pages || [])
    setTokens(s.tokens || {})
    setPpi(s.ppi || 120)
  }, [])

  const loadComments = useCallback(async () => {
    setComments(await api.getComments())
  }, [])

  useEffect(() => { loadState(); loadComments() }, [loadState, loadComments])

  // fast poll for live preview re-render (resolver bumps the version ~instantly), and
  // for room rotation: if the backend rotated the CRDT room (corruption self-heal),
  // adopt the new name so the editor remounts with a FRESH doc (discards any poison).
  // NOTE: r.version is the RESOLVER's version, which RESETS to 1 on every project switch
  // (the resolver restarts). Comparing it alone misses a switch between two projects both at
  // v1 — the preview would keep showing the PREVIOUS project's render ("historical data").
  // So we refresh whenever the ROOM changes (project switched) OR the version changes, and we
  // bump `rv` MONOTONICALLY so the per-page SVG URL (?v=rv) can never collide across projects.
  const lastRenderRef = useRef({ room: null, version: -1, externalEditSeq: null })
  useEffect(() => {
    const t = setInterval(async () => {
      try {
        const r = await api.renderVersion()
        const last = lastRenderRef.current
        const roomChanged = !!r.room && r.room !== last.room
        const externalEditSeq = Number.isFinite(r.external_edit_seq) ? r.external_edit_seq : 0
        if (roomChanged || r.version !== last.version) {
          lastRenderRef.current = { room: r.room || last.room, version: r.version, externalEditSeq }
          setPages(r.pages || [])
          setTokens(r.tokens || {})  // per-page content tokens → only changed pages get a new URL
          setRv(nextRv())
          setMsg(`✓ ${(r.pages || []).length} pages`)  // deck loaded / auto-render finished
        } else if (externalEditSeq !== last.externalEditSeq) {
          lastRenderRef.current = { ...last, externalEditSeq }
        }
        // Remount the editor ONLY on room rotation (corruption self-heal / project switch),
        // which needs a fresh Yjs doc. We DON'T remount on external (MCP) edits any more:
        // now that MCP edits go through the SAME shared room as the browser, they already
        // broadcast into the live editor over the websocket. Remounting on every MCP edit
        // was the leftover from the split-brain-room era and caused the visible flash/flicker.
        if (roomChanged) setEditorSyncSeq((n) => n + 1)
        if (r.room) setMeta((m) => (r.room !== m.room ? { ...m, room: r.room } : m))
        setCompileError((cur) => {
          const next = r.error && r.error.length ? r.error : null
          if (JSON.stringify(next) !== JSON.stringify(cur) && next) setMsg(`✗ compile error (${next.length})`)
          return JSON.stringify(next) === JSON.stringify(cur) ? cur : next
        })
      } catch {}
    }, 300)
    return () => clearInterval(t)
  }, [])
  // slower poll for comment status (MCP-driven changes)
  useEffect(() => {
    const t = setInterval(loadComments, 2500)
    return () => clearInterval(t)
  }, [loadComments])

  async function doCompile() {
    setMsg('rendering…')
    const r = await api.compile()
    setPages(r.pages || []); setTokens(r.tokens || {}); setRv(nextRv())
    if (r.ok) {
      setCompileError(null)
      setMsg(`✓ ${r.pages.length} pages`)
    } else {
      const errs = r.errors && r.errors.length ? r.errors : [r.stderr || 'compile failed']
      setCompileError(errs)
      setMsg(`✗ compile error (${errs.length})`)
    }
  }

  const loadSlideMap = useCallback(async () => {
    try {
      const r = await api.getSlideMap()
      setSlideMap(r.pages || [])
      setNoteOrphans(r.orphans || [])
    } catch { setSlideMap([]); setNoteOrphans([]) }
  }, [])
  // load the per-slide map (page/slide/subslide labels + notes) whenever the deck re-renders,
  // so the page-number badges show slide/subslide info even when the notes column is hidden.
  useEffect(() => { loadSlideMap() }, [rv, loadSlideMap])

  // Broadcast the presentation state to the projection window CONTINUOUSLY — even when the
  // presenter view is closed — so editor edits reflect live on the projector and the current
  // page is remembered across exiting/re-entering Present. The App is always mounted.
  // The latest state lives in a ref so the channel handler (bound once) can always reply to a
  // new projection's "hello" with current data without re-binding on every state change.
  useEffect(() => {
    presentStateRef.current = { page: presentPage, pages, tokens, pointer: presentPointerRef.current }
  }, [presentPage, pages, tokens])
  useEffect(() => {
    const ch = new BroadcastChannel('tcb-present')
    presentChRef.current = ch
    ch.onmessage = (e) => {
      const d = e.data || {}
      if (d.hello) ch.postMessage(presentStateRef.current) // a new projection asks for current state
      if (d.pong) lastPongRef.current = Date.now()          // a projection answered → it's alive
    }
    // heartbeat: ping the projection ~every 1.5s; "live" = one answered within the last 4s
    const t = setInterval(() => {
      ch.postMessage({ ping: true })
      setPresentationLive(Date.now() - lastPongRef.current < 4000)
    }, 1500)
    return () => { clearInterval(t); ch.close(); presentChRef.current = null }
  }, [])
  // push fresh state to the projection whenever it changes
  useEffect(() => {
    const ch = presentChRef.current
    if (ch) ch.postMessage(presentStateRef.current)
  }, [presentPage, pages, tokens])

  // Pointer updates are transient presentation state, not React application state. Keeping
  // them in refs avoids re-rendering the editor for every mouse movement while still letting
  // a newly-opened projection receive the current pointer in its hello response.
  const sendPresentationPointer = useCallback((pointer) => {
    presentPointerRef.current = pointer || null
    presentStateRef.current = { ...presentStateRef.current, pointer: presentPointerRef.current }
    const ch = presentChRef.current
    if (ch) ch.postMessage({ pointer: presentPointerRef.current })
  }, [])

  async function exportPdf() {
    setPdfBusy(true); setMsg('compiling PDF…')
    try {
      const r = await fetch('/api/export-pdf', { method: 'POST' })
      if (!r.ok) {
        let detail = ''
        try { detail = (await r.json()).detail || '' } catch {}
        setMsg('✗ PDF export failed' + (detail ? `: ${String(detail).slice(0, 60)}` : ''))
        return
      }
      const blob = await r.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = (meta.main || 'deck').replace(/\.typ$/, '') + '.pdf'
      document.body.appendChild(a); a.click(); a.remove()
      URL.revokeObjectURL(url)
      setMsg('✓ PDF exported')
    } catch (e) {
      setMsg('✗ PDF export error')
    } finally { setPdfBusy(false) }
  }

  function addSelection(s) {
    // route new picks into the comment being edited, else into the new-comment composer
    if (editingCardId) {
      setEditSels((cur) => (cur.some((x) => selKey(x) === selKey(s)) ? cur : [...cur, s]))
      return
    }
    setSelections((cur) => (cur.some((x) => selKey(x) === selKey(s)) ? cur : [...cur, s]))
  }
  function removeSelection(k) {
    setSelections((cur) => cur.filter((x) => selKey(x) !== k))
  }

  // ── comment edit session (text + anchors, shared state) ─────────────────────
  function startEditComment(c) {
    setEditingCardId(c.id)
    setEditBody(c.body || '')
    setEditSels(Array.isArray(c.selection) ? c.selection : [])
  }
  function cancelEditComment() { setEditingCardId(null); setEditBody(''); setEditSels([]) }
  function removeEditSel(k) { setEditSels((cur) => cur.filter((s) => selKey(s) !== k)) }
  async function saveEditComment() {
    if (!editingCardId) return
    await api.patchComment(editingCardId, { body: editBody.trim(), selection: editSels })
    cancelEditComment()
    loadComments()
  }

  // pure resolve: page coordinate (pt) -> source range. No editor side effects (hover).
  async function resolveRaw(page_no, x, y) {
    setBusy(true)
    try {
      return await api.resolve(page_no, x, y)
    } finally { setBusy(false) }
  }

  // click an element -> jump the editor to its source (no selection change)
  function jumpToSource(s) {
    const [sl, sc] = s.start || []
    const [el, ec] = s.end || s.start || []
    skipNextLocate.current = true // slide-originated: highlight in the editor, don't bounce back
    editorRef.current && editorRef.current.highlight({ startLine: sl, startCol: sc, endLine: el, endCol: ec })
  }

  // "+ add" on an element -> capture its WHOLE source line as the anchor (a single word is
  // ambiguous). The word stays highlighted in the editor; the captured text is the full line.
  function addFromSource(s, page_no) {
    const [sl, sc] = s.start || []
    const [el, ec] = s.end || s.start || []
    skipNextLocate.current = true // slide-originated: highlight in the editor, don't bounce back
    const hit = editorRef.current && editorRef.current.highlight({ startLine: sl, startCol: sc, endLine: el, endCol: ec })
    if (!hit) return
    const text = (hit.lineText && hit.lineText.trim()) || hit.text
    // Carry the code-point span (from/to) so the backend can bind a drift-proof StickyIndex
    // anchor (→ the comment's live `location`). Without these the anchor is never built and
    // `location` is always null — the whole live-anchoring layer silently no-ops.
    if (text) addSelection({ kind: 'element', text, from: hit.from, to: hit.to, line: hit.line, page: page_no })
  }

  // click a page's number badge -> jump editor to that page's start
  async function jumpToPage(page_no) {
    setBusy(true)
    try {
      const r = await api.pageStart(page_no)
      if (!r.ok) { setMsg('could not locate page start'); return }
      const [sl, sc] = r.start || []
      editorRef.current && editorRef.current.highlight({ startLine: sl, startCol: sc })
    } finally { setBusy(false) }
  }

  // reverse-locate: editor caret moved -> find where that source renders and mark it in the
  // preview. Debounced so navigating the editor doesn't spam the resolver.
  const locateTimer = useRef(null)
  const skipNextLocate = useRef(false) // set by slide-originated jumps to avoid the round-trip
  function onEditorCursor(byteOff) {
    // A jump that STARTED in the slide (click/+add) moves the editor caret, which would
    // otherwise bounce a reverse-locate marker right back onto the slide we clicked. Skip it.
    if (skipNextLocate.current) { skipNextLocate.current = false; return }
    clearTimeout(locateTimer.current)
    locateTimer.current = setTimeout(async () => {
      try {
        const r = await api.locate(byteOff)
        if (r && r.ok && r.positions && r.positions.length) {
          const p = r.positions[0]
          setLocateMark({ page: p.page, x: p.x, y: p.y, key: Date.now() })
        } else {
          setLocateMark(null)
        }
      } catch { /* ignore */ }
    }, 220)
  }

  // click a stored selection chip on a comment -> jump the editor to it (if it still exists).
  // element chips jump by their captured text; page chips jump to their slide's opening line.
  function jumpToChip(s) {
    const ed = editorRef.current
    if (!ed || !s) return
    if (s.kind === 'page') {
      const line = s.slide && s.slide.slide_line
      if (line) ed.highlight({ startLine: line - 1, startCol: 0 }) // slide_line is 1-based
    } else if (s.text) {
      ed.jumpToText(s.text)
    }
  }

  function addEditorSelection() {
    if (editorSel && editorSel.text) {
      addSelection({ kind: 'element', text: editorSel.text, from: editorSel.from, to: editorSel.to, line: editorSel.line, page: null })
      setEditorSel(null)
    }
  }

  async function addComment() {
    if (!body.trim() || selections.length === 0) return
    const firstEl = selections.find((s) => s.kind === 'element')
    const firstPage = selections.find((s) => s.page != null || s.page_no != null)
    await api.addComments([{
      kind: selections.every((s) => s.kind === 'page') ? 'page' : 'element',
      page: firstPage ? (firstPage.page ?? firstPage.page_no) : null,
      anchor_text: firstEl ? firstEl.text : '',
      selections,
      body: body.trim(),
    }])
    setBody(''); setSelections([])
    loadComments()
  }

  const pendingCount = comments.filter((c) => c.status === 'pending').length
  const shown = comments.filter((c) => filter === 'all' || c.status === filter)
  const editingComment = editingCardId ? comments.find((c) => c.id === editingCardId) : null
  const presentationActive = presenting || presentationLive
  const livePage = Math.min(Math.max(presentPage || 1, 1), pages.length || 1)
  const shortFile = meta.file ? meta.file.split('/').slice(-2).join('/') : ''

  return (
    <div className="app">
      <header className="bar">
        {onBackToProjects && (
          <button className="back-btn" onClick={onBackToProjects} title="Back to projects">← Projects</button>
        )}
        {meta.project_name && (
          <div className="bar-title" title={meta.project_name}>{meta.project_name}</div>
        )}
        <button className="openbtn" onClick={() => setFileMgrOpen((v) => !v)} title="Manage project files">📁 Files</button>
        <button className="openbtn" onClick={exportPdf} disabled={pdfBusy} title="compile the current deck to PDF and download">{pdfBusy ? '⏳ exporting…' : '⬇ PDF'}</button>
        <button className="openbtn present" onClick={() => setPresenting(true)} title="presenter view (current + next slide, script, dual-screen)">▶ Present</button>
        <div className="actions">
          <span className={'status-chip live' + (presentationActive ? ' on' : '')}
            title={presentationActive ? 'a projection / presentation is open and live' : 'open a projection (Present → ⧉ Open projection) to control it from here'}>
            <span className="status-dot" />{presentationActive ? `live · ${livePage}` : 'no presentation'}
          </span>
          {msg && <span className={'status-chip msg' + (msg.startsWith('✗') ? ' err' : msg.startsWith('✓') ? ' ok' : '')}>{msg}</span>}
          <span className={'status-chip pending' + (pendingCount > 0 ? ' on' : '')}>
            <span className="status-dot" />{pendingCount} pending
          </span>
        </div>
      </header>

      <main className="grid">
        {leftCollapsed && (
          <button className="expand-tab" title="show source" onClick={() => setLeftCollapsed(false)}><Chevron dir="right" /></button>
        )}
        {/* Kept MOUNTED and hidden with CSS when collapsed (not unmounted): remounting this
            section would tear down and reconnect the terminal's WebSocket, refreshing the shell
            just from a collapse/expand toggle. */}
        <section className="pane source" style={{ width: leftW, ...(leftCollapsed ? { display: 'none' } : null) }}>
              <div className="pane-head">
                <FilePicker activeMain={meta.main} activeFile={meta.file} onOpen={onOpened} />
                <span className="grow" />
                <button className={'termtoggle' + (termOpen ? ' on' : '')} title="show/hide terminal" onClick={() => setTermOpen((v) => { if (!v) setTermMounted(true); return !v })}><TerminalIcon size={15} /></button>
                <button className="iconbtn" title="collapse" onClick={() => setLeftCollapsed(true)}><Chevron dir="left" /></button>
              </div>
              <div className="editor-area">
                {meta.room ? (
                  <TypstEditor key={`${meta.room}:${editorSyncSeq}`} room={meta.room} ref={editorRef} onSelect={setEditorSel} onCursor={onEditorCursor} onDocChange={() => setMsg('rendering…')} />
                ) : (
                  <div className="empty">connecting…</div>
                )}
              </div>
              {termMounted && (
                <>
                  <div className="hdivider" onMouseDown={startTermDrag} title="drag to resize terminal" style={{ display: termOpen ? 'block' : 'none' }} />
                  <div className="term-area" style={{ height: termH, display: termOpen ? 'flex' : 'none' }}>
                    <div className="term-head">
                      <span className="termpath" title={termInfo.cwd || ''}><TerminalIcon size={13} /> {shortPath(termInfo.cwd) || '~'}</span>
                      <span className="grow" />
                      {termInfo.cwd && projectDir && !samePath(termInfo.cwd, projectDir) && !termInfo.agent && (
                        <button className="cdbtn" onClick={cdTerminal} title={`cd ${projectDir}`}>cd to deck</button>
                      )}
                      {termInfo.agent && (
                        <button className="askbtn" onClick={tellAgent} title="ask the active agent to fetch & apply the new comments">✦ apply comments</button>
                      )}
                    </div>
                    <TermPanel ref={termRef} />
                  </div>
                </>
              )}
            </section>
            <div className="divider" onMouseDown={startDrag} title="drag to resize" style={leftCollapsed ? { display: 'none' } : undefined} />

        <PreviewPane
          pages={pages}
          tokens={tokens}
          presentPage={presentPage}
          presentationActive={presenting || presentationLive}
          onSetPresentPage={setPresentPage}
          busy={busy}
          compileError={compileError}
          locateMark={locateMark}
          notesOn={notesOn}
          setNotesOn={setNotesOn}
          slideMap={slideMap}
          noteOrphans={noteOrphans}
          onReloadNotes={loadSlideMap}
          onResolve={resolveRaw}
          onJump={jumpToSource}
          onAdd={addFromSource}
          onJumpPage={jumpToPage}
          onAddPage={(pn) => addSelection({ kind: 'page', page_no: pn })}
          onCompile={doCompile}
        />

        {rightCollapsed ? (
          <button className="expand-tab right" title="show comments" onClick={() => setRightCollapsed(false)}><Chevron dir="left" /></button>
        ) : (
          <>
        <div className="divider" onMouseDown={startRightDrag} title="drag to resize" />

        <section className="pane comments" style={{ width: rightW }}>
          <div className="pane-head">
            <span>Comments</span>
            <span className="grow" />
            <button className="iconbtn" title="collapse" onClick={() => setRightCollapsed(true)}><Chevron dir="right" /></button>
          </div>
          <div className="composer">
            {editingCardId && (
              <div className="sel-capture-banner">
                ✎ Editing comment{editingComment ? ` #${editingComment.seq}` : ''} — element picks &amp; text changes apply to <b>it</b>, not a new comment. <a onClick={cancelEditComment}>Cancel</a> to create new comments.
              </div>
            )}
            <div className="sel-list">
              {selections.length === 0 && (
                <div className="sel-empty">Click a slide to jump. Hover an element’s <b>+ add</b>, or a page badge’s <b>+ add page</b>, to select.</div>
              )}
              {selections.map((s) => (
                <span key={selKey(s)} className={'chip ' + s.kind}>
                  {s.kind === 'page'
                    ? `page ${s.page_no}`
                    : `${(s.text || '').slice(0, 26)}${(s.text || '').length > 26 ? '…' : ''}${s.line ? ` ·L${s.line}` : ''}`}
                  <a className="chip-x" onClick={() => removeSelection(selKey(s))}>✕</a>
                </span>
              ))}
            </div>
            {editorSel && editorSel.text && (
              <button className="ghost" onClick={addEditorSelection}>+ add editor selection ({editorSel.text.slice(0, 20)}…)</button>
            )}
            <textarea
              placeholder="Describe the change for the selected element(s)…"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') addComment() }}
            />
            <button className="primary wide" disabled={!!editingCardId || !body.trim() || selections.length === 0} onClick={addComment}>
              <span className="plus">+</span> {editingCardId ? 'Editing a comment…' : `Add comment (${selections.length})`}
            </button>
          </div>

          <div className="tabs">
            {FILTERS.map((f) => (
              <button key={f} className={filter === f ? 'tab on' : 'tab'} onClick={() => setFilter(f)}>{f}</button>
            ))}
          </div>

          <div className="clist">
            {shown.length === 0 && <div className="empty">No {filter} comments.</div>}
            {shown.map((c) => (
              <CommentCard key={c.id} c={c} onChange={loadComments} onJumpChip={jumpToChip}
                isEditing={editingCardId === c.id}
                editBody={editBody} editSels={editSels}
                onStartEdit={() => startEditComment(c)}
                onChangeBody={setEditBody}
                onRemoveSel={removeEditSel}
                onSaveEdit={saveEditComment}
                onCancelEdit={cancelEditComment} />
            ))}
          </div>
        </section>
          </>
        )}
      </main>

      {fileMgrOpen && <FileManager activeFile={meta.file} mainFile={meta.main} onOpenFile={onOpened} onClose={() => setFileMgrOpen(false)} onRoomChange={room => setMeta(m => ({ ...m, room }))} />}
      {presenting && <Presenter onClose={() => { setPresenting(false); loadSlideMap() }} onSaved={loadSlideMap} onPointer={sendPresentationPointer} page={presentPage} setPage={setPresentPage} pages={pages} tokens={tokens} />}
    </div>
  )
}
