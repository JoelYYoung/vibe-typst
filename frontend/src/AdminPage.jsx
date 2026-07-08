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

function ConfirmDialog({ msg, confirmLabel, onConfirm, onCancel }) {
  return (
    <div className="fm-confirm-overlay" onClick={onCancel}>
      <div className="fm-confirm-box" onClick={e => e.stopPropagation()}>
        <div className="fm-confirm-msg">{msg}</div>
        <div className="fm-confirm-actions">
          <button className="mini" onClick={onCancel}>Cancel</button>
          <button className="mini primary warn" onClick={onConfirm}>{confirmLabel || 'Confirm'}</button>
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
  const [menuUserId, setMenuUserId] = useState(null)

  const load = useCallback(async () => {
    try { const r = await api.adminListUsers(); setUsers(r.users || []) }
    catch (e) { toast.error(e.message || 'Failed to load users') }
    finally { setLoading(false) }
  }, [])
  useEffect(() => { load(); api.whoami().then(setMe).catch(() => {}) }, [load])
  useEffect(() => {
    const close = () => setMenuUserId(null)
    window.addEventListener('click', close)
    return () => window.removeEventListener('click', close)
  }, [])

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
    setMenuUserId(null)
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
  async function setLocked(u, locked) {
    setDialog(null)
    try {
      await api.adminSetLocked(u.id, locked)
      toast.success(`${locked ? 'Locked' : 'Unlocked'} “${u.username}”`)
      await load()
    } catch (e) { toast.error(e.message || 'Failed') }
  }
  async function forceOffline(u) {
    setDialog(null)
    try {
      await api.adminForceOffline(u.id)
      toast.success(`Forced “${u.username}” offline`)
      await load()
    } catch (e) { toast.error(e.message || 'Failed') }
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
              <span>User</span><span>Role</span><span>Account</span><span>Workspace</span><span>Created</span><span className="admin-actions-col">Actions</span>
            </div>
            {users.map(u => (
              <div key={u.id} className="admin-row">
                <span className="admin-user">
                  <span className={'usermenu-avatar' + (u.role === 'admin' ? ' admin' : '')}>{(u.username[0] || '?').toUpperCase()}</span>
                  <span className="admin-user-name">{u.username}</span>
                  {me && u.id === me.id && <span className="admin-you">you</span>}
                </span>
                <span><span className={'admin-role ' + u.role}>{u.role}</span></span>
                <span><span className={'admin-state' + (u.locked ? ' locked' : '')}>{u.locked ? 'locked' : 'active'}</span></span>
                <span className={'admin-ws' + (u.running ? ' on' : '')}><span className="admin-ws-dot" />{u.running ? 'running' : 'stopped'}</span>
                <span className="admin-date">{fmtDate(u.created_at)}</span>
                <span className="admin-actions-col">
                  <span className="admin-actions-menu" onClick={e => e.stopPropagation()}>
                    <button className="iconbtn" onClick={() => setMenuUserId(menuUserId === u.id ? null : u.id)} title="More actions" aria-label={`More actions for ${u.username}`}>⋯</button>
                    {menuUserId === u.id && (
                      <div className="admin-action-popover">
                        <button onClick={() => toggleRole(u)}>{u.role === 'admin' ? 'Make user' : 'Make admin'}</button>
                        <button onClick={() => { setMenuUserId(null); setDialog({ kind: 'pw', user: u }) }}>Reset password</button>
                        <button disabled={me && u.id === me.id} onClick={() => { setMenuUserId(null); setDialog({ kind: 'offline', user: u }) }}>Force offline</button>
                        <button disabled={me && u.id === me.id} className={u.locked ? '' : 'danger'} onClick={() => {
                          setMenuUserId(null)
                          u.locked ? setLocked(u, false) : setDialog({ kind: 'lock', user: u })
                        }}>{u.locked ? 'Unlock' : 'Lock'}</button>
                      </div>
                    )}
                  </span>
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
      {dialog?.kind === 'offline' && (
        <ConfirmDialog msg={`Force “${dialog.user.username}” offline? Their sessions are cleared and their workspace container is stopped.`}
          confirmLabel="Force offline" onConfirm={() => forceOffline(dialog.user)} onCancel={() => setDialog(null)} />
      )}
      {dialog?.kind === 'lock' && (
        <ConfirmDialog msg={`Lock “${dialog.user.username}”? They will be signed out, their workspace container will be stopped, and they will not be able to log in until unlocked.`}
          confirmLabel="Lock" onConfirm={() => setLocked(dialog.user, true)} onCancel={() => setDialog(null)} />
      )}
      {dialog?.kind === 'del' && (
        <ConfirmDialog msg={`Delete “${dialog.user.username}”? Their account is removed and their workspace container stopped (files on disk are kept).`}
          confirmLabel="Delete" onConfirm={() => del(dialog.user)} onCancel={() => setDialog(null)} />
      )}
    </div>
  )
}
