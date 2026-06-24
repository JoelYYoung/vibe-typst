import React, { useEffect, useRef, useState } from 'react'
import * as api from './api.js'

// A simple dropdown in the editor pane-head: pick WHICH .typ file is the edit target.
// Just a selector — no create / multi-select / file ops (those live in the Files modal).
// Clicking a file opens it via the same /api/open-file flow, switching the active deck.
export default function FilePicker({ activeMain, activeFile, onOpen }) {
  const [open, setOpen] = useState(false)
  const [files, setFiles] = useState([])
  const [loading, setLoading] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    if (!open) return
    setLoading(true)
    api.listProjectFiles()
      .then((r) => {
        const typ = (r.items || []).filter((it) => it.type === 'file' && it.is_typ)
        typ.sort((a, b) => a.path.localeCompare(b.path))
        setFiles(typ)
      })
      .catch(() => setFiles([]))
      .finally(() => setLoading(false))
  }, [open])

  useEffect(() => {
    if (!open) return
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDoc); document.removeEventListener('keydown', onKey) }
  }, [open])

  async function pick(it) {
    setOpen(false)
    if (it.abs_path === activeFile) return
    try { const r = await api.openFile(it.abs_path); onOpen && onOpen(r) } catch {}
  }

  return (
    <div className="filepicker" ref={ref}>
      <button className={'filepicker-btn' + (open ? ' open' : '')} onClick={() => setOpen((o) => !o)}
        title={activeFile || 'select the .typ file to edit'}>
        <span className="fp-ico">📄</span>
        <span className="fp-name">{activeMain || '—'}</span>
        <span className="fp-caret">▾</span>
      </button>
      {open && (
        <div className="filepicker-menu">
          <div className="fp-list">
            {loading ? (
              <div className="fp-loading">Loading…</div>
            ) : files.length === 0 ? (
              <div className="fp-empty">No .typ files</div>
            ) : (
              files.map((it) => {
                const isActive = it.abs_path === activeFile
                return (
                  <button key={it.path} className={'fp-item' + (isActive ? ' active' : '')}
                    onClick={() => pick(it)} title={it.path}>
                    <span className="fp-ico">📄</span>
                    <span className="fp-item-name">{it.path}</span>
                    {isActive && <span className="fp-check">✓</span>}
                  </button>
                )
              })
            )}
          </div>
        </div>
      )}
    </div>
  )
}
