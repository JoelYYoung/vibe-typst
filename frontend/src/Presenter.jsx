import React, { useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import { toast } from './Toaster.jsx'

// PowerPoint-style presenter view: big current slide, next-slide preview, the speaker note
// ("script") for the current slide, a timer, and navigation. A second "projection" window
// (opened here) shows the audience just the current slide and follows via BroadcastChannel.
// `page`/`setPage`/`pages`/`rv` are owned by the App (so the page survives exiting + re-entering
// presenter mode, and the App keeps the projection live even when this view is closed).
export default function Presenter({ onClose, onSaved, page, setPage, pages, tokens }) {
  const [map, setMap] = useState([])
  const [elapsed, setElapsed] = useState(0)
  const [showThumbs, setShowThumbs] = useState(true)
  const thumbRef = useRef(null)
  const noteRef = useRef(null)

  useEffect(() => {
    api.getSlideMap().then((r) => setMap(r.pages || [])).catch(() => {})
    const t = setInterval(() => setElapsed((e) => e + 1), 1000)
    return () => clearInterval(t)
  }, [])

  const total = pages.length
  const go = (d) => setPage((p) => Math.min(Math.max(total, 1), Math.max(1, p + d)))

  useEffect(() => {
    const onKey = (e) => {
      // don't hijack arrows/space while the user is editing the script
      const tag = (e.target && e.target.tagName) || ''
      const editing = tag === 'TEXTAREA' || tag === 'INPUT'
      if (e.key === 'Escape') { if (editing) e.target.blur(); else onClose(); return }
      if (editing) return
      if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' ') { e.preventDefault(); go(1) }
      else if (e.key === 'ArrowLeft' || e.key === 'PageUp') { e.preventDefault(); go(-1) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [total])

  // editable script for the current slide
  const [draft, setDraft] = useState('')
  const [savedFor, setSavedFor] = useState(-1)
  useEffect(() => { setDraft((map[page - 1] || {}).note || ''); setSavedFor(page) }, [page, map])
  // On every slide change, scroll the transcript back to the top so the next page starts
  // from line 1 instead of wherever the previous slide's note was scrolled to.
  useEffect(() => { if (noteRef.current) noteRef.current.scrollTop = 0 }, [page])
  async function saveScript() {
    const info = map[page - 1]
    if (!info || !(info.slide_line || info.note_raw)) return
    const savedDraft = draft
    // Inline #speaker-note in the .typ source — the single shared source of truth.
    const r = await api.saveNote(info, savedDraft)
    if (r && r.ok) {
      // Optimistic: update local map so the save button hides immediately
      setMap(prev => { const next = [...prev]; next[page-1] = {...next[page-1], note: savedDraft}; return next })
      api.getSlideMap().then((res) => setMap(res.pages || [])).catch(() => {})
      if (r.warning) toast.info(r.warning, 6000)
      onSaved && onSaved()  // tell the App to refresh the editor's inline notes too
    } else if (r && r.error) {
      toast.error(r.error)
    }
  }

  // keep the active thumbnail in view as we navigate
  useEffect(() => {
    if (showThumbs && thumbRef.current) thumbRef.current.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [page, showThumbs])

  const cur = pages[page - 1]
  const nxt = pages[page]
  const info = map[page - 1] || {}
  // `draft` is only meaningful once the effect has synced it to THIS page. Until then
  // (the moment right after a slide switch) the old draft vs the new note would falsely
  // look "dirty" and flash the save/revert buttons — so gate on savedFor === page.
  const ready = savedFor === page
  const dirty = ready && draft !== (info.note || '')
  const mmss = `${String(Math.floor(elapsed / 60)).padStart(2, '0')}:${String(elapsed % 60).padStart(2, '0')}`
  const openProjection = () => window.open(location.pathname + '?project', 'tcb-projection', 'width=1280,height=720')

  return (
    <div className="presenter">
      <div className="pr-top">
        <button className="pr-btn" onClick={onClose} title="exit (Esc)">✕ Exit</button>
        <button className={'pr-btn' + (showThumbs ? ' on' : '')} onClick={() => setShowThumbs((v) => !v)} title="show/hide slide thumbnails">▤ Overview</button>
        <span className="pr-title">{info.section || `Slide ${page}`}</span>
        <span className="grow" />
        <span className="pr-clock">⏱ {mmss}</span>
        <button className="pr-btn" onClick={openProjection} title="open the audience/projector window">⧉ Open projection</button>
      </div>
      <div className="pr-main">
        <div className={'pr-thumbs' + (showThumbs ? ' open' : '')}>
          {pages.map((name, i) => {
            const pn = i + 1
            return (
              <button key={name} ref={pn === page ? thumbRef : null}
                className={'pr-thumb' + (pn === page ? ' on' : '')}
                onClick={() => setPage(pn)} title={`slide ${pn}`}>
                <span className="pr-thumb-n">{pn}</span>
                <img src={api.renderUrl(name, tokens[name])} alt="" loading="lazy" />
              </button>
            )
          })}
        </div>
      <div className="pr-body">
        <div className="pr-current">
          <div className="pr-label">PAGE {page}/{total}{info.slide_no ? ` · SLIDE ${info.slide_no}/${info.slide_total}` : ''}{info.sub_total > 1 ? ` · SUBSLIDE ${info.sub_index}/${info.sub_total}` : ''}</div>
          {cur ? <img className="pr-slide" src={api.renderUrl(cur, tokens[cur])} alt="" /> : <div className="proj-empty">…</div>}
        </div>
        <div className="pr-side">
          <div className="pr-next">
            <div className="pr-label">NEXT{nxt ? ` · ${page + 1}` : ' · end'}</div>
            {nxt ? <img className="pr-slide" src={api.renderUrl(nxt, tokens[nxt])} alt="" /> : <div className="proj-empty pr-end">— end —</div>}
          </div>
          <div className="pr-notes">
            <div className="pr-label">TRANSCRIPT{(info.slide_line || info.note_raw) ? ' · editable' : ''}{info.sub_total > 1 ? ` · sub ${info.sub_index}/${info.sub_total}` : ''}</div>
            {(info.slide_line || info.note_raw) ? (
              <>
                <textarea
                  ref={noteRef}
                  className="pr-note-edit"
                  value={draft}
                  placeholder="Write a transcript for this slide… (⌘↵ to save)"
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') saveScript() }}
                />
                {ready && (dirty || !info.note_raw) && (
                  <div className="pr-note-actions">
                    <button className="pr-btn" disabled={draft === (info.note || '')} onClick={saveScript}>{info.note_raw ? 'save' : 'add'} transcript</button>
                    {dirty && <button className="pr-btn" onClick={() => setDraft(info.note || '')}>revert</button>}
                  </div>
                )}
              </>
            ) : (
              <div className="pr-note-text" style={{color: '#5a6a7a', fontStyle: 'italic', fontSize: 14}}>Speaker notes require a touying deck.</div>
            )}
          </div>
        </div>
      </div>
      </div>
      <div className="pr-nav">
        <button className="pr-nav-btn" onClick={() => go(-1)} disabled={page <= 1}>◀ Prev</button>
        <span className="pr-page">{page} / {total}</span>
        <button className="pr-nav-btn" onClick={() => go(1)} disabled={page >= total}>Next ▶</button>
      </div>
    </div>
  )
}
