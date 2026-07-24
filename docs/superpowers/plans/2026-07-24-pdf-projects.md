# PDF Projects Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add immutable PDF projects with one primary PDF, page-number transcripts, a terminal-first workspace, presenter support, and safe agent-driven PDF replacement.

**Architecture:** Existing projects remain Typst projects and keep their current runtime, CRDT, resolver, comments, and MCP behavior. PDF projects take a separate type-aware path: PyMuPDF validates, extracts, and renders the primary `document.pdf`; `pdf_transcript.py` owns atomic `transcript.json`; `pdf_mcp_server.py` exposes only PDF/transcript operations; and `PdfWorkspace.jsx` omits the Typst editor and comment system while reusing terminal, versioning, projection, and presenter primitives.

**Tech Stack:** FastAPI, PyMuPDF, SQLite-independent JSON sidecar storage, existing Git version service, MCP Python SDK, React 18, Vite, Node test runner, Python unittest.

## Global Constraints

- Project type is exactly `typst` or `pdf` and cannot be changed after creation.
- Projects whose metadata lacks `type` are treated as `typst`.
- A PDF project contains exactly one primary PDF stored as `document.pdf`; the original filename is metadata only.
- PDF transcripts use 1-based page-number mapping in `transcript.json`.
- When page count shrinks, removed-page transcripts move to `orphans` and are never silently deleted.
- PDF projects expose no comment UI or comment MCP tools; AI discussion happens in the terminal.
- PDF replacement must validate and render first, snapshot versions, and use same-directory `os.replace`.
- Initial release extracts embedded PDF text and rendered page images but does not perform OCR.
- Typst project behavior and APIs remain backward compatible.

---

### Task 1: PDF metadata, validation, and project creation

**Files:**
- Create: `backend/pdf_service.py`
- Modify: `backend/projects.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Test: `tests/test_pdf_projects.py`

**Interfaces:**
- Produces: `pdf_service.inspect_pdf(path: Path) -> dict`, `pdf_service.render_pdf(path: Path, destination: Path) -> dict`
- Produces: `projects.create_pdf_project(name: str, filename: str, content: bytes) -> dict`
- Produces: project info fields `type`, `main_file`, and `original_filename`

- [ ] **Step 1: Write failing project-domain tests**

```python
def test_legacy_project_defaults_to_typst(self):
    self.assertEqual(self.projects.get_project("legacy")["type"], "typst")

def test_pdf_project_requires_valid_pdf_and_uses_stable_primary_name(self):
    info = self.projects.create_pdf_project("Paper", "paper.pdf", ONE_PAGE_PDF)
    self.assertEqual(info["type"], "pdf")
    self.assertEqual(info["main_file"], "document.pdf")
    self.assertEqual(info["original_filename"], "paper.pdf")
    self.assertTrue((Path(info["path"]) / "document.pdf").exists())

def test_invalid_pdf_leaves_no_project_directory(self):
    with self.assertRaises(ValueError):
        self.projects.create_pdf_project("Broken", "broken.pdf", b"not pdf")
    self.assertEqual(list(self.root.iterdir()), [])
```

- [ ] **Step 2: Verify RED**

Run: `/Users/xavier/Projects/web-services/vibe-typst/backend/.venv/bin/python -m unittest tests.test_pdf_projects.PdfProjectCreationTest -v`

Expected: FAIL because `create_pdf_project` and PDF type metadata do not exist.

- [ ] **Step 3: Implement the PDF service and project factory**

```python
def inspect_pdf(path: Path) -> dict:
    try:
        with fitz.open(path) as doc:
            if not doc.is_pdf or doc.page_count < 1:
                raise ValueError("PDF must contain at least one page")
            return {"page_count": doc.page_count, "metadata": dict(doc.metadata or {})}
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"invalid PDF: {exc}") from exc
```

`create_pdf_project` writes upload bytes to a temporary sibling, validates it, creates the UUID directory, atomically installs it as `document.pdf`, writes immutable type metadata, and removes temporary data on failure.

- [ ] **Step 4: Add `pymupdf>=1.26,<2` and refresh the lock**

Run: `uv lock --project backend`

Expected: `backend/uv.lock` contains the platform-independent PyMuPDF resolution.

- [ ] **Step 5: Verify GREEN and commit**

Run: `/Users/xavier/Projects/web-services/vibe-typst/backend/.venv/bin/python -m unittest tests.test_pdf_projects.PdfProjectCreationTest -v`

Expected: PASS.

Commit: `feat: add immutable PDF project metadata`

---

### Task 2: Atomic page transcript storage

**Files:**
- Create: `backend/pdf_transcript.py`
- Test: `tests/test_pdf_projects.py`

**Interfaces:**
- Produces: `load(project_dir, pdf_name, page_count) -> dict`
- Produces: `set_page(project_dir, pdf_name, page_count, page, text) -> dict`
- Produces: `set_pages(project_dir, pdf_name, page_count, updates) -> dict`
- Produces: `restore_orphan(project_dir, pdf_name, page_count, orphan_page, target_page) -> dict`

- [ ] **Step 1: Write failing transcript tests**

```python
def test_new_sidecar_has_one_blank_entry_per_page(self):
    data = self.transcript.load(self.root, "document.pdf", 2)
    self.assertEqual(data["pages"], {"1": {"text": ""}, "2": {"text": ""}})

def test_shrink_moves_text_to_orphans_without_loss(self):
    self.transcript.set_page(self.root, "document.pdf", 3, 3, "closing")
    data = self.transcript.load(self.root, "document.pdf", 2)
    self.assertEqual(data["orphans"]["3"]["text"], "closing")

def test_batch_validation_is_atomic(self):
    before = self.transcript.load(self.root, "document.pdf", 2)
    with self.assertRaises(ValueError):
        self.transcript.set_pages(self.root, "document.pdf", 2, [
            {"page": 1, "text": "valid"}, {"page": 9, "text": "invalid"}
        ])
    self.assertEqual(self.transcript.load(self.root, "document.pdf", 2), before)
```

- [ ] **Step 2: Verify RED**

Run: `/Users/xavier/Projects/web-services/vibe-typst/backend/.venv/bin/python -m unittest tests.test_pdf_projects.PdfTranscriptTest -v`

Expected: FAIL because `pdf_transcript` does not exist.

- [ ] **Step 3: Implement schema reconciliation and atomic writes**

```python
def _atomic_write(path: Path, data: dict) -> None:
    fd, raw = tempfile.mkstemp(prefix=".transcript-", suffix=".json", dir=path.parent)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
```

The sidecar schema is:

```json
{
  "schema_version": 1,
  "pdf": "document.pdf",
  "pages": {"1": {"text": ""}},
  "orphans": {}
}
```

- [ ] **Step 4: Verify GREEN and commit**

Run: `/Users/xavier/Projects/web-services/vibe-typst/backend/.venv/bin/python -m unittest tests.test_pdf_projects.PdfTranscriptTest -v`

Expected: PASS.

Commit: `feat: add atomic per-page PDF transcripts`

---

### Task 3: Type-aware runtime, rendering, and HTTP APIs

**Files:**
- Modify: `backend/runtime.py`
- Modify: `backend/app.py`
- Modify: `backend/pdf_service.py`
- Test: `tests/test_pdf_projects.py`

**Interfaces:**
- Produces: `runtime.document_type() -> str`
- Produces: `GET /api/pdf/transcripts`
- Produces: `PATCH /api/pdf/transcripts/{page}`
- Produces: `POST /api/pdf/transcripts/batch`
- Produces: `GET /api/pdf/text?page=N`
- Produces: type-aware `/api/state`, `/api/render-version`, `/api/render/{name}`, and `/api/slide-map`

- [ ] **Step 1: Write failing API/runtime tests**

Tests activate a PDF project against the ASGI app and assert:

```python
self.assertEqual(runtime.set_file(str(pdf)), str(pdf.resolve()))
self.assertEqual(runtime.document_type(), "pdf")
self.assertEqual(state["project_type"], "pdf")
self.assertEqual(state["source"], "")
self.assertEqual(state["pages"], ["page-1.png"])
self.assertEqual(slide_map["pages"][0]["project_type"], "pdf")
```

Also assert opening PDF stops the Typst resolver and never creates a CRDT room or comment store.

- [ ] **Step 2: Verify RED**

Run: `/Users/xavier/Projects/web-services/vibe-typst/backend/.venv/bin/python -m unittest tests.test_pdf_projects.PdfRuntimeApiTest -v`

Expected: FAIL because runtime accepts only `.typ` and state is Typst-only.

- [ ] **Step 3: Implement type dispatch**

`runtime.set_file` accepts `.typ` and `.pdf`; `document_type` derives from the active suffix. `open_project`, lifespan, state, render-version, slide-map, and render serving branch on the immutable project type. Typst paths retain their existing bodies. PDF opening stops the resolver, renders pages through PyMuPDF, reconciles transcripts, and sets up only the PDF workdir.

- [ ] **Step 4: Implement transcript/text endpoints**

The page patch endpoint validates a 1-based page and a string `text`. The batch endpoint validates every update before writing. Text extraction returns embedded page text only and reports `ocr: false`.

- [ ] **Step 5: Verify GREEN and commit**

Run: `/Users/xavier/Projects/web-services/vibe-typst/backend/.venv/bin/python -m unittest tests.test_pdf_projects.PdfRuntimeApiTest -v`

Expected: PASS.

Commit: `feat: serve PDF pages and transcript APIs`

---

### Task 4: Safe replacement and version capture

**Files:**
- Modify: `backend/pdf_service.py`
- Modify: `backend/app.py`
- Modify: `backend/projects.py`
- Test: `tests/test_pdf_projects.py`

**Interfaces:**
- Produces: `replace_primary(project_dir, candidate, primary, render_dir) -> dict`
- Produces: `POST /api/pdf/replace` with `{candidate, message}`

- [ ] **Step 1: Write failing replacement tests**

```python
def test_invalid_candidate_never_changes_primary(self):
    before = self.primary.read_bytes()
    with self.assertRaises(ValueError):
        replace_primary(self.root, self.bad, self.primary, self.render_dir)
    self.assertEqual(self.primary.read_bytes(), before)

def test_shrinking_replacement_preserves_removed_transcript_as_orphan(self):
    result = self.replace_via_api(two_pages_to_one_page)
    self.assertTrue(result["ok"])
    self.assertEqual(result["page_count"], 1)
    self.assertEqual(self.transcripts()["orphans"]["2"]["text"], "page two")

def test_successful_replacement_snapshots_before_and_after(self):
    self.assertEqual([v["tag"] for v in vcs.list_versions(self.root)], ["v2", "v1"])
```

- [ ] **Step 2: Verify RED**

Run: `/Users/xavier/Projects/web-services/vibe-typst/backend/.venv/bin/python -m unittest tests.test_pdf_projects.PdfReplacementTest -v`

Expected: FAIL because replacement does not exist.

- [ ] **Step 3: Implement prepare-then-swap replacement**

Resolve the candidate inside the project, reject `document.pdf` itself, render it to a staging directory, save the current version, copy and fsync to a same-directory temporary file, call `os.replace`, atomically install the staged render, reconcile transcripts, and save the resulting version. Failures before `os.replace` leave the primary and render untouched.

- [ ] **Step 4: Reject extra PDF uploads and primary deletion/rename**

General file APIs return HTTP 400 if a PDF project attempts to upload a second `.pdf`, delete `document.pdf`, rename it, or open another file as the active document.

- [ ] **Step 5: Verify GREEN and commit**

Run: `/Users/xavier/Projects/web-services/vibe-typst/backend/.venv/bin/python -m unittest tests.test_pdf_projects.PdfReplacementTest -v`

Expected: PASS.

Commit: `feat: replace PDFs atomically with version snapshots`

---

### Task 5: PDF-specific MCP and agent workdir

**Files:**
- Create: `backend/pdf_mcp_server.py`
- Modify: `backend/workdir.py`
- Test: `tests/test_pdf_projects.py`

**Interfaces:**
- Produces MCP tools: `get_pdf_info`, `get_pdf_text`, `get_transcripts`, `set_transcript`, `set_transcripts`, `list_orphan_transcripts`, `restore_orphan_transcript`, `replace_pdf`

- [ ] **Step 1: Write failing MCP/workdir tests**

Assert PDF setup points `.mcp.json` and `.codex/config.toml` to `pdf_mcp_server.py`, its managed `AGENTS.md` section says native PDF utilities are allowed, and it contains none of `apply_edits`, `get_pending_comments`, or Typst/touying instructions. Exercise every PDF MCP tool through an in-process backend shim.

- [ ] **Step 2: Verify RED**

Run: `/Users/xavier/Projects/web-services/vibe-typst/backend/.venv/bin/python -m unittest tests.test_pdf_projects.PdfMcpTest -v`

Expected: FAIL because the PDF MCP server and PDF instructions do not exist.

- [ ] **Step 3: Implement the focused MCP**

The server calls the new HTTP APIs, URL-encodes candidate paths, and contains no comment or Typst-edit tools. Replacement documentation requires the agent to generate a candidate file and call `replace_pdf`; direct overwrite of `document.pdf` is forbidden because it bypasses validation and versioning.

- [ ] **Step 4: Make workdir setup type-aware**

Select `_pdf_section` and `pdf_mcp_server.py` when `runtime.document_type() == "pdf"`; keep the existing Typst section and server byte-for-byte for Typst projects.

- [ ] **Step 5: Verify GREEN and commit**

Run: `/Users/xavier/Projects/web-services/vibe-typst/backend/.venv/bin/python -m unittest tests.test_pdf_projects.PdfMcpTest -v`

Expected: PASS.

Commit: `feat: add terminal PDF agent tools`

---

### Task 6: Project creation UI and workspace routing

**Files:**
- Modify: `frontend/src/api.js`
- Modify: `frontend/src/ProjectsPage.jsx`
- Modify: `frontend/src/main.jsx`
- Create: `frontend/src/projectTypes.js`
- Test: `frontend/test/projectTypes.test.js`

**Interfaces:**
- Produces: `createPdfProject(name, file)`
- Produces: `workspaceComponentFor(project)` returning `typst` or `pdf`

- [ ] **Step 1: Write failing frontend helper tests**

```javascript
test('missing project type remains typst', () => {
  assert.equal(projectType({}), 'typst')
})
test('pdf project routes to pdf workspace', () => {
  assert.equal(projectType({ type: 'pdf' }), 'pdf')
})
```

- [ ] **Step 2: Verify RED**

Run: `/Users/xavier/Projects/nsw-driving-test-slot-monitor/.node/bin/node --test frontend/test/projectTypes.test.js`

Expected: FAIL because `projectTypes.js` does not exist.

- [ ] **Step 3: Implement type selection and multipart creation**

The New Project form offers Typst/PDF choices. PDF requires one `.pdf` file and calls `POST /api/projects/pdf` with `FormData`. Project cards display a PDF or Typst badge. `ProjectsPage` passes the opened project to `Root`, which routes to `App` or `PdfWorkspace`.

- [ ] **Step 4: Verify GREEN and commit**

Run: `/Users/xavier/Projects/nsw-driving-test-slot-monitor/.node/bin/node --test frontend/test/*.test.js`

Expected: all frontend unit tests pass.

Commit: `feat: create and route PDF projects`

---

### Task 7: Terminal-first PDF viewer and transcripts

**Files:**
- Create: `frontend/src/PdfWorkspace.jsx`
- Create: `frontend/src/PdfPreviewPane.jsx`
- Modify: `frontend/src/PreviewPane.jsx`
- Modify: `frontend/src/Presenter.jsx`
- Modify: `frontend/src/api.js`
- Modify: `frontend/src/styles.css`
- Test: `frontend/test/pdfWorkspace.test.js`

**Interfaces:**
- Consumes generic page names/tokens from render-version and page transcript rows from slide-map.
- Produces pure helpers `pdfTranscriptDirty`, `nextPdfRenderState`, and `pdfWorkspacePanes` for unit tests.

- [ ] **Step 1: Write failing workspace behavior tests**

Tests assert the PDF workspace pane model contains `terminal`, `preview`, and `presenter`, excludes `editor` and `comments`, and marks transcript text dirty only when it differs from the saved page text.

- [ ] **Step 2: Verify RED**

Run: `/Users/xavier/Projects/nsw-driving-test-slot-monitor/.node/bin/node --test frontend/test/pdfWorkspace.test.js`

Expected: FAIL because the PDF workspace helpers do not exist.

- [ ] **Step 3: Implement `PdfWorkspace`**

Render a permanently mounted terminal pane, PDF page preview, file/version drawer, and Present action. Poll render-version so successful agent replacement refreshes pages automatically. Do not call comments, resolver compile, CRDT, locate, or source-edit APIs.

- [ ] **Step 4: Implement PDF preview transcripts**

`PdfPreviewPane` renders `<img>` pages, current/total controls, transcript toggle, per-page textarea saving through `PATCH /api/pdf/transcripts/{page}`, transcript export, and an orphan count warning. It has no source jump or comment-selection controls.

- [ ] **Step 5: Reuse presenter and projection safely**

Allow transcript rows with `project_type: "pdf"` and `page` to save through the PDF transcript API. Existing Typst rows continue through patch/create speaker-note APIs. Image URLs remain generic, so projection and pointer behavior are unchanged.

- [ ] **Step 6: Verify GREEN and commit**

Run: `/Users/xavier/Projects/nsw-driving-test-slot-monitor/.node/bin/node --test frontend/test/*.test.js`

Expected: all frontend unit tests pass.

Commit: `feat: add terminal-first PDF workspace`

---

### Task 8: Full integration, build artifacts, and deployment dependency

**Files:**
- Modify: `frontend/dist/index.html`
- Replace generated: `frontend/dist/assets/index-*.js`, optional CSS hash
- Modify: `Containerfile`
- Test: `tests/test_pdf_projects.py`, all existing test suites

**Interfaces:**
- Produces a deployable image containing PyMuPDF and the PDF-specific MCP server.

- [ ] **Step 1: Add end-to-end regression**

Create a PDF project through the FastAPI endpoint, open it, verify one rendered page and blank transcript, update its transcript, replace it with a two-page candidate, verify presenter map and version tags, and confirm Typst project creation/opening remains unchanged.

- [ ] **Step 2: Verify the end-to-end test fails before final integration wiring**

Run: `/Users/xavier/Projects/web-services/vibe-typst/backend/.venv/bin/python -m unittest tests.test_pdf_projects.PdfEndToEndTest -v`

Expected: FAIL at the first missing integration boundary, then pass after wiring it.

- [ ] **Step 3: Refresh dependency artifact instructions and frontend build**

Run:

```bash
uv sync --project backend
PATH=/Users/xavier/Projects/nsw-driving-test-slot-monitor/.node/bin:$PATH npm run build --prefix frontend
```

The Containerfile continues consuming `backend/.venv.tar.gz`; deployment documentation must state that this artifact is rebuilt after adding PyMuPDF.

- [ ] **Step 4: Run complete verification**

Run:

```bash
/Users/xavier/Projects/web-services/vibe-typst/backend/.venv/bin/python -m unittest discover -s tests -v
PATH=/Users/xavier/Projects/nsw-driving-test-slot-monitor/.node/bin:$PATH npm test --prefix frontend
CARGO_TARGET_DIR=/Users/xavier/Projects/web-services/vibe-typst/resolver/target cargo test --manifest-path resolver/Cargo.toml
bash -n docker-entrypoint.sh
git diff --check
```

Expected: all Python and frontend tests pass; resolver passes with only the existing `FileError` warning; shell and diff checks are clean.

- [ ] **Step 5: Request code review and commit**

Commit: `feat: complete PDF project workflow`

