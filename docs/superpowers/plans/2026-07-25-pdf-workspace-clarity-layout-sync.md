# PDF Workspace Clarity, Layout, and Page Sync Implementation Plan

> **Required execution skill:** Use `superpowers:executing-plans` to execute this plan task by task. Before changing implementation code, use `superpowers:using-git-worktrees` and `superpowers:test-driven-development`. Before reporting completion, use `superpowers:verification-before-completion`.

**Goal:** Make PDF projects render sharply, give them a Typst-quality resizable terminal layout, add explicit two-way Preview/Presentation page synchronization, and replace the native PDF upload control with a polished drag-and-drop picker.

**Architecture:** Keep the existing PDF page-image API and atomic replacement flow, but render every cached page with a versioned 2560-pixel-long-edge profile. Split the frontend's single page cursor into independent preview and presentation cursors, expose explicit synchronization controls, and compose the PDF workspace from a reusable terminal header plus a draggable split pane. Keep PDF selection state in `ProjectsPage`, with a focused picker component and pure validation helpers.

**Tech Stack:** Python 3.12, FastAPI, PyMuPDF, React 18, xterm.js, Vite, Node's built-in test runner, Python `unittest`, Puppeteer, Docker Compose.

---

## Task 1: Add a versioned high-resolution PDF render profile

**Files:**

- Modify: `backend/pdf_service.py`
- Modify: `backend/app.py`
- Modify: `tests/test_pdf_projects.py`

### Step 1: Write failing render-dimension and profile tests

Extend `PdfRenderingTest` in `tests/test_pdf_projects.py` with real PyMuPDF documents so the tests exercise both landscape and portrait pages:

```python
def test_render_pdf_scales_landscape_page_to_qhd_long_edge(self):
    source = self.root / "landscape.pdf"
    document = fitz.open()
    document.new_page(width=720, height=405)
    document.save(source)
    document.close()

    result = pdf_service.render_pdf(source, self.root / "rendered")

    pixmap = fitz.Pixmap(str(self.root / "rendered" / "page-0001.png"))
    self.assertEqual(max(pixmap.width, pixmap.height), 2560)
    self.assertEqual(result["render_profile"], "pdf-qhd-v1")

def test_render_pdf_scales_portrait_page_to_qhd_long_edge(self):
    source = self.root / "portrait.pdf"
    document = fitz.open()
    document.new_page(width=405, height=720)
    document.save(source)
    document.close()

    pdf_service.render_pdf(source, self.root / "rendered")

    pixmap = fitz.Pixmap(str(self.root / "rendered" / "page-0001.png"))
    self.assertEqual(max(pixmap.width, pixmap.height), 2560)
```

Update the fake page used by prepared-replacement tests so it accepts and records the new PyMuPDF arguments:

```python
class _FakePage:
    def __init__(self, pixmap, calls):
        self.rect = SimpleNamespace(width=720, height=405)
        self._pixmap = pixmap
        self._calls = calls

    def get_pixmap(self, *, matrix=None, alpha=True):
        self._calls.append({"matrix": matrix, "alpha": alpha})
        return self._pixmap
```

Add an assertion to the existing prepared-render test:

```python
self.assertFalse(calls[0]["alpha"])
self.assertAlmostEqual(calls[0]["matrix"].a, 2560 / 720)
```

Add a render-record test around `_record_pdf_render_version`:

```python
record = app_module._record_pdf_render_version(
    ["page-0001.png"], render_path, identity
)
self.assertEqual(record["profile"], pdf_service.PDF_RENDER_PROFILE)
```

### Step 2: Run the focused backend tests and confirm failure

Run:

```bash
backend/.venv/bin/python -m unittest tests.test_pdf_projects.PdfRenderingTest
```

Expected: failures because the current pixmaps use the default 72 DPI and render results/records do not expose a profile.

### Step 3: Implement the bounded render helper and use it everywhere

In `backend/pdf_service.py`, introduce the fixed profile and a single helper used by normal activation and atomic replacement:

```python
import math

PDF_RENDER_LONG_EDGE = 2560
PDF_RENDER_PROFILE = "pdf-qhd-v1"


def _render_page_pixmap(page):
    width = float(page.rect.width)
    height = float(page.rect.height)
    longest_edge = max(width, height)
    if not math.isfinite(longest_edge) or longest_edge <= 0:
        raise PdfValidationError("PDF page has invalid dimensions")
    scale = PDF_RENDER_LONG_EDGE / longest_edge
    return page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
```

Replace both direct `page.get_pixmap()` calls in `render_pdf` and `_prepared_render` with `_render_page_pixmap(page)`. Return the profile with the existing page metadata:

```python
return {
    "pages": pages,
    "page_count": len(pages),
    "render_profile": PDF_RENDER_PROFILE,
}
```

Do not change the rendered filenames, API URLs, staging directory, fsync order, atomic rename, rollback, or replacement-candidate validation.

### Step 4: Include the render profile in generation identity

In `backend/app.py`, include `pdf_service.PDF_RENDER_PROFILE` in the digest before page bytes are hashed, and persist it on the record:

```python
profile = pdf_service.PDF_RENDER_PROFILE
digest.update(profile.encode("utf-8"))
digest.update(b"\0")

record = {
    "fingerprint": digest.hexdigest(),
    "version": version,
    "page_count": len(pages),
    "profile": profile,
}
```

This makes the first render under `pdf-qhd-v1` a new observable generation even when filenames are unchanged. Keep `_record_pdf_render_version`'s public call signature unchanged so existing concurrency tests and callers remain valid.

### Step 5: Run focused and full backend tests

Run:

```bash
backend/.venv/bin/python -m unittest tests.test_pdf_projects.PdfRenderingTest
backend/.venv/bin/python -m unittest discover -s tests
```

Expected: both commands pass, including replacement rollback and generation-observation tests.

### Step 6: Commit the backend slice

```bash
git add backend/pdf_service.py backend/app.py tests/test_pdf_projects.py
git commit -m "feat: render PDF pages at high resolution"
```

---

## Task 2: Model independent PDF page cursors and pane constraints

**Files:**

- Modify: `frontend/src/pdfWorkspace.js`
- Modify: `frontend/test/pdfWorkspace.test.js`

### Step 1: Write failing pure-state tests

Add table-driven tests covering invalid input, shrinking documents, presentation startup, and divider boundaries:

```javascript
test('reconcilePdfPageCursors clamps preview and presentation independently', () => {
  assert.deepEqual(
    reconcilePdfPageCursors({ previewPage: 9, presentPage: 2 }, 4),
    { previewPage: 4, presentPage: 2 },
  )
  assert.deepEqual(
    reconcilePdfPageCursors({ previewPage: 1, presentPage: 3 }, 0),
    { previewPage: 1, presentPage: 1 },
  )
})

test('startPdfPresentationPage starts a new presentation from preview', () => {
  assert.equal(startPdfPresentationPage(5, 1, false, 10), 5)
  assert.equal(startPdfPresentationPage(5, 1, true, 10), 1)
})

test('clampPdfTerminalWidth preserves pane minimums', () => {
  assert.equal(clampPdfTerminalWidth(40, 0, 1200), 280)
  assert.equal(clampPdfTerminalWidth(1100, 0, 1200), 714)
  assert.equal(clampPdfTerminalWidth(420, 100, 1200), 320)
})
```

Update existing `nextPdfRenderState` expectations so rendering state describes pages/tokens/generation only. Page cursors will be reconciled separately by the component.

### Step 2: Run the focused frontend test and confirm failure

Run from `frontend`:

```bash
node --test test/pdfWorkspace.test.js
```

Expected: imports fail because the new helpers do not exist.

### Step 3: Implement the pure helpers

Add the following exports to `frontend/src/pdfWorkspace.js`:

```javascript
export const PDF_TERMINAL_MIN_WIDTH = 280
export const PDF_PREVIEW_MIN_WIDTH = 480
export const PDF_DIVIDER_WIDTH = 6

export function clampPdfPage(page, totalPages) {
  const total = Number.isFinite(totalPages) ? Math.max(0, Math.trunc(totalPages)) : 0
  if (total === 0) return 1
  const value = Number.isFinite(page) ? Math.trunc(page) : 1
  return Math.min(Math.max(value, 1), total)
}

export function reconcilePdfPageCursors(cursors, totalPages) {
  return {
    previewPage: clampPdfPage(cursors.previewPage, totalPages),
    presentPage: clampPdfPage(cursors.presentPage, totalPages),
  }
}

export function startPdfPresentationPage(
  previewPage,
  presentPage,
  presentationActive,
  totalPages,
) {
  return clampPdfPage(
    presentationActive ? presentPage : previewPage,
    totalPages,
  )
}

export function clampPdfTerminalWidth(
  pointerX,
  containerLeft,
  containerWidth,
) {
  const maximum = Math.max(
    0,
    containerWidth - PDF_PREVIEW_MIN_WIDTH - PDF_DIVIDER_WIDTH,
  )
  const minimum = Math.min(PDF_TERMINAL_MIN_WIDTH, maximum)
  const requested = pointerX - containerLeft
  return Math.min(Math.max(requested, minimum), maximum)
}
```

Remove `page` from `nextPdfRenderState`. Preserve its existing stale-response, token, generation, and page-list logic.

### Step 4: Run the focused test

Run:

```bash
node --test test/pdfWorkspace.test.js
```

Expected: all PDF workspace unit tests pass.

### Step 5: Commit the state model

```bash
git add frontend/src/pdfWorkspace.js frontend/test/pdfWorkspace.test.js
git commit -m "refactor: split PDF preview and presentation state"
```

---

## Task 3: Build the resizable PDF workspace and explicit page sync

**Files:**

- Add: `frontend/src/terminalUi.jsx`
- Modify: `frontend/src/App.jsx`
- Modify: `frontend/src/PdfWorkspace.jsx`
- Modify: `frontend/src/PdfPreviewPane.jsx`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/test/pdfWorkspace.test.js`
- Add: `frontend/test/pdfWorkspaceUi.e2e.js`

### Step 1: Write a failing browser behavior test

Create `frontend/test/pdfWorkspaceUi.e2e.js`. It opens the existing PDF project through the real Projects UI and asserts user-visible behavior rather than source text:

```javascript
import assert from 'node:assert/strict'
import puppeteer from 'puppeteer'

const baseUrl = process.env.VIBE_TYPST_URL || 'http://127.0.0.1:9003'
const browser = await puppeteer.launch({ headless: true })
const page = await browser.newPage()
await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 2 })
await page.goto(baseUrl, { waitUntil: 'networkidle0' })

const projectName = await page.evaluate(async () => {
  const response = await fetch('/api/projects')
  const body = await response.json()
  return body.projects.find(project => project.type === 'pdf')?.name || null
})
assert.ok(projectName, 'test deployment must contain one PDF project')

await page.evaluate(name => {
  const cards = [...document.querySelectorAll('.project-card')]
  cards.find(card => card.textContent.includes(name))
    ?.querySelector('.project-card-body')
    ?.click()
}, projectName)
await page.waitForSelector('.pdf-workspace')

const initial = await page.evaluate(() => ({
  layout: getComputedStyle(document.querySelector('.pdf-workspace-main')).display,
  divider: Boolean(document.querySelector('.pdf-workspace-divider')),
  terminalHeader: document.querySelector('.pdf-terminal-pane .term-head')?.textContent,
  longestImageEdge: Math.max(
    document.querySelector('.pdf-page-stage img').naturalWidth,
    document.querySelector('.pdf-page-stage img').naturalHeight,
  ),
}))
assert.equal(initial.layout, 'flex')
assert.equal(initial.divider, true)
assert.ok(initial.terminalHeader)
assert.ok(initial.longestImageEdge >= 2500)

const divider = await page.$('.pdf-workspace-divider')
const box = await divider.boundingBox()
const beforeWidth = await page.$eval('.pdf-terminal-pane', node => node.getBoundingClientRect().width)
await page.mouse.move(box.x + box.width / 2, box.y + 100)
await page.mouse.down()
await page.mouse.move(box.x + 140, box.y + 100, { steps: 8 })
await page.mouse.up()
const afterWidth = await page.$eval('.pdf-terminal-pane', node => node.getBoundingClientRect().width)
assert.ok(afterWidth > beforeWidth + 100)

await browser.close()
```

Extend the same script with the approved cursor behavior: navigate Preview, open a new Presenter and verify it starts on that Preview page, advance Presenter while Preview remains unchanged, close Presenter while Projection stays live, then exercise `Follow presentation` and `Send preview` in both directions.

### Step 2: Run the focused test and confirm failure

Run it against the current live application before implementation:

```bash
VIBE_TYPST_URL=http://127.0.0.1:9003 node test/pdfWorkspaceUi.e2e.js
```

Expected: it fails because the current PDF workspace has no draggable divider, no Typst terminal header, a roughly 454-pixel-wide rendered page, and no independent sync controls.

### Step 3: Extract the established terminal chrome

Move `shortPath` and `TerminalIcon` from `frontend/src/App.jsx` into `frontend/src/terminalUi.jsx`:

```jsx
export function shortPath(path) {
  if (!path) return ''
  const segments = path.split('/').filter(Boolean)
  return segments.length <= 5
    ? path
    : `…/${segments.slice(-3).join('/')}`
}

export function TerminalIcon({ size = 16 }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ display: 'block' }}
      aria-hidden="true"
    >
      <rect x="2" y="4" width="20" height="16" rx="2" />
      <path d="M6 9l3 3-3 3" />
      <path d="M13 15h4" />
    </svg>
  )
}
```

Import these exports back into `App.jsx`. Do not change Typst behavior or its terminal styling.

### Step 4: Split the PDF cursors and wire presenter semantics

In `PdfWorkspace.jsx`:

```jsx
const [previewPage, setPreviewPage] = useState(1)
const [presentPage, setPresentPage] = useState(1)
const presentationActive = presenting || presentationLive

useEffect(() => {
  const next = reconcilePdfPageCursors(
    { previewPage, presentPage },
    render.pages.length,
  )
  if (next.previewPage !== previewPage) setPreviewPage(next.previewPage)
  if (next.presentPage !== presentPage) setPresentPage(next.presentPage)
}, [render.pages.length, previewPage, presentPage])

function openPresenter() {
  setPresentPage(current =>
    startPdfPresentationPage(
      previewPage,
      current,
      presentationActive,
      render.pages.length,
    ),
  )
  setPresenting(true)
}
```

Use `previewPage` for `PdfPreviewPane` and transcript selection. Use `presentPage` for `Presenter`, projection broadcast state, and presenter navigation.

Pass explicit callbacks to the preview:

```jsx
<PdfPreviewPane
  page={previewPage}
  setPage={setPreviewPage}
  pages={render.pages}
  tokens={render.tokens}
  slideMap={render.slideMap}
  orphans={render.orphans}
  resetEpoch={transcriptResetEpoch}
  presentPage={presentPage}
  presentationActive={presentationActive}
  onFollowPresentation={() => setPreviewPage(presentPage)}
  onSendPreview={() => setPresentPage(previewPage)}
  onTranscriptSaved={refreshAfterTranscriptSave}
/>
```

Only enable the synchronization controls when there are rendered pages and a Presenter or Projection is active.

### Step 5: Implement the horizontal divider

Use a flex row for the main workspace:

```jsx
const mainRef = useRef(null)
const [terminalWidth, setTerminalWidth] = useState(null)

function startDividerDrag(event) {
  event.preventDefault()
  const rect = mainRef.current.getBoundingClientRect()

  function move(moveEvent) {
    setTerminalWidth(
      clampPdfTerminalWidth(moveEvent.clientX, rect.left, rect.width),
    )
  }

  function stop() {
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', stop)
  }

  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', stop)
}
```

Render:

```jsx
<div className="pdf-workspace-main" ref={mainRef}>
  <section
    className="pdf-terminal-pane"
    style={{ width: terminalWidth == null ? '38%' : terminalWidth }}
  >
    <div className="term-head">
      <span className="termpath">
        <TerminalIcon />
        {shortPath(projectDir) || '~'}
      </span>
    </div>
    <div className="pdf-terminal-host">
      <TermPanel initialCwd={projectDir} />
    </div>
  </section>
  <div
    className="pdf-workspace-divider"
    role="separator"
    aria-label="Resize terminal and PDF preview"
    aria-orientation="vertical"
    onPointerDown={startDividerDrag}
  />
  <PdfPreviewPane
    pages={render.pages}
    tokens={render.tokens}
    page={previewPage}
    setPage={setPreviewPage}
    slideMap={render.slideMap}
    orphans={render.orphans}
    resetEpoch={transcriptResetEpoch}
    presentPage={presentPage}
    presentationActive={presentationActive}
    onFollowPresentation={() => setPreviewPage(presentPage)}
    onSendPreview={() => setPresentPage(previewPage)}
    onTranscriptSaved={refreshAfterTranscriptSave}
  />
</div>
```

Keep `TermPanel` mounted during resizing. Its existing ResizeObserver must refit xterm without reconnecting.

### Step 6: Match the Typst terminal and add synchronization styling

Replace the fixed PDF grid CSS with:

```css
.pdf-workspace-main {
  display: flex;
  min-height: 0;
  flex: 1;
  overflow: hidden;
}

.pdf-terminal-pane {
  display: flex;
  flex-direction: column;
  flex: 0 0 auto;
  min-width: 0;
  min-height: 0;
  background: #101114;
}

.pdf-terminal-host {
  flex: 1;
  min-width: 0;
  min-height: 0;
}

.pdf-workspace-divider {
  flex: 0 0 6px;
  cursor: col-resize;
  background: var(--border);
  transition: background 120ms ease;
}

.pdf-workspace-divider:hover {
  background: var(--accent);
}
```

Reuse the exact existing `.term-head`, `.termpath`, and terminal icon styles from the Typst workspace.

Add compact sync buttons in `PdfPreviewPane`:

```jsx
<button
  type="button"
  disabled={!canSyncPresentation}
  onClick={onFollowPresentation}
  title="Show the page currently being presented"
>
  ⇤ Follow presentation
</button>
<button
  type="button"
  disabled={!canSyncPresentation}
  onClick={onSendPreview}
  title="Present the page currently open in Preview"
>
  ⇥ Send preview
</button>
```

Keep the PDF image centered and use `image-rendering: auto`; do not use CSS pixelation or client-side canvas upscaling.

### Step 7: Run focused and full frontend tests

Run:

```bash
node --test test/pdfWorkspace.test.js
node --test test/*.test.js
```

Expected: all frontend tests pass.

### Step 8: Commit the workspace UI

```bash
git add \
  frontend/src/terminalUi.jsx \
  frontend/src/App.jsx \
  frontend/src/PdfWorkspace.jsx \
  frontend/src/PdfPreviewPane.jsx \
  frontend/src/styles.css \
  frontend/test/pdfWorkspace.test.js \
  frontend/test/pdfWorkspaceUi.e2e.js
git commit -m "feat: refine PDF workspace layout and page sync"
```

---

## Task 4: Guarantee one initial terminal directory command per connection

**Files:**

- Add: `frontend/src/terminalSession.js`
- Modify: `frontend/src/TermPanel.jsx`
- Modify: `frontend/test/pdfWorkspace.test.js`

### Step 1: Write a failing idempotence test

Add:

```javascript
test('sendInitialCwd sends exactly once for one connected socket', () => {
  const sent = []
  const socket = {
    readyState: 1,
    send(command) {
      sent.push(command)
    },
  }

  let applied = null
  applied = sendInitialCwd(socket, '/workspace/project', applied)
  applied = sendInitialCwd(socket, '/workspace/project', applied)

  assert.equal(applied, '/workspace/project')
  assert.equal(sent.length, 1)
  assert.match(sent[0], /^cd -- /)
})
```

Also test that a new socket/session with `applied = null` sends once again, and that a not-yet-open socket sends nothing.

### Step 2: Run the focused test and confirm failure

Run:

```bash
node --test test/pdfWorkspace.test.js
```

Expected: the helper import fails.

### Step 3: Implement the guarded sender

Create `frontend/src/terminalSession.js`:

```javascript
import { pdfTerminalCdCommand } from './pdfWorkspace.js'

export function sendInitialCwd(socket, cwd, appliedCwd) {
  if (!cwd || !socket || socket.readyState !== WebSocket.OPEN) {
    return appliedCwd
  }
  if (appliedCwd === cwd) return appliedCwd
  socket.send(`${pdfTerminalCdCommand(cwd)}\r`)
  return cwd
}
```

For Node tests, avoid requiring a browser global by comparing `readyState` with the WebSocket open value `1`, or export a local `SOCKET_OPEN = 1`.

In both the `cwd` effect and `ws.onopen` in `TermPanel.jsx`, assign the returned value before another path can send:

```jsx
appliedCwdRef.current = sendInitialCwd(
  socket,
  cwd,
  appliedCwdRef.current,
)
```

Reset `appliedCwdRef.current = null` only when creating a genuinely new WebSocket connection. Do not reset it during a pane resize or React rerender.

### Step 4: Run focused and full frontend tests

Run:

```bash
node --test test/pdfWorkspace.test.js
node --test test/*.test.js
```

Expected: all tests pass, and the helper test records one `cd` command per socket.

### Step 5: Commit the terminal fix

```bash
git add \
  frontend/src/terminalSession.js \
  frontend/src/TermPanel.jsx \
  frontend/test/pdfWorkspace.test.js
git commit -m "fix: initialize PDF terminal directory once"
```

---

## Task 5: Replace the native PDF file input with a polished picker

**Files:**

- Add: `frontend/src/PdfFilePicker.jsx`
- Modify: `frontend/src/projectCreation.js`
- Modify: `frontend/src/ProjectsPage.jsx`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/test/projectCreationRouting.test.js`
- Add: `frontend/test/pdfFilePicker.e2e.js`

### Step 1: Write failing selection and formatting tests

Add pure helper tests:

```javascript
test('selectPdfFile accepts exactly one PDF', () => {
  const file = { name: 'deck.pdf', size: 2048, type: 'application/pdf' }
  assert.deepEqual(selectPdfFile([file], null), { file, error: null })
})

test('selectPdfFile preserves the current file after an invalid drop', () => {
  const current = { name: 'deck.pdf', size: 2048, type: 'application/pdf' }
  const result = selectPdfFile(
    [{ name: 'notes.txt', size: 10, type: 'text/plain' }],
    current,
  )
  assert.equal(result.file, current)
  assert.match(result.error, /PDF/)
})

test('selectPdfFile rejects multiple files and preserves the current file', () => {
  const current = { name: 'deck.pdf', size: 2048, type: 'application/pdf' }
  const result = selectPdfFile(
    [
      { name: 'a.pdf', type: 'application/pdf' },
      { name: 'b.pdf', type: 'application/pdf' },
    ],
    current,
  )
  assert.equal(result.file, current)
  assert.match(result.error, /one PDF/)
})

test('formatFileSize produces compact human-readable sizes', () => {
  assert.equal(formatFileSize(2048), '2 KB')
  assert.equal(formatFileSize(1572864), '1.5 MB')
})
```

### Step 2: Run the focused test and confirm failure

Run:

```bash
node --test test/projectCreationRouting.test.js
```

Expected: helper/component contracts fail.

Before implementation, also create `frontend/test/pdfFilePicker.e2e.js` and run it against the current application. The script opens the create dialog, chooses PDF mode, requires a styled `.pdf-file-picker`, uploads a small PDF through its hidden input, verifies filename and size text, drops a text file, and verifies the valid PDF remains selected:

```bash
VIBE_TYPST_URL=http://127.0.0.1:9003 node test/pdfFilePicker.e2e.js
```

Expected: it fails because the current dialog exposes only the native file input and has no drop target, selected-file actions, or retained invalid-drop behavior.

### Step 3: Implement selection helpers

In `frontend/src/projectCreation.js`:

```javascript
export function selectPdfFile(files, currentFile = null) {
  const candidates = Array.from(files || [])
  if (candidates.length !== 1) {
    return {
      file: currentFile,
      error: 'Choose exactly one PDF file.',
    }
  }

  const candidate = candidates[0]
  const pdf =
    candidate.type === 'application/pdf'
    || candidate.name?.toLowerCase().endsWith('.pdf')
  if (!pdf) {
    return {
      file: currentFile,
      error: 'Only PDF files are supported.',
    }
  }

  return { file: candidate, error: null }
}

export function formatFileSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 1024) return `${Math.max(0, bytes || 0)} B`
  if (bytes < 1024 * 1024) return `${trimNumber(bytes / 1024)} KB`
  return `${trimNumber(bytes / (1024 * 1024))} MB`
}
```

Keep `pdfFileFromSelection` as a compatibility wrapper if existing callers or tests still use it.

### Step 4: Build the picker component

Create a `forwardRef` component so `ProjectsPage` can clear the real input when project type changes or the modal closes:

```jsx
const PdfFilePicker = forwardRef(function PdfFilePicker(
  { file, onFile, onError },
  ref,
) {
  const inputRef = useRef(null)
  const [dragging, setDragging] = useState(false)

  useImperativeHandle(ref, () => ({
    clear() {
      if (inputRef.current) inputRef.current.value = ''
    },
  }))

  function applyFiles(files) {
    const result = selectPdfFile(files, file)
    if (result.error) {
      onError(result.error)
      return
    }
    onFile(result.file)
  }

  return (
    <div
      className={`pdf-file-picker${dragging ? ' is-dragging' : ''}`}
      onDragEnter={event => {
        event.preventDefault()
        setDragging(true)
      }}
      onDragOver={event => event.preventDefault()}
      onDragLeave={() => setDragging(false)}
      onDrop={event => {
        event.preventDefault()
        setDragging(false)
        applyFiles(event.dataTransfer.files)
      }}
    >
      <input
        ref={inputRef}
        className="pdf-file-picker-input"
        type="file"
        accept="application/pdf,.pdf"
        onChange={event => applyFiles(event.target.files)}
      />
      {file ? (
        <div className="pdf-file-picker-selection">
          <span className="pdf-file-picker-name">{file.name}</span>
          <span className="pdf-file-picker-size">{formatFileSize(file.size)}</span>
          <button type="button" onClick={() => inputRef.current?.click()}>
            Change
          </button>
          <button
            type="button"
            onClick={() => {
              if (inputRef.current) inputRef.current.value = ''
              onFile(null)
            }}
          >
            Remove
          </button>
        </div>
      ) : (
        <>
          <button type="button" onClick={() => inputRef.current?.click()}>
            Select PDF
          </button>
          <span>or drop one PDF here</span>
        </>
      )}
    </div>
  )
})
```

Use a drag-depth counter or related-target containment check if nested elements cause flicker during browser verification.

### Step 5: Integrate reset, error, and create behavior

In `ProjectsPage.jsx`:

```jsx
<PdfFilePicker
  ref={pdfInputRef}
  file={pdfFile}
  onFile={setPdfFile}
  onError={message => toast.error(message)}
/>
```

Replace direct `.value = ''` operations with:

```javascript
pdfInputRef.current?.clear()
```

Continue sending the same single `File` object to the existing create-project request. Switching to Typst and cancelling the modal must both call `setPdfFile(null)` and clear the hidden input.

### Step 6: Style the drop target and selected-file state

Add styles for:

- a dashed bordered drop target with consistent radius and spacing;
- a primary `Select PDF` button matching existing application buttons;
- a highlighted `.is-dragging` state;
- filename ellipsis and subdued size text;
- compact `Change` and `Remove` actions;
- a visually hidden `.pdf-file-picker-input`.

Ensure the picker has a visible keyboard focus state and remains readable at the create dialog's narrowest supported width.

### Step 7: Run focused and full frontend tests

Run:

```bash
node --test test/projectCreationRouting.test.js
node --test test/*.test.js
```

Expected: all frontend tests pass.

### Step 8: Commit the picker

```bash
git add \
  frontend/src/PdfFilePicker.jsx \
  frontend/src/projectCreation.js \
  frontend/src/ProjectsPage.jsx \
  frontend/src/styles.css \
  frontend/test/projectCreationRouting.test.js \
  frontend/test/pdfFilePicker.e2e.js
git commit -m "feat: add polished PDF project picker"
```

---

## Task 6: Build, verify visually, integrate, deploy, and preserve data

**Files:**

- Modify generated assets only through the normal frontend build: `frontend/dist/**`
- Do not modify or stage: `docs/design/**`

### Step 1: Run clean backend verification

Run:

```bash
backend/.venv/bin/python -m unittest discover -s tests
```

If the host virtual environment contains an incompatible Linux-only resolver artifact, temporarily move only that exact generated artifact aside, run the suite, and restore it before continuing.

### Step 2: Run frontend tests and a clean production build

Because the host does not provide a global npm and the canonical ignored `node_modules` may be stale, build in a clean Node 20 container from the implementation worktree:

```bash
docker run --rm --platform linux/amd64 \
  -v "$PWD/frontend:/src" \
  -w /src \
  node:20-bookworm \
  bash -lc 'npm ci && npm test && npm run build'
```

Expected: all frontend tests pass and Vite creates a fresh `frontend/dist`.

### Step 3: Run a local browser smoke test before deployment

Start the application from the implementation worktree on unused local ports, then use Puppeteer to verify:

1. The active 43-page PDF project's page image has a natural longest edge of approximately 2560 pixels.
2. The displayed image is sharp at device pixel ratio 2 and `image-rendering` is `auto`.
3. Dragging the divider changes both pane widths and the xterm canvas refits without a WebSocket reconnect.
4. Preview navigation does not change the Presenter page until `Send preview` is clicked.
5. Presenter navigation does not change Preview until `Follow presentation` is clicked.
6. Opening a new Presenter starts from the current Preview page.
7. The create dialog supports Select, Change, Remove, and drag/drop while preserving a valid selection after an invalid drop.
8. The initial terminal output contains only one automatic `cd` command for the connection.

Capture screenshots of the PDF workspace and create-project picker in `/tmp` for inspection; do not commit them.

### Step 4: Review the diff and protect unrelated work

Run:

```bash
git status --short
git diff --check
git diff --stat
```

Confirm:

- `docs/design/` remains untracked and untouched;
- no workspace PDF, transcript, user auth, or container-mounted data is staged;
- only intended source, tests, plan/spec, and generated frontend assets are included.

### Step 5: Commit generated production assets

```bash
git add frontend/dist
git commit -m "build: update PDF workspace assets"
```

If the build produces byte-identical assets and there is nothing to stage, record that fact and continue without an empty commit.

### Step 6: Run pre-integration verification

Run:

```bash
backend/.venv/bin/python -m unittest discover -s tests
docker run --rm --platform linux/amd64 \
  -v "$PWD/frontend:/src" \
  -w /src \
  node:20-bookworm \
  bash -lc 'npm ci && npm test && npm run build'
git diff --check
```

Expected: all commands pass on the final feature-worktree state.

### Step 7: Integrate the feature branch into `main`

Follow `superpowers:finishing-a-development-branch` after verification. Merge the feature branch into the canonical `main` worktree without touching the pre-existing `docs/design/` directory. Re-run the same backend/frontend verification on the merge result.

### Step 8: Push the verified main branch

Push `main` using the repository's working HTTPS remote configuration if the SSH remote remains unavailable:

```bash
git push https://github.com/JoelYYoung/vibe-typst.git main
```

Do not invent or echo credentials. Confirm the pushed commit hash from Git output.

### Step 9: Rebuild the clean workspace image

Use the existing deployment inputs already established for this repository:

- build for `linux/amd64`;
- retain the current PyMuPDF-enabled environment archive;
- retain the existing Linux resolver artifact;
- build `tcb-workspace:latest` from the verified `main` source.

Inspect the resulting image metadata and run its health command before updating the user's container.

### Step 10: Hot-update the existing user container without replacing its mount

Resolve and verify the exact target first:

```bash
docker inspect tcb-ws-joelyang
```

Confirm the preserved mount is:

```text
/Users/xavier/Projects/web-services/vibe-typst/workspaces/joelyang -> /workspace
```

Stop only `tcb-ws-joelyang`, copy the verified `/app/backend` and `/app/frontend/dist` from a temporary container created from `tcb-workspace:latest`, and restart `tcb-ws-joelyang`. Do not delete, recreate, or overwrite `/workspace`, the authentication state, or the existing PDF project.

### Step 11: Verify the deployed application

Against the live port 9003 deployment:

```bash
curl -fsS http://127.0.0.1:9003/api/health
docker inspect tcb-ws-joelyang
```

Use Puppeteer at device pixel ratio 2 to repeat the eight smoke checks from Step 3. Confirm the existing project still reports 43 pages and that its rendered image's longest natural edge is approximately 2560 pixels.

### Step 12: Clean only temporary deployment artifacts

Remove temporary containers and temporary staging directories created during this task. Leave the canonical repository, `workspaces/joelyang`, user data, `docs/design/`, and the running `tcb-ws-joelyang` container intact.

### Step 13: Report completion with evidence

Report:

- the merged and pushed commit hash;
- backend and frontend test counts/results;
- production build success;
- live health and container status;
- the measured deployed PDF natural dimensions;
- successful divider and page-sync browser checks;
- confirmation that the existing 43-page project and canonical mount were preserved.
