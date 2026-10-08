import React, { useEffect, useRef, useState } from 'react'
import { formatRecordingTime } from './slideRecorder.js'
import { allocateTranscriptTime, recordingDurations } from './presentationTiming.js'
import Icon from './PresenterIcon.jsx'
import RecordingExportDialog from './RecordingExportDialog.jsx'
import { exportRunning } from './usePresentationExport.js'

export default function RecordingControls({ recording: r, exportController, page, total, transcripts, targetMinutes, onTargetMinutesChange }) {
  const [backup, setBackup] = useState(null)
  const [confirmExport, setConfirmExport] = useState(false)
  const exportTrigger = useRef(null)
  useEffect(() => {
    if (!r.pending) { setBackup(null); return }
    const url = URL.createObjectURL(r.pending.video)
    setBackup(url)
    return () => URL.revokeObjectURL(url)
  }, [r.pending])
  const current = r.takes[page]
  const stale = r.pageStates[page - 1] === 'stale'
  const live = r.status === 'recording'
  const durations = recordingDurations(r.takes, total, page, live ? r.elapsed : null)
  const suggested = allocateTranscriptTime(transcripts, targetMinutes)[page - 1]
  const startLabel = r.status === 'starting' ? 'Starting microphone…' : r.status === 'saving' ? 'Saving page…' : current ? 'Re-record page' : 'Start page'
  const missingPages = r.pageStates.flatMap((state, index) => state === 'missing' ? [index + 1] : [])
  const hasStalePages = r.pageStates.includes('stale')
  const exporting = r.status === 'exporting' || exportRunning(r.job)
  const exportProgress = exportRunning(r.job) ? Math.max(0, Math.min(100, r.job.progress ?? 0)) : 0
  const exportDisabled = r.locked || !r.loaded || exporting
    || !r.exportAvailable || !r.completed || hasStalePages
  function requestExport() {
    setConfirmExport(true)
  }
  return <section className="pr-recording" aria-label="Slide recording">
    <div className="pr-recording-row">
      <div className="pr-recording-actions">
      <span className={`pr-recording-dot ${r.status === 'recording' ? 'live' : ''}`} aria-hidden="true" />
      <span className="pr-recording-time" aria-label="Current page recording time" title={`Page ${page}: recorded duration`}><Icon name="clock" />{formatRecordingTime(durations.page)}</span>
      <span className="pr-recording-total" aria-label="Total recording time" title={live ? 'Total duration including this take' : 'Total recorded duration'}><Icon name="total" /><strong>{formatRecordingTime(durations.total)}</strong></span>
      <label className="pr-target-time" title="Target presentation duration in minutes"><Icon name="target" /><input type="number" min="0.5" max="240" step="0.5"
        aria-label="Target presentation minutes" value={targetMinutes} placeholder="20"
        onChange={(event) => onTargetMinutesChange(event.target.value)} /><span>min</span></label>
      <span className="pr-page-budget" aria-label="Suggested page time" title="Suggested time for this page, allocated by transcript length"><Icon name="guide" /><strong>{suggested === null ? '—' : formatRecordingTime(Math.round(suggested))}</strong></span>
      <span className="pr-recording-ready" title="Recorded pages ready for export">{r.completed}/{total}</span>
      <span className="pr-toolbar-divider" />
      {r.status === 'recording'
        ? <button className="pr-btn pr-icon-btn pr-record-stop" onClick={r.stop} aria-label="Stop and save page" title="Stop recording and save this page"><Icon name="stop" /></button>
        : <button className="pr-btn pr-icon-btn pr-record-start" onClick={r.start} aria-label={startLabel}
            title={stale ? 'This slide changed. Record a new take for this page.' : current ? 'Record a new take for this page; replaces the previous take after saving.' : 'Start recording this page. Hold the left mouse button on the slide to show the laser.'}
            disabled={r.locked || !r.loaded || !total || r.status === 'exporting'}>
            <Icon name="record" />
          </button>}
      <button className="pr-btn pr-icon-btn" disabled={r.busy || !current} onClick={() => r.setPreview(!r.preview)}
        aria-label={r.preview ? 'Close preview' : 'Preview page'} title={r.preview ? 'Close recording preview' : 'Preview the recording for this page'}>
        <Icon name={r.preview ? 'close' : 'play'} />
      </button>
      <button className="pr-btn pr-icon-btn pr-record-clear" disabled={r.locked || r.status !== 'idle' || !current} onClick={r.clearPage}
        aria-label="Clear page recording" title="Clear only this page's recording">
        <Icon name="trash" />
      </button>
      <button ref={exportTrigger} className={`pr-btn pr-icon-btn${exporting ? ' pr-exporting' : ''}`} onClick={requestExport} aria-label="Export full MP4"
        title={exporting ? `Exporting ${exportProgress}%` : !r.exportAvailable ? 'MP4 export requires FFmpeg' : hasStalePages ? 'Re-record changed pages before exporting' : !r.completed ? 'Record at least one page to export' : 'Export recorded pages as one MP4 video'}
        disabled={exportDisabled}>
        {exporting && <span className="pr-export-progress" style={{ width: `${exportProgress}%` }} role="progressbar"
          aria-label="Video export progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={exportProgress} />}
        <Icon name="export" />
      </button>
      <button className="pr-btn pr-icon-btn pr-record-reload" onClick={r.load} disabled={r.locked || r.status !== 'idle'}
        aria-label="Reload recordings" title="Reload saved page recordings"><Icon name="refresh" /></button>
      {r.exportUrl && <a className="pr-btn pr-icon-btn" href={r.exportUrl} download="presentation.mp4" aria-label="Download MP4" title="Download the exported MP4 video"><Icon name="download" /></a>}
      </div>
      {exportController.visible && <div className="pr-recording-task-slot" ref={exportController.setDock} />}
      {live && <div className="pr-mic-meter" role="meter" aria-label="Microphone input level" aria-valuemin={-60} aria-valuemax={0}
        aria-valuenow={r.audioLevel ?? undefined} aria-valuetext={r.audioLevel === null ? 'Waiting for microphone level' : `${Math.round(r.audioLevel)} dBFS`}
        title="Live microphone input level (dBFS). Higher values mean louder audio; red indicates a level close to clipping.">
        <Icon name="mic" />
        <span className="pr-mic-bars" aria-hidden="true">{Array.from({ length: 12 }, (_, index) =>
          <span key={index} className={'pr-mic-bar' + (r.audioLevel !== null && r.audioLevel > -60 + index * 5 ? ' lit' : '') + (index >= 10 ? ' hot' : index >= 8 ? ' warm' : '')} />)}</span>
        <output className="pr-mic-value" aria-hidden="true">{r.audioLevel === null ? '—' : Math.round(r.audioLevel)} <small>dBFS</small></output>
      </div>}
    </div>
    {confirmExport && <RecordingExportDialog missingPages={missingPages} disabled={exportDisabled} triggerRef={exportTrigger}
      takes={r.takes} pageStates={r.pageStates}
      onCancel={() => setConfirmExport(false)} onExport={(audio, reference) => { setConfirmExport(false); r.exportVideo(missingPages, audio, reference) }} />}
    {(r.error || r.job?.status === 'failed') && <div className="pr-recording-error" role="alert">{r.error || r.job.error}</div>}
    {r.pending && <div className="pr-recording-recovery">
      <button className="pr-btn pr-icon-btn" disabled={r.busy} onClick={r.retry} aria-label={`Retry saving page ${r.pending.page}`} title="Retry saving the unsaved recording"><Icon name="save" /></button>
      {backup && <a className="pr-btn pr-icon-btn" href={backup} download={`page-${r.pending.page}.${r.pending.metadata.mime.startsWith('video/mp4') ? 'mp4' : 'webm'}`} aria-label="Download take backup" title="Download a backup of this unsaved recording"><Icon name="download" /></a>}
      <button className="pr-btn pr-icon-btn" disabled={r.busy} onClick={r.discard} aria-label="Discard unsaved take" title="Discard this unsaved recording"><Icon name="trash" /></button>
    </div>}
    {r.preview && r.previewUrl && <div className="pr-recording-preview">
      <video key={r.previewUrl} src={r.previewUrl} controls autoPlay preload="metadata" aria-label={`Recording preview for page ${page}`} />
    </div>}
  </section>
}
