import React from 'react'
import Icon from './PresenterIcon.jsx'
import { exportRunning } from './usePresentationExport.js'
import * as api from './api.js'

export default function RecordingExportProgress({ controller: r }) {
  if (!r.visible) return null
  const job = r.job
  const running = exportRunning(job)
  const progress = Math.max(0, Math.min(100, job.progress || 0))
  const stage = { prepare: 'Preparing', audio: 'Audio', video: 'Video', merge: 'Finishing' }[job.stage] || 'Exporting'
  const label = job.status === 'cancelling' ? 'Cancelling' : job.status === 'complete' ? 'MP4 ready'
    : job.status === 'cancelled' ? 'Cancelled' : job.status === 'failed' ? 'Export failed'
      : `${stage}${job.page ? ` · ${job.page}/${job.total}` : ''}`
  return <aside className="recording-export-task" aria-label="Video export task">
    <div className="recording-export-task-row">
      <Icon name="export" />
      <span className="recording-export-task-label" role="status" title={job.error || r.connectionError || label}>{label}</span>
      {running && <output className="recording-export-task-percent">{progress}%</output>}
      {job.status === 'complete' && <a className="pr-btn pr-icon-btn" href={api.recordingExportUrl(job.id)} download="presentation.mp4" aria-label="Download exported video" title="Download MP4"><Icon name="download" /></a>}
      {running ? <button className="pr-btn pr-icon-btn recording-export-cancel" onClick={r.cancel} disabled={job.status === 'cancelling'} aria-label="Cancel video export" title="Cancel this export"><Icon name="stop" /></button>
        : <button className="pr-btn pr-icon-btn" onClick={r.dismiss} aria-label="Dismiss export status" title="Dismiss"><Icon name="close" /></button>}
    </div>
    {running && <div className="recording-export-task-track" role="progressbar" aria-label="Background video export progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}>
      <span style={{ width: `${progress}%` }} />
    </div>}
    {r.connectionError && running && <div className="recording-export-task-detail" title={r.connectionError}>Reconnecting…</div>}
    {job.status === 'failed' && <div className="recording-export-task-detail" role="alert">{job.error}</div>}
  </aside>
}
