import React, { forwardRef, useEffect, useImperativeHandle, useRef } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'

// A real shell over the backend PTY (WebSocket). Server-hosted, so it works the same
// locally and from a remote browser. Starts in HOME; the parent can `runCommand('cd …')`
// to move it to the deck's directory on demand.
const TermPanel = forwardRef(function TermPanel(_props, ref) {
  const hostRef = useRef(null)
  const wsRef = useRef(null)
  const refitRef = useRef(null)
  const roTimerRef = useRef(null)

  useImperativeHandle(ref, () => ({
    runCommand(cmd) {
      const ws = wsRef.current
      if (ws && ws.readyState === 1) ws.send(JSON.stringify({ t: 'i', d: cmd + '\n' }))
    },
    // type text into the shell WITHOUT a trailing newline — lets the user review and press
    // Enter themselves (e.g. an agent TUI prompt, where a stray newline is just a blank line)
    typeText(text) {
      const ws = wsRef.current
      if (ws && ws.readyState === 1) ws.send(JSON.stringify({ t: 'i', d: text }))
    },
    // type text and SUBMIT it. Uses '\r' (carriage return = the Enter key), which a TUI like
    // Agent TUIs interpret as "run", unlike '\n' which they treat as a newline-in-input. We send
    // the text, then the Enter a beat later so the TUI has registered the input first.
    runInAgent(text) {
      const ws = wsRef.current
      if (!ws || ws.readyState !== 1) return
      ws.send(JSON.stringify({ t: 'i', d: text }))
      setTimeout(() => {
        if (ws.readyState === 1) ws.send(JSON.stringify({ t: 'i', d: '\r' }))
      }, 120)
    },
    // re-measure after the panel is un-hidden (display:none leaves xterm at 0x0)
    refit() { refitRef.current && refitRef.current() },
  }), [])

  useEffect(() => {
    const term = new Terminal({
      fontSize: 12,
      fontFamily: "'SF Mono', ui-monospace, Menlo, monospace",
      cursorBlink: true,
      theme: { background: '#11161d', foreground: '#d6dde6', cursor: '#cdd5df' },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(hostRef.current)

    let alive = true
    let ro = null
    let reconnectTimer = null

    // Keystrokes / resizes always target the CURRENT socket (survives reconnects).
    const send = (o) => { const ws = wsRef.current; if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)) }
    // Re-fit WITHOUT yanking the viewport to the top: keep the reader where they were (or pinned
    // to the bottom if they were already there). fit.fit() reflows and otherwise resets scroll.
    const resize = () => {
      // While the terminal is HIDDEN (collapsed left column, or terminal toggled off) the host
      // is 0×0. Fitting then yields the 2×1 floor and would SIGWINCH the PTY to 2×1, corrupting
      // a running TUI (codex/claude) so its text spills past the edges when shown again. Skip.
      const host = hostRef.current
      if (!host || host.offsetWidth === 0 || host.offsetHeight === 0) return
      const b = term.buffer.active
      const atBottom = b.viewportY >= b.baseY - 1
      const fromBottom = b.baseY - b.viewportY          // lines above the bottom, pre-reflow
      try { fit.fit() } catch {}
      // Report the EXACT fitted grid so a TUI (codex/claude) never draws past the visible area.
      send({ t: 'r', c: term.cols, r: term.rows })
      // Restore scroll AFTER xterm's async resize render (which otherwise yanks the viewport to
      // line 0). Keep the reader the same distance from the bottom; if a widen re-wrapped the
      // buffer shorter than that, pin to the bottom (recent output) — NEVER jump to the top.
      const restore = () => {
        const nb = term.buffer.active
        const target = nb.baseY - fromBottom
        if (atBottom || target <= 0) term.scrollToBottom()
        else term.scrollToLine(target)
      }
      requestAnimationFrame(restore)
    }
    refitRef.current = resize
    term.onData((d) => send({ t: 'i', d }))

    // Open a PTY socket; each connection forks a FRESH shell on the backend. When the shell
    // exits (the user types `exit` / Ctrl-D, or it dies), the backend closes the socket — we
    // clear the pane and reconnect, so a new shell is launched automatically instead of leaving
    // a dead terminal. (Ctrl-C interrupts the foreground command; it does not exit the shell.)
    const connect = () => {
      if (!alive) return
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      const ws = new WebSocket(`${proto}://${location.host}/pty`)
      ws.binaryType = 'arraybuffer'
      wsRef.current = ws
      // Fit on open, then again once layout/fonts have fully settled — an early fit can
      // measure cell metrics slightly small and over-report cols/rows, which makes a TUI
      // (codex/claude) draw past the right and bottom edges.
      ws.onopen = () => { resize(); setTimeout(() => { if (alive && wsRef.current === ws) resize() }, 150) }
      ws.onmessage = (e) => term.write(new Uint8Array(e.data))
      ws.onclose = () => {
        wsRef.current = null
        if (!alive) return                       // intentional unmount — do not relaunch
        term.write('\r\n\x1b[33m[shell exited — restarting…]\x1b[0m\r\n')
        reconnectTimer = setTimeout(() => { if (!alive) return; term.reset(); connect() }, 400)
      }
    }

    // Defer the first connect until after two layout frames so the pane is fully measured
    // before we tell the PTY how many cols/rows to use (else the first prompt renders at the
    // 80-col default then reflows, producing apparent duplicate lines).
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!alive) return
      try { fit.fit() } catch {}
      ro = new ResizeObserver(() => {
        clearTimeout(roTimerRef.current)
        roTimerRef.current = setTimeout(resize, 80)
      })
      ro.observe(hostRef.current)
      connect()
    }))

    return () => {
      alive = false
      clearTimeout(roTimerRef.current)
      clearTimeout(reconnectTimer)
      if (ro) ro.disconnect()
      const ws = wsRef.current
      if (ws) try { ws.close() } catch {}
      term.dispose()
      wsRef.current = null
    }
  }, [])

  return <div className="term-host" ref={hostRef} />
})

export default TermPanel
