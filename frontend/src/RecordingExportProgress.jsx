import React from 'react'
import { createPortal } from 'react-dom'
import Icon from './PresenterIcon.jsx'
import { exportRunning } from './usePresentationExport.js'
import * as api from './api.js'

export default function RecordingExportProgress({ controller: r }) {
  if (!r.visible) return null
  const job = r.job
  const running = exportRunning(job)
  const inline = !!r.dock
  const progress = Math.max(0, Math.min(100, job.progress || 0))
  const stage = { prepare: 'Preparing', audio: 'Audio', video: 'Video', merge: 'Finishing' }[job.stage] || 'Exporting'
  const label = job.status === 'cancelling' ? 'Cancelling' : job.status === 'complete' ? 'MP4 ready'
    : job.status === 'cancelled' ? 'Cancelled' : job.status === 'failed' ? 'Export failed'
      : `${stage}${job.page ? ` · ${job.page}/${job.total}` : ''}`
  const track = running && <div className="recording-export-task-track" role="progressbar" aria-label="Background video export progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}>
    <span style={{ width: `${progress}%` }} />
  </div>
  const view = <aside className={`recording-export-task${inline ? ' inline' : ''}`} aria-label="Video export task">
    <div className="recording-export-task-row">
      <Icon name="export" />
      <div className="recording-export-task-summary">
        <span className="recording-export-task-label" role="status" title={job.error || r.connectionError || label}>{inline && running && r.connectionError ? 'Reconnecting…' : label}</span>
        {inline && track}
      </div>
      {running && <output className="recording-export-task-percent">{progress}%</output>}
      {!inline && job.status === 'complete' && <a className="pr-btn pr-icon-btn" href={api.recordingExportUrl(job.id)} download="presentation.mp4" aria-label="Download exported video" title="Download MP4"><Icon name="download" /></a>}
      {running ? <button className="pr-btn pr-icon-btn recording-export-cancel" onClick={r.cancel} disabled={job.status === 'cancelling'} aria-label="Cancel video export" title="Cancel this export"><Icon name="stop" /></button>
        : <button className="pr-btn pr-icon-btn" onClick={r.dismiss} aria-label="Dismiss export status" title="Dismiss"><Icon name="close" /></button>}
    </div>
    {!inline && track}
    {!inline && r.connectionError && running && <div className="recording-export-task-detail" title={r.connectionError}>Reconnecting…</div>}
    {!inline && job.status === 'failed' && <div className="recording-export-task-detail" role="alert">{job.error}</div>}
  </aside>
  return inline ? createPortal(view, r.dock) : view
}
