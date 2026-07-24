import React, { useCallback, useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import { toast } from './Toaster.jsx'
import { projectType } from './projectTypes.js'
import {
  canSubmitProjectCreation,
  pdfFileFromSelection,
  resetProjectCreation as resetProjectCreationState,
  switchProjectCreationType,
} from './projectCreation.js'
import { canonicalProjectFromOpen } from './projectRouting.js'

function fmtDate(iso) {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
  } catch { return '' }
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

function PromptDialog({ label, defaultValue, onConfirm, onCancel }) {
  const [value, setValue] = useState(defaultValue || '')
  const inputRef = useRef(null)
  useEffect(() => { inputRef.current?.select() }, [])
  return (
    <div className="fm-confirm-overlay" onClick={onCancel}>
      <div className="fm-confirm-box" onClick={e => e.stopPropagation()}>
        <div className="fm-confirm-msg">{label}</div>
        <input
          ref={inputRef}
          className="fm-prompt-input"
          value={value}
          onChange={e => setValue(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && value.trim()) onConfirm(value.trim())
            if (e.key === 'Escape') onCancel()
          }}
        />
        <div className="fm-confirm-actions">
          <button className="mini" onClick={onCancel}>Cancel</button>
          <button className="mini primary" disabled={!value.trim()} onClick={() => onConfirm(value.trim())}>Duplicate</button>
        </div>
      </div>
    </div>
  )
}

// change-your-own-password dialog (server mode)
function ChangePasswordDialog({ onClose }) {
  const [cur, setCur] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const mismatch = confirm.length > 0 && next !== confirm
  async function submit(e) {
    e.preventDefault()
    if (!cur || next.length < 6 || next !== confirm) return
    setBusy(true)
    try { await api.changePassword(cur, next); toast.success('Password changed'); onClose() }
    catch (e2) { toast.error(e2.message || 'Failed') }
    finally { setBusy(false) }
  }
  return (
    <div className="fm-confirm-overlay" onClick={onClose}>
      <form className="fm-confirm-box" onClick={e => e.stopPropagation()} onSubmit={submit}>
        <div className="fm-confirm-msg">Change your password</div>
        <input className="fm-prompt-input" type="password" placeholder="current password" value={cur} onChange={e => setCur(e.target.value)} autoFocus />
        <input className="fm-prompt-input" type="password" placeholder="new password (min 6)" value={next} onChange={e => setNext(e.target.value)} />
        <input className="fm-prompt-input" type="password" placeholder="confirm new password" value={confirm} onChange={e => setConfirm(e.target.value)} />
        {mismatch && <div className="fp-err">passwords don’t match</div>}
        <div className="fm-confirm-actions">
          <button type="button" className="mini" onClick={onClose}>Cancel</button>
          <button type="submit" className="mini primary" disabled={busy || !cur || next.length < 6 || next !== confirm}>Change</button>
        </div>
      </form>
    </div>
  )
}

// Toggleable account widget (server mode only): username, change password, admin, log out.
// Renders nothing in local mode (where /whoami isn't served).
function UserMenu({ onOpenAdmin }) {
  const [user, setUser] = useState(undefined) // undefined = loading, null = none
  const [open, setOpen] = useState(false)
  const [pwOpen, setPwOpen] = useState(false)
  const ref = useRef(null)

  useEffect(() => { api.whoami().then((r) => setUser(r && r.username ? r : null)) }, [])
  useEffect(() => {
    if (!open) return
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  if (!user) return null
  const initial = (user.username[0] || '?').toUpperCase()
  return (
    <div className="usermenu" ref={ref}>
      <button className={'usermenu-btn' + (open ? ' open' : '')} onClick={() => setOpen((o) => !o)} title="Account">
        <span className="usermenu-avatar">{initial}</span>
        <span className="usermenu-name">{user.username}</span>
        <span className="fp-caret">▾</span>
      </button>
      {open && (
        <div className="usermenu-pop">
          <div className="usermenu-head">
            <span className="usermenu-avatar lg">{initial}</span>
            <div className="usermenu-meta">
              <div className="usermenu-name-lg">{user.username}</div>
              <div className="usermenu-sub">{user.role === 'admin' ? 'Administrator' : 'Signed in'}</div>
            </div>
          </div>
          <button className="usermenu-item" onClick={() => { setOpen(false); setPwOpen(true) }}>Change password</button>
          {user.role === 'admin' && (
            <button className="usermenu-item" onClick={() => { setOpen(false); onOpenAdmin && onOpenAdmin() }}>Manage users</button>
          )}
          <button className="usermenu-logout" onClick={() => api.logout()}>Log out</button>
        </div>
      )}
      {pwOpen && <ChangePasswordDialog onClose={() => setPwOpen(false)} />}
    </div>
  )
}

function ProjectCard({ project, onOpen, onRename, onDelete, onCopy }) {
  const [menu, setMenu] = useState(false)
  const [menuPos, setMenuPos] = useState({ top: 0, right: 0 })
  const [renaming, setRenaming] = useState(false)
  const [draft, setDraft] = useState('')
  const menuBtnRef = useRef(null)
  const dropdownRef = useRef(null)

  useEffect(() => {
    if (!menu) return
    function close(e) {
      // The dropdown is position:fixed (outside the button), so close only when the click
      // is outside BOTH — otherwise mousedown closes the menu before a menu item's click fires.
      const inBtn = menuBtnRef.current && menuBtnRef.current.contains(e.target)
      const inMenu = dropdownRef.current && dropdownRef.current.contains(e.target)
      if (!inBtn && !inMenu) setMenu(false)
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [menu])

  function openMenu(e) {
    e.stopPropagation()
    const rect = menuBtnRef.current.getBoundingClientRect()
    setMenuPos({ top: rect.bottom + 4, right: window.innerWidth - rect.right })
    setMenu(v => !v)
  }

  function startRename() { setMenu(false); setDraft(project.name); setRenaming(true) }
  function cancelRename() { setRenaming(false); setDraft('') }
  async function submitRename(e) {
    e.preventDefault()
    if (!draft.trim() || draft.trim() === project.name) { cancelRename(); return }
    await onRename(project.id, draft.trim())
    setRenaming(false)
  }

  return (
    <div className="project-card">
      <div className="project-card-body" onClick={() => !renaming && onOpen(project.id)} title="Open project">
        <div className="project-icon">✦</div>
        {renaming ? (
          <form onSubmit={submitRename} onClick={(e) => e.stopPropagation()} style={{ flex: 1 }}>
            <input
              className="rename-input"
              autoFocus
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => e.key === 'Escape' && cancelRename()}
              onBlur={cancelRename}
            />
          </form>
        ) : (
          <div className="project-info">
            <div className="project-name-row">
              <div className="project-name">{project.name}</div>
              <span className={`project-type-badge ${projectType(project)}`}>{projectType(project) === 'pdf' ? 'PDF' : 'Typst'}</span>
            </div>
            {project.created && <div className="project-date">{fmtDate(project.created)}</div>}
          </div>
        )}
      </div>
      {!renaming && (
        <div className="project-menu-wrap">
          <button
            ref={menuBtnRef}
            className="project-menu-btn"
            title="Project options"
            onClick={openMenu}
          >⋯</button>
          {menu && (
            <div
              ref={dropdownRef}
              className="project-dropdown"
              style={{ position: 'fixed', top: menuPos.top, right: menuPos.right, left: 'auto', bottom: 'auto' }}
            >
              <button onClick={startRename}>Rename</button>
              <button onClick={() => { setMenu(false); onCopy(project.id, project.name) }}>Duplicate</button>
              <button className="danger" onClick={() => { setMenu(false); onDelete(project.id, project.name) }}>Delete</button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function ProjectsPage({ onOpen, onOpenAdmin }) {
  const [projects, setProjects] = useState([])
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [newType, setNewType] = useState('typst')
  const [pdfFile, setPdfFile] = useState(null)
  const [busy, setBusy] = useState(false)
  const [confirm, setConfirm] = useState(null)   // {msg, onConfirm}
  const [copyPrompt, setCopyPrompt] = useState(null)  // {id, defaultName}
  const newInputRef = useRef(null)
  const pdfInputRef = useRef(null)

  const load = useCallback(async () => {
    try {
      const r = await api.listProjects()
      setProjects(r.projects || [])
    } catch (e) {
      toast.error(e.message || 'Failed to load projects')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])
  useEffect(() => { if (creating) newInputRef.current?.focus() }, [creating])

  function resetNewProject() {
    const reset = resetProjectCreationState()
    setCreating(false)
    setNewName(reset.name)
    setNewType(reset.type)
    setPdfFile(reset.file)
    if (pdfInputRef.current) pdfInputRef.current.value = ''
  }

  function selectProjectType(type) {
    const next = switchProjectCreationType(
      { name: newName, type: newType, file: pdfFile }, type,
    )
    setNewType(next.type)
    setPdfFile(next.file)
    if (type !== 'pdf') {
      if (pdfInputRef.current) pdfInputRef.current.value = ''
    }
  }

  async function handleCreate(e) {
    e.preventDefault()
    const name = newName.trim()
    if (!canSubmitProjectCreation({ name, type: newType, file: pdfFile, busy })) return
    setBusy(true)
    try {
      if (newType === 'pdf') await api.createPdfProject(name, pdfFile)
      else await api.createProject(name)
      resetNewProject()
      await load()
    } catch (err) {
      toast.error(err.message || 'Failed to create project')
    } finally { setBusy(false) }
  }

  async function handleOpen(id) {
    setBusy(true)
    try {
      const result = await api.openProject(id)
      onOpen(canonicalProjectFromOpen(result))
    } catch (err) {
      toast.error(err.message || 'Failed to open project')
    } finally { setBusy(false) }
  }

  async function handleRename(id, name) {
    try {
      await api.renameProject(id, name)
      await load()
    } catch (err) {
      toast.error(err.message || 'Failed to rename project')
    }
  }

  function handleDelete(id, name) {
    setConfirm({
      msg: `Delete "${name}"? This will permanently remove all project files.`,
      onConfirm: async () => {
        setConfirm(null)
        try {
          await api.deleteProject(id)
          await load()
          toast.success(`Deleted "${name}"`)
        } catch (err) {
          toast.error(err.message || 'Failed to delete project')
        }
      }
    })
  }

  function handleCopy(id, name) {
    setCopyPrompt({ id, defaultName: `${name} copy` })
  }

  async function doCopy(id, newName) {
    setCopyPrompt(null)
    try {
      await api.copyProject(id, newName)
      await load()
      toast.success(`Duplicated as "${newName}"`)
    } catch (err) {
      toast.error(err.message || 'Failed to duplicate project')
    }
  }

  return (
    <div className="projects-bg">
      <header className="projects-header">
        <div className="brand">✦ Vibe Typst</div>
        <span className="grow" />
        <UserMenu onOpenAdmin={onOpenAdmin} />
      </header>

      <main className="projects-main">
        <div className="projects-title-row">
          <h1>Projects</h1>
          <button className="primary" onClick={() => { setCreating(true) }}
            disabled={creating}>+ New project</button>
        </div>

        {creating && (
          <form className="new-project-form" onSubmit={handleCreate}>
            <input
              ref={newInputRef}
              className="new-project-input"
              placeholder="Project name"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => e.key === 'Escape' && resetNewProject()}
              maxLength={128}
            />
            <div className="new-project-type" role="group" aria-label="Project type">
              <button type="button" className={newType === 'typst' ? 'selected' : ''} onClick={() => selectProjectType('typst')}>Typst</button>
              <button type="button" className={newType === 'pdf' ? 'selected' : ''} onClick={() => selectProjectType('pdf')}>PDF</button>
            </div>
            {newType === 'pdf' && (
              <input
                ref={pdfInputRef}
                className="new-project-file"
                type="file"
                accept=".pdf,application/pdf"
                onChange={(e) => {
                  setPdfFile(pdfFileFromSelection(e.target.files))
                }}
              />
            )}
            <button type="submit" className="primary" disabled={!canSubmitProjectCreation({ name: newName, type: newType, file: pdfFile, busy })}>
              {busy ? 'Creating…' : 'Create'}
            </button>
            <button type="button" onClick={resetNewProject}>Cancel</button>
          </form>
        )}

        {loading ? (
          <div className="projects-loading">Loading…</div>
        ) : projects.length === 0 && !creating ? (
          <div className="projects-empty">
            <p>No projects yet.</p>
            <button className="primary" onClick={() => setCreating(true)}>Create your first project</button>
          </div>
        ) : (
          <div className="projects-grid">
            {projects.map((p) => (
              <ProjectCard
                key={p.id}
                project={p}
                onOpen={handleOpen}
                onRename={handleRename}
                onDelete={handleDelete}
                onCopy={handleCopy}
              />
            ))}
          </div>
        )}
      </main>

      {confirm && <ConfirmDialog msg={confirm.msg} onConfirm={confirm.onConfirm} onCancel={() => setConfirm(null)} />}
      {copyPrompt && (
        <PromptDialog
          label="Name for the duplicate project:"
          defaultValue={copyPrompt.defaultName}
          onConfirm={(name) => doCopy(copyPrompt.id, name)}
          onCancel={() => setCopyPrompt(null)}
        />
      )}
    </div>
  )
}
