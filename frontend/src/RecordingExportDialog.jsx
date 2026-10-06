import React, { useEffect, useRef } from 'react'
import Icon from './PresenterIcon.jsx'

export default function RecordingExportDialog({ missingPages, recordedCount, disabled, triggerRef, onCancel, onExport }) {
  const dialog = useRef(null)
  const cancel = useRef(null)
  useEffect(() => {
    const node = dialog.current
    node.showModal()
    cancel.current.focus()
    return () => { node.close(); triggerRef.current?.focus() }
  }, [])

  function onKeyDown(event) {
    event.stopPropagation()
    if (event.key !== 'Tab') return
    const buttons = dialog.current.querySelectorAll('button:not(:disabled)')
    const first = buttons[0], last = buttons[buttons.length - 1]
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus()
    }
  }

  return <dialog ref={dialog} className="pr-export-dialog" aria-labelledby="pr-export-title" aria-describedby="pr-export-description"
    onCancel={event => { event.preventDefault(); onCancel() }} onKeyDown={onKeyDown}>
    <div className="pr-export-dialog-heading">
      <h2 id="pr-export-title">Some pages have no recording</h2>
      <button className="pr-btn pr-icon-btn" aria-label="Close export dialog" title="Cancel export" onClick={onCancel}><Icon name="close" /></button>
    </div>
    <p id="pr-export-description">The MP4 will include {recordedCount} recorded {recordedCount === 1 ? 'page' : 'pages'} in slide order. The pages below will be skipped.</p>
    <div className="pr-export-missing">
      <div className="pr-export-missing-heading">{missingPages.length} {missingPages.length === 1 ? 'page' : 'pages'} without recording</div>
      <ul aria-label="Pages without recordings">{missingPages.map(page => <li key={page}>Page {page}</li>)}</ul>
    </div>
    <div className="pr-export-dialog-actions">
      <button ref={cancel} className="pr-btn" onClick={onCancel}>Cancel</button>
      <button className="pr-btn pr-export-confirm" disabled={disabled} onClick={onExport}><Icon name="export" />Continue export</button>
    </div>
  </dialog>
}
