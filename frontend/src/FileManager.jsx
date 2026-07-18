import React, { useCallback, useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import { toast } from './Toaster.jsx'
import { canSaveVersion } from './versioning.js'

const VIEWABLE_EXTS = new Set(['pdf', 'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'])
const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'])
const MD_EXTS = new Set(['md', 'txt'])

function fileExt(name) {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i + 1).toLowerCase() : ''
}

function fmtSize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function buildTree(items) {
  const nodeMap = {}
  items.forEach(item => {
    nodeMap[item.path] = item.type === 'dir' ? { ...item, children: [] } : { ...item }
  })
  const roots = []
  items.forEach(item => {
    const parts = item.path.split('/')
    const parentPath = parts.slice(0, -1).join('/')
    const node = nodeMap[item.path]
    if (!parentPath) {
      roots.push(node)
    } else if (nodeMap[parentPath]) {
      nodeMap[parentPath].children.push(node)
    }
  })
  function sortNodes(nodes) {
    nodes.sort((a, b) => {
      if (a.type !== b.type) return a.type === 'dir' ? -1 : 1
      return a.name.localeCompare(b.name)
    })
    nodes.forEach(n => n.children && sortNodes(n.children))
  }
  sortNodes(roots)
  return roots
}

// flat list of paths in display order (DFS) — used for Shift-range selection
function flattenPaths(nodes, out = []) {
  for (const n of nodes) { out.push(n.path); if (n.children) flattenPaths(n.children, out) }
  return out
}

function ConfirmDialog({ msg, confirmLabel, onConfirm, onCancel }) {
  return (
    <div className="fm-confirm-overlay" onClick={onCancel}>
      <div className="fm-confirm-box" onClick={e => e.stopPropagation()}>
        <div className="fm-confirm-msg">{msg}</div>
        <div className="fm-confirm-actions">
          <button className="mini" onClick={onCancel}>Cancel</button>
          <button className="mini primary warn" onClick={onConfirm}>{confirmLabel || 'Delete'}</button>
        </div>
      </div>
    </div>
  )
}

function MdEditor({ item, onClose }) {
  const [text, setText] = useState(null)
  const [saving, setSaving] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [orig, setOrig] = useState('')

  useEffect(() => {
    fetch(`/api/project/files/view?path=${encodeURIComponent(item.path)}`)
      .then(r => r.text()).then(t => { setText(t); setOrig(t) }).catch(() => setText(''))
  }, [item.path])

  async function handleSave() {
    setSaving(true)
    try {
      await fetch('/api/project/files/write', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: item.path, content: text }),
      })
      setOrig(text); setDirty(false)
    } finally { setSaving(false) }
  }

  return (
    <div className="fm-viewer-overlay" onClick={onClose}>
      <div className="fm-viewer-modal fm-md-modal" onClick={e => e.stopPropagation()}>
        <div className="fm-viewer-head">
          <span className="fm-viewer-name">{item.name}</span>
          <span className="fm-grow" />
          <button className="mini primary" disabled={saving || !dirty} onClick={handleSave}>
            {saving ? '…' : 'Save'}
          </button>
          {dirty && <button className="mini" onClick={() => { setText(orig); setDirty(false) }}>Revert</button>}
          <a className="fm-action" href={api.downloadFileUrl(item.path)} download={item.name} title="Download">⬇</a>
          <button className="iconbtn" onClick={onClose} title="Close">✕</button>
        </div>
        <div className="fm-md-body">
          {text === null ? <div style={{padding:16,color:'#888'}}>Loading…</div> : (
            <textarea
              className="fm-md-textarea"
              value={text}
              onChange={e => { setText(e.target.value); setDirty(e.target.value !== orig) }}
              spellCheck={false}
            />
          )}
        </div>
      </div>
    </div>
  )
}

function TreeRow({ item, depth, activeFile, mainFile, onOpenFile, onViewFile, onEditMd, onReload, setBusy, setError, setConfirm, dnd }) {
  const [expanded, setExpanded] = useState(true)
  const [renaming, setRenaming] = useState(false)
  const [renameVal, setRenameVal] = useState(item.name)
  const [dropHover, setDropHover] = useState(false)
  const renameRef = useRef(null)
  const isSelected = dnd.selected.has(item.path)

  useEffect(() => { if (renaming) { renameRef.current?.focus(); renameRef.current?.select() } }, [renaming])

  const activeFilename = activeFile ? activeFile.split('/').pop().split('\\').pop() : ''
  const isActive = item.type === 'file' && item.name === activeFilename
  const isMain = item.type === 'file' && item.name === mainFile
  const ext = item.type === 'file' ? fileExt(item.name) : ''
  const isViewable = VIEWABLE_EXTS.has(ext)
  const isImage = IMAGE_EXTS.has(ext)

  async function doRename(e) {
    e.preventDefault()
    const newName = renameVal.trim()
    if (!newName || newName === item.name) { setRenaming(false); return }
    setBusy(true)
    try {
      await api.renameItem(item.path, newName)
      setRenaming(false)
      onReload()
    } catch (err) {
      setError(err.message || 'Rename failed')
      setRenaming(false)
      setRenameVal(item.name)
    } finally { setBusy(false) }
  }

  function doDelete() {
    const msg = item.type === 'dir'
      ? `Delete folder "${item.name}" and all its contents?`
      : `Delete "${item.name}"?`
    setConfirm({ msg, onConfirm: async () => {
      setConfirm(null)
      setBusy(true)
      try {
        if (item.type === 'dir') {
          await api.rmdir(item.path)
        } else {
          await api.deleteProjectFile(item.path)
        }
        onReload()
      } catch (err) {
        setError(err.message || 'Delete failed')
      } finally { setBusy(false) }
    }})
  }

  const isMd = MD_EXTS.has(ext)

  async function doOpen() {
    if (item.type !== 'file') return
    if (isViewable) { onViewFile(item); return }
    if (isMd) { onEditMd(item); return }
    if (!item.is_typ) return
    setBusy(true)
    try {
      const r = await api.openFile(item.abs_path)
      onOpenFile && onOpenFile(r)
    } catch (err) {
      setError(err.message || 'Failed to open file')
    } finally { setBusy(false) }
  }

  const indent = depth * 14
  const canRename = true  // all files and dirs can be renamed
  const isClickable = item.is_typ || isViewable || isMd
  const clickTitle = item.type === 'file'
    ? (isClickable ? 'Click to open · double-click to rename' : 'Double-click to rename')
    : (expanded ? 'Collapse' : 'Expand')

  // Drag-to-move: dragging an item carries its path (or the whole selection if it's part of
  // one). Folders are drop targets; dropping moves the dragged item(s) into them.
  function onDragStart(e) {
    if (renaming) { e.preventDefault(); return }
    const paths = isSelected && dnd.selected.size > 1 ? [...dnd.selected] : [item.path]
    e.dataTransfer.setData('application/x-fm-move', JSON.stringify(paths))
    e.dataTransfer.effectAllowed = 'move'
  }
  const isDropTarget = item.type === 'dir'
  function onDragOver(e) {
    if (!isDropTarget) return
    const types = [...e.dataTransfer.types]
    const isUpload = types.includes('Files')
    const isMove = types.includes('application/x-fm-move')
    if (!isUpload && !isMove) return
    e.preventDefault(); e.stopPropagation()
    e.dataTransfer.dropEffect = isUpload ? 'copy' : 'move'
    if (isUpload) dnd.clearRootUploadHover()
    if (!dropHover) setDropHover(true)
  }
  function onDrop(e) {
    if (!isDropTarget) return
    if (e.dataTransfer.files && e.dataTransfer.files.length) {
      e.preventDefault(); e.stopPropagation(); setDropHover(false); setExpanded(true)
      dnd.uploadInto(e.dataTransfer.files, item.path)
      return
    }
    const raw = e.dataTransfer.getData('application/x-fm-move')
    if (!raw) return
    e.preventDefault(); e.stopPropagation(); setDropHover(false); setExpanded(true)
    try { dnd.moveInto(JSON.parse(raw), item.path) } catch {}
  }

  return (
    <>
      <div
        className={'fm-row' + (isActive ? ' active' : '') + (isMain ? ' fm-main' : '') + (isSelected ? ' fm-selected' : '') + (dropHover ? ' fm-drop' : '')}
        style={{ paddingLeft: 10 + indent }}
        draggable={!renaming && !isMain}
        onDragStart={onDragStart}
        onDragOver={onDragOver}
        onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setDropHover(false) }}
        onDrop={onDrop}
      >
        {dnd.selMode && !isMain && (
          <input
            type="checkbox"
            className="fm-rowcheck"
            checked={isSelected}
            onClick={(e) => { e.stopPropagation(); if (e.shiftKey) dnd.selectRange(item.path); else dnd.toggleSelect(item.path) }}
            onChange={() => {}}
            title="select (⇧ for a range)"
          />
        )}
        {item.type === 'dir' ? (
          <button className="fm-toggle" onClick={() => setExpanded(v => !v)} title={expanded ? 'Collapse' : 'Expand'}>
            {expanded ? '▾' : '▸'}
          </button>
        ) : (
          <span className="fm-indent" />
        )}

        <span className="fm-icon">
          {item.type === 'dir' ? '📁' : isImage ? '🖼' : ext === 'pdf' ? '📑' : isMd ? '📝' : item.is_typ ? '📄' : '📎'}
        </span>

        {renaming ? (
          <form className="fm-rename-form" onSubmit={doRename}>
            <input
              ref={renameRef}
              className="fm-rename-input"
              value={renameVal}
              onChange={e => setRenameVal(e.target.value)}
              onKeyDown={e => e.key === 'Escape' && (setRenaming(false), setRenameVal(item.name))}
              onBlur={() => { setRenaming(false); setRenameVal(item.name) }}
            />
          </form>
        ) : (
          <button
            className={'fm-name' + (isClickable ? ' clickable' : '')}
            onClick={(e) => {
              if (dnd.selMode && !isMain) { e.preventDefault(); if (e.shiftKey) dnd.selectRange(item.path); else dnd.toggleSelect(item.path) }
              else { doOpen() }
            }}
            onDoubleClick={() => { if (!dnd.selMode) { setRenaming(true); setRenameVal(item.name) } }}
            title={dnd.selMode ? 'click to select' : clickTitle}
          >
            {isActive && <span className="fm-active-dot" />}
            {item.name}
          </button>
        )}

        <span className="fm-grow" />

        {item.type === 'file' && (
          <>
            <span className="fm-size">{fmtSize(item.size)}</span>
            <a className="fm-action" href={api.downloadFileUrl(item.path)} download={item.name} title="Download">⬇</a>
          </>
        )}
        {!isMain && (
          <button className="fm-action del" onClick={doDelete} title={item.type === 'dir' ? 'Delete folder' : 'Delete file'}>✕</button>
        )}
      </div>

      {item.type === 'dir' && expanded && item.children && item.children.map(child => (
        <TreeRow
          key={child.path}
          item={child}
          depth={depth + 1}
          activeFile={activeFile}
          mainFile={mainFile}
          onOpenFile={onOpenFile}
          onViewFile={onViewFile}
          onEditMd={onEditMd}
          onReload={onReload}
          setBusy={setBusy}
          setError={setError}
          setConfirm={setConfirm}
          dnd={dnd}
        />
      ))}
    </>
  )
}

function FileViewer({ item, onClose }) {
  const ext = fileExt(item.name)
  const url = `/api/project/files/view?path=${encodeURIComponent(item.path)}`
  const isImg = IMAGE_EXTS.has(ext)
  return (
    <div className="fm-viewer-overlay" onClick={onClose}>
      <div className="fm-viewer-modal" onClick={e => e.stopPropagation()}>
        <div className="fm-viewer-head">
          <span className="fm-viewer-name">{item.name}</span>
          <span className="fm-grow" />
          <a className="fm-action" href={api.downloadFileUrl(item.path)} download={item.name} title="Download">⬇</a>
          <button className="iconbtn" onClick={onClose} title="Close">✕</button>
        </div>
        <div className="fm-viewer-body">
          {isImg
            ? <img src={url} alt={item.name} className="fm-viewer-img" />
            : <iframe src={url} title={item.name} className="fm-viewer-frame" />
          }
        </div>
      </div>
    </div>
  )
}

function GitPanel({ onRoomChange, setConfirm }) {
  const [status, setStatus] = useState(null)      // {initialized, dirty, current}
  const [versions, setVersions] = useState([])
  const [expanded, setExpanded] = useState(true)
  const [committing, setCommitting] = useState(false)
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const [statusMsg, setStatusMsg] = useState('')
  const msgRef = useRef(null)

  const reload = useCallback(async () => {
    try {
      const [st, vs] = await Promise.all([api.gitStatus(), api.gitVersions()])
      setStatus(st)
      setVersions(vs)
    } catch {}
  }, [])

  useEffect(() => { reload() }, [reload])

  async function doCommit(e) {
    e.preventDefault()
    setBusy(true); setStatusMsg('')
    try {
      const r = await api.gitCommit(msg.trim())
      if (r.ok) {
        setMsg(''); setCommitting(false)
        setStatusMsg(r.skipped ? 'No changes to save' : `Saved version ${r.tag}`)
        await reload()
      } else {
        setStatusMsg(r.error || 'Save failed')
      }
    } catch (err) {
      setStatusMsg(err.message || 'Error')
    } finally { setBusy(false) }
  }

  async function runRestore(v) {
    setBusy(true); setStatusMsg('')
    try {
      const r = await api.gitRestore(v.tag)
      if (r.ok) {
        setStatusMsg(`Restored "${v.message}"`)
        if (r.room && onRoomChange) onRoomChange(r.room)
        await reload()
      } else {
        setStatusMsg(r.error || 'Restore failed')
      }
    } catch (err) {
      setStatusMsg(err.message || 'Error')
    } finally { setBusy(false) }
  }

  // Only prompt to discard when git itself reports uncommitted changes — re-check
  // live so an edit made since the panel loaded is reflected.
  async function askRestore(v) {
    let dirty = status?.dirty
    try { dirty = (await api.gitStatus()).dirty } catch {}
    if (dirty) {
      setConfirm({
        msg: `You have unsaved changes. Restore to "${v.message}" and discard them?`,
        confirmLabel: 'Discard & restore',
        onConfirm: () => { setConfirm(null); runRestore(v) },
      })
    } else {
      runRestore(v)
    }
  }

  function askDelete(v) {
    setConfirm({
      msg: `Delete version "${v.message}"? The snapshot will be removed.`,
      onConfirm: () => { setConfirm(null); doDelete(v) },
    })
  }

  async function doDelete(v) {
    setBusy(true); setStatusMsg('')
    try {
      const r = await api.gitDeleteVersion(v.tag)
      if (r.ok) { setStatusMsg(`Deleted "${v.message}"`); await reload() }
      else setStatusMsg(r.error || 'Delete failed')
    } catch (err) {
      setStatusMsg(err.message || 'Error')
    } finally { setBusy(false) }
  }

  const canSave = canSaveVersion(status, versions.length)

  return (
    <div className="git-panel">
      <div className="git-header" onClick={() => setExpanded(x => !x)}>
        <span className="git-icon">{expanded ? '▾' : '▸'}</span>
        <span className="git-title">Versions</span>
        {status?.dirty && <span className="git-dirty-dot" title="unsaved changes" />}
        <span className="fm-grow" />
        <button
          className="mini git-commit-btn"
          onClick={e => { e.stopPropagation(); setCommitting(c => !c); setTimeout(() => msgRef.current?.focus(), 50) }}
          title={canSave ? 'Save current state as a version' : 'Already at the latest version — no changes to save'}
          disabled={busy || !canSave}
        >+ Save version</button>
      </div>

      {expanded && (
        <div className="git-body">
          {committing && (
            <form className="git-commit-form" onSubmit={doCommit}>
              <input
                ref={msgRef}
                className="git-msg-input"
                placeholder="Version name (optional)"
                value={msg}
                onChange={e => setMsg(e.target.value)}
                onKeyDown={e => e.key === 'Escape' && setCommitting(false)}
                disabled={busy}
              />
              <div className="git-commit-actions">
                {/* A version-less project must always be saveable, even when Git is not initialized
                    or HEAD is clean. Once a version exists, disable only truly redundant saves. */}
                <button type="submit" className="mini primary" disabled={busy || !canSave}
                        title={canSave ? '' : 'No changes since the last saved version'}>{busy ? '…' : 'Save'}</button>
                <button type="button" className="mini" onClick={() => setCommitting(false)}>Cancel</button>
              </div>
            </form>
          )}

          {statusMsg && <div className="git-status-msg">{statusMsg}</div>}

          {versions.length === 0 ? (
            <div className="git-empty">No versions yet. Click “Save version” to snapshot the project.</div>
          ) : (
            <div className="git-log">
              {versions.map(v => (
                <div key={v.tag} className={'git-entry' + (v.is_current ? ' git-head' : '')}>
                  <span className="git-dot">{v.is_current ? '●' : '○'}</span>
                  <div className="git-entry-info">
                    <span className="git-msg">{v.message}</span>
                    <span className="git-meta">{v.date}</span>
                  </div>
                  {v.is_current
                    ? <span className="git-head-tag">current</span>
                    : <button className="mini git-checkout-btn" disabled={busy} onClick={() => askRestore(v)}>Restore</button>
                  }
                  <button className="git-del-btn" disabled={busy} title="Delete version" onClick={() => askDelete(v)}>✕</button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function FileManager({ activeFile, mainFile, onOpenFile, onClose, onRoomChange }) {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(null)  // 'file' | 'dir' | null
  const [newName, setNewName] = useState('')
  const [viewingFile, setViewingFile] = useState(null)
  const [editingMd, setEditingMd] = useState(null)
  const [confirm, setConfirm] = useState(null)  // {msg, onConfirm}
  const [selMode, setSelMode] = useState(false)        // selection mode on/off (shows checkboxes)
  const [selected, setSelected] = useState(new Set())  // multi-select: set of paths
  const [anchor, setAnchor] = useState(null)           // last-clicked path, for Shift-range
  const [renameTarget, setRenameTarget] = useState(null) // {path,name,type,abs_path} being renamed
  const [renameVal, setRenameVal] = useState('')
  const [uploadHover, setUploadHover] = useState(false)
  const newInputRef = useRef(null)
  const renameRef = useRef(null)
  const fileInputRef = useRef(null)
  const flatOrderRef = useRef([])  // display-order paths, refreshed each render for Shift-range

  const load = useCallback(async () => {
    try {
      const r = await api.listProjectFiles()
      setItems(r.items || [])
    } catch (e) {
      setError(e.message || 'Failed to load files')
    } finally { setLoading(false) }
  }, [])

  function toggleSelect(path) {
    setSelected(prev => { const n = new Set(prev); n.has(path) ? n.delete(path) : n.add(path); return n })
    setAnchor(path)
  }
  // Shift-click: select the contiguous range from the anchor to `path` (in display order)
  function selectRange(path) {
    const order = flatOrderRef.current
    const i1 = anchor == null ? -1 : order.indexOf(anchor)
    const i2 = order.indexOf(path)
    if (i1 < 0 || i2 < 0) { toggleSelect(path); return }
    const [lo, hi] = [Math.min(i1, i2), Math.max(i1, i2)]
    setSelected(new Set(order.slice(lo, hi + 1)))
  }
  async function moveInto(paths, destDir) {
    setError('')
    try {
      for (const p of paths) {
        if (p === destDir) continue
        const result = await api.moveItem(p, destDir)
        if (result.collision_renamed) toast.info(`A same-named item already existed; moved as ${result.name}`)
      }
      setSelected(new Set())
      await load()
    } catch (err) { setError(err.message || 'Move failed') }
  }
  function exitSelMode() { setSelMode(false); setSelected(new Set()); setAnchor(null) }
  async function uploadFiles(fileList, destDir = '') {
    const files = [...(fileList || [])]
    if (!files.length) return
    setBusy(true); setError('')
    try {
      for (const f of files) {
        const result = await api.uploadFile(f, destDir)
        if (result.collision_renamed) toast.info(`A same-named file already existed; uploaded as ${result.name}`)
      }
      await load()
    } catch (err) {
      setError(err.message || 'Upload failed')
    } finally { setBusy(false) }
  }
  const dnd = {
    selected, toggleSelect, selectRange, moveInto, selMode,
    uploadInto: uploadFiles,
    clearRootUploadHover: () => setUploadHover(false),
  }

  function bulkDelete() {
    // never delete the file currently being edited
    const paths = [...selected].filter(p => {
      const it = items.find(i => i.path === p)
      return !(it && it.type === 'file' && it.abs_path === activeFile)
    })
    if (!paths.length) { setError("Can't delete the file you're editing."); return }
    setConfirm({
      msg: `Delete ${paths.length} selected item${paths.length > 1 ? 's' : ''}?`,
      onConfirm: async () => {
        setConfirm(null); setBusy(true); setError('')
        try {
          for (const p of paths) {
            const it = items.find(i => i.path === p)
            if (it && it.type === 'dir') await api.rmdir(p)
            else await api.deleteProjectFile(p)
          }
          setSelected(new Set()); setAnchor(null); await load()
        } catch (err) { setError(err.message || 'Delete failed') }
        finally { setBusy(false) }
      },
    })
  }

  async function doDuplicate(item) {
    if (!item || item.type !== 'file') return
    setBusy(true); setError('')
    try {
      const info = await api.duplicateProjectFile(item.path)
      await load()
      setSelected(new Set(info ? [info.path] : [])); setAnchor(info ? info.path : null)
    } catch (err) { setError(err.message || 'Duplicate failed') }
    finally { setBusy(false) }
  }

  function startRename(item) {
    if (!item) return
    setRenameTarget(item); setRenameVal(item.name)
    setTimeout(() => { renameRef.current?.focus(); renameRef.current?.select() }, 30)
  }
  async function doRename(e) {
    e.preventDefault()
    const name = renameVal.trim()
    if (!renameTarget || !name || name === renameTarget.name) { setRenameTarget(null); return }
    setBusy(true); setError('')
    try {
      const res = await api.renameItem(renameTarget.path, name)
      const wasActive = renameTarget.type === 'file' && renameTarget.abs_path === activeFile
      setRenameTarget(null); setRenameVal(''); setSelected(new Set()); setAnchor(null)
      await load()
      // if we renamed the file being edited, re-open it so the editor follows the new path
      if (wasActive && res && res.abs_path) { const r = await api.openFile(res.abs_path); onOpenFile && onOpenFile(r) }
    } catch (err) { setError(err.message || 'Rename failed') }
    finally { setBusy(false) }
  }

  useEffect(() => { load() }, [load])
  useEffect(() => { if (creating) newInputRef.current?.focus() }, [creating])

  // filter out the main .typ file from the tree
  const filteredItems = mainFile
    ? items.filter(it => !(it.type === 'file' && it.name === mainFile && !it.path.includes('/')))
    : items
  const tree = buildTree(filteredItems)
  flatOrderRef.current = flattenPaths(tree)  // keep display order current for Shift-range

  async function handleCreate(e) {
    e.preventDefault()
    const name = newName.trim()
    if (!name) return
    setBusy(true); setError('')
    try {
      if (creating === 'dir') {
        await api.mkdir(name)
      } else {
        await api.createProjectFile(name)
      }
      setNewName(''); setCreating(null)
      await load()
    } catch (err) {
      setError(err.message || 'Failed to create')
    } finally { setBusy(false) }
  }

  async function handleUpload(e) {
    await uploadFiles(e.target.files)
    e.target.value = ''
  }

  return (
    <>
      <div className="filemgr-overlay" onClick={onClose}>
        <div className="filemgr-panel" onClick={e => e.stopPropagation()}>
          <div className="filemgr-head">
            <span>Project files</span>
            <span className="grow" />
            <button className="iconbtn" onClick={onClose} title="Close">✕</button>
          </div>

          {error && (
            <div className="filemgr-error">
              {error}
              <button className="mini" style={{ marginLeft: 8 }} onClick={() => setError('')}>✕</button>
            </div>
          )}

          <div className={'filemgr-toolbar' + (selMode ? ' selmode' : '')}>
            {!selMode ? (
              <>
                <button className="mini" onClick={() => { setCreating('file'); setNewName('') }}>+ File</button>
                <button className="mini" onClick={() => { setCreating('dir'); setNewName('') }}>+ Folder</button>
                <button className="mini" onClick={() => fileInputRef.current?.click()} disabled={busy}>⬆ Upload</button>
                <span className="fm-grow" />
                <button className="mini" onClick={() => { setCreating(null); setSelMode(true) }} title="Select files to rename, duplicate or delete">Select</button>
              </>
            ) : (() => {
              // ONE consistent button set so the bar never reflows as the selection count
              // changes — Rename/Duplicate/Delete are always present, just enabled when valid.
              const one = selected.size === 1 ? items.find(i => i.path === [...selected][0]) : null
              return (
                <>
                  <button className="mini" onClick={exitSelMode}>Done</button>
                  <span className="fm-selcount">{selected.size} selected</span>
                  <span className="fm-grow" />
                  <button className="ghostbtn" disabled={busy || !one} onClick={() => one && startRename(one)}>Rename</button>
                  <button className="ghostbtn" disabled={busy || !one || one.type !== 'file'} onClick={() => one && doDuplicate(one)}>Duplicate</button>
                  <button className="ghostbtn danger" disabled={busy || selected.size === 0} onClick={bulkDelete}>Delete{selected.size > 1 ? ` (${selected.size})` : ''}</button>
                </>
              )
            })()}
            <input ref={fileInputRef} type="file" multiple style={{ display: 'none' }} onChange={handleUpload} />
          </div>

          {creating && (
            <form className="filemgr-new" onSubmit={handleCreate}>
              <span style={{ fontSize: 14 }}>{creating === 'dir' ? '📁' : '📄'}</span>
              <input
                ref={newInputRef}
                placeholder={creating === 'dir' ? 'folder-name' : 'filename.ext (e.g. refs.bib · defaults to .typ)'}
                value={newName}
                onChange={e => setNewName(e.target.value)}
                onKeyDown={e => e.key === 'Escape' && (setCreating(null), setNewName(''))}
              />
              <button type="submit" className="mini primary" disabled={busy || !newName.trim()}>
                {busy ? '…' : 'Create'}
              </button>
              <button type="button" className="mini" onClick={() => { setCreating(null); setNewName('') }}>✕</button>
            </form>
          )}

          {renameTarget && (
            <form className="filemgr-new" onSubmit={doRename}>
              <span style={{ fontSize: 14 }}>{renameTarget.type === 'dir' ? '📁' : '📄'}</span>
              <input
                ref={renameRef}
                value={renameVal}
                onChange={e => setRenameVal(e.target.value)}
                onKeyDown={e => e.key === 'Escape' && (setRenameTarget(null), setRenameVal(''))}
              />
              <button type="submit" className="mini primary" disabled={busy || !renameVal.trim()}>
                {busy ? '…' : 'Rename'}
              </button>
              <button type="button" className="mini" onClick={() => { setRenameTarget(null); setRenameVal('') }}>✕</button>
            </form>
          )}

          {loading ? (
            <div className="filemgr-loading">Loading…</div>
          ) : (
            <div
              className={'filemgr-list' + (uploadHover ? ' fm-upload-hover' : '')}
              onDragOver={(e) => {
                const t = [...e.dataTransfer.types]
                if (t.includes('Files')) { e.preventDefault(); if (!uploadHover) setUploadHover(true) }       // external upload
                else if (t.includes('application/x-fm-move')) { e.preventDefault(); e.dataTransfer.dropEffect = 'move' }  // move to root
              }}
              onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setUploadHover(false) }}
              onDrop={(e) => {
                setUploadHover(false)
                if (e.dataTransfer.files && e.dataTransfer.files.length) { e.preventDefault(); uploadFiles(e.dataTransfer.files); return }
                const raw = e.dataTransfer.getData('application/x-fm-move')
                if (raw) { e.preventDefault(); try { moveInto(JSON.parse(raw), '') } catch {} }  // drop on empty area → project root
              }}
            >
              {tree.length === 0 && (
                <div className="filemgr-empty">No supporting files. Drop files here to upload, or create above.</div>
              )}
              {tree.map(item => (
                <TreeRow
                  key={item.path}
                  item={item}
                  depth={0}
                  activeFile={activeFile}
                  mainFile={mainFile}
                  onOpenFile={onOpenFile}
                  onViewFile={setViewingFile}
                  onEditMd={setEditingMd}
                  onReload={load}
                  setBusy={setBusy}
                  setError={setError}
                  setConfirm={setConfirm}
                  dnd={dnd}
                />
              ))}
              {uploadHover && <div className="fm-drop-hint">Drop to upload</div>}
            </div>
          )}

          <GitPanel onRoomChange={onRoomChange} setConfirm={setConfirm} />
        </div>
      </div>

      {viewingFile && <FileViewer item={viewingFile} onClose={() => setViewingFile(null)} />}
      {editingMd && <MdEditor item={editingMd} onClose={() => setEditingMd(null)} />}
      {confirm && <ConfirmDialog msg={confirm.msg} confirmLabel={confirm.confirmLabel} onConfirm={confirm.onConfirm} onCancel={() => setConfirm(null)} />}
    </>
  )
}
