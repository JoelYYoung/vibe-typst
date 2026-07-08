import React, { useEffect, useState } from 'react'

// Module-level bus: any file can call toast.show() without React context
const _listeners = []
export const toast = {
  show(msg, type = 'info', duration = 3500) {
    const id = performance.now() + Math.random()
    _listeners.forEach(l => l({ id, msg, type, duration }))
  },
  success(msg, d) { this.show(msg, 'success', d) },
  error(msg, d)   { this.show(msg, 'error', d) },
  info(msg, d)    { this.show(msg, 'info', d) },
}

export default function Toaster() {
  const [toasts, setToasts] = useState([])

  useEffect(() => {
    function handle({ id, msg, type, duration }) {
      setToasts(prev => [...prev, { id, msg, type }])
      setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), duration)
    }
    _listeners.push(handle)
    return () => { const i = _listeners.indexOf(handle); if (i >= 0) _listeners.splice(i, 1) }
  }, [])

  function dismiss(id) { setToasts(prev => prev.filter(t => t.id !== id)) }

  return (
    <div className="toast-container">
      {toasts.map(t => (
        <div key={t.id} className={`toast toast-${t.type}`}>
          <span className="toast-msg">{t.msg}</span>
          <button className="toast-x" onClick={() => dismiss(t.id)}>✕</button>
        </div>
      ))}
    </div>
  )
}
