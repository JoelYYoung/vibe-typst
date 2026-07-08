# Workbook — Design (requirements)

Captured design intent for the Typst Comment Bridge. This file is the **what/why**;
`workbook_implementation.md` is the **how**.

## Goal

A web app that is a **shared human↔Claude editor + commenting layer** for a Typst
slide deck. The human points at slide elements visually and leaves comments; Claude
reads them over MCP, edits the Typst, and ticks them done. Referencing "this element
on this slide" must be effortless and robust.

## Requirements

### R1 — Choose the working file
- Pick which `.typ` to work on (browse `.typ` files under a project root / file picker).
- Switching files reloads source + render + that file's comments.

### R2 — Split editor + preview
- Left: the Typst **source editor**. Right: the **rendered slides**.

### R3 — Element-level selection with bidirectional source mapping
- Select a specific **element inside a page** (not just a spot), e.g. a title, a code
  line, an arrow, a box.
- The selected element's **corresponding Typst source highlights on the left**, like
  clicking in VSCode's Typst preview jumps/highlights the source.
- Interactions: **click** to select, **box/marquee** select, **Ctrl/Cmd-click** to
  multi-select several elements.

### R4 — Page-level selection
- A separate mode to select a **page as a unit** (by page number / the page frame),
  not its text. Enables operations like "insert a slide after this page" or "delete
  this page".
- A page selection does **not** attach the whole page's content as context — just the
  page identity.

### R5 — Comments
- Add **multiple comments** in a session before handing off.
- Anchored by **content/position so they survive Claude's edits** (no line-number drift).
- Each comment can **show the raw context** that will be handed to Claude (anchor text,
  surrounding source, page, region, body) — for debugging what Claude actually sees.

### R6 — Durable storage
- Comments are **persisted** (never lost on restart) and kept as **history**.

### R7 — Human↔Claude shared editor
- Claude edits the Typst **through MCP** (tool calls), **not** by writing the file
  directly behind the app's back.
- The human can **also edit directly** in the web editor.
- **Conflicts between Claude's and the human's edits are handled gracefully** (real-time
  merge, no clobbering).

### R8 — A real Typst editor
- The editor must be reasonably complete and **Typst-aware**: syntax highlighting,
  **undo/redo**, find, etc. — not a bare textarea.

### R9 — Claude handoff
- MCP tools to fetch pending comments, read the current document, apply edits, and mark
  done / dismissed.
- A "Run Claude" affordance to kick off the headless agent, plus the option to drive
  Claude manually with the MCP server configured.

## Non-goals (for now)
- Multi-user (beyond one human + Claude).
- Editing arbitrary projects with complex multi-file imports (single main file first;
  imports can render but commenting targets the main file initially).
