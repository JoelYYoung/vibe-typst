import React, { useEffect, useRef, useState } from 'react'
import * as api from './api.js'

// One slide as INLINE typst SVG. Each text run (<g> of glyph <use>s) and shape (<path>)
// is hit-tested in PAGE coordinates (points): we take each element's local getBBox() and
// push it through getCTM() to the SVG root space, which is the same point-space tinymist's
// resolver uses. Hover highlights the smallest element under the cursor and pins "+ add"
// to its right edge. Hover-intent keeps it alive while you move to the button, then clears.
export default function PageSvg({ name, pageNo, token, mark, onResolve, onJump, onAdd }) {
  const hostRef = useRef(null)
  const vbRef = useRef(null)        // {svg, w, h}
  const candsRef = useRef([])       // [{el,x,y,w,h,area}] in page points
  const activeRef = useRef(null)
  const clearRef = useRef(null)
  const [active, setActive] = useState(null) // {x,y,w,h} in points
  const [vb, setVb] = useState(null)
  const [marker, setMarker] = useState(null) // {x,y} reverse-locate pulse, auto-clears
  const wrapRef = useRef(null)

  // reverse-locate: when this page is the cursor target, scroll it into view and pulse a
  // marker at the spot, fading out so it doesn't linger.
  useEffect(() => {
    if (!mark) { setMarker(null); return }
    setMarker({ x: mark.x, y: mark.y })
    if (wrapRef.current) wrapRef.current.scrollIntoView({ behavior: 'smooth', block: 'center' })
    const t = setTimeout(() => setMarker(null), 2400)
    return () => clearTimeout(t)
  }, [mark && mark.key, mark && mark.page])

  useEffect(() => {
    let cancelled = false
    fetch(api.renderUrl(name, token)).then((r) => r.text()).then((txt) => {
      if (cancelled || !hostRef.current) return
      hostRef.current.innerHTML = txt
      const svg = hostRef.current.querySelector('svg')
      if (!svg) return
      const bw = svg.viewBox.baseVal.width, bh = svg.viewBox.baseVal.height
      svg.removeAttribute('width'); svg.removeAttribute('height')
      svg.style.width = '100%'; svg.style.height = 'auto'; svg.style.display = 'block'
      vbRef.current = { svg, w: bw, h: bh }
      candsRef.current = []
      activeRef.current = null
      setVb({ w: bw, h: bh })
      setActive(null)
    }).catch(() => {})
    return () => { cancelled = true }
  }, [name, token])

  // Compute candidates lazily (first hover). Measure each element with
  // getBoundingClientRect (screen px, accounts for ALL transforms unambiguously) and
  // normalize to page points relative to the SVG box — scroll/resize independent.
  function ensureCands() {
    const v = vbRef.current
    if (!v || candsRef.current.length) return
    const svgRect = v.svg.getBoundingClientRect()
    if (!svgRect.width || !svgRect.height) return // not laid out yet; retry next move
    const els = []
    v.svg.querySelectorAll('g').forEach((g) => { if (g.querySelector(':scope > use')) els.push(g) })
    v.svg.querySelectorAll('path').forEach((p) => { if (!p.closest('defs')) els.push(p) })
    v.svg.querySelectorAll('image').forEach((im) => els.push(im))
    const pageArea = v.w * v.h
    candsRef.current = els.map((el) => {
      const r = el.getBoundingClientRect()
      if (r.width <= 0 || r.height <= 0) return null
      const x = ((r.left - svgRect.left) / svgRect.width) * v.w
      const y = ((r.top - svgRect.top) / svgRect.height) * v.h
      const w = (r.width / svgRect.width) * v.w
      const h = (r.height / svgRect.height) * v.h
      return { el, x, y, w, h, area: w * h }
    }).filter((c) => c && c.w > 1.5 && c.h > 1.5
      && !(c.w > v.w * 0.95 && c.h > v.h * 0.95) // drop page-spanning background
      && c.area < pageArea * 0.9)
  }

  function cancelClear() { if (clearRef.current) { clearTimeout(clearRef.current); clearRef.current = null } }
  function scheduleClear() {
    if (clearRef.current) return
    clearRef.current = setTimeout(() => { clearRef.current = null; activeRef.current = null; setActive(null) }, 220)
  }

  function onMove(e) {
    const v = vbRef.current
    if (!v) return
    ensureCands()
    const rect = v.svg.getBoundingClientRect()
    const px = ((e.clientX - rect.left) / rect.width) * v.w
    const py = ((e.clientY - rect.top) / rect.height) * v.h
    let best = null
    for (const c of candsRef.current) {
      if (px >= c.x && px <= c.x + c.w && py >= c.y && py <= c.y + c.h) {
        if (!best || c.area < best.area) best = c
      }
    }
    if (best) {
      cancelClear()
      if (best !== activeRef.current) {
        activeRef.current = best
        setActive({ x: best.x, y: best.y, w: best.w, h: best.h })
      }
    } else {
      scheduleClear() // over a gap -> clear shortly (unless we enter the add button)
    }
  }

  // Find the smallest element under a pointer event, computed from the event coordinates
  // directly. Crucial: after an edit bumps rv, the SVG is re-fetched and hover state
  // (activeRef/candsRef) is reset. If the mouse is sitting still over an element, onMove
  // never fires to rebuild that state, so a plain click would see no active element and do
  // nothing ("click does nothing after editing"). Resolving from the click point sidesteps
  // that entirely: a click always works, stationary mouse or not.
  function elAt(e) {
    const v = vbRef.current
    if (!v) return null
    ensureCands()
    const rect = v.svg.getBoundingClientRect()
    if (!rect.width || !rect.height) return null
    const px = ((e.clientX - rect.left) / rect.width) * v.w
    const py = ((e.clientY - rect.top) / rect.height) * v.h
    let best = null
    for (const c of candsRef.current) {
      if (px >= c.x && px <= c.x + c.w && py >= c.y && py <= c.y + c.h) {
        if (!best || c.area < best.area) best = c
      }
    }
    return { best, px, py }
  }

  // Always resolve fresh (~8ms warm). No caching: a cached [row,col] would be stale after
  // any edit shifts the source, and it's what caused the "click again does nothing" bug.
  async function resolvePoint(cx, cy) {
    let r = await onResolve(pageNo, cx, cy)
    if (!(r && r.ok)) r = await onResolve(pageNo, cx, cy) // first attempt warms a cold page; retry
    return r && r.ok ? { start: r.start, end: r.end } : null
  }

  async function onClick(e) {
    const hit = elAt(e)
    if (!hit || !hit.best) return // only jump on a real element, not blank margin
    const c = hit.best
    const s = await resolvePoint(c.x + c.w / 2, c.y + c.h / 2)
    if (s) onJump(s)
  }

  async function onAddClick(e) {
    e.stopPropagation()
    // Use the element this button is VISUALLY attached to (the rendered `active`), NOT the
    // live hover ref: moving the mouse to reach the button can drift activeRef onto another
    // element, which would otherwise add the wrong (often the previously-clicked) element.
    // Resolving the attached element also jumps the editor to it (via onAdd), so clicking
    // "+ add" behaves like clicking the element first and then adding it.
    const c = active
    if (!c) return
    const s = await resolvePoint(c.x + c.w / 2, c.y + c.h / 2)
    if (s) onAdd(s, pageNo)
  }

  const pct = (val, total) => `${(val / total) * 100}%`

  return (
    <div className="svg-wrap" ref={wrapRef} onMouseMove={onMove} onMouseLeave={scheduleClear} onClick={onClick}>
      <div className="svg-host" ref={hostRef} />
      {marker && vb && (
        <div className="locate-mark" style={{ left: pct(marker.x, vb.w), top: pct(marker.y, vb.h) }} />
      )}
      {active && vb && (
        <>
          <div className="el-hi" style={{
            left: pct(active.x, vb.w), top: pct(active.y, vb.h),
            width: pct(active.w, vb.w), height: pct(active.h, vb.h),
          }} />
          <button className="el-add" style={{
            left: pct(active.x + active.w, vb.w), top: pct(active.y + active.h / 2, vb.h),
          }}
            onMouseEnter={cancelClear}
            onMouseMove={(e) => { e.stopPropagation(); cancelClear() }}
            onClick={onAddClick}
          >+ add</button>
        </>
      )}
    </div>
  )
}
