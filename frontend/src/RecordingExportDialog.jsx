import React, { useEffect, useRef, useState } from 'react'
import Icon from './PresenterIcon.jsx'
import { getRecordingAudioModels } from './api.js'

export default function RecordingExportDialog({ missingPages, recordedCount, disabled, triggerRef, onCancel, onExport, takes, pageStates }) {
  const dialog = useRef(null)
  const cancel = useRef(null)
  const [models, setModels] = useState(null)
  const [denoise, setDenoise] = useState('basic')
  const [voice, setVoice] = useState(false)
  const eligible = Object.values(takes).filter(take => pageStates[take.page - 1] === 'recorded' && take.duration >= 1)
  const [referencePage, setReferencePage] = useState(String(eligible[0]?.page || 'upload'))
  const [reference, setReference] = useState(null)
  const referenceTake = eligible.find(take => take.page === Number(referencePage))
  const [fileError, setFileError] = useState('')
  const [checking, setChecking] = useState(true)
  const sequence = useRef(0)
  async function checkModels() {
    const current = ++sequence.current
    setChecking(true)
    try {
      const result = await getRecordingAudioModels()
      if (current === sequence.current) {
        setModels(result)
        if (!result.denoise) setDenoise('basic')
        if (!result.seed_vc || !result.denoise) setVoice(false)
      }
    } catch {
      if (current === sequence.current) { setModels({ denoise: false, seed_vc: false }); setDenoise('basic'); setVoice(false) }
    } finally { if (current === sequence.current) setChecking(false) }
  }
  useEffect(() => {
    const node = dialog.current
    node.showModal()
    cancel.current.focus()
    checkModels()
    return () => { ++sequence.current; node.close(); triggerRef.current?.focus() }
  }, [])

  function onKeyDown(event) {
    event.stopPropagation()
    if (event.key !== 'Tab') return
    const controls = [...dialog.current.querySelectorAll('button, input, select, a[href], summary')]
      .filter(node => !node.disabled && node.getClientRects().length)
    const first = controls[0], last = controls[controls.length - 1]
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus()
    }
  }
  function submit() {
    const audio = { denoise, voice: voice ? 'seed-vc' : 'original' }
    if (voice && referencePage !== 'upload') {
      const take = referenceTake
      audio.reference_page = take.page
      audio.reference_take = take.take
    }
    onExport(audio, voice && referencePage === 'upload' ? reference : null)
  }
  const canExport = !disabled && (!voice || (models?.seed_vc && (referencePage === 'upload' ? reference : referenceTake)))
    && (denoise !== 'model' || models?.denoise)
  return <dialog ref={dialog} className="pr-export-dialog" aria-labelledby="pr-export-title" aria-describedby="pr-export-description"
    onCancel={event => { event.preventDefault(); onCancel() }} onKeyDown={onKeyDown}>
    <div className="pr-export-dialog-heading">
      <h2 id="pr-export-title">Export presentation</h2>
      <button className="pr-btn pr-icon-btn" aria-label="Close export dialog" title="Cancel export" onClick={onCancel}><Icon name="close" /></button>
    </div>
    <p id="pr-export-description">{recordedCount} recorded {recordedCount === 1 ? 'page' : 'pages'} · MP4 · 1080p</p>
    {!!missingPages.length && <div className="pr-export-missing">
      <div className="pr-export-missing-heading">{missingPages.length} {missingPages.length === 1 ? 'page' : 'pages'} without recording will be skipped</div>
      <ul aria-label="Pages without recordings">{missingPages.map(page => <li key={page}>Page {page}</li>)}</ul>
    </div>}
    <div className="pr-audio-options">
      <label className="pr-audio-field"><span>Noise reduction</span>
        <select aria-label="Noise reduction" value={denoise} disabled={voice} onChange={event => setDenoise(event.target.value)}>
          <option value="basic">Light noise reduction</option>
          <option value="model" disabled={!models?.denoise}>{models?.denoiser || 'Model noise reduction'}{!models?.denoise ? ' (not installed)' : ''}</option>
        </select>
      </label>
      <label className="pr-audio-voice"><span><strong>Unify voice tone</strong><small>{models?.seed_vc ? 'Seed-VC' : 'Seed-VC · not installed'}</small></span>
        <input type="checkbox" role="switch" aria-label="Unify voice tone" checked={voice} disabled={!models?.seed_vc || !models?.denoise}
          onChange={event => { setVoice(event.target.checked); if (event.target.checked) setDenoise('model') }} />
      </label>
      {voice && <div className="pr-voice-reference">
        <label className="pr-audio-field"><span>Reference voice</span>
          <select aria-label="Reference voice" value={referencePage} onChange={event => setReferencePage(event.target.value)}>
            {eligible.map(take => <option key={take.take} value={take.page}>Page {take.page} recording</option>)}
            <option value="upload">Upload reference audio</option>
          </select>
        </label>
        {referencePage === 'upload' && <label className="pr-reference-upload"><input type="file" accept="audio/*,.wav,.mp3,.m4a,.flac,.ogg,.webm" aria-label="Upload reference audio"
          onChange={event => {
            const file = event.target.files[0]
            setReference(file && file.size <= 20 * 1024 * 1024 ? file : null)
            setFileError(file?.size > 20 * 1024 * 1024 ? 'Choose an audio file under 20 MB.' : '')
          }} /><span>Use a clean sample of 1–25 seconds.</span></label>}
        {fileError && <div className="pr-recording-error" role="alert">{fileError}</div>}
      </div>}
      <div className="pr-model-status"><span>{checking ? 'Checking optional models…' : models?.seed_vc || models?.denoise ? 'Optional models connected' : 'Optional models not installed or connected'}</span>
        <button className="pr-btn pr-icon-btn" title="Check optional models again" aria-label="Check optional models" disabled={checking} onClick={checkModels}><Icon name="refresh" /></button>
      </div>
      <details className="pr-model-help"><summary>Optional model setup</summary>
        <p>Install audio models on the computer that exports videos. A compatible GPU or Apple Silicon is recommended. The original voice option works without models.</p>
        <a href="https://github.com/JoelYYoung/vibe-typst/blob/main/docs/audio-models.md" target="_blank" rel="noreferrer">Installation guide</a>
      </details>
      <p className="pr-audio-note">Noise reduction runs first. Volume is balanced across pages; original recordings are kept.</p>
    </div>
    <div className="pr-export-dialog-actions">
      <button ref={cancel} className="pr-btn" onClick={onCancel}>Cancel</button>
      <button className="pr-btn pr-export-confirm" disabled={!canExport} onClick={submit}><Icon name="export" />Continue export</button>
    </div>
  </dialog>
}
