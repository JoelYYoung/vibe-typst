import React, { useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import { pdfTranscriptDirty, shouldSyncPdfTranscriptDraft } from './pdfWorkspace.js'
import { toast } from './Toaster.jsx'

function transcriptText(slideMap, page) {
  const row = Array.isArray(slideMap) ? slideMap[page - 1] : null
  return row && typeof row.note === 'string' ? row.note : ''
}

export default function PdfPreviewPane({ pages, tokens, page, setPage, slideMap, orphans, onTranscriptSaved }) {
  const [transcriptsOn, setTranscriptsOn] = useState(true)
  const [draft, setDraft] = useState('')
  const [base, setBase] = useState('')
  const [saving, setSaving] = useState(false)
  const draftRef = useRef('')
  const baseRef = useRef('')
  const pageRef = useRef(0)
  const savedRef = useRef('')
  const total = pages.length
  const saved = transcriptText(slideMap, page)
  const current = pages[page - 1]
  const dirty = pdfTranscriptDirty(draft, base)

  useEffect(() => { draftRef.current = draft }, [draft])
  useEffect(() => { baseRef.current = base }, [base])
  useEffect(() => {
    // A page change selects a different authoritative transcript. Render polls on the same page
    // only replace a draft that still matches its previous saved value. Deliberately depend on
    // the authoritative page text, not `base`: setting base after a save must not replay an
    // already-stale slide-map value over the fresh draft.
    const pageChanged = pageRef.current !== page
    const savedChanged = savedRef.current !== saved
    if (shouldSyncPdfTranscriptDraft({
      pageChanged,
      savedChanged,
      dirty: pdfTranscriptDirty(draftRef.current, baseRef.current),
    })) {
      pageRef.current = page
      setBase(saved)
      setDraft(saved)
    }
    savedRef.current = saved
  }, [page, saved])

  async function save() {
    if (!dirty || !page) return
    setSaving(true)
    const text = draft
    try {
      const result = await api.savePdfTranscript(page, text)
      if (result && result.ok !== false) {
        setBase(text)
        onTranscriptSaved && onTranscriptSaved()
      }
    } catch (error) {
      toast.error(error.message || 'Could not save transcript')
    } finally {
      setSaving(false)
    }
  }

  function downloadTranscripts() {
    const body = pages.map((_, index) => `Page ${index + 1}\n${transcriptText(slideMap, index + 1)}`).join('\n\n') + '\n'
    const url = URL.createObjectURL(new Blob([body], { type: 'text/plain;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = 'transcript.txt'
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
  }

  return (
    <section className="pdf-preview-pane">
      <div className="pdf-preview-head">
        <strong>PDF preview</strong>
        <span className="grow" />
        <button onClick={downloadTranscripts} disabled={!total}>↓ Transcripts</button>
        <label className="switch" title="show the current page transcript">
          <input type="checkbox" checked={transcriptsOn} onChange={(event) => setTranscriptsOn(event.target.checked)} />
          <span className="switch-track"><span className="switch-knob" /></span>
          <span className="switch-text">Transcript</span>
        </label>
      </div>
      {orphans.length > 0 && <div className="pdf-orphans">⚠ {orphans.length} orphaned transcript{orphans.length === 1 ? '' : 's'} need review.</div>}
      <div className="pdf-page-stage">
        {current ? <img src={api.renderUrl(current, tokens[current])} alt={`PDF page ${page}`} /> : <div className="empty">No PDF pages available.</div>}
      </div>
      <div className="pdf-page-controls">
        <button onClick={() => setPage((value) => value - 1)} disabled={page <= 1}>◀ Previous</button>
        <span>Page {total ? page : 0} / {total}</span>
        <button onClick={() => setPage((value) => value + 1)} disabled={page >= total}>Next ▶</button>
      </div>
      {transcriptsOn && total > 0 && (
        <div className="pdf-transcript">
          <div className="pdf-transcript-label">PAGE {page} TRANSCRIPT</div>
          <textarea value={draft} placeholder="Write a transcript for this page…"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') save() }} />
          <div className="pdf-transcript-actions">
            <button className="primary" disabled={!dirty || saving} onClick={save}>{saving ? 'Saving…' : 'Save transcript'}</button>
            {dirty && <button onClick={() => setDraft(base)}>Revert</button>}
            {dirty && <span>⌘↵ save</span>}
          </div>
        </div>
      )}
    </section>
  )
}
