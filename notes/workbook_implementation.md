# Workbook — Implementation plan

How we satisfy `workbook_design.md`.

## Decision (2026-06-18)

User chose the **full-function, most-robust** path, not the minimal one:
- Build the whole architecture (CRDT shared doc + typst.ts element mapping), not a
  cut-down version. The "phases" below are now just build order, not feature gates.
- Preview: **embed typst.ts directly** (self-contained, full control), not the
  tinymist sidecar.
- Storage: **SQLite** with an append-only `comment_events` history.

### Build status
- [x] Durable SQLite store (`store.py`) with history + legacy JSON import — R6.
- [x] Runtime active-file state (`runtime.py`) + file picker (`files.py`) — R1.
- [x] Per-file render dirs; `typst_service.py` keyed off the active file.
- [x] Server-generated `raw_context` per comment (`context.py`) — R5.
- [x] CRDT docstore + pycrdt-websocket room (`docstore.py`, `/ws/{room}`) — R7.
      **Verified live**: browser edit -> persists to disk; Claude HTTP edit -> broadcasts
      to browser. (Gotcha found: observe at DOC level, not Text level — Text.observe
      misses remote apply_update in pycrdt 0.14.)
- [x] MCP edit tools routed through the doc over HTTP (get_document / replace_anchor /
      insert_before_anchor / insert_after_anchor / replace_range) — R7/R9. All
      content-anchored with an ambiguity guard.
- [x] tinymist `preview` sidecar manager (`preview.py`) — owns span->source resolution.
      Span research was definitive: pure-browser typst.ts CANNOT resolve a span to a
      source range; tinymist (Rust) is required. Frontend embeds its webview + listens
      on the control plane for `editorScrollTo`. — R3 infra.
- [x] Frontend: CM6 + codemirror-lang-typst + y-codemirror.next + yUndoManager
      collaborative editor (`TypstEditor.jsx`) — R8.
- [x] Frontend: file picker, page/element comment composer, raw-context + history UI
      (`App.jsx`, `CommentCard.jsx`).
- [x] `.mcp.json` added to the Paper Agents project so Claude can be driven manually.
- [~] Element click -> source highlight: single-click works via tinymist control plane.
      **Marquee + Ctrl-multi-select still TODO** — needs in-DOM typst-dom rendering
      (iframe can't be hit-tested cross-origin). Page-selection is a composer toggle now;
      page->slide span mapping via the `outline` event is a follow-up.

### Remaining
- Reverse sync (editor cursor -> preview scroll) via control-plane `panelScrollTo`.
- Marquee rectangle select (sample a grid of points -> resolve each -> dedupe).

## Redesign (2026-06-18, second pass — selection UX + file browser)

Driven by user feedback. Two big architecture changes:

### Preview = images + a coordinate resolver (no iframe, no typst-dom)
The span research said in-DOM rendering needs vendoring tinymist's *private* `typst-dom`
(not on npm) at pinned rc versions — heavy/fragile. But the click protocol turned out to
be **coordinate-based** (`src-point {page_no,x,y}`, verified working over a bare socket).
So we DON'T render with tinymist at all:
- Frontend renders pages as **server-side typst PNGs** (real fidelity, full DOM control).
- Backend owns a persistent connection to tinymist's two planes and exposes
  `POST /api/preview/resolve {page_no,x,y} -> {start:[row,col]}`. (tinymist's Origin check
  blocks browser->tinymist sockets, so the backend, with no Origin, owns them.)
- **Gotcha found**: tinymist splits the data-plane message on whitespace, so the
  `src-point` JSON MUST be compact (`json.dumps(..., separators=(',',':'))`). Spaces ->
  silent no-op. Cost an hour to find (node's `JSON.stringify` is compact, so it "worked"
  in JS but not from Python).
- UX (`PreviewPane.jsx`): each page has a number badge (hover -> **+ add page**); hovering
  a page shows a floating **+ add to selection** at the cursor; a plain click jumps the
  editor to that element's source. A caret resolves to the word/line under it for anchor text.
- Multi-select: selections accumulate as removable chips in the composer; one comment
  can carry many element/page selections (`selections[]`, rendered into `raw_context`).

### File model = open any file, cache beside it
- `runtime.py` now tracks an absolute current FILE; its directory is the project
  (tinymist `--root`, comment store live there). Store path is dynamic (`store.set_path`).
- `GET /api/browse` + `POST /api/open-file` + `FileBrowser.jsx` modal (navigate dirs under
  `TCB_BROWSE_ROOT`, default home; open any `.typ`). The comment `.slide-comments.db`
  now lives in the opened file's own directory.

### Status: built + verified at the API level on the real MSLI deck (39 pages, resolve OK).
Browser interaction (hover/click/chips) awaits a manual pass.

## Architecture (target)

```
            ┌──────────────────────── Browser (React) ─────────────────────────┐
            │  CodeMirror 6 editor            typst.ts SVG preview              │
            │  - lezer-typst highlight        - renders the Y.Doc text          │
            │  - y-codemirror.next binding    - SVG carries source spans        │
            │  - yUndoManager (undo/redo)     - click/marquee element → span    │
            │        │  Yjs Doc (shared)             │  span → highlight in CM   │
            └────────┼───────────────────────────────┼──────────────────────────┘
                     │ y-websocket                    │ /api (REST)
            ┌────────▼───────────────────────────────▼──────────────────────────┐
            │  FastAPI backend                                                   │
            │  - pycrdt Y.Doc per file  ← single source of truth for text        │
            │  - pycrdt-websocket room  ← human + Claude sync here               │
            │  - persists Y.Doc → .typ  (debounced) + reconciles external edits  │
            │  - SQLite: comments (durable + history)                            │
            │  - typst compile → SVG/PNG                                          │
            │  - apply_edit(...) endpoint  ← MCP server calls this               │
            └────────┬───────────────────────────────────────────────────────────┘
                     │ stdio MCP                  ▲ HTTP
            ┌────────▼──────────┐                 │
            │  MCP server       │  get_pending_comments, get_document,            │
            │  (Claude Code)    │  apply_edit / replace_anchor / insert_after,    │
            │                   │  mark_comment_done … ──────────────────────────┘
            └───────────────────┘
```

Key idea: **the Y.Doc (CRDT) is the single source of truth for the file's text.** Both
the human (CodeMirror) and Claude (MCP→backend) mutate it; the backend persists it to
`.typ`. CRDT merge means edits never clobber each other.

## Library choices (researched)

| Need | Library | Notes |
|------|---------|-------|
| In-browser Typst render **with source spans** | **typst.ts / reflexo-typst** (`@myriaddreamin/typst.ts`) | SVG renderer that embeds source spans; this is what `typst-preview`/`tinymist` use for SyncTeX-like click-to-source. Gives element → byte-span in the `.typ`. |
| (alt) preview sidecar | **tinymist preview** | Run as a sidecar, embed its renderer + jump protocol. Less code, heavier dep. Fallback if typst.ts integration is too raw. |
| Code editor | **CodeMirror 6** | Mature; find, multi-cursor, etc. |
| Typst syntax | **lezer-typst** grammar for CM6 | highlighting (community grammar). |
| Collab binding | **y-codemirror.next** | binds Yjs ↔ CM6, includes remote cursors. |
| Undo/redo | **yUndoManager** (Yjs) | collaborative-aware undo (only undoes *your* changes). |
| CRDT backend | **pycrdt** + **pycrdt-websocket** | Python Y.Doc + y-websocket room; per-update persistence to file/db. |
| Comment store | **SQLite** | durable + history (replaces the current JSON file). |

## The drift problem → CRDT relative positions

Today a comment anchors by `anchor_text` (content search). Once the Y.Doc exists, the
**robust anchor is a Yjs `RelativePosition`** (or a pair, for a range): it is a CRDT
position that automatically tracks the right spot across insertions/deletions by either
party. So:
- Phase ≤2: anchor = `anchor_text` (+ advisory offsets) — search to locate.
- Phase 3: anchor = `{ rel_from, rel_to }` RelativePositions + `anchor_text` as a
  human-readable label / fallback. Comments then never get lost or misplaced.

## Element ↔ source ↔ page mapping

- typst.ts SVG nodes carry a source span. **Click** → read span → set CodeMirror
  selection (highlight). **Marquee** → union of spans of enclosed nodes. **Ctrl-click**
  → accumulate spans (multi-select), store as a list.
- **Page selection (R4):** each rendered page maps to the enclosing `#slide[...]`/
  `#slide(...)` call. Resolve by taking any element-span on the page and walking out to
  the top-level slide call (or use `typst query` for `<slide>` metadata if we add a
  label). Store `{ kind: "page", page_n, slide_span }` with **no body content** — only
  the slide identity, so "insert after / delete page" can act structurally.

## Comments — data model (SQLite)

```
comment(
  id TEXT PK, seq INT, file TEXT,
  kind TEXT,                 -- 'element' | 'page'
  page INT,
  anchor_text TEXT,          -- selected source (element kind)
  anchor_spans JSON,         -- [[from,to], ...] advisory byte offsets / multi-select
  rel_anchors JSON,          -- Yjs RelativePositions (phase 3)
  region JSON,               -- normalized bbox(es) on the page (visual)
  body TEXT,                 -- the instruction
  raw_context TEXT,          -- exact text handed to Claude (R5) — generated at create
  status TEXT,               -- pending | done | dismissed
  created_at, updated_at, done_at, done_note
)
```

`raw_context` is rendered when the comment is created and shown behind a "raw" toggle
on each card (R5): page, anchor snippet ± N lines of surrounding source, region, body —
exactly the blob `get_pending_comments` returns.

## MCP tools (target)

- `get_pending_comments()` → list with `raw_context`.
- `get_document(file?)` → current text (so Claude reads the live Y.Doc, not stale disk).
- `apply_edit(anchor, new_text)` / `replace_range(from,to,text)` / `insert_after_page(n,text)` /
  `delete_page(n)` → mutate via backend → Y.Doc → broadcast + persist.
- `mark_comment_done(id, note)` / `mark_comment_dismissed(id, reason)`.

Claude editing goes **through these tools** (R7), so the backend, the human's editor,
and the file all stay in sync.

## Phases

- **Phase 1 — quick wins on the current stack** (no new heavy deps):
  - R1 file picker (backend lists `.typ` under a root; frontend dropdown).
  - R6 SQLite store + history (migrate from JSON).
  - R5 raw-context generation + "raw" toggle per comment.
  - R4 page-selection mode (select page → structural comment, no content).
- **Phase 2 — element-level source mapping:**
  - Swap PNG preview for **typst.ts SVG**; click/marquee/Ctrl-select element → highlight
    source in CM (R3). Anchors become spans.
- **Phase 3 — collaborative editing:**
  - **pycrdt** Y.Doc backend + **y-websocket** room; CM6 + **y-codemirror.next** +
    **lezer-typst** + **yUndoManager** (R7, R8). Claude edits via MCP→backend→Y.Doc.
  - Move comment anchors to Yjs RelativePositions.

## Risks / open questions

- typst.ts span granularity & API stability (it's young) — Phase-2 spike needed.
- Mapping a page → its `#slide` call reliably; may add a hidden `<slide-N>` label per
  slide in the deck to make `typst query` trivial.
- pycrdt ↔ y-websocket protocol version compatibility with `y-codemirror.next`.
- Persisting Y.Doc → `.typ` while reconciling out-of-band file edits (git, other editor).
- Multi-file Typst (`#import`): render is fine; commenting targets main file first.

## What exists today (Phase 0, done)

PNG render, click-a-spot region, source-text-selection anchor, JSON comment store,
MCP (`get_pending_comments` / `mark_comment_done` …), "Run Claude" button. See `README.md`.
