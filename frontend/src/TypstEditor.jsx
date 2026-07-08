import React, { forwardRef, useEffect, useImperativeHandle, useRef } from 'react'
import * as Y from 'yjs'
import { WebsocketProvider } from 'y-websocket'
import { yCollab, yUndoManagerKeymap } from 'y-codemirror.next'
import { EditorState } from '@codemirror/state'
import { EditorView, keymap, lineNumbers, highlightActiveLine, drawSelection } from '@codemirror/view'
import { defaultKeymap, indentWithTab } from '@codemirror/commands'
import { searchKeymap, highlightSelectionMatches } from '@codemirror/search'
import { syntaxHighlighting, defaultHighlightStyle, bracketMatching } from '@codemirror/language'
import { typst } from 'codemirror-lang-typst'

const wsBase = () =>
  `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`

// A CodeMirror 6 editor whose document IS a shared Yjs text (the CRDT room). The human
// edits here; Claude edits the same room over the backend; both merge. Collaborative
// undo/redo via Y.UndoManager. Imperative API: highlight a (row,col) range from the
// preview, and report manual text selections back up for comment anchoring.
const enc = new TextEncoder()
const TypstEditor = forwardRef(function TypstEditor({ room, onSelect, onReady, onCursor, onDocChange }, ref) {
  const hostRef = useRef(null)
  const viewRef = useRef(null)
  const docRef = useRef(null)

  useEffect(() => {
    if (!room || !hostRef.current) return
    const ydoc = new Y.Doc()
    const provider = new WebsocketProvider(wsBase(), room, ydoc)
    const ytext = ydoc.getText('source')
    const undoManager = new Y.UndoManager(ytext)
    docRef.current = { ydoc, provider, ytext }

    const selectionListener = EditorView.updateListener.of((vu) => {
      if (!vu.selectionSet && !vu.docChanged) return
      // Only treat LOCAL user edits as "rendering…". Remote doc changes — the initial CRDT sync
      // that loads the document, and Claude's edits — arrive as programmatic yCollab dispatches
      // with no userEvent. The initial sync in particular changes content that already matches
      // disk, so it triggers no recompile and the resolver version never bumps → a "rendering…"
      // set here would never clear (the "stuck on Rendering when entering a project" bug).
      const userEdit = vu.docChanged && vu.transactions.some(
        (tr) => tr.isUserEvent('input') || tr.isUserEvent('delete') || tr.isUserEvent('undo') || tr.isUserEvent('redo'))
      if (userEdit && onDocChange) onDocChange()
      // reverse-locate: when the caret MOVES (navigation, not typing), report its UTF-8 byte
      // offset so the preview can show where that source renders.
      if (vu.selectionSet && !vu.docChanged && onCursor) {
        const head = vu.state.selection.main.head
        onCursor(enc.encode(vu.state.doc.sliceString(0, head)).length)
      }
      const sel = vu.state.selection.main
      if (sel.empty) return
      const text = vu.state.sliceDoc(sel.from, sel.to)
      const line = vu.state.doc.lineAt(sel.from).number
      onSelect && onSelect({ text, from: sel.from, to: sel.to, line })
    })

    const state = EditorState.create({
      doc: ytext.toString(),
      extensions: [
        lineNumbers(),
        highlightActiveLine(),
        drawSelection(),
        bracketMatching(),
        highlightSelectionMatches(),
        syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
        typst(),
        yCollab(ytext, provider.awareness, { undoManager }),
        keymap.of([...yUndoManagerKeymap, ...defaultKeymap, ...searchKeymap, indentWithTab]),
        selectionListener,
        EditorView.lineWrapping,
        EditorView.theme({ '&': { height: '100%' }, '.cm-scroller': { overflow: 'auto' } }),
      ],
    })
    const view = new EditorView({ state, parent: hostRef.current })
    viewRef.current = view
    provider.once('sync', () => {
      // Make Claude's edits undoable too. They arrive as REMOTE updates whose transaction
      // origin is the websocket `provider` (y-websocket applies them with that origin),
      // which the UndoManager does not track by default — so Ctrl+Z would skip them. Add the
      // origin only AFTER the initial sync (so the whole-document load isn't itself an undo
      // step), and clear any history captured during load.
      undoManager.addTrackedOrigin(provider)
      undoManager.clear()
      onReady && onReady()
    })

    return () => {
      view.destroy()
      provider.destroy()
      ydoc.destroy()
      viewRef.current = null
      docRef.current = null
    }
  }, [room])

  useImperativeHandle(ref, () => ({
    // Convert a tinymist (0-based row,col) range to offsets, select + scroll, return slice.
    highlight({ startLine, startCol, endLine, endCol }) {
      const view = viewRef.current
      if (!view) return null
      const d = view.state.doc
      const off = (ln, col) => {
        const line = d.line(Math.min(Math.max(ln + 1, 1), d.lines))
        return Math.min(line.from + Math.max(col, 0), line.to)
      }
      let from = off(startLine, startCol)
      let to = endLine == null ? from : off(endLine, endCol ?? startCol)
      if (to < from) [from, to] = [to, from]
      // tinymist resolves a click to a caret (from == to). A single word is ambiguous and,
      // for CJK runs, the word boundary cuts the block in half ("嘉北洋" -> "嘉北"). Expand
      // to the WHOLE LINE content instead (skipping leading indentation), so a click selects
      // the full sentence/line — what the user actually means by selecting a block.
      if (from === to) {
        const ln = d.lineAt(from)
        const lead = ln.text.length - ln.text.trimStart().length
        from = ln.from + lead
        to = ln.to
      }
      view.dispatch({
        selection: { anchor: from, head: to },
        effects: EditorView.scrollIntoView(from, { y: 'center' }),
      })
      view.focus()
      const text = view.state.sliceDoc(from, to)
      const lineObj = d.lineAt(from)
      const line = lineObj.number
      // also expose the whole line: an element click resolves to a caret that we expand to
      // a single word for the visual highlight, but callers often want the full line as the
      // anchor text (a lone word is ambiguous / non-unique).
      return { text, from, to, line, lineText: lineObj.text }
    },
    getText() {
      return viewRef.current ? viewRef.current.state.doc.toString() : ''
    },
    // Find `text` in the live document and select+scroll to it. Returns false if it no longer
    // exists (so the caller can do nothing rather than jump to a stale spot).
    jumpToText(text) {
      const view = viewRef.current
      if (!view || !text) return false
      const idx = view.state.doc.toString().indexOf(text)
      if (idx < 0) return false
      view.dispatch({
        selection: { anchor: idx, head: idx + text.length },
        effects: EditorView.scrollIntoView(idx, { y: 'center' }),
      })
      view.focus()
      return true
    },
  }), [])

  return <div className="cm-host" ref={hostRef} />
})

export default TypstEditor
