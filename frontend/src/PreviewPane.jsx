import React, { useState, useRef, useCallback, useEffect, useLayoutEffect } from 'react'
import PageSvg from './PageSvg.jsx'
import * as api from './api.js'
import { toast } from './Toaster.jsx'

// One slide's speaker note, shown beside the slide and editable in place. Edits route through
// the shared doc (replace the note content anchored on its exact source text).
function NoteInline({ info, onSaved }) {
  const [draft, setDraft] = useState(info.note || '')
  const [saving, setSaving] = useState(false)
  const [base, setBase] = useState(info.note || '')
  const justSaved = useRef(false)
  // Keep in sync when parent gives us a new note (e.g. after reload), but skip if we just
  // saved — prevents the stale-prop flash before the background reload finishes.
  if ((info.note || '') !== base && !justSaved.current) {
    setBase(info.note || ''); setDraft(info.note || '')
  }
  // Editable if we can place a new note (slide_line) OR one already exists (note_raw).
  const canSave = !!(info.slide_line || info.note_raw)
  const dirty = draft !== base

  async function save() {
    if (!dirty || !canSave) return
    setSaving(true)
    const savedText = draft
    try {
      // Inline #speaker-note in the .typ source (anchored), so it's the single source of
      // truth shared by the editor, the presenter, the version system, and Claude.
      const r = await api.saveNote(info, savedText)
      if (r && r.ok) {
        justSaved.current = true
        setBase(savedText)  // clear dirty immediately; background reload will confirm
        if (r.warning) toast.info(r.warning, 6000)
        onSaved && onSaved()
      } else if (r && r.error) {
        toast.error(r.error)
      }
    } finally { setSaving(false) }
  }

  if (!canSave) {
    return (
      <div className="note-inline">
        <div className="ni-label">TRANSCRIPT</div>
        <div className="ni-unavail">Speaker notes require a touying deck (use #slide[…]).</div>
      </div>
    )
  }

  return (
    <div className="note-inline">
      <div className="ni-label">TRANSCRIPT{info.sub_total > 1 ? ` · sub ${info.sub_index}/${info.sub_total}` : ''}</div>
      <textarea className={'note-input' + (info.note ? '' : ' empty')} value={draft}
        placeholder="Write a transcript for this slide…"
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') save() }} />
      {(dirty || !info.note_raw) && (
        <div className="note-actions">
          <button className="mini primary" disabled={saving || !dirty} onClick={save}>
            {info.note_raw ? 'save' : 'add note'}
          </button>
          {dirty && <button className="mini" onClick={() => setDraft(base)}>revert</button>}
          {dirty && <span className="edit-hint">⌘↵ save</span>}
        </div>
      )}
    </div>
  )
}

// Slides as inline SVG. Element-level hover/select lives in PageSvg; here we add the page
// number badge. A "Notes" toggle reveals each slide's speaker note beside it (they scroll
// together), with a one-click export of the whole script.
export default function PreviewPane({ pages, tokens, presentPage, presentationActive, onSetPresentPage, busy, compileError, locateMark, notesOn, setNotesOn, slideMap, noteOrphans, onReloadNotes, onResolve, onJump, onAdd, onJumpPage, onAddPage, onCompile }) {
  const hasErr = Array.isArray(compileError) && compileError.length > 0
  const pageListRef = useRef(null)
  const anchorRef = useRef(null)
  // the slide currently centered in the scroll viewport (1-based, 0 = none). Updated live as
  // the user scrolls / resizes, and used by the two presentation-sync buttons below.
  const [centerPage, setCenterPage] = useState(0)
  const rafRef = useRef(0)

  const computeCenterPage = useCallback(() => {
    const list = pageListRef.current
    if (!list) return
    const figs = list.querySelectorAll('figure.page')
    if (!figs.length) { setCenterPage(0); return }
    const mid = list.scrollTop + list.clientHeight / 2
    let best = 1, bestDist = Infinity
    figs.forEach((fig, i) => {
      const figMid = fig.offsetTop + fig.offsetHeight / 2
      const d = Math.abs(figMid - mid)
      if (d < bestDist) { bestDist = d; best = i + 1 }
    })
    setCenterPage(best)
  }, [])

  const onListScroll = useCallback(() => {
    if (rafRef.current) return
    rafRef.current = requestAnimationFrame(() => { rafRef.current = 0; computeCenterPage() })
  }, [computeCenterPage])

  // recompute when the page set changes (after the new figures lay out)
  useLayoutEffect(() => { computeCenterPage() }, [pages, computeCenterPage])
  // recompute on width/height changes — slide heights track pane width, so the centered slide
  // shifts even without a scroll event.
  useEffect(() => {
    const list = pageListRef.current
    if (!list || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => computeCenterPage())
    ro.observe(list)
    return () => ro.disconnect()
  }, [computeCenterPage])

  // scroll the list so a given 1-based page sits centered (Pin to Presentation)
  const scrollToPage = useCallback((pn) => {
    const list = pageListRef.current
    if (!list || !pn) return
    const fig = list.querySelectorAll('figure.page')[pn - 1]
    if (!fig) return
    list.scrollTo({ top: fig.offsetTop - (list.clientHeight - fig.offsetHeight) / 2, behavior: 'smooth' })
  }, [])

  const total = pages ? pages.length : 0
  const canControl = presentationActive && total > 0
  const livePage = Math.min(Math.max(presentPage || 1, 1), total || 1)

  const handleNotesToggle = useCallback((val) => {
    const list = pageListRef.current
    if (list) {
      const mid = list.scrollTop + list.clientHeight / 2
      let best = null, bestDist = Infinity
      for (const fig of list.querySelectorAll('figure.page')) {
        const figMid = fig.offsetTop + fig.offsetHeight / 2
        const dist = Math.abs(figMid - mid)
        if (dist < bestDist) { bestDist = dist; best = fig }
      }
      if (best) {
        anchorRef.current = { el: best, offset: best.offsetTop + best.offsetHeight / 2 - mid }
      }
    }
    setNotesOn(val)
  }, [setNotesOn])

  useLayoutEffect(() => {
    const anchor = anchorRef.current
    if (!anchor) return
    anchorRef.current = null
    const list = pageListRef.current
    if (!list) return
    const figMid = anchor.el.offsetTop + anchor.el.offsetHeight / 2
    list.scrollTop = figMid - list.clientHeight / 2 - anchor.offset
  }, [notesOn])

  return (
    <section className="pane preview">
      <div className="pane-head">
        <span>Preview</span>
        {total > 0 && <span className="pb-cur" title="the slide currently centered in your view">▸ {centerPage || '—'} / {total}</span>}
        {hasErr && <span className="compile-badge" title="the source does not compile — preview is stale">⚠ compile error</span>}
        <div className="pane-head-actions">
          <a className="export-md" href={api.notesExportUrl} download title="download the per-slide narration (transcript) as plain text (TTS-ready)">⬇ Trans</a>
          <a className="export-md" href={api.notesPdfpcUrl} download title="download the .pdfpc file (per-page speaker notes) for the pdfpc presenter">⬇ .pdfpc</a>
          <label className="switch" title="show each slide's transcript beside it">
            <input type="checkbox" checked={notesOn} onChange={(e) => handleNotesToggle(e.target.checked)} />
            <span className="switch-track"><span className="switch-knob" /></span>
            <span className="switch-text">Trans</span>
          </label>
          <button className="pb-btn icon" disabled={!canControl} onClick={() => scrollToPage(livePage)}
            title="Pin to Presentation — scroll this preview to the slide the presentation is currently showing">⇤</button>
          <button className="pb-btn icon" disabled={!canControl || !centerPage} onClick={() => onSetPresentPage(centerPage)}
            title="Jump to this page — make the presentation jump to the slide you're viewing here">⇥</button>
          {busy && <span className="dot off">resolving…</span>}
          <button className="refreshbtn icon" onClick={onCompile} title="Refresh — recompile the deck">
            <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M13.65 6A6 6 0 1 0 12 12.5" />
              <polyline points="14 2 14 7 9 7" />
            </svg>
          </button>
        </div>
      </div>
      {hasErr && (
        <div className="compile-error">
          <div className="ce-title">⚠ Source does not compile — showing the last good render (stale)</div>
          <ul>{compileError.map((e, i) => <li key={i}>{e}</li>)}</ul>
        </div>
      )}
      {notesOn && noteOrphans && noteOrphans.length > 0 && (
        <div className="note-orphans">
          ⚠ {noteOrphans.length} transcript{noteOrphans.length > 1 ? 's' : ''} render on no slide
          (likely a <code>self.subslide == k</code> beyond the slide's subslide count):
          {noteOrphans.slice(0, 3).map((o, i) => <span key={i} className="no-chip">“{o.text}”</span>)}
        </div>
      )}
      <div ref={pageListRef} className={'page-list' + (notesOn ? ' with-notes' : '')} onScroll={onListScroll}>
        {(!pages || pages.length === 0) && <div className="empty">No pages yet. Hit Refresh.</div>}
        {pages && pages.map((name, i) => {
          const pn = i + 1
          const info = (slideMap && slideMap[pn - 1]) || {}
          return (
            <figure key={name} className="page">
              <div className="page-slide">
                <div className="pnum">
                  <span className="pnum-label" title="jump editor to this page" onClick={() => onJumpPage(pn)}>
                    {pn}/{pages.length}{info.slide_no ? ` · S${info.slide_no}` : ''}{info.sub_total > 1 ? ` · ${info.sub_index}/${info.sub_total}` : ''}
                  </span>
                  <button className="badge-add" onClick={() => onAddPage(pn)}>+ add page</button>
                </div>
                <PageSvg name={name} pageNo={pn} token={tokens[name]} mark={locateMark && locateMark.page === pn ? locateMark : null} onResolve={onResolve} onJump={onJump} onAdd={onAdd} />
              </div>
              {notesOn && <NoteInline info={info} onSaved={onReloadNotes} />}
            </figure>
          )
        })}
        {/* trailing blank space so the LAST slide can be scrolled up into view (and become the
            centered/selected page) instead of being stuck pinned near the bottom edge. */}
        {pages && pages.length > 0 && <div className="page-list-tail" aria-hidden="true" />}
      </div>
    </section>
  )
}
