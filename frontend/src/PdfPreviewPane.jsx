import React, { useEffect, useState } from 'react'
import * as api from './api.js'
import {
  editPdfTranscriptDraft,
  finishPdfTranscriptSave,
  pdfTranscriptDirty,
  reconcilePdfTranscriptDrafts,
  startPdfTranscriptSave,
} from './pdfWorkspace.js'
import { toast } from './Toaster.jsx'

function transcriptText(slideMap, page) {
  const row = Array.isArray(slideMap) ? slideMap[page - 1] : null
  return row && typeof row.note === 'string' ? row.note : ''
}

export default function PdfPreviewPane({ pages, tokens, page, setPage, slideMap, orphans, onTranscriptSaved }) {
  const [transcriptsOn, setTranscriptsOn] = useState(true)
  const [drafts, setDrafts] = useState({})
  const total = pages.length
  const current = pages[page - 1]
  const draftState = drafts[page] || { draft: transcriptText(slideMap, page), base: transcriptText(slideMap, page), saving: false }
  const dirty = pdfTranscriptDirty(draftState.draft, draftState.base)

  useEffect(() => {
    setDrafts((previous) => reconcilePdfTranscriptDrafts(previous, slideMap, total))
  }, [slideMap, total])

  async function save() {
    if (!dirty || !page || draftState.saving) return
    const request = { page, text: draftState.draft }
    setDrafts((previous) => {
      const started = startPdfTranscriptSave(previous, request)
      return started.drafts
    })
    try {
      const result = await api.savePdfTranscript(request.page, request.text)
      if (result && result.ok !== false) {
        setDrafts((previous) => finishPdfTranscriptSave(previous, request, true))
        onTranscriptSaved && onTranscriptSaved()
      } else {
        setDrafts((previous) => finishPdfTranscriptSave(previous, request, false))
      }
    } catch (error) {
      setDrafts((previous) => finishPdfTranscriptSave(previous, request, false))
      toast.error(error.message || 'Could not save transcript')
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
          <textarea value={draftState.draft} placeholder="Write a transcript for this page…"
            onChange={(event) => setDrafts((previous) => editPdfTranscriptDraft(previous, page, event.target.value))}
            onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') save() }} />
          <div className="pdf-transcript-actions">
            <button className="primary" disabled={!dirty || draftState.saving} onClick={save}>{draftState.saving ? 'Saving…' : 'Save transcript'}</button>
            {dirty && <button onClick={() => setDrafts((previous) => editPdfTranscriptDraft(previous, page, draftState.base))}>Revert</button>}
            {dirty && <span>⌘↵ save</span>}
          </div>
        </div>
      )}
    </section>
  )
}
