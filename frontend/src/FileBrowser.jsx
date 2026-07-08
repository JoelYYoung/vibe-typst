import React, { useEffect, useRef, useState } from 'react'
import * as api from './api.js'

// File navigator: type a directory path (with a Go button), or browse. Single-click
// selects; double-click a folder enters it. The bottom-right button is context-aware:
// "Open" enters a selected folder or opens a selected .typ file.
export default function FileBrowser({ onOpen, onClose }) {
  const [dir, setDir] = useState(null)     // { cwd, parent, dirs, files }
  const [sel, setSel] = useState(null)     // { kind:'dir'|'file', path, name }
  const [pathInput, setPathInput] = useState('')
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)

  async function go(path) {
    setLoading(true); setErr(''); setSel(null)
    try {
      const r = await api.browse(path)
      if (r.error) { setErr(r.error + (path ? `: ${path}` : '')); return }
      setDir(r); setPathInput(r.cwd || '')
    } catch (e) {
      setErr(String(e))
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { go() }, [])

  async function openFile(path) {
    setErr('')
    try { const r = await api.openFile(path); onOpen && onOpen(r) } catch (e) { setErr(String(e)) }
  }

  // the context-aware bottom-right action
  function primary() {
    if (!sel) return
    if (sel.kind === 'dir') go(sel.path)
    else openFile(sel.path)
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <b>Open Typst file</b>
          <span className="grow" />
          <button onClick={onClose}>close</button>
        </div>

        <div className="pathbar">
          <input
            value={pathInput}
            placeholder="/path/to/folder"
            onChange={(e) => setPathInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') go(pathInput) }}
          />
          <button onClick={() => go(pathInput)}>Go</button>
        </div>
        {err && <div className="err">{err}</div>}

        <div className="browser">
          {loading && <div className="empty">loading…</div>}
          {dir && dir.parent && (
            <div className="row dir" onDoubleClick={() => go(dir.parent)} onClick={() => setSel(null)}>📁 ..</div>
          )}
          {dir && dir.dirs.map((d) => (
            <div
              key={d.path}
              className={'row dir' + (sel && sel.path === d.path ? ' sel' : '')}
              onClick={() => setSel({ kind: 'dir', path: d.path, name: d.name })}
              onDoubleClick={() => go(d.path)}
            >📁 {d.name}</div>
          ))}
          {dir && dir.files.map((f) => (
            <div
              key={f.path}
              className={'row file' + (sel && sel.path === f.path ? ' sel' : '')}
              onClick={() => setSel({ kind: 'file', path: f.path, name: f.name })}
              onDoubleClick={() => openFile(f.path)}
            >📄 {f.name} <span className="sz">{(f.size / 1024).toFixed(1)} KB</span></div>
          ))}
          {dir && dir.dirs.length === 0 && dir.files.length === 0 && !loading && (
            <div className="empty">empty folder</div>
          )}
        </div>

        <div className="modal-foot">
          <span className="sel-name">{sel ? sel.name : ''}</span>
          <button className="primary" disabled={!sel} onClick={primary}>
            {sel && sel.kind === 'dir' ? 'Open folder' : 'Open'}
          </button>
        </div>
      </div>
    </div>
  )
}
