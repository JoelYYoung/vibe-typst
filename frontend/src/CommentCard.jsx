import React, { useState } from 'react'
import * as api from './api.js'

// stable identity for a selection chip — must match App.jsx's selKey so de-dup is consistent
const selKey = (s) => (s.kind === 'page' ? `p${s.page_no}` : `${s.page}:${s.text}`)
const chipLabel = (s) =>
  s.kind === 'page'
    ? `page ${s.page_no}`
    : `${(s.text || '').slice(0, 22)}${(s.text || '').length > 22 ? '…' : ''}`

// One comment. When editing (a single shared session owned by App), the SAME mode edits both
// the body text and the anchors: removing a chip here, and any "+ add" picked in the preview or
// editor, all apply to THIS card. New-comment creation is a separate flow in the composer above.
export default function CommentCard({
  c, onChange, onJumpChip,
  isEditing, editBody, editSels,
  onStartEdit, onChangeBody, onRemoveSel, onSaveEdit, onCancelEdit,
}) {
  const [showRaw, setShowRaw] = useState(false)
  const [events, setEvents] = useState(null)

  async function toggleHistory() {
    if (events) { setEvents(null); return }
    setEvents(await api.commentEvents(c.id))
  }

  const sels = Array.isArray(c.selection) ? c.selection : []
  const canEdit = c.status === 'pending'

  return (
    <div className={'card ' + c.status + (isEditing ? ' editing-sel' : '')}>
      <div className="card-top">
        <span className="seq">#{c.seq}</span>
        <span className="kindbadge">{c.kind || 'element'}</span>
        {c.page != null && <span className="pgbadge">p{c.page}</span>}
        <span className={'status ' + c.status}>{c.status}</span>
        <span className="grow" />
        {c.status !== 'pending' && <a onClick={() => api.reopen(c.id).then(onChange)}>reopen</a>}
        {c.status === 'pending' && <a onClick={() => api.markDone(c.id).then(onChange)}>done</a>}
        <a className="del" onClick={() => api.delComment(c.id).then(onChange)}>✕</a>
      </div>

      {/* anchors */}
      {isEditing ? (
        <div className="card-sels">
          {editSels.length === 0 && <span className="sel-empty-mini">No anchors — click an element/page in the preview, or select in the editor.</span>}
          {editSels.map((s, i) => (
            <span key={selKey(s) + i} className={'chip ' + (s.kind || 'element')}>
              {chipLabel(s)}
              <a className="chip-x" title="remove this anchor" onClick={() => onRemoveSel(selKey(s))}>✕</a>
            </span>
          ))}
        </div>
      ) : sels.length > 0 ? (
        <div className="card-sels">
          {sels.map((s, i) => (
            <span key={i} className={'chip jumpable ' + (s.kind || 'element')}
              title="jump to this in the editor" onClick={() => onJumpChip && onJumpChip(s)}>
              {chipLabel(s)}
            </span>
          ))}
        </div>
      ) : (
        c.anchor_text && <code className="anchor">{c.anchor_text}</code>
      )}

      {/* body */}
      {isEditing ? (
        <div className="body-edit">
          <textarea
            className="body-input"
            value={editBody}
            autoFocus
            onChange={(e) => onChangeBody(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') onCancelEdit()
              if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') onSaveEdit()
            }}
          />
          <div className="edit-actions">
            <button className="mini primary" disabled={!editBody.trim()} onClick={onSaveEdit}>save</button>
            <button className="mini" onClick={onCancelEdit}>cancel</button>
            <span className="edit-hint">⌘↵ save · esc cancel · pick elements to add</span>
          </div>
        </div>
      ) : (
        <div className="body" onDoubleClick={() => canEdit && onStartEdit()} title={canEdit ? 'double-click to edit' : ''}>{c.body}</div>
      )}

      {c.done_note && <div className="note">↳ {c.done_note}</div>}
      <div className="card-actions">
        {!isEditing && canEdit && <a onClick={onStartEdit}>edit</a>}
        <a onClick={() => setShowRaw((v) => !v)}>{showRaw ? 'hide' : 'raw'} context</a>
        <a onClick={toggleHistory}>{events ? 'hide' : 'history'}</a>
      </div>
      {showRaw && <pre className="raw">{c.raw_context || '(none)'}</pre>}
      {events && (
        <ul className="events">
          {events.map((e, i) => (
            <li key={i}><span className="ets">{e.ts.slice(5, 16).replace('T', ' ')}</span> {e.kind}{e.detail ? `: ${e.detail}` : ''}</li>
          ))}
        </ul>
      )}
    </div>
  )
}
