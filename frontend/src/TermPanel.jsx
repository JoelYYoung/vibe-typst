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

    // Defer WebSocket until after two layout frames so the pane is fully
    // measured before we tell the PTY how many cols/rows to use. Without
    // this, the first shell prompt can render at 80-col default and then
    // reflow to the actual (narrow) width, producing apparent duplicate lines.
    let alive = true
    let ws = null
    let ro = null

    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!alive) return
      try { fit.fit() } catch {}

      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${proto}://${location.host}/pty`)
      ws.binaryType = 'arraybuffer'
      wsRef.current = ws

      const send = (o) => { if (ws.readyState === 1) ws.send(JSON.stringify(o)) }
      const resize = () => { try { fit.fit() } catch {} send({ t: 'r', c: term.cols, r: term.rows }) }
      refitRef.current = resize

      ws.onopen = () => resize()
      ws.onmessage = (e) => term.write(new Uint8Array(e.data))
      ws.onclose = () => term.write('\r\n\x1b[31m[terminal disconnected]\x1b[0m\r\n')
      term.onData((d) => send({ t: 'i', d }))

      ro = new ResizeObserver(() => {
        clearTimeout(roTimerRef.current)
        roTimerRef.current = setTimeout(resize, 80)
      })
      ro.observe(hostRef.current)
    }))

    return () => {
      alive = false
      clearTimeout(roTimerRef.current)
      if (ro) ro.disconnect()
      if (ws) try { ws.close() } catch {}
      term.dispose()
      wsRef.current = null
    }
  }, [])

  return <div className="term-host" ref={hostRef} />
})

export default TermPanel
