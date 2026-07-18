const J = async (r) => {
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`
    try { const j = await r.json(); if (j && j.detail) msg = j.detail } catch {}
    throw new Error(msg)
  }
  return r.json()
}
const POST = (url, body) =>
  fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  }).then(J)
const PATCH = (url, body) =>
  fetch(url, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  }).then(J)

export const getState = () => fetch('/api/state').then(J)

// ── account (server mode only; served by the control plane, 404 in local mode) ──
export const whoami = () => fetch('/whoami').then((r) => (r.ok ? r.json() : null)).catch(() => null)
export const logout = () => {
  // real form POST so the browser follows the 303 → /login and the cleared cookie sticks
  const f = document.createElement('form')
  f.method = 'POST'; f.action = '/logout'
  document.body.appendChild(f); f.submit()
}
// detail-aware JSON (surfaces the control plane's HTTPException message)
const JD = J
const JSONHDR = { 'Content-Type': 'application/json' }
export const changePassword = (current, neu) =>
  fetch('/account/password', { method: 'POST', headers: JSONHDR, body: JSON.stringify({ current, new: neu }) }).then(JD)
export const adminListUsers = () => fetch('/admin/users').then(JD)
export const adminCreateUser = (username, password, role) =>
  fetch('/admin/users', { method: 'POST', headers: JSONHDR, body: JSON.stringify({ username, password, role }) }).then(JD)
export const adminResetPassword = (id, neu) =>
  fetch(`/admin/users/${id}/password`, { method: 'POST', headers: JSONHDR, body: JSON.stringify({ new: neu }) }).then(JD)
export const adminSetRole = (id, role) =>
  fetch(`/admin/users/${id}/role`, { method: 'POST', headers: JSONHDR, body: JSON.stringify({ role }) }).then(JD)
export const adminSetLocked = (id, locked) =>
  fetch(`/admin/users/${id}/locked`, { method: 'POST', headers: JSONHDR, body: JSON.stringify({ locked }) }).then(JD)
export const adminForceOffline = (id) =>
  fetch(`/admin/users/${id}/offline`, { method: 'POST', headers: JSONHDR, body: JSON.stringify({}) }).then(JD)
export const adminDeleteUser = (id) => fetch(`/admin/users/${id}`, { method: 'DELETE' }).then(JD)
export const browse = (path) => fetch('/api/browse' + (path ? `?path=${encodeURIComponent(path)}` : '')).then(J)
export const openFile = (path) => POST('/api/open-file', { path })
export const setupWorkdir = () => POST('/api/setup-workdir')
export const compile = () => POST('/api/compile')
export const renderVersion = () => fetch('/api/render-version').then(J)
export const getDocument = (file) => fetch('/api/document' + (file ? `?file=${encodeURIComponent(file)}` : '')).then(J)
export const resolve = (page_no, x, y) => POST('/api/preview/resolve', { page_no, x, y })
export const pageStart = (page_no) => POST('/api/preview/page-start', { page_no })
export const locate = (off) => POST('/api/preview/locate', { off })

export const getNotes = () => fetch('/api/notes').then(J)
export const patchNote = (raw, text) => PATCH('/api/notes', { raw, text })
export const createNote = (slide_line, text, sub_index, sub_total) =>
  POST('/api/notes', { slide_line, text, sub_index, sub_total })
// save a note whether it exists (patch) or not yet (create). Creation is subslide-aware:
// pass the page's subslide index/total so a multi-subslide slide gets a gated note.
export const saveNote = (info, text) =>
  info && info.note_raw
    ? patchNote(info.note_raw, text)
    : createNote(info && info.slide_line, text, info && info.sub_index, info && info.sub_total)
export const notesExportUrl = '/api/notes/export'
export const notesPdfpcUrl = '/api/notes/pdfpc'
export const getSlideMap = () => fetch('/api/slide-map').then(J)

export const getComments = (status, file) => {
  const q = new URLSearchParams()
  if (status) q.set('status', status)
  if (file) q.set('file', file)
  const s = q.toString()
  return fetch('/api/comments' + (s ? `?${s}` : '')).then(J)
}
export const addComments = (items) => POST('/api/comments', items)
export const patchComment = (id, fields) => PATCH(`/api/comments/${id}`, fields)
export const commentEvents = (id) => fetch(`/api/comments/${id}/events`).then(J)
export const markDone = (id) => POST(`/api/comments/${id}/done`)
export const reopen = (id) => POST(`/api/comments/${id}/reopen`)
export const delComment = (id) => fetch(`/api/comments/${id}`, { method: 'DELETE' }).then(J)

export const terminalInfo = () => fetch('/api/terminal/info').then(J)
// `v` is a per-page CONTENT token (hash of that page's bytes, from the backend). Same content
// → same URL → browser cache hit (no refetch); changed content → new URL → fetched once.
// Falls back to Date.now() only in the brief window before the first token arrives.
export const renderUrl = (name, v) => `/api/render/${name}?v=${v ?? Date.now()}`

// ── app state / config ──────────────────────────────────────────────────────
export const getAppState = () => fetch('/api/app/state').then(J)
export const setAppConfig = (config) =>
  fetch('/api/app/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  }).then(J)

// ── projects ────────────────────────────────────────────────────────────────
export const listProjects = () => fetch('/api/projects').then(J)
export const createProject = (name) => POST('/api/projects', { name })
export const renameProject = (id, name) => PATCH(`/api/projects/${encodeURIComponent(id)}`, { name })
export const deleteProject = (id) =>
  fetch(`/api/projects/${encodeURIComponent(id)}`, { method: 'DELETE' }).then(J)
export const copyProject = (id, name) => POST(`/api/projects/${encodeURIComponent(id)}/copy`, { name })
export const openProject = (id) => POST(`/api/projects/${encodeURIComponent(id)}/open`)
export const closeProject = () => POST('/api/projects/close')

// ── file management within project ─────────────────────────────────────────
export const listProjectFiles = () => fetch('/api/project/files').then(J)
export const createProjectFile = (name) => POST('/api/project/files/create', { name })
export const duplicateProjectFile = (path) => POST('/api/project/files/duplicate', { path })
export const deleteProjectFile = (path) =>
  fetch('/api/project/files', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  }).then(J)
export const mkdir = (path) => POST('/api/project/files/mkdir', { path })
export const rmdir = (path) =>
  fetch('/api/project/dirs', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  }).then(J)
export const renameItem = (from, to) => PATCH('/api/project/files/rename', { from, to })
export const moveItem = (from, dest) => POST('/api/project/files/move', { from, dest })
export const downloadFileUrl = (path) =>
  `/api/project/files/download?path=${encodeURIComponent(path)}`

// git / vcs (tag-based versions)
export const gitStatus = () => fetch('/api/git/status').then(J)
export const gitVersions = () => fetch('/api/git/versions').then(J)
export const gitCommit = (message = '') => POST('/api/git/commit', { message })
export const gitRestore = (tag) => POST('/api/git/restore', { tag })
export const gitDeleteVersion = (tag) => POST('/api/git/delete', { tag })
export const viewFileUrl = (path) =>
  `/api/project/files/view?path=${encodeURIComponent(path)}`
export const uploadFile = async (file, dest = '') => {
  const fd = new FormData()
  fd.append('file', file)
  const query = dest ? `?dest=${encodeURIComponent(dest)}` : ''
  return fetch('/api/project/files/upload' + query, { method: 'POST', body: fd }).then(J)
}
