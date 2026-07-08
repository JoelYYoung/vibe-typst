# Architecture & Design

This document covers the design philosophy and technical decisions behind Vibe Typst.

---

## The core problem

The usual approach to AI-assisted document editing has a mismatch: the AI operates on raw text (line numbers, string matches), while the human operates on visual output (rendered slides). Every edit request requires translating between the two views — "the bold text in the top-right corner of slide 4" becomes a hunt through source code.

Vibe Typst closes that gap by making the rendered slide directly interactive. The human points at what they want changed; the system resolves the visual selection to the exact source span; the AI edits source. No translation required on either side.

---

## Why Typst

Typst occupies the gap between LaTeX (powerful but painful) and presentation tools like Keynote or PowerPoint (easy but unscriptable). It compiles fast (milliseconds, not seconds), has a clean programmable syntax, and outputs publication-quality PDFs. It's also a single source file — no binary format, no XML inside a zip — which makes version control, diffs, and AI editing natural.

The tradeoff is that it requires a compiler. That compiler is what makes the visual-to-source mapping possible.

---

## Source as single source of truth

The `.typ` file on disk is the only durable state. Everything else is derived from it:

- The rendered SVGs are produced by the compiler and cached temporarily
- The CRDT document in memory is seeded from disk on project open
- Speaker notes, comments, and versioned snapshots all reference the source text

This means the system is simple to reason about: if you lose the database or the render cache, you lose nothing important. `git restore` to any saved version gives you the exact state that was committed.

---

## CRDT for live collaboration between human and AI

The source text is stored as a Yjs/pycrdt CRDT document. The browser editor (CodeMirror) and Claude (via MCP) both write to the same document through the backend. Changes from either side merge automatically — Claude can be in the middle of editing one paragraph while the human is typing in another, and neither will see a conflict or lose work.

The CRDT is held in the backend process and persisted to disk on every change. It is NOT persisted as a separate sidecar — the `.typ` file is the persistence layer. On restart, the CRDT is reconstructed from whatever is on disk. This keeps the system simple and eliminates a class of divergence bugs where the sidecar and the file get out of sync.

---

## Content-anchored comments, not line numbers

Comments store a text snippet (the "anchor") from the source at the time of creation, not a line number. When Claude needs to edit a comment's anchor, it searches for the snippet in the current document and replaces it.

This is intentional: line numbers become wrong the moment anything above them changes. Content anchors remain valid through any restructuring, as long as the targeted text itself hasn't been removed. When a comment's anchor can no longer be found, it's flagged rather than silently editing the wrong place.

---

## Server-side rendering and click-to-source

The Typst compiler is run server-side by a Rust process (`tcb-resolver`). This is the only way to get reliable position metadata: the compiler can emit a mapping from rendered element positions to source spans. Pure-browser approaches (typst.ts) can render slides but cannot produce this mapping — they don't have access to the internal compiler representation.

The resolver watches the source file via mtime polling, recompiles on change, and emits SVG pages. Each page's URL carries a content hash (BLAKE2b of the SVG bytes), so unchanged pages are served from the browser cache without a network round-trip; only changed pages are re-fetched.

---

## Presentation as a two-window system

The presenter console and the projection screen are two separate browser windows (or tabs) communicating over `BroadcastChannel` — a browser-native API that works without a server round-trip. The presenter window sends its current page number; the projection window displays it. The latency is effectively zero.

Speaker notes are stored inline in the source as `#speaker-note["…"]` (touying's convention). This means they travel with the deck through version control and are visible to Claude, which can draft, edit, or translate them in the same workflow as slide content.

---

## Multi-user server mode

In server mode, each user gets an isolated Podman container running the full application stack. The control plane handles authentication (cookie-based sessions, PBKDF2 passwords), routes requests to the right container, and manages container lifecycle.

This design gives strong isolation — one user's files and processes cannot interfere with another's — without requiring a complex distributed system. Each container is a single-process FastAPI application with no external dependencies except the shared Podman host.

The Cloudflare tunnel means the control plane doesn't need a public IP or open firewall ports. The tunnel terminates at localhost; all traffic is end-to-end encrypted between the browser and Cloudflare.

The control plane also tracks authenticated HTTP and WebSocket activity. Workspaces that are
idle past the configured threshold are stopped and their sessions are cleared; the next login
starts the existing container again.

---

## Component map

```
browser
  ├── CodeMirror 6          Typst editor with syntax highlighting
  ├── Yjs + y-websocket     CRDT sync with backend
  ├── React                 UI, state, routing
  └── BroadcastChannel      Presenter ↔ projection sync (no server)

backend  (FastAPI, single process)
  ├── pycrdt room           CRDT document, seeded from .typ on open
  ├── WebSocket endpoint    Yjs sync channel
  ├── tcb-resolver          Rust subprocess: watches .typ, renders SVGs
  ├── SQLite                Comment store (append-only history)
  ├── vcs.py                Git wrapper: save/restore named versions
  └── mcp_server.py         MCP stdio server for Claude Code

control plane  (server mode only)
  ├── FastAPI               Auth, session management, user admin
  ├── SQLite                Users, sessions
  ├── Podman                Per-user container lifecycle
  ├── Idle sweeper          Stops inactive workspaces and clears sessions
  └── HTTP/WS proxy         Routes requests to the right container
```
