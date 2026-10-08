import React, { useCallback, useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import { toast } from './Toaster.jsx'
import { clientToSlidePoint } from './presentationPointer.js'
import {
  finishPresenterDraftSave,
  presenterRenderIdentity,
  reconcilePresenterDraft,
} from './presenterState.js'
import { projectionUrl } from './workspaceRouting.js'
import usePresentationRecording from './usePresentationRecording.js'
import RecordingControls from './RecordingControls.jsx'
import Icon from './PresenterIcon.jsx'

const PREFERENCES_KEY = 'vibe-typst.presenter-preferences'
function loadPreferences() {
  try {
    const saved = JSON.parse(localStorage.getItem(PREFERENCES_KEY)) || {}
    return {
      showNextPreview: saved.showNextPreview !== false,
      fontSize: Number.isInteger(saved.fontSize) ? Math.min(36, Math.max(12, saved.fontSize)) : 17,
      targetMinutes: Number(saved.targetMinutes) >= .5 && Number(saved.targetMinutes) <= 240 ? String(saved.targetMinutes) : '',
    }
  } catch { return { showNextPreview: true, fontSize: 17, targetMinutes: '' } }
}

// PowerPoint-style presenter view: big current slide, next-slide preview, the speaker note
// ("script") for the current slide, a timer, and navigation. A second "projection" window
// (opened here) shows the audience just the current slide and follows via BroadcastChannel.
// `page`/`setPage`/`pages`/`rv` are owned by the App (so the page survives exiting + re-entering
// presenter mode, and the App keeps the projection live even when this view is closed).
export default function Presenter({ onClose, onSaved, onPointer, page, setPage, pages, tokens, slideMap, generation, renderVersion, exportController }) {
  const [localMap, setLocalMap] = useState([])
  const map = Array.isArray(slideMap) ? slideMap : localMap
  const [elapsed, setElapsed] = useState(0)
  const [showThumbs, setShowThumbs] = useState(true)
  const [preferences, setPreferences] = useState(loadPreferences)
  const { showNextPreview, fontSize } = preferences
  const thumbRef = useRef(null)
  const noteRef = useRef(null)
  const currentSlideRef = useRef(null)
  const activePointerRef = useRef(null)
  const [localPointer, setLocalPointer] = useState(null)
  const [recordingMode, setRecordingMode] = useState(false)
  const recording = usePresentationRecording({ enabled: recordingMode, page, pages, tokens, renderVersion, exportController })
  const recordingRef = useRef(recording)
  recordingRef.current = recording
  const closePresenter = () => { if (!recordingRef.current.locked) onClose() }

  useEffect(() => {
    try { localStorage.setItem(PREFERENCES_KEY, JSON.stringify(preferences)) } catch {}
  }, [preferences])

  useEffect(() => {
    let cancelled = false
    if (!Array.isArray(slideMap)) {
      api.getSlideMap().then((r) => { if (!cancelled) setLocalMap(r.pages || []) }).catch(() => {})
    }
    return () => { cancelled = true }
  }, [renderVersion])
  useEffect(() => {
    const t = setInterval(() => setElapsed((e) => e + 1), 1000)
    return () => clearInterval(t)
  }, [])

  const total = pages.length
  const go = (d) => { if (!recordingRef.current.locked) setPage((p) => Math.min(Math.max(total, 1), Math.max(1, p + d))) }
  const renderIdentity = presenterRenderIdentity(page, pages, tokens, generation)

  const hidePointer = useCallback(() => {
    setLocalPointer(null)
    onPointer && onPointer(null)
    recordingRef.current.movePointer(null)
  }, [onPointer])
  const clearPointer = useCallback(() => {
    activePointerRef.current = null
    hidePointer()
  }, [hidePointer])

  function pointFromEvent(e) {
    const img = currentSlideRef.current
    if (!img || !img.naturalWidth || !img.naturalHeight) return null
    const rect = e.currentTarget.getBoundingClientRect()
    const point = clientToSlidePoint(
      e.clientX,
      e.clientY,
      rect,
      img.naturalWidth,
      img.naturalHeight,
    )
    return point ? { ...point, left: e.clientX - rect.left, top: e.clientY - rect.top } : null
  }

  function startPointer(e) {
    if (!e.isPrimary || e.button !== 0) return
    const point = pointFromEvent(e)
    if (!point) return
    e.preventDefault()
    activePointerRef.current = e.pointerId
    e.currentTarget.setPointerCapture && e.currentTarget.setPointerCapture(e.pointerId)
    setLocalPointer(point)
    recordingRef.current.movePointer(point)
    onPointer && onPointer({ page, x: point.x, y: point.y })
  }

  function movePointer(e) {
    if (activePointerRef.current !== e.pointerId) return
    if (e.pointerType === 'mouse' && !(e.buttons & 1)) { clearPointer(); return }
    const point = pointFromEvent(e)
    if (!point) { hidePointer(); return }
    setLocalPointer(point)
    recordingRef.current.movePointer(point)
    onPointer && onPointer({ page, x: point.x, y: point.y })
  }

  function stopPointer(e) {
    if (activePointerRef.current !== e.pointerId) return
    if (e.currentTarget.hasPointerCapture && e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId)
    }
    clearPointer()
  }

  // A laser dot must never get stranded on the audience screen after navigation, focus loss,
  // or closing the presenter while the primary button is still held.
  useEffect(() => {
    clearPointer()
  }, [renderIdentity, clearPointer])
  useEffect(() => {
    window.addEventListener('blur', clearPointer)
    return () => {
      window.removeEventListener('blur', clearPointer)
      activePointerRef.current = null
      onPointer && onPointer(null)
    }
  }, [clearPointer, onPointer])

  useEffect(() => {
    const onKey = (e) => {
      // don't hijack arrows/space while the user is editing the script
      const tag = (e.target && e.target.tagName) || ''
      const editing = tag === 'TEXTAREA' || tag === 'INPUT'
      if (e.key === 'Escape') { if (editing) e.target.blur(); else closePresenter(); return }
      if (editing) return
      if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' ') { e.preventDefault(); go(1) }
      else if (e.key === 'ArrowLeft' || e.key === 'PageUp') { e.preventDefault(); go(-1) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [total, onClose])

  // Editable script for the current slide. A map refresh advances the saved baseline, but
  // must not replace text typed after a save request captured an older draft.
  const info = map[page - 1] || {}
  const [script, setScript] = useState({ page: -1, draft: '', base: '' })
  useEffect(() => {
    setScript((previous) => reconcilePresenterDraft(previous, page, info.note, generation))
  }, [page, map, generation])
  // On every slide change, scroll the transcript back to the top so the next page starts
  // from line 1 instead of wherever the previous slide's note was scrolled to.
  useEffect(() => { if (noteRef.current) noteRef.current.scrollTop = 0 }, [page])
  async function saveScript() {
    const savedInfo = map[page - 1]
    const pdfTranscript = savedInfo && savedInfo.project_type === 'pdf' && Number.isInteger(savedInfo.page)
    if (!savedInfo || !(pdfTranscript || savedInfo.slide_line || savedInfo.note_raw)) return
    const request = { page, generation, text: script.draft }
    // Typst uses an anchored source note; PDFs use their authoritative page-number sidecar.
    try {
      const r = pdfTranscript ? await api.savePdfTranscript(savedInfo.page, request.text) : await api.saveNote(savedInfo, request.text)
      if ((pdfTranscript && r) || (!pdfTranscript && r && r.ok)) {
        setScript((previous) => finishPresenterDraftSave(previous, request))
        // Typst owns an internal map. PDF Presenter consumes the workspace's sequenced map.
        if (!Array.isArray(slideMap)) {
          setLocalMap(prev => { const next = [...prev]; next[request.page-1] = {...next[request.page-1], note: request.text}; return next })
          api.getSlideMap().then((res) => setLocalMap(res.pages || [])).catch(() => {})
        }
        if (r.warning) toast.info(r.warning, 6000)
        onSaved && onSaved()  // tell the App to refresh the editor's inline notes too
      } else if (r && r.error) {
        toast.error(r.error)
      } else {
        toast.error('Could not save transcript')
      }
    } catch (error) {
      toast.error(error.message || 'Could not save transcript')
    }
  }

  // keep the active thumbnail in view as we navigate
  useEffect(() => {
    if (showThumbs && thumbRef.current) thumbRef.current.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [page, showThumbs])

  const cur = pages[page - 1]
  const nxt = pages[page]
  // `script.draft` is only meaningful once the effect has synced it to THIS page. Until then
  // (the moment right after a slide switch) the old draft vs the new note would falsely
  // look "dirty" and flash the save/revert buttons — so gate on script.page === page.
  const ready = script.page === page && (generation === undefined || script.generation === generation)
  const dirty = ready && script.draft !== script.base
  const editableTranscript = info.project_type === 'pdf' || info.slide_line || info.note_raw
  const mmss = `${String(Math.floor(elapsed / 60)).padStart(2, '0')}:${String(elapsed % 60).padStart(2, '0')}`
  // One projection, always: the constant window name makes a second presenter TAKE OVER the
  // existing audience window rather than open a rival one.
  const openProjection = () => window.open(
    projectionUrl(undefined, api.projectScope()),
    'tcb-projection',
    'width=1280,height=720',
  )

  return (
    <div className="presenter">
      <div className="pr-top">
        <button className="pr-btn pr-icon-btn" onClick={closePresenter} disabled={recording.locked} aria-label="Exit presenter" title="Exit presenter (Esc)"><Icon name="close" /></button>
        <button className={'pr-btn pr-icon-btn' + (showThumbs ? ' on' : '')} onClick={() => setShowThumbs((v) => !v)} aria-label="Toggle slide overview" title="Show or hide slide thumbnails"><Icon name="grid" /></button>
        <span className="pr-title">{info.section || `Slide ${page}`}</span>
        <span className="grow" />
        <button className={'pr-btn pr-icon-btn pr-record-mode' + (recordingMode ? ' on' : '')}
          disabled={recording.locked} onClick={() => setRecordingMode((value) => !value)}
          aria-label={recordingMode ? 'Close recording mode' : 'Record presentation'} aria-pressed={recordingMode}
          title={recordingMode ? 'Close recording mode' : 'Record presentation page by page'}>
          <Icon name="record" />
        </button>
        <span className="pr-clock">⏱ {mmss}</span>
        <button className="pr-btn pr-icon-btn" onClick={openProjection} disabled={recording.busy} aria-label="Open projection" title="Open the audience or projector window"><Icon name="screen" /></button>
      </div>
      {recordingMode && <RecordingControls recording={recording} page={page} total={total}
        transcripts={pages.map((_, index) => index + 1 === page && ready ? script.draft : map[index]?.note || '')}
        targetMinutes={preferences.targetMinutes}
        onTargetMinutesChange={(targetMinutes) => setPreferences((previous) => ({ ...previous, targetMinutes }))} />}
      <div className="pr-main">
        <div className={'pr-thumbs' + (showThumbs ? ' open' : '')}>
          {pages.map((name, i) => {
            const pn = i + 1
            return (
              <button key={name} ref={pn === page ? thumbRef : null}
                className={'pr-thumb' + (pn === page ? ' on' : '')}
                disabled={recording.locked} onClick={() => setPage(pn)} title={`slide ${pn}`}>
                <span className="pr-thumb-n">{pn}</span>
                {recordingMode && recording.pageStates[i] !== 'missing' && <span
                  className={`pr-thumb-recorded ${recording.pageStates[i]}`}
                  title={recording.pageStates[i] === 'stale' ? 'Slide changed: re-record' : 'Recorded'}>
                  {recording.pageStates[i] === 'stale' ? '!' : 'REC'}
                </span>}
                <img src={api.renderUrl(name, tokens[name])} alt="" loading="lazy" />
              </button>
            )
          })}
        </div>
      <div className="pr-body">
        <div className="pr-current pr-pointer-surface"
          onPointerDown={startPointer}
          onPointerMove={movePointer}
          onPointerUp={stopPointer}
          onPointerCancel={clearPointer}
          onLostPointerCapture={clearPointer}>
          <div className="pr-label">PAGE {page}/{total}{info.slide_no ? ` · SLIDE ${info.slide_no}/${info.slide_total}` : ''}{info.sub_total > 1 ? ` · SUBSLIDE ${info.sub_index}/${info.sub_total}` : ''}</div>
          {cur ? <img ref={currentSlideRef} className="pr-slide" src={api.renderUrl(cur, tokens[cur])} alt="" draggable="false" /> : <div className="proj-empty">…</div>}
          {localPointer && <span className="presentation-pointer pr-pointer" aria-hidden="true"
            style={{ left: `${localPointer.left}px`, top: `${localPointer.top}px` }} />}
        </div>
        <div className="pr-side">
          {showNextPreview && <div className="pr-next" id="presenter-next-preview">
            <div className="pr-label">NEXT{nxt ? ` · ${page + 1}` : ' · end'}</div>
            <button className="pr-btn pr-hide-preview" onClick={() => setPreferences((previous) => ({ ...previous, showNextPreview: false }))}
              aria-label="Hide next page preview" title="Hide next page preview to expand the transcript" aria-controls="presenter-next-preview" aria-expanded="true"><Icon name="collapse" /></button>
            {nxt ? <img className="pr-slide" src={api.renderUrl(nxt, tokens[nxt])} alt="" /> : <div className="proj-empty pr-end">— end —</div>}
          </div>}
          <div className="pr-notes">
            <div className="pr-notes-head">
              <div className="pr-label">TRANSCRIPT{editableTranscript ? ' · editable' : ''}{info.sub_total > 1 ? ` · sub ${info.sub_index}/${info.sub_total}` : ''}</div>
              <div className="pr-notes-tools">
                {!showNextPreview && <button className="pr-btn pr-show-preview"
                  onClick={() => setPreferences((previous) => ({ ...previous, showNextPreview: true }))}
                  aria-label="Show next page preview" title="Show next page preview" aria-expanded="false"><Icon name="expand" /></button>}
                <div className="pr-font-controls" role="group" aria-label="Transcript font size">
                  <button className="pr-btn" aria-label="Decrease transcript font size" title="Decrease transcript font size"
                    disabled={fontSize <= 12} onClick={() => setPreferences((previous) => ({ ...previous, fontSize: Math.max(12, previous.fontSize - 1) }))}>A−</button>
                  <output className="pr-font-size" aria-live="polite">{fontSize}px</output>
                  <button className="pr-btn" aria-label="Increase transcript font size" title="Increase transcript font size"
                    disabled={fontSize >= 36} onClick={() => setPreferences((previous) => ({ ...previous, fontSize: Math.min(36, previous.fontSize + 1) }))}>A+</button>
                </div>
              </div>
            </div>
            {editableTranscript ? (
              <>
                <textarea
                  ref={noteRef}
                  className="pr-note-edit"
                  style={{ fontSize: `${fontSize}px` }}
                  aria-label="Slide transcript"
                  value={script.draft}
                  placeholder="Write a transcript for this slide… (⌘↵ to save)"
                  onChange={(e) => setScript((previous) => ({
                    ...reconcilePresenterDraft(previous, page, info.note, generation),
                    draft: e.target.value,
                  }))}
                  onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') saveScript() }}
                />
                {ready && (dirty || !info.note_raw) && (
                  <div className="pr-note-actions">
                    <button className="pr-btn pr-icon-btn" disabled={!dirty} onClick={saveScript} aria-label="Save transcript" title="Save transcript (⌘/Ctrl + Enter)"><Icon name="save" /></button>
                    {dirty && <button className="pr-btn pr-icon-btn" onClick={() => setScript((previous) => ({ ...previous, draft: previous.base }))} aria-label="Revert transcript" title="Revert unsaved transcript changes"><Icon name="refresh" /></button>}
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
        <button className="pr-nav-btn" onClick={() => go(-1)} disabled={recording.locked || page <= 1} aria-label="Previous page" title="Previous page (←)"><Icon name="prev" /></button>
        <span className="pr-page">{page} / {total}</span>
        <button className="pr-nav-btn" onClick={() => go(1)} disabled={recording.locked || page >= total} aria-label="Next page" title="Next page (→)"><Icon name="next" /></button>
      </div>
    </div>
  )
}
