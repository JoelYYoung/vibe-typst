import React, { useState } from 'react'
import * as api from './api.js'

export default function OnboardingPage({ onDone }) {
  const [dir, setDir] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const defaultSuggestion = (() => {
    // Suggest a sensible default based on the OS (client-side heuristic — not definitive).
    // On Windows the user agent includes "Windows"; on Mac it includes "Mac".
    const ua = navigator.userAgent || ''
    if (ua.includes('Windows')) return 'C:\\Users\\YourName\\Vibe Typst'
    return '~/Vibe Typst'
  })()

  async function handleSubmit(e) {
    e.preventDefault()
    const trimmed = dir.trim()
    if (!trimmed) { setError('Please enter a directory path.'); return }
    setSaving(true)
    setError('')
    try {
      await api.setAppConfig({ projects_root: trimmed })
      onDone()
    } catch (err) {
      setError(err.message || 'Failed to set directory. Make sure the path is valid and writable.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="onboarding-bg">
      <div className="onboarding-card">
        <div className="brand-lg">✦ Vibe Typst</div>
        <h1>Welcome</h1>
        <p>
          Choose a folder where your Typst projects will be stored.
          Vibe Typst will create one subfolder per project inside this directory.
        </p>
        <form onSubmit={handleSubmit}>
          <label htmlFor="dir-input">Projects folder</label>
          <input
            id="dir-input"
            type="text"
            className="dir-input"
            placeholder={defaultSuggestion}
            value={dir}
            onChange={(e) => { setDir(e.target.value); setError('') }}
            autoFocus
            spellCheck={false}
            autoComplete="off"
          />
          <p className="hint">
            Enter an absolute path (e.g. <code>/home/you/typst-projects</code>).
            The folder will be created if it does not exist.
          </p>
          {error && <p className="form-error">{error}</p>}
          <button type="submit" className="primary wide" disabled={saving || !dir.trim()}>
            {saving ? 'Setting up…' : 'Get started →'}
          </button>
        </form>
      </div>
    </div>
  )
}
