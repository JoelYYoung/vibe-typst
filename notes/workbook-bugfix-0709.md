# Bugfix Workbook - 2026-07-09

## Scope

This note summarizes the July 9 fixes for workspace startup, Codex/Claude project bootstrap, MCP edit propagation, stale preview rendering, and Codex sandbox prerequisites.

## Fixes

### Workspace Relaunch and Remote Access

- Checked the running workspace containers and service image state.
- Updated the workspace image and hot-updated existing containers without deleting mounted project data.
- Preserved container bind mounts, environment variables, port bindings, and running/stopped state during recreation.
- Confirmed the server-side restart path still uses the existing containerized workspace setup.

### Project Bootstrap for Codex

- Added Codex project setup alongside the existing Claude setup.
- New projects now get `AGENTS.md` and Codex configuration in addition to Claude files.
- `CLAUDE.md` can be represented as a symlink to `AGENTS.md`, so both assistants share one source of project instructions.
- Installed Codex into the workspace image.
- Added a Codex wrapper so project-local Codex config is loaded when running inside a generated project and cleared when leaving it.
- Added detection for Codex terminal sessions in addition to Claude terminal sessions.

### MCP Naming

- Renamed the MCP server from `web-typst` to `vibe-typst`.
- Updated generated configs and runtime paths so Codex and Claude use the expected `vibe-typst` MCP name.

### MCP Edit Propagation

- Fixed the bug where MCP edits reported success and appeared in MCP readback, but the browser editor stayed stale.
- Added an external edit sequence to server-side edit responses so browser clients can distinguish MCP-originated changes from ordinary editor typing.
- Hardened the frontend editor sync path so external edits force the editor model to reload from the authoritative document content.
- Added a stale Yjs replay guard in the backend so an older browser state cannot overwrite a newer MCP edit.
- Verified the Codex failure mode where a change flashed in the editor and then reverted was caused by stale client replay after an external edit.

### Preview and Figure Staleness

- Fixed stale preview behavior by adding content-derived render tokens.
- Preview pages now change identity when SVG bytes change, preventing old rendered figures from being reused after source edits.
- This addresses the case where the editor reflected a change but the figure or preview only refreshed after a manual browser reload.

### Codex Sandbox Warning

- Investigated the Codex warning:

  ```text
  Codex could not find bubblewrap on PATH.
  ```

- Confirmed the warning is worth fixing in the Linux workspace image because Codex officially expects `bubblewrap` for its Linux sandbox path.
- Added `bubblewrap` to the workspace image packages.
- Rebuilt `tcb-workspace:latest`.
- Hot-updated existing containers from the rebuilt image without deleting workspace data.
- Verified inside the updated container:

  ```text
  /usr/bin/bwrap
  bubblewrap 0.8.0
  codex doctor: 17 ok, 1 idle, 0 warn, 0 fail
  ```

## Regression Tests

Added focused regression tests for the real failures observed during debugging:

- Codex wrapper loads project Codex config inside a project directory and clears it outside the project.
- Stale browser/Yjs replay cannot undo a newer external MCP edit.
- The external edit guard does not clobber unrelated concurrent edits.
- Preview render tokens are content hashes and change when rendered SVG bytes change.

Current test result:

```text
Ran 4 tests
OK
```

## Deployment and Git

- Built and verified the updated workspace image locally.
- Updated existing containers from the new image.
- Committed the fixes through these local commits:

  ```text
  557ddac Add Codex workspace setup
  e46fbd1 Rename MCP server to vibe-typst
  8e74005 Detect Codex terminal sessions
  b4cbaf8 Resync editor after MCP edits
  3fa16f7 Force editor sync after MCP edits
  1b51e99 Harden editor sync for external edits
  81ebafb Fix preview cache and Codex MCP loading
  1b05eca Prevent stale Yjs replay after MCP edits
  df2f2b5 Install bubblewrap for Codex sandbox
  ```

- Initial SSH push failed because the host did not have a valid GitHub SSH key.
- Used authenticated `gh` HTTPS credentials for the final push.
- Merged the remote `main` history without force-pushing and preserved remote-only files such as `LICENSE` and `docs/figure/*`.
- Pushed successfully to:

  ```text
  git@github.com:JoelYYoung/vibe-typst.git
  ```

Final pushed head:

```text
fdcd4b4 Merge remote main history
```

## Follow-up: the "Split-Brain Room" revert bug (root cause the earlier fixes missed)

### Symptom

When **Codex** (not Claude) applied a comment via the MCP edit tools, the new content
rendered in the preview for a brief flash and then reverted to the **unedited** version.
The July 9 fixes above (external-edit sequence, stale-Yjs-replay guard, editor remount)
did **not** resolve it, because they all assume a single shared CRDT room — and the real
bug is that there were **two rooms for one file**.

### Root cause

A CRDT room stores two independently-computed fields:

- `key` — the room's identity (dict key + WebSocket room name), from
  `room_name` → `_base_key` → `runtime.file_key`.
- `path` — the disk file it flushes to, from `docstore._resolve`.

These resolved a **relative** path differently:

- `runtime.file_key("main.typ")` → `Path("main.typ").resolve()` → **process CWD** +
  `main.typ`.
- `_resolve("main.typ")` → `runtime.project_dir()` + `main.typ` (correct).

Codex passes the file as the relative string `"main.typ"` (that is what the comment store
records and hands back). The browser connects using the **absolute** path. So whenever the
server's CWD ≠ the project dir (always true in the container: uvicorn runs under `/app`,
projects live under `/workspace`), the two got **different room keys but the same disk
`path`**:

```
browser room:  key = hash(/workspace/example/main.typ)   path = /workspace/example/main.typ
codex   room:  key = hash(/app/backend/main.typ)          path = /workspace/example/main.typ   <- same file!
```

Two rooms, one whiteboard, neither reading the other. Combined with the invariant that **a
room reads disk exactly once (at birth) and thereafter only writes it**, the browser room
was born holding OLD and kept trying to impose OLD, unaware Codex had written NEW. The
revert is last-writer-wins on disk, and the browser is structurally the last writer:

1. Codex's room writes NEW to disk → resolver recompiles → preview flashes the new slide.
2. The same edit bumps the global `external_edit_seq`; the browser poll sees it and
   **remounts** the editor, reconnecting to *its* (stale) room.
3. That reconnect/activity nudges the stale room's observer → it flushes its in-memory OLD
   back to disk → preview reverts to unedited.

The July 9 remount mechanism, added to cure editor staleness, is what *weaponises* the
split brain: it guarantees the stale room gets poked — and gets the last flush — right
after every Codex edit.

### Fix

Derive the room identity from the same absolute path the disk writer uses. One line in
`backend/docstore.py`:

```python
def _base_key(file=None) -> str:
    return runtime.file_key(_resolve(file))   # was: file_key(file or current_file())
```

`_resolve` maps `None` → current file and `"main.typ"` → `project_dir/main.typ` (absolute),
so Codex's relative path and the browser's absolute path collapse to **one** room. No
regression for the `None` / absolute cases (identical key). All the existing single-room
safety machinery then works as designed.

### Regression test

`tests/test_regressions.py::SplitBrainRoomKeyTest` forces the failing condition (process
CWD ≠ project dir) and asserts `room_name(None)`, `room_name("main.typ")`, and
`room_name(<abs path>)` are all equal. Verified it fails on the old logic (mismatched
`hash`) and passes on the fix.

```text
Ran 5 tests
OK
```

## Follow-up 2: fixing anchor drift (line-addressed edits + StickyIndex comment anchors)

The room-unify fix above stops the disk revert, but a second, milder problem remained: MCP
edits are located by **content anchors**, which are *positional* references against a stale
snapshot. Once the agent has made one edit, an anchor copied from the original read no longer
matches (whitespace / `\\` escaping / the block already changed) → the "anchor not found"
retry churn seen in Codex's trace. Two complementary fixes:

### (a) Line-addressed edit tools (for the agent)

New MCP tools `replace_lines(start, end, new_text)` and `insert_at_line(line, text)` edit by
the **1-based line numbers get_document already prints** — no exact anchor, no quoting, no
escaping. Each call is applied atomically to the *current* room text (no offset drift between
calls). Backing functions live in `docstore.py`; the `/api/edit` dispatch and `mcp_server.py`
expose them. Guidance in the tool docstrings: read with get_document, then edit by line;
re-read before the next line-addressed edit (numbers shift). This is the preferred path for
structural rewrites like "split this slide into two".

Consecutive edits are already **serialized** in the backend: once `ensure_room` returns an
existing room, `replace_anchor`/`replace_lines`/… have no `await` before their
read-modify-write, so the second edit always sees the first's committed text. The only way
the first "breaks" the second is if the second's anchor referred to text the first changed —
which now correctly returns `anchor not found` instead of corrupting anything.

### (b) StickyIndex anchors (for comments)

A comment stored as a code-point span goes stale the instant anyone edits above it. pycrdt's
`StickyIndex` (the Yjs `RelativePosition`) binds to the **character's CRDT identity**, not its
offset, so it follows the text across inserts/deletes by either party and converges on every
replica. `docstore.make_rel_anchors(spans)` / `resolve_rel_anchors(rel)` create and resolve
them; `/api/comments` now stores `rel_anchors` for element selections at create time, and
`GET /api/comments/{id}/anchor` resolves them to live spans + text. Verified:

- Anchor on line 2's "banana", insert a line above → resolves to offset 6→14, still "banana"
  (no drift).
- Delete the anchored line → resolution stays in-bounds and deterministic (lands on the
  surviving boundary; the deleted text is not resurrected).

CRDT caveat (why this helps comments but not the agent's reasoning): the agent thinks in
*text*, not character IDs, so it can't plan edits against opaque StickyIndex handles — hence
line-addressed edits for the agent, StickyIndex for the durable comment layer.

### Tests

`tests/test_regressions.py::McpEditFlowTest` simulates the real path — an MCP call routed
through the actual `/api/edit` endpoint into a live pycrdt room, with CWD ≠ project dir:

- line edit lands in the browser's room and on disk (also re-guards the unify fix);
- `insert_at_line` pushes following lines down;
- two consecutive edits: the second sees the first, and a stale anchor fails cleanly;
- a StickyIndex anchor does not drift when a line is inserted above it;
- a StickyIndex anchor resolves deterministically after its target line is deleted.

`tests/test_regressions.py::McpFullCoverageTest` is the "real environment" harness: it calls
EVERY MCP tool the way the agent does and routes each through the actual FastAPI app (real
routing + JSON via httpx ASGITransport) into the real docstore room and SQLite store, with
CWD ≠ project dir, monitoring the live document text and comment state after each op. Covers
get_pending_comments / get_comment / list_all_comments / mark_comment_done /
mark_comment_dismissed / get_transcripts / get_document / find_in_document / replace_anchor
(incl. ambiguous + not-found refusals and `occurrence`) / insert_before_anchor /
insert_after_anchor / replace_range / replace_lines / insert_at_line, plus the
`/api/comments/{id}/anchor` StickyIndex round-trip. It already caught one real detail
(`anchor_text` is supplied by the browser payload, not derived server-side).

```text
Ran 11 tests
OK
```

## Follow-up 3: unified `apply_edits` primitive (redesign of the edit MCP)

The edit surface had grown to 7 mutating tools, 3 addressing schemes, and inconsistent
failure modes. Redesigned around a single primitive; the old tools remain as thin sugar so
nothing calling them breaks.

### The primitive

`docstore.apply_edits(edits, file, base_rev)` — one operation over a tagged-union **Selector**,
applied as an **atomic, all-or-nothing batch**:

- Selectors: `{by:"anchor", text, occurrence?, side?}` · `{by:"lines", start, end?}` ·
  `{by:"range", from, to}`. `side` = `in`|`before`|`after`; `lines` with no `end` is an
  insertion point.
- Each edit `{selector, text, expect?}`. `expect` is a per-edit **compare-and-swap**: the edit
  applies only if the selected span still equals `expect`, else the whole batch is refused.
- All selectors resolve against **one snapshot**; the batch applies together (highest offset
  first, so lower byte positions stay valid); overlaps are rejected. A per-room monotonic
  `rev` is returned and surfaced by `get_document` so the agent can pass `base_rev`.
- On failure: `{ok:false, conflict:true, index, error, context, rev}` — the live neighborhood
  so the agent re-aims in one round instead of blind retries.

This structurally kills the "first edit breaks the second's anchor" cascade (a whole intent is
one atomic batch computed against one read) and makes concurrent human edits safe (stale
`expect` conflicts instead of hitting the wrong place). `replace_anchor`, `insert_before/after`,
`replace_range`, `insert_text`, `replace_lines`, `insert_at_line` now delegate to it.

### Comments get the live location

The agent previously saw only the frozen `raw_context`, whose line numbers drift. Now
`get_pending_comments` / `get_comment` carry `location = {lines, current_text, rev}` resolved
live from the comment's StickyIndex (via `GET /api/comments/{id}/anchor`, extended to return
`lines` + `rev`). The tool text tells the agent to trust `location` over `raw_context`'s frozen
numbers — this is the "necessary and enough information" the agent needs to edit precisely.

### Agent-facing docs

`apply_edits` is documented as the preferred tool (atomic batch); the shared `_GUARD` and the
single-edit docstrings now point at it. `get_document` returns `rev`. See also
`notes/learn-crdt-mcp.md` for the model.

### Addressing choice (line vs anchor vs identity)

Recorded in `learn-crdt-mcp.md` §6: line numbers are most token-efficient for the agent but
unsafe alone in a live shared doc; content anchors are safer; CRDT `StickyIndex` is drift-proof
but only usable where we set it (comments). The `expect` CAS on line/anchor edits closes the
concurrency gap, so line-addressed edits can be the agent default while staying safe.

### Tests

- `McpFullCoverageTest` — every MCP tool (now incl. `apply_edits` batch, CAS/conflict,
  consecutive calls, overlap rejection, comment-survives-batch, and the live `location`).
- `ApplyEditsEdgeCaseTest` — multibyte/emoji byte-offset correctness, files without a trailing
  newline, empty document, insert-past-EOF append, adjacent (touching) batch edits, delete+insert
  in one batch, stale-`base_rev` still applies when the region is unchanged, `expect` guards a
  concurrently-changed region, and a comment anchor staying in-bounds after delete+reinsert.

```text
Ran 25 tests
OK
```

## Follow-up 4: adversarial testing round — StickyIndex panic + batch-order bugs

An adversarial scenario sweep (subagent-generated real situations, then verified against
pycrdt 0.14.1) turned up genuine defects in the follow-up 2/3 work. Fixed:

### Bug A (HIGH): `sticky_index(EOF, Assoc.AFTER)` panics → comment creation 500s

`pycrdt.Text.sticky_index(index, Assoc.AFTER)` panics (Rust `Option::unwrap() on None`) when
`index` is at end-of-document or the doc is empty — there is no successor element for AFTER to
bind to. Critically the panic surfaces as a `pyo3_runtime.PanicException`, which is a
**BaseException**, so it slips past `except Exception`. `make_rel_anchors` used `Assoc.AFTER`
for the span start, so **creating a comment on the last word of a file with no trailing
newline (or on an empty doc) crashed `POST /api/comments`** and left the comment un-anchored.
Fix: `_safe_sticky_index` binds with `Assoc.BEFORE` at/after EOF (verified always safe there)
and catches `BaseException`; the `/api/comments` guard now catches `BaseException` too. Also
`resolve_rel_anchors` catches `BaseException` and clamps `from <= to` so a zero-width caret
anchor can't resolve to a negative-length span after edits.

### Bug B (HIGH): two batch edits at the same offset applied in reverse input order

`apply_edits` applied resolved edits sorted by offset descending; the sort was stable, so two
inserts at the *same* offset kept input order under a descending sort and thus landed reversed
(`"AB"` + inserts `"X"`,`"Y"` at offset 1 → `"AYXB"`, not `"AXYB"`). Fix: sort by
`(from, input_index)` descending, so same-offset edits apply last-input-first and their text
ends up in input order.

### Bug C (MED): deleting the last line of a no-trailing-newline file left a phantom line

`replace_lines(3, 3, "")` on `"a\nb\nc"` produced `"a\nb\n"` (a dangling empty line) instead of
`"a\nb"`. Fix: when a `lines` deletion reaches EOF and the file has no trailing newline, also
consume the preceding `\n`.

### Non-bugs confirmed safe (kept as regression tests)

NFC vs NFD anchor mismatch refuses cleanly (no corruption near a combining mark); malformed
selectors (empty anchor, out-of-bounds range, inverted line range, unknown kind) all refuse
without mutating; insert at line 1 pushes the document down.

### Tests

Added to `ApplyEditsEdgeCaseTest`: comment anchor on trailing text of a no-newline file, comment
anchor on an empty doc (no crash), zero-width caret resolves in order, same-offset batch inserts
keep input order, last-line delete leaves no phantom, NFC/NFD refusal, insert-at-line-1
push-down, and malformed-selector refusals.

```text
Ran 33 tests
OK
```

## Follow-up 5: remove the redundant per-edit editor remount (flicker fix)

With the room unified (Follow-up 1), MCP edits go through the SAME shared CRDT room as the
browser, so they already broadcast into the live editor over the websocket. The frontend's
old behavior of **remounting the editor on every `external_edit_seq` bump** (a leftover from
the split-brain era, where it was the only way the browser picked up MCP edits) is now
redundant and is exactly what produced the visible flash/flicker. `App.jsx` now remounts the
editor ONLY on room rotation (corruption self-heal / project switch), which genuinely needs a
fresh Yjs doc. Missed-update safety is still covered by Yjs's own resync on reconnect.

## Follow-up 6: terminal lifecycle fixes

### Terminal must not refresh on left-pane collapse/expand

`App.jsx` unmounted the entire left `<section>` (editor + terminal) when `leftCollapsed`
toggled, so expanding the pane remounted `TermPanel` and reconnected its shell WebSocket —
refreshing the terminal just from a collapse/expand. Fixed by keeping the section MOUNTED and
hiding it with CSS (`display:none`) when collapsed (the `.grid`/`.pane` are flexbox, so a hidden
child collapses cleanly). The existing `refit()` path now also fires on expand (xterm was 0×0
while hidden). The terminal (and its shell) survives collapse/expand untouched.

### Relaunch a fresh shell when the shell exits

When the shell exited (`exit` / Ctrl-D / process death) the backend `sender()` stopped but the
`/pty` receive loop kept the WebSocket OPEN, so the browser never learned the shell was dead and
the terminal hung. Two changes:
- **Backend** (`app.py`): `sender()` now `await websocket.close()` on PTY EOF, so the socket
  closes when the shell exits.
- **Frontend** (`TermPanel.jsx`): refactored the socket into a `connect()` that relaunches on
  `onclose` (unless the panel is unmounting) — clears the pane and opens a new `/pty` socket,
  which forks a FRESH shell. (Ctrl-C interrupts the foreground command; it does not exit the
  shell, so it correctly does not relaunch.)

Verified live in the container: `GET /` serves the new bundle, and a scripted `/pty` client that
sends `exit` observes the server close the socket (which is what drives the frontend relaunch).

### Terminal corrupted (text out of margin) after hiding/collapsing while a TUI runs

Symptom: running `codex` (or `claude`) in the terminal, then collapsing the left column or
toggling the terminal off and back, left the TUI drawing past the right/bottom edges. Read the
FitAddon 0.11 source to rule out fit over-computing (it doesn't — it even reserves 14px for the
scrollbar). Real cause: the `ResizeObserver` fires `resize()` on EVERY host size change,
including when the terminal is hidden (`display:none` → host 0×0). `fit()` then returns the 2×1
floor and `resize()` SIGWINCH'd the PTY to **2×1**, so the TUI redrew into a 2×1 grid and stayed
corrupted when shown again. The Follow-up-5 collapse fix (hide instead of unmount) exposed this,
since the hidden terminal now stays mounted and keeps observing. Fix: `resize()` no-ops when
`hostRef.current.offsetWidth/Height === 0`, so a hidden terminal never shrinks the PTY. (Also
kept: scroll-position preservation across re-fit, and a settle re-fit after the socket opens.)

## Follow-up 7: presenter-to-projection indicator pointer

The presenter current-slide surface now acts as a press-and-hold indicator. A left press inside
the painted slide broadcasts normalized slide coordinates to the projection window; moving the
mouse moves the red indicator, and release, pointer cancellation, leaving presenter mode, or
slide navigation clears it. Coordinates are mapped through the actual contained slide rectangle,
so clicks in black letterbox space are ignored and the point remains aligned when presenter and
projection windows have different aspect ratios.

The transport is a presentation-scoped `BroadcastChannel`, independent from document editing and
the backend. Unit tests cover coordinate containment, normalization, cross-aspect projection, and
input validation. The Puppeteer regression covers press, drag, release, letterbox rejection, and
navigation cleanup against the live service.

## Follow-up 8: upload directly into a folder and preserve move collisions

External operating-system drops were handled only by the File Manager's root drop zone. Folder
rows understood the internal move MIME type but did not consume `Files`, so an uploaded file had
to land at the root before it could be moved. Folder rows now recognize external files, stop the
drop from bubbling to the root, upload with a destination-folder query, and expand to show the
result.

The reported `409` moving
`First-Class_Verification_Dialects_for_MLIR.md` into `papers` was a genuine collision: files with
that exact name already existed at both the project root and `papers/`, and their byte lengths and
SHA-256 hashes were identical. The old backend deliberately refused to overwrite the destination,
while the frontend discarded the response detail and displayed only `409 Conflict`.

Both uploads and internal moves now use the same non-destructive collision policy: keep the
existing destination and choose the first available numbered name (`paper_1.md`, `paper_2.md`,
and so on) for the incoming item. The API reports `collision_renamed`, and the frontend shows the
chosen name. API failures now surface the backend's `detail` message instead of only the HTTP
status. Project path validation also uses `Path.relative_to` containment, avoiding sibling-prefix
and macOS `/var` versus `/private/var` path errors.

Regression coverage includes direct destination-folder upload, upload collision preservation,
the exact First-Class-style move collision, and traversal through a sibling with a matching path
prefix. A live Puppeteer test dispatches the real external and internal drag events, verifies no
root upload is produced, checks both colliding file contents, asserts that no 409 occurs, and
removes only its uniquely named test artifacts.

## Follow-up 9: first version must be creatable from a fresh project

The redundant-save guard disabled both Save buttons whenever `status.dirty` was false. A project
with no Git repository returned `{initialized:false, dirty:false}` and an empty version list, so
the guard made the only action capable of initializing Git unreachable. This was reproduced on
joelyang's `paper-reading-group-20260716` project.

Fresh projects now report their state as unsaved, and the frontend independently permits Save
whenever there are zero versions. The second condition also handles a clean existing repository
whose tags were deleted: saving creates a new `v1` on its current HEAD without a redundant
commit. Backend tests create `v1` from both states; unit and intercepted-browser regressions
verify that a clean repository with an existing version still disables redundant saves while
both first-version controls remain enabled for the zero-version state.

## Follow-up 10: newest completed comments first

Comments were always returned and rendered in ascending creation sequence, although the store
already records `done_at`. The Done view now orders by completion time descending, with
`updated_at`, `created_at`, and descending sequence as deterministic fallbacks for legacy/tied
rows. Pending and All retain their existing sequence order.

The backend regression deliberately completes comment 2 after comment 1 and requires `[2, 1]`,
so the former `ORDER BY seq` implementation fails the test. Frontend unit and Puppeteer tests
likewise feed creation order `[1, 2]` with comment 2 completed later and assert that the rendered
cards are `#2`, then `#1`.

## Operational Notes

- Existing workspace data was not intentionally removed or regenerated during the container hot updates.
- During the bubblewrap rollout, the `joelyang` container was recreated and initially left
  stopped to preserve its prior state. It is now running after the later hot updates, with
  restart policy `unless-stopped` so Docker relaunches it after a host/runtime restart.
- `kangaroo` was running and was restarted on the new image.
- Future changes to MCP edit behavior should run the regression suite before image rebuilds or container rollout.
