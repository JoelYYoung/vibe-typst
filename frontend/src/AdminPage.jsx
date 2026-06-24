import React, { useCallback, useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import { toast } from './Toaster.jsx'

function fmtDate(ts) {
  if (!ts) return ''
  try { return new Date(ts * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) }
  catch { return '' }
}

// Inline prompt/confirm reused from the projects-page look (fm-confirm-*).
function PromptDialog({ label, password, confirmLabel, onConfirm, onCancel }) {
  const [value, setValue] = useState('')
  const ref = useRef(null)
  useEffect(() => { ref.current?.focus() }, [])
  return (
    <div className="fm-confirm-overlay" onClick={onCancel}>
      <div className="fm-confirm-box" onClick={e => e.stopPropagation()}>
        <div className="fm-confirm-msg">{label}</div>
        <input ref={ref} className="fm-prompt-input" type={password ? 'password' : 'text'} value={value}
          onChange={e => setValue(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && value.trim()) onConfirm(value.trim()); if (e.key === 'Escape') onCancel() }} />
        <div className="fm-confirm-actions">
          <button className="mini" onClick={onCancel}>Cancel</button>
          <button className="mini primary" disabled={!value.trim()} onClick={() => onConfirm(value.trim())}>{confirmLabel || 'OK'}</button>
        </div>
      </div>
    </div>
  )
}

function ConfirmDialog({ msg, onConfirm, onCancel }) {
  return (
    <div className="fm-confirm-overlay" onClick={onCancel}>
      <div className="fm-confirm-box" onClick={e => e.stopPropagation()}>
        <div className="fm-confirm-msg">{msg}</div>
        <div className="fm-confirm-actions">
          <button className="mini" onClick={onCancel}>Cancel</button>
          <button className="mini primary warn" onClick={onConfirm}>Delete</button>
        </div>
      </div>
    </div>
  )
}

export default function AdminPage({ onBack }) {
  const [users, setUsers] = useState([])
  const [me, setMe] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  // create form
  const [nu, setNu] = useState('')
  const [np, setNp] = useState('')
  const [nrole, setNrole] = useState('user')
  const [dialog, setDialog] = useState(null) // {kind, user}

  const load = useCallback(async () => {
    try { const r = await api.adminListUsers(); setUsers(r.users || []) }
    catch (e) { toast.error(e.message || 'Failed to load users') }
    finally { setLoading(false) }
  }, [])
  useEffect(() => { load(); api.whoami().then(setMe).catch(() => {}) }, [load])

  async function create(e) {
    e.preventDefault()
    if (!nu.trim() || !np.trim()) return
    setBusy(true)
    try {
      await api.adminCreateUser(nu.trim(), np.trim(), nrole)
      setNu(''); setNp(''); setNrole('user')
      toast.success(`Created “${nu.trim()}”`)
      await load()
    } catch (e2) { toast.error(e2.message || 'Create failed') }
    finally { setBusy(false) }
  }

  async function toggleRole(u) {
    const role = u.role === 'admin' ? 'user' : 'admin'
    try { await api.adminSetRole(u.id, role); await load() }
    catch (e) { toast.error(e.message || 'Failed') }
  }
  async function resetPw(u, pw) {
    setDialog(null)
    try { await api.adminResetPassword(u.id, pw); toast.success(`Password reset for “${u.username}”`) }
    catch (e) { toast.error(e.message || 'Failed') }
  }
  async function del(u) {
    setDialog(null)
    try { await api.adminDeleteUser(u.id); toast.success(`Deleted “${u.username}”`); await load() }
    catch (e) { toast.error(e.message || 'Failed') }
  }

  return (
    <div className="projects-bg">
      <header className="projects-header">
        <button className="back-btn" onClick={onBack} title="Back to projects">← Projects</button>
        <span className="grow" />
        <div className="brand">✦ Vibe Typst</div>
      </header>

      <main className="projects-main admin-main">
        <div className="projects-title-row">
          <h1>Users</h1>
          {!loading && <span className="admin-count">{users.length}</span>}
        </div>
        <p className="admin-subtitle">Invite-only — accounts can only be created here by an admin.</p>

        <div className="admin-card">
          <div className="admin-card-head">＋ Invite a user</div>
          <form className="admin-create" onSubmit={create}>
            <label className="admin-field">
              <span>Username</span>
              <input placeholder="e.g. alice" value={nu} onChange={e => setNu(e.target.value)} autoCapitalize="off" autoCorrect="off" />
            </label>
            <label className="admin-field">
              <span>Initial password</span>
              <input placeholder="min 6 characters" type="password" value={np} onChange={e => setNp(e.target.value)} />
            </label>
            <div className="admin-field admin-field-role">
              <span>Role</span>
              <div className="seg" role="group" aria-label="Role">
                <button type="button" className={'seg-opt' + (nrole === 'user' ? ' on' : '')} onClick={() => setNrole('user')}>User</button>
                <button type="button" className={'seg-opt' + (nrole === 'admin' ? ' on' : '')} onClick={() => setNrole('admin')}>Admin</button>
              </div>
            </div>
            <button type="submit" className="primary" disabled={busy || !nu.trim() || np.trim().length < 6}>Create user</button>
          </form>
        </div>

        {loading ? (
          <div className="projects-loading">Loading…</div>
        ) : (
          <div className="admin-table">
            <div className="admin-row admin-head">
              <span>User</span><span>Role</span><span>Workspace</span><span>Created</span><span className="admin-actions-col">Actions</span>
            </div>
            {users.map(u => (
              <div key={u.id} className="admin-row">
                <span className="admin-user">
                  <span className={'usermenu-avatar' + (u.role === 'admin' ? ' admin' : '')}>{(u.username[0] || '?').toUpperCase()}</span>
                  <span className="admin-user-name">{u.username}</span>
                  {me && u.id === me.id && <span className="admin-you">you</span>}
                </span>
                <span><span className={'admin-role ' + u.role}>{u.role}</span></span>
                <span className={'admin-ws' + (u.running ? ' on' : '')}><span className="admin-ws-dot" />{u.running ? 'running' : 'stopped'}</span>
                <span className="admin-date">{fmtDate(u.created_at)}</span>
                <span className="admin-actions-col">
                  <button className="ghostbtn" onClick={() => toggleRole(u)} title={u.role === 'admin' ? 'Demote to user' : 'Promote to admin'}>{u.role === 'admin' ? 'Make user' : 'Make admin'}</button>
                  <button className="ghostbtn" onClick={() => setDialog({ kind: 'pw', user: u })}>Reset password</button>
                  <button className="ghostbtn danger" onClick={() => setDialog({ kind: 'del', user: u })} title="Delete user">Delete</button>
                </span>
              </div>
            ))}
          </div>
        )}
      </main>

      {dialog?.kind === 'pw' && (
        <PromptDialog label={`New password for “${dialog.user.username}”:`} password confirmLabel="Reset"
          onConfirm={(pw) => resetPw(dialog.user, pw)} onCancel={() => setDialog(null)} />
      )}
      {dialog?.kind === 'del' && (
        <ConfirmDialog msg={`Delete “${dialog.user.username}”? Their account is removed and their workspace container stopped (files on disk are kept).`}
          onConfirm={() => del(dialog.user)} onCancel={() => setDialog(null)} />
      )}
    </div>
  )
}
