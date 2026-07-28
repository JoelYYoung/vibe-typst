import React, { useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import { toast } from './Toaster.jsx'
import {
  TOKEN_EXPIRIES,
  TOKEN_PRESETS,
  displayTokenPrefix,
  expiryTimestamp,
} from './tokenManagement.js'

function formatTimestamp(value) {
  if (value === null || value === undefined) return 'Never'
  const date = new Date(Number(value) * 1000)
  return Number.isNaN(date.getTime())
    ? 'Unknown'
    : date.toLocaleString()
}

function tokenStatus(token, now = Date.now() / 1000) {
  if (token.revoked_at) return 'Revoked'
  if (token.expires_at !== null && token.expires_at <= now) return 'Expired'
  return 'Active'
}

export default function AccountTokensDialog({ onClose }) {
  const [tokens, setTokens] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [name, setName] = useState('')
  const [preset, setPreset] = useState('editor')
  const [expiry, setExpiry] = useState('90d')
  const [busy, setBusy] = useState(false)
  const [secret, setSecret] = useState('')
  const [revokeId, setRevokeId] = useState(null)
  const nameRef = useRef(null)

  async function load() {
    setLoading(true)
    setError('')
    try {
      const result = await api.listAccountTokens()
      setTokens(Array.isArray(result.tokens) ? result.tokens : [])
    } catch (loadError) {
      setError(loadError.message || 'Could not load personal access tokens')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    const timer = setTimeout(() => nameRef.current?.focus(), 0)
    return () => clearTimeout(timer)
  }, [])

  useEffect(() => {
    function onKeyDown(event) {
      if (event.key === 'Escape' && !secret && !busy) onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [busy, onClose, secret])

  function close() {
    setSecret('')
    onClose()
  }

  async function createToken(event) {
    event.preventDefault()
    if (!name.trim() || busy) return
    setBusy(true)
    setError('')
    try {
      const result = await api.createAccountToken(
        name.trim(),
        preset,
        expiryTimestamp(expiry),
      )
      setSecret(typeof result.secret === 'string' ? result.secret : '')
      setTokens((current) => [
        result.token,
        ...current.filter((item) => item.id !== result.token?.id),
      ].filter(Boolean))
      setName('')
    } catch (createError) {
      setError(createError.message || 'Could not create token')
    } finally {
      setBusy(false)
    }
  }

  async function copySecret() {
    try {
      await navigator.clipboard.writeText(secret)
      toast.success('Token copied')
    } catch {
      toast.error('Could not copy token')
    }
  }

  async function revoke(token) {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      await api.revokeAccountToken(token.id)
      setTokens((current) => current.map((item) => (
        item.id === token.id
          ? { ...item, revoked_at: Date.now() / 1000 }
          : item
      )))
      setRevokeId(null)
      toast.success(`Revoked “${token.name}”`)
    } catch (revokeError) {
      setError(revokeError.message || 'Could not revoke token')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="token-dialog-backdrop" onMouseDown={close}>
      <section
        className="token-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="token-dialog-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="token-dialog-head">
          <div>
            <h2 id="token-dialog-title">Personal access tokens</h2>
            <p>Connect a remote AI client to your projects without sharing your password.</p>
          </div>
          <button className="token-dialog-close" type="button" aria-label="Close token settings" onClick={close}>×</button>
        </header>

        <div className="token-dialog-body">
          {secret && (
            <section className="token-secret-panel" aria-live="polite">
              <strong>Copy this token now</strong>
              <p>It is shown only once. Store it in your AI client’s secret store.</p>
              <code data-testid="token-secret">{secret}</code>
              <div className="token-secret-actions">
                <button type="button" className="mini primary" data-action="copy-token-secret" onClick={copySecret}>Copy token</button>
                <button type="button" className="mini" data-action="close-token-secret" onClick={() => setSecret('')}>I’ve saved it</button>
              </div>
            </section>
          )}

          <form className="token-create-form" onSubmit={createToken}>
            <div className="token-section-title">Create a token</div>
            <label>
              <span>Name</span>
              <input
                ref={nameRef}
                name="token-name"
                value={name}
                maxLength={128}
                placeholder="e.g. remote-codex"
                onChange={(event) => setName(event.target.value)}
              />
            </label>
            <label>
              <span>Access</span>
              <select name="token-preset" value={preset} onChange={(event) => setPreset(event.target.value)}>
                {TOKEN_PRESETS.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
              <small>{TOKEN_PRESETS.find((option) => option.value === preset)?.description}</small>
            </label>
            <label>
              <span>Expires</span>
              <select name="token-expiry" value={expiry} onChange={(event) => setExpiry(event.target.value)}>
                {TOKEN_EXPIRIES.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>
            <button type="submit" className="primary" disabled={busy || !name.trim()}>
              {busy ? 'Creating…' : 'Create token'}
            </button>
          </form>

          {error && <div className="token-error" role="alert">{error}</div>}

          <section className="token-list-section">
            <div className="token-section-title">Existing tokens</div>
            {loading ? (
              <div className="token-empty">Loading…</div>
            ) : tokens.length === 0 ? (
              <div className="token-empty">No personal access tokens yet.</div>
            ) : (
              <div className="token-list">
                {tokens.map((token) => {
                  const status = tokenStatus(token)
                  const confirming = revokeId === token.id
                  return (
                    <article className="token-row" key={token.id}>
                      <div className="token-row-main">
                        <div className="token-row-title">
                          <strong>{token.name}</strong>
                          <span className={`token-status ${status.toLowerCase()}`}>{status}</span>
                        </div>
                        <code>{displayTokenPrefix(token)}</code>
                        <div className="token-meta">
                          <span>{token.preset === 'editor' ? 'Editor' : 'Viewer'}</span>
                          <span>Created {formatTimestamp(token.created_at)}</span>
                          <span>Expires {formatTimestamp(token.expires_at)}</span>
                          <span>Last used {token.last_used_at ? formatTimestamp(token.last_used_at) : 'Never'}</span>
                        </div>
                      </div>
                      <div className="token-row-actions">
                        {confirming ? (
                          <>
                            <span>Revoke now?</span>
                            <button type="button" className="mini warn" data-action="confirm-revoke-token" disabled={busy} onClick={() => revoke(token)}>Revoke</button>
                            <button type="button" className="mini" disabled={busy} onClick={() => setRevokeId(null)}>Cancel</button>
                          </>
                        ) : (
                          <button
                            type="button"
                            className="mini warn"
                            data-action="revoke-token"
                            disabled={status !== 'Active' || busy}
                            onClick={() => setRevokeId(token.id)}
                          >
                            Revoke
                          </button>
                        )}
                      </div>
                    </article>
                  )
                })}
              </div>
            )}
          </section>
        </div>
      </section>
    </div>
  )
}
