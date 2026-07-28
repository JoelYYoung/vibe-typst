# Remote Project-Control MCP Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the authenticated project-control MCP with safe file transfer/trash, Typst CRDT/comment tools, PDF transcript/replacement tools, previews, and exports.

**Architecture:** Workspace-owned code performs every filesystem or document mutation so existing project-root, CRDT, and PDF-managed-state invariants remain authoritative. The control plane supplies bounded transfer capabilities and maps MCP tools to the active workspace through the handle/gateway foundation. Large bytes move through short-lived upload/download endpoints rather than MCP JSON.

**Tech Stack:** Python 3.11+, FastAPI streaming requests/responses, MCP Python SDK 1.x, SQLite, SHA-256, PyMuPDF, existing Typst CRDT and PDF services, unittest.

## Global Constraints

- This plan depends on the completed foundation plan and its exact `PatIdentity`, project-handle,
  `WorkspaceGateway`, `McpServiceError`, and FastMCP interfaces.
- The server-side project remains the only source of truth; no directory synchronization exists.
- Inline decoded uploads are limited to 1 MiB.
- Staged uploads are limited to 100 MiB, expire after 15 minutes, and allow at most two live
  sessions per PAT.
- Download capabilities expire after five minutes.
- Upload installation is atomic and verifies declared byte length and SHA-256.
- Generic file operations cannot mutate the active Typst main document or PDF-managed state.
- Ordinary text writes require `expected_sha256`; no silent overwrite is allowed.
- Deleted ordinary files remain recoverable for 30 days; MCP cannot purge trash.
- Typst edits go through CRDT `/api/edit` with `base_rev`.
- PDF replacement delegates to the existing locked validation/version-capture flow.
- PDF projects do not expose comment operations.
- No tool executes a shell command supplied by the caller.
- Do not touch the existing untracked `docs/design/` directory.

---

## File Structure

- Create `backend/remote_files.py`: bounded reads, hash-guarded text writes, protected-path checks,
  trash, restore, and staged-file installation.
- Create `backend/preview_service.py`: active Typst/PDF page-to-PNG observation.
- Modify `backend/app.py`: workspace-only JSON endpoints for remote file/trash/staged operations
  and preview bytes.
- Modify `backend/projects.py`: reusable protected-path and regular-file helpers where required.
- Extend `control/mcp_store.py`: upload/download session persistence and cleanup.
- Create `control/mcp_transfer.py`: one-time HTTP PUT/GET capability routes.
- Extend `control/workspace_gateway.py`: file, document, comment, preview, transcript, and PDF calls.
- Extend `control/remote_mcp.py`: all remaining MCP tool definitions and type-capability dispatch.
- Create `tests/test_remote_files.py`: file hashes, managed-path protection, trash, restore, staging.
- Create `tests/test_control_mcp_transfer.py`: upload/download capability and isolation tests.
- Create `tests/test_control_mcp_operations.py`: remote tool adapter and real MCP integration tests.

### Task 1: Hash-guarded ordinary file service

**Files:**
- Create: `backend/remote_files.py`
- Modify: `backend/projects.py:309-379`
- Modify: `backend/app.py:1868-2135`
- Test: `tests/test_remote_files.py`

**Interfaces:**
- Produces:
  - `read_text(project: dict, rel_path: str, offset: int = 1, limit: int = 120) -> dict`
  - `write_text(project: dict, rel_path: str, content: str, expected_sha256: str) -> dict`
  - `create_directory(project: dict, rel_path: str) -> dict`
  - `move_item(project: dict, old_rel: str, dest_rel: str) -> dict`
  - `install_file(project: dict, staged_path: Path, dest_rel: str, overwrite: bool, expected_sha256: str | None) -> dict`
  - `sha256_file(path: Path) -> str`
- Consumes: `projects._resolve_project_path`, `projects.reject_pdf_managed_mutation`, active project
  metadata, and runtime current main path.

- [ ] **Step 1: Write failing safe-file tests**

```python
def test_write_requires_current_hash_and_refuses_active_main(self):
    asset = self.project_dir / "notes.md"
    asset.write_text("old", encoding="utf-8")
    observed = hashlib.sha256(b"old").hexdigest()
    result = remote_files.write_text(self.project, "notes.md", "new", observed)
    self.assertEqual(result["sha256"], hashlib.sha256(b"new").hexdigest())
    with self.assertRaises(remote_files.RevisionConflict):
        remote_files.write_text(self.project, "notes.md", "lost", observed)
    with self.assertRaisesRegex(ValueError, "active Typst main"):
        remote_files.write_text(self.project, "main.typ", "bypass", self.main_hash)

def test_symlink_and_pdf_managed_paths_are_rejected(self):
    (self.project_dir / "escape").symlink_to(self.outside_dir, target_is_directory=True)
    with self.assertRaises(PermissionError):
        remote_files.read_text(self.project, "escape/secret.txt")
```

Include a PDF fixture proving `document.pdf`, `transcript.json`, `.vibe-typst.json`, lock files,
and version state cannot be installed/written/moved.

- [ ] **Step 2: Run and verify RED**

Run:

```bash
backend/.venv/bin/python -m unittest discover -s tests -p 'test_remote_files.py' -v
```

Expected: missing `remote_files`.

- [ ] **Step 3: Implement bounded reads**

Return at most 400 lines and at most 256 KiB of UTF-8 text:

```python
{
    "path": rel_path,
    "sha256": sha256_file(target),
    "size": target.stat().st_size,
    "total_lines": len(lines),
    "shown": f"{start}-{end}",
    "text": "\n".join(numbered_lines),
    "truncated": end < len(lines),
    "next": end + 1 if end < len(lines) else None,
}
```

Reject directories, symlinks, and hidden/private paths. For invalid UTF-8 or files larger than
4 MiB, return the path, size, SHA-256, and `download_required=True` without file bytes; the
control-plane `read_text_file` tool turns that result into a five-minute download capability.

- [ ] **Step 4: Implement guarded writes and atomic installs**

For existing files:

1. open and hash a pinned regular file;
2. compare `expected_sha256`;
3. write a same-directory temporary file;
4. `flush`, `os.fsync`, re-check destination identity/hash;
5. `os.replace` the temporary file.

For new upload destinations, require `overwrite=False`. For an existing destination with
`overwrite=True`, require a matching `expected_sha256`. Never follow a destination symlink.

- [ ] **Step 5: Add active-project remote file endpoints**

Add endpoints to `backend/app.py` that call only `remote_files` for the current active project:

```text
GET  /api/agent/files/read
POST /api/agent/files/write
POST /api/agent/files/mkdir
POST /api/agent/files/move
POST /api/agent/files/install
```

Each response includes `project_id` and `context_version`. Require a valid active project and
map `RevisionConflict` to HTTP 409 with current SHA-256.

- [ ] **Step 6: Run focused and existing file/PDF tests**

```bash
backend/.venv/bin/python -m unittest discover -s tests -p 'test_remote_files.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_pdf_projects.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_regressions.py' -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add backend/remote_files.py backend/projects.py backend/app.py tests/test_remote_files.py
git commit -m "feat: add safe remote file operations"
```

### Task 2: Recoverable project file trash

**Files:**
- Modify: `backend/remote_files.py`
- Modify: `backend/app.py:1868-2135`
- Test: `tests/test_remote_files.py`

**Interfaces:**
- Produces:
  - `trash_item(project: dict, rel_path: str, actor_token_id: str, now: float | None = None) -> dict`
  - `list_trash(project: dict, now: float | None = None) -> list[dict]`
  - `restore_trash(project: dict, trash_id: str) -> dict`
  - `sweep_trash(projects_root: Path, now: float | None = None) -> int`
- Consumes: safe path helpers and `/workspace/.tcb/trash/<project-id>`.

- [ ] **Step 1: Write failing trash tests**

```python
def test_delete_moves_payload_outside_project_and_restore_is_collision_safe(self):
    original = self.project_dir / "assets" / "logo.svg"
    original.parent.mkdir()
    original.write_text("<svg/>", encoding="utf-8")
    deleted = remote_files.trash_item(
        self.project, "assets/logo.svg", "pat-1", now=100
    )
    self.assertFalse(original.exists())
    self.assertEqual(remote_files.list_trash(self.project, now=101)[0]["id"], deleted["id"])
    original.write_text("replacement", encoding="utf-8")
    with self.assertRaises(FileExistsError):
        remote_files.restore_trash(self.project, deleted["id"])
    original.unlink()
    remote_files.restore_trash(self.project, deleted["id"])
    self.assertEqual(original.read_text(), "<svg/>")
```

Also test recursive directory trash, 30-day sweep, symlink rejection, active main rejection, and
PDF-managed-state rejection.

- [ ] **Step 2: Run and verify RED**

```bash
backend/.venv/bin/python -m unittest discover -s tests -p 'test_remote_files.py' -v
```

Expected: missing trash functions.

- [ ] **Step 3: Implement trash layout and atomic moves**

Use:

```text
<projects-root>/.tcb/trash/<project-id>/<trash-id>/payload
<projects-root>/.tcb/trash/<project-id>/<trash-id>/metadata.json
```

Metadata contains only:

```python
{
    "id": trash_id,
    "project_id": project["id"],
    "original_path": rel_path,
    "kind": "file" or "directory",
    "deleted_at": now,
    "expires_at": now + 30 * 86400,
    "actor_token_id": actor_token_id,
}
```

Move with `os.replace` only on the same filesystem. Write metadata to a temporary file and rename
it after the payload move. If metadata publication fails, restore the payload to its original path.

- [ ] **Step 4: Add trash endpoints**

```text
POST /api/agent/files/delete
GET  /api/agent/files/trash
POST /api/agent/files/restore
```

The delete body contains relative path and non-secret `actor_token_id`. No endpoint purges trash.

- [ ] **Step 5: Run tests and commit**

```bash
backend/.venv/bin/python -m unittest discover -s tests -p 'test_remote_files.py' -v
git add backend/remote_files.py backend/app.py tests/test_remote_files.py
git commit -m "feat: add recoverable project file trash"
```

### Task 3: One-time upload and download capabilities

**Files:**
- Modify: `control/mcp_store.py`
- Create: `control/mcp_transfer.py`
- Modify: `control/main.py:510-780`
- Modify: `control/workspace_gateway.py`
- Modify: `backend/app.py:1641-1732`
- Modify: `backend/app.py:1280-1392`
- Test: `tests/test_control_mcp_transfer.py`
- Test: `tests/test_remote_files.py`

**Interfaces:**
- Produces:
  - `begin_upload(db_path, identity, kind, project_id, destination, size, sha256, filename) -> tuple[dict, str]`
  - `authorize_upload(db_path, upload_id, capability, now=None) -> UploadSession`
  - `complete_upload(db_path, upload_id, identity) -> UploadSession`
  - `begin_download(db_path, identity, project_id, backend_path) -> tuple[dict, str]`
  - HTTP `PUT /mcp-upload/{upload_id}`
  - HTTP `GET /mcp-download/{download_id}`
- Consumes: `WORKSPACE_BASE/<username>/.tcb/uploads`, PAT identity, gateway, and backend staged
  install/create/replace endpoints.

- [ ] **Step 1: Write failing session-binding tests**

```python
def test_upload_capability_is_one_time_and_bound_to_metadata(self):
    public, capability = mcp_store.begin_upload(
        self.db_path, self.identity, "file", "p1", "assets/a.png",
        4, hashlib.sha256(b"data").hexdigest(), "a.png",
    )
    session = mcp_store.authorize_upload(
        self.db_path, public["id"], capability, now=public["created_at"] + 1
    )
    self.assertEqual(session.destination, "assets/a.png")
    mcp_store.mark_upload_received(self.db_path, session.id, 4)
    completed = mcp_store.complete_upload(
        self.db_path, session.id, self.identity
    )
    self.assertEqual(completed.state, "finishing")
    with self.assertRaisesRegex(McpServiceError, "UPLOAD_ALREADY_USED"):
        mcp_store.complete_upload(self.db_path, session.id, self.identity)
```

Test wrong PAT, wrong capability, expiry, more than two live sessions, oversized body, short body,
long body, checksum mismatch, interrupted PUT cleanup, and cross-user access.

- [ ] **Step 2: Run and verify RED**

```bash
control/.venv/bin/python -m unittest discover -s tests -p 'test_control_mcp_transfer.py' -v
```

Expected: upload session functions/routes are absent.

- [ ] **Step 3: Add transfer schemas and state machine**

Add `upload_sessions` and `download_sessions` with hashed capabilities and explicit states:

```text
pending -> received -> finishing -> complete
                         \-> failed
```

Only one state transition transaction may claim `received -> finishing`. Store declared size,
SHA-256, kind, destination, owner, PAT ID, timestamps, and relative staging path. Never store a
capability secret.

- [ ] **Step 4: Implement bounded streaming PUT**

The upload route authenticates `Authorization: Upload <capability>`, creates:

```text
<WORKSPACE_BASE>/<username>/.tcb/uploads/<upload-id>.part
```

with exclusive creation, streams `request.stream()` while hashing/counting, fsyncs, compares exact
size/hash, then renames to `.ready`. On any failure, close and unlink `.part`; mark the session
failed with a stable code. Set `Cache-Control: no-store`. The matching begin tool returns
`{public_base_url}/mcp-upload/{upload_id}` plus the required
`Authorization: Upload <capability>` header; the capability never appears in the URL.

- [ ] **Step 5: Add backend staged operations**

Add JSON endpoints that accept only an upload ID, never an arbitrary host path:

```text
POST /api/agent/files/install-upload
POST /api/agent/projects/pdf-from-upload
POST /api/agent/pdf/replace-from-upload
```

Resolve the candidate strictly beneath `/workspace/.tcb/uploads`, pin a regular non-symlink file,
and re-check size/hash before calling:

- `remote_files.install_file`;
- `projects.create_pdf_project_from_file`;
- the existing PDF locked replacement service.

The backend unlinks a successfully consumed staged file. A failed install leaves it for expiry
cleanup and does not change visible project data.

- [ ] **Step 6: Implement bounded download proxy**

Download sessions store a fixed backend-relative URL selected by server code, not caller input.
The GET route checks the one-time capability, streams the backend response, enforces the five-minute
expiry, sets `Content-Disposition`, and marks the session consumed only after headers are accepted.

- [ ] **Step 7: Add transfer expiry cleanup**

Add `sweep_transfer_sessions(db_path, workspace_base, now)` that deletes expired database rows and
only their exact `.part`/`.ready` files beneath the owning user's `.tcb/uploads` directory. Run it
at control startup and every 60 seconds from a cancellable lifespan task. Tests create one expired
and one live session and assert only the expired payload is removed.

- [ ] **Step 8: Run focused backend/control tests**

```bash
control/.venv/bin/python -m unittest discover -s tests -p 'test_control_mcp_transfer.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_remote_files.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_pdf_projects.py' -v
```

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add control/mcp_store.py control/mcp_transfer.py control/main.py control/workspace_gateway.py backend/app.py tests/test_control_mcp_transfer.py tests/test_remote_files.py
git commit -m "feat: add bounded MCP file transfers"
```

### Task 4: File MCP tools

**Files:**
- Modify: `control/workspace_gateway.py`
- Modify: `control/remote_mcp.py`
- Test: `tests/test_control_mcp_operations.py`

**Interfaces:**
- Produces tools:
  - `list_files`
  - `read_text_file`
  - `write_text_file`
  - `create_directory`
  - `move_file`
  - `upload_file`
  - `begin_file_upload`
  - `finish_file_upload`
  - `delete_file`
  - `list_deleted_files`
  - `restore_deleted_file`
- Consumes: foundation handle validation, remote file endpoints, and transfer sessions.

- [ ] **Step 1: Write failing MCP file-tool tests**

```python
async def test_viewer_reads_but_cannot_write_and_editor_uses_hash(self):
    read = await self.viewer.call_tool(
        "read_text_file",
        {"project_handle": self.viewer_handle, "path": "notes.md"},
    )
    current_hash = read.structuredContent["sha256"]
    denied = await self.viewer.call_tool(
        "write_text_file",
        {
            "project_handle": self.viewer_handle,
            "path": "notes.md",
            "content": "new",
            "expected_sha256": current_hash,
        },
    )
    self.assertEqual(tool_error_code(denied), "SCOPE_DENIED")
```

Add tests for active-main rejection, stale handle, inline upload limit, no-overwrite default, trash,
restore collision, and audit target redaction.

- [ ] **Step 2: Run and verify RED**

```bash
control/.venv/bin/python -m unittest discover -s tests -p 'test_control_mcp_operations.py' -v
```

Expected: tools absent.

- [ ] **Step 3: Extend gateway methods**

Every method first validates a fresh active context, then calls one fixed backend path. Validate
relative-path argument length and reject NUL before the backend call. For inline upload, decode
base64 with `validate=True`, require exact size/hash, cap decoded bytes at 1 MiB, and stage through
the same backend install primitive. When the backend read result has `download_required=True`,
create a fixed-path download session and return its URL, expiry, size, and SHA-256 instead of
returning file bytes.

- [ ] **Step 4: Register file tools with exact scopes**

Read/list/trash-list require `files:read`; every mutation requires `files:write`. Tool docstrings
must state:

- the project handle requirement;
- main Typst/PDF-managed protections;
- `expected_sha256` conflict behavior;
- no-overwrite default;
- large-upload begin/PUT/finish sequence.

- [ ] **Step 5: Run tests and commit**

```bash
control/.venv/bin/python -m unittest discover -s tests -p 'test_control_mcp_operations.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_remote_files.py' -v
git add control/workspace_gateway.py control/remote_mcp.py tests/test_control_mcp_operations.py
git commit -m "feat: expose remote project file tools"
```

### Task 5: Typst document, comment, preview, and export tools

**Files:**
- Create: `backend/preview_service.py`
- Modify: `backend/app.py:1393-1600`
- Modify: `control/workspace_gateway.py`
- Modify: `control/remote_mcp.py`
- Test: `tests/test_control_mcp_operations.py`
- Test: `tests/test_regressions.py`

**Interfaces:**
- Produces tools:
  - `get_document`
  - `find_in_document`
  - `locate`
  - `apply_edits`
  - `get_transcripts`
  - `get_pending_comments`
  - `get_comment`
  - `mark_comment_done`
  - `mark_comment_dismissed`
  - `get_slide_preview`
  - `export_pdf`
- Produces backend route `GET /api/agent/preview/{page}` returning `image/png`.
- Consumes: existing `/api/document`, `/api/edit`, `/api/locate`, `/api/slide-map`,
  `/api/comments*`, `/api/export-pdf`, rendered SVG files, and transfer download sessions.

- [ ] **Step 1: Write failing Typst tool integration tests**

```python
async def test_apply_edits_updates_live_document_and_comment_flow(self):
    before = await self.editor.call_tool(
        "get_document", {"project_handle": self.handle, "offset": 1, "limit": 40}
    )
    edited = await self.editor.call_tool(
        "apply_edits",
        {
            "project_handle": self.handle,
            "base_rev": before.structuredContent["rev"],
            "edits": [{
                "by": "anchor",
                "anchor": "Old title",
                "new_text": "New title",
            }],
        },
    )
    self.assertTrue(edited.structuredContent["ok"])
    pending = await self.editor.call_tool(
        "get_pending_comments", {"project_handle": self.handle}
    )
    self.assertEqual(pending.structuredContent["comments"][0]["status"], "pending")
```

Test PDF handles return `CAPABILITY_NOT_AVAILABLE` for comment and Typst-edit tools.

- [ ] **Step 2: Run and verify RED**

```bash
control/.venv/bin/python -m unittest discover -s tests -p 'test_control_mcp_operations.py' -v
```

Expected: tools absent.

- [ ] **Step 3: Implement bounded document windows and search**

Gateway `get_document` fetches `/api/document`, then returns the same 120-line default/400-line
maximum window used by the project-local MCP. `find_in_document` returns at most 40 literal,
case-sensitive hits with line number and one-line context; reject empty queries and queries over
512 characters. Never return the whole document unless it fits inside the requested bounded
window.

- [ ] **Step 4: Route CRDT and comment operations**

`apply_edits` sends one `op="apply_edits"` request with caller `base_rev`. Map a failed revision
to `REVISION_CONFLICT`. Comment reads combine list data with `/anchor` live locations. Dismissal
uses `PATCH /api/comments/{id}` with status `dismissed` and an optional reason; completion uses
the existing done endpoint.

- [ ] **Step 5: Convert active rendered pages to PNG safely**

For PDF, pin and return the already-rendered page PNG. For Typst, pin the requested rendered SVG
and convert with PyMuPDF:

```python
with fitz.open(stream=svg_bytes, filetype="svg") as document:
    pixmap = document[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    png = pixmap.tobytes("png")
```

Validate `1 <= page <= page_count`, cap output at 8 MiB, and do not accept a filesystem name from
the caller. The MCP tool returns `mcp.server.fastmcp.utilities.types.Image(data=png, format="png")`.

- [ ] **Step 6: Export through a download capability**

The gateway asks the existing backend to compile/export, then creates a five-minute download
session bound to the fixed export response. Return download URL, expiry, SHA-256, and size; do not
embed the PDF in JSON.

- [ ] **Step 7: Run tests and commit**

```bash
control/.venv/bin/python -m unittest discover -s tests -p 'test_control_mcp_operations.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_regressions.py' -v
git add backend/preview_service.py backend/app.py control/workspace_gateway.py control/remote_mcp.py tests/test_control_mcp_operations.py tests/test_regressions.py
git commit -m "feat: expose remote Typst collaboration tools"
```

### Task 6: PDF transcript, preview, creation, and replacement tools

**Files:**
- Modify: `control/workspace_gateway.py`
- Modify: `control/remote_mcp.py`
- Modify: `backend/app.py:1202-1404`
- Test: `tests/test_control_mcp_operations.py`
- Test: `tests/test_pdf_projects.py`

**Interfaces:**
- Produces tools:
  - `get_pdf_info`
  - `get_pdf_text`
  - `get_transcripts` with type dispatch
  - `set_transcript`
  - `set_transcripts`
  - `get_page_preview` with type dispatch
  - `begin_pdf_project_upload`
  - `finish_pdf_project_upload`
  - `begin_pdf_replacement`
  - `finish_pdf_replacement`
- Consumes: upload capabilities, existing PDF text/transcript APIs, locked PDF replacement, and
  preview service.

- [ ] **Step 1: Write failing PDF operation tests**

```python
async def test_pdf_transcript_round_trip_and_replacement_is_versioned(self):
    saved = await self.editor.call_tool(
        "set_transcript",
        {"project_handle": self.pdf_handle, "page": 1, "text": "Opening"},
    )
    self.assertEqual(saved.structuredContent["pages"]["1"], "Opening")
    begun = await self.editor.call_tool(
        "begin_pdf_replacement",
        {
            "project_handle": self.pdf_handle,
            "filename": "candidate.pdf",
            "size": len(self.candidate),
            "sha256": hashlib.sha256(self.candidate).hexdigest(),
        },
    )
    await put_upload(begun.structuredContent, self.candidate)
    finished = await self.editor.call_tool(
        "finish_pdf_replacement",
        {
            "project_handle": self.pdf_handle,
            "upload_id": begun.structuredContent["upload_id"],
            "message": "remote update",
        },
    )
    self.assertTrue(finished.structuredContent["ok"])
```

Test one-PDF-only rules, invalid PDF rollback, page bounds, Viewer write denial, Typst handle
capability errors, and transcript preservation.

- [ ] **Step 2: Run and verify RED**

```bash
control/.venv/bin/python -m unittest discover -s tests -p 'test_control_mcp_operations.py' -v
```

Expected: PDF tools absent.

- [ ] **Step 3: Implement project-type dispatch and scopes**

Resolve the active project's type once per call after handle validation. Use:

- `projects:read` for `get_pdf_info`;
- `files:read` for PDF embedded text;
- `transcripts:read/write` for transcript tools;
- `slides:read` for preview;
- `documents:write` for replacement;
- `projects:write` for PDF project creation.

Return `CAPABILITY_NOT_AVAILABLE` before sending a type-incompatible backend request.

- [ ] **Step 4: Complete upload-to-PDF flows**

`finish_pdf_project_upload` does not require a project handle because the project does not yet
exist; it requires the same PAT that began the upload. Return the new project metadata, then let
the agent call `open_project`. Replacement requires a current handle both before claiming the
upload and immediately before backend installation.

- [ ] **Step 5: Run PDF/control regressions and commit**

```bash
control/.venv/bin/python -m unittest discover -s tests -p 'test_control_mcp_operations.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_pdf_projects.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_regressions.py' -v
git add control/workspace_gateway.py control/remote_mcp.py backend/app.py tests/test_control_mcp_operations.py tests/test_pdf_projects.py
git commit -m "feat: expose remote PDF project tools"
```

### Task 7: Operations verification checkpoint

**Files:**
- No planned source modifications.

**Interfaces:**
- Produces: the complete non-UI remote MCP tool surface in the approved design.
- Consumes: foundation plus all operations tasks.

- [ ] **Step 1: Run all Python tests**

```bash
control/.venv/bin/python -m unittest discover -s tests -p 'test_control_*.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_project_context.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_remote_files.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_pdf_projects.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_regressions.py' -v
```

Expected: all pass.

- [ ] **Step 2: Run a real MCP scenario**

Run the integration test that performs:

```text
issue PAT
list projects
create/open Typst project
upload asset
read/apply Typst edit
fetch PNG preview
fetch/complete comment
create/open PDF project from staged upload
set transcript
replace PDF
revoke PAT
verify next MCP call is unauthorized
```

Command:

```bash
control/.venv/bin/python -m unittest discover -s tests -p 'test_control_mcp_operations.py' -v
```

The `RemoteMcpScenarioTest` case must pass with no leaked PAT/handle in logs.

- [ ] **Step 3: Run repository checks**

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; only intentional changes and the pre-existing `docs/design/`.
