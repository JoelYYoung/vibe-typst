import React, { useEffect, useRef, useState } from 'react'
import Icon from './PresenterIcon.jsx'
import { getRecordingAudioModels } from './api.js'

export default function RecordingExportDialog({ missingPages, disabled, triggerRef, onCancel, onExport, takes, pageStates }) {
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
  async function checkModels(refresh = false) {
    const current = ++sequence.current
    setChecking(true)
    try {
      const result = await getRecordingAudioModels(refresh)
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
  useEffect(() => {
    if (!Object.values(models?.checks || {}).some(check => check.state === 'checking')) return
    const timer = setTimeout(checkModels, 1500)
    return () => clearTimeout(timer)
  }, [models])

  function modelStatus(key) {
    const state = models?.checks?.[key]?.state || (models?.[key] ? 'ready' : checking ? 'checking' : 'unavailable')
    const label = { ready: 'Ready', checking: 'Checking', unavailable: 'Unavailable' }[state] || 'Unavailable'
    return <span className={`pr-model-state ${state}`} title={models?.checks?.[key]?.message || label}>{label}</span>
  }

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
  return <dialog ref={dialog} className="pr-export-dialog" aria-labelledby="pr-export-title"
    onCancel={event => { event.preventDefault(); onCancel() }} onKeyDown={onKeyDown}>
    <div className="pr-export-dialog-heading">
      <h2 id="pr-export-title">Export</h2>
      <div className="pr-export-heading-actions">
        <button className="pr-btn pr-icon-btn" title="Check model runtime" aria-label="Check optional models" disabled={checking} onClick={() => checkModels(true)}><Icon name="refresh" /></button>
        <button className="pr-btn pr-icon-btn" aria-label="Close export dialog" title="Cancel" onClick={onCancel}><Icon name="close" /></button>
      </div>
    </div>
    {!!missingPages.length && <div className="pr-export-missing">
      <div className="pr-export-missing-heading">Skip {missingPages.length} unrecorded {missingPages.length === 1 ? 'page' : 'pages'}</div>
      <ul aria-label="Pages without recordings">{missingPages.map(page => <li key={page}>Page {page}</li>)}</ul>
    </div>}
    <div className="pr-audio-options">
      <label className="pr-audio-voice" title={models?.checks?.denoise?.message || 'DPDFNet model noise reduction'}>
        <span>AI denoise</span>
        {modelStatus('denoise')}
        <input type="checkbox" aria-label="Noise reduction" checked={denoise === 'model'} disabled={voice || !models?.denoise}
          onChange={event => setDenoise(event.target.checked ? 'model' : 'basic')} />
      </label>
      <label className="pr-audio-voice" title={models?.checks?.seed_vc?.message || 'Seed-VC uses one reference voice for all pages; denoising runs first.'}>
        <span>Unify voice</span>
        {modelStatus('seed_vc')}
        <input type="checkbox" role="switch" aria-label="Unify voice tone" checked={voice} disabled={!models?.seed_vc || !models?.denoise}
          onChange={event => { setVoice(event.target.checked); if (event.target.checked) setDenoise('model') }} />
      </label>
      {voice && <div className="pr-voice-reference">
        <label className="pr-audio-field"><span>Reference</span>
          <select aria-label="Reference voice" value={referencePage} onChange={event => setReferencePage(event.target.value)}>
            {eligible.map(take => <option key={take.take} value={take.page}>Page {take.page}</option>)}
            <option value="upload">Upload audio</option>
          </select>
        </label>
        {referencePage === 'upload' && <label className="pr-reference-upload" title="A voiced sample of 1–25 seconds, under 20 MB"><input type="file" accept="audio/*,.wav,.mp3,.m4a,.flac,.ogg,.webm" aria-label="Upload reference audio"
          onChange={event => {
            const file = event.target.files[0]
            setReference(file && file.size <= 20 * 1024 * 1024 ? file : null)
            setFileError(file?.size > 20 * 1024 * 1024 ? 'Choose an audio file under 20 MB.' : '')
          }} /></label>}
        {fileError && <div className="pr-recording-error" role="alert">{fileError}</div>}
      </div>}
    </div>
    <div className="pr-export-dialog-actions">
      <button ref={cancel} className="pr-btn" onClick={onCancel}>Cancel</button>
      <button className="pr-btn pr-export-confirm" disabled={!canExport} onClick={submit}><Icon name="export" />Export</button>
    </div>
  </dialog>
}
