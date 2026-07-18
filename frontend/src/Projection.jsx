import React, { useEffect, useState } from 'react'
import * as api from './api.js'
import { sanitizePresentationPointer, slidePointToPixels } from './presentationPointer.js'

// The AUDIENCE screen: just the current slide, full-bleed. Opened as a second window and
// dragged onto the projector; it follows the presenter window via a BroadcastChannel.
export default function Projection() {
  const [page, setPage] = useState(1)
  const [pages, setPages] = useState([])
  const [tokens, setTokens] = useState({}) // per-page content token {name: hash} → SVG URL cache-buster
  const [pointer, setPointer] = useState(null)
  const [imageSize, setImageSize] = useState(null)
  const [viewport, setViewport] = useState(() => ({ width: window.innerWidth, height: window.innerHeight }))

  useEffect(() => {
    // Per-page content tokens: each slide's URL changes only when that slide's bytes change,
    // so it never collides with a previously-cached project. Live updates arrive via the
    // BroadcastChannel below (the presenter window pushes its current pages/tokens/page).
    api.renderVersion().then((r) => { setPages(r.pages || []); setTokens(r.tokens || {}) }).catch(() => {})
    const ch = new BroadcastChannel('tcb-present')
    ch.onmessage = (e) => {
      const d = e.data || {}
      if (d.ping) { ch.postMessage({ pong: true }); return } // liveness heartbeat from the editor
      if (d.pages) setPages(d.pages)
      if (d.tokens) setTokens(d.tokens)
      if (d.page) setPage(d.page)
      if (Object.prototype.hasOwnProperty.call(d, 'pointer')) {
        setPointer(sanitizePresentationPointer(d.pointer))
      }
    }
    ch.postMessage({ hello: true }) // ask the presenter to send current state
    return () => ch.close()
  }, [])

  useEffect(() => {
    const onResize = () => setViewport({ width: window.innerWidth, height: window.innerHeight })
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const name = pages[page - 1]
  const visiblePointer = pointer && pointer.page === page && imageSize && imageSize.name === name
    ? slidePointToPixels(pointer, viewport.width, viewport.height, imageSize.width, imageSize.height)
    : null
  return (
    <div className="projection">
      {name
        ? <img className="proj-slide" src={api.renderUrl(name, tokens[name])} alt={`slide ${page}`}
            onLoad={(e) => setImageSize({ name, width: e.currentTarget.naturalWidth, height: e.currentTarget.naturalHeight })} />
        : <div className="proj-empty">waiting for slides…</div>}
      {visiblePointer && <span className="presentation-pointer proj-pointer" aria-hidden="true"
        style={{ left: `${visiblePointer.left}px`, top: `${visiblePointer.top}px` }} />}
    </div>
  )
}
