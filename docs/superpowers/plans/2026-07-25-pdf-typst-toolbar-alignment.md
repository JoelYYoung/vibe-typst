# PDF and Typst Toolbar Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align the PDF workspace toolbar and page-synchronization controls with the existing Typst interface while preserving PDF-specific terminal, transcript, navigation, and presentation behavior.

**Architecture:** Reuse the established Typst `bar`, `bar-title`, `openbtn present`, `actions`, `status-chip`, and `pb-btn icon` patterns directly in the PDF components. Remove the PDF-only Files & versions drawer and its unreachable state/API calls, but do not introduce a shared toolbar abstraction or alter any backend behavior.

**Tech Stack:** React 18, CSS, Node.js test runner, Puppeteer, Vite

## Global Constraints

- PDF projects must not show Files, PDF export, Files & versions, or pending-comment controls.
- The PDF terminal stays permanently visible in the left resizable pane.
- Transcript download, visibility, editing, saving, and page navigation remain unchanged.
- Preview and Presentation retain independent page cursors and existing Presenter start behavior.
- Synchronization controls are icon-only `⇤` and `⇥`, with both `title` and `aria-label`.
- No backend, storage, transcript-format, rendering, or project-creation changes.
- Preserve the pre-existing untracked `docs/design/` directory.

---

### Task 1: Align the PDF workspace toolbar and remove Files & versions

**Files:**
- Modify: `frontend/test/pdfWorkspace.test.js`
- Modify: `frontend/test/pdfWorkspaceUi.e2e.js`
- Modify: `frontend/src/PdfWorkspace.jsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: `PdfWorkspace({ project, onBack })`, existing `openPresenter()`, `presentationActive`, and `presentPage`.
- Produces: a PDF `<header className="bar">` using `back-btn`, `bar-title`, `openbtn present`, `actions`, and `status-chip live`.

- [ ] **Step 1: Add a focused source-level regression test**

Append this test to `frontend/test/pdfWorkspace.test.js`:

```js
test('PDF toolbar reuses Typst structure without Files and versions behavior', () => {
  const workspace = fs.readFileSync(new URL('../src/PdfWorkspace.jsx', import.meta.url), 'utf8')

  assert.match(workspace, /className="bar-title"/)
  assert.match(workspace, /className="openbtn present"/)
  assert.match(workspace, /className="actions"/)
  assert.match(workspace, /status-chip live/)
  assert.doesNotMatch(workspace, /PdfFilesDrawer/)
  assert.doesNotMatch(workspace, /Files & versions/)
  assert.doesNotMatch(workspace, /listProjectFiles|gitVersions|gitRestore/)
})
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
/Users/xavier/Projects/nsw-driving-test-slot-monitor/.node/bin/node \
  --test frontend/test/pdfWorkspace.test.js
```

Expected: FAIL because `PdfWorkspace.jsx` still uses `project-name-chip`,
renders Files & versions, and contains the drawer/API calls.

- [ ] **Step 3: Remove the PDF-only drawer and restore-only component state**

In `frontend/src/PdfWorkspace.jsx`:

- delete `PdfFilesDrawer`;
- remove `pdfVersions` and `createPdfRestoreResetLatch` from imports;
- remove `drawerOpen`, `transcriptResetEpoch`, `restoreResetLatchRef`, and
  `refreshAfterRestore`;
- remove the restore-latch `consume()` call from the poll controller;
- stop passing `resetEpoch` to `PdfPreviewPane`; and
- remove the conditional drawer render.

Do not remove the pure restore helpers from `pdfWorkspace.js`; their existing
tests remain valid and no unrelated API surface changes are needed.

- [ ] **Step 4: Replace the PDF toolbar with the Typst layout pattern**

Use this structure in `PdfWorkspace.jsx`:

```jsx
<header className="bar">
  <button className="back-btn" onClick={onBack} title="Back to projects">
    ← Projects
  </button>
  <div className="bar-title" title={project?.name || 'PDF project'}>
    {project?.name || 'PDF project'}
  </div>
  <button
    className="openbtn present"
    onClick={openPresenter}
    disabled={!render.pages.length}
    title="presenter view (current + next page, transcript, dual-screen)"
  >
    ▶ Present
  </button>
  <div className="actions">
    <span
      className={'status-chip live' + (presentationActive ? ' on' : '')}
      title={presentationActive
        ? 'a projection / presentation is open and live'
        : 'open a projection from Present to control it from here'}
    >
      <span className="status-dot" />
      {presentationActive ? `live · ${presentPage}` : 'no presentation'}
    </span>
  </div>
</header>
```

The PDF difference is deliberate: it has no Files, export, or pending-comment
buttons, and its Present action stays disabled until rendered pages exist.

- [ ] **Step 5: Remove unreachable PDF drawer and project-chip CSS**

Delete `.pdf-drawer*`, `.project-name-chip`, and PDF mobile overrides that only
target those removed elements. Keep all terminal, divider, preview, transcript,
and responsive pane rules unchanged.

- [ ] **Step 6: Run the focused test and verify GREEN**

Run:

```bash
/Users/xavier/Projects/nsw-driving-test-slot-monitor/.node/bin/node \
  --test frontend/test/pdfWorkspace.test.js
```

Expected: all tests in the file PASS.

- [ ] **Step 7: Add browser assertions for layout and status**

Add a test to `frontend/test/pdfWorkspaceUi.e2e.js` that opens a PDF project and
asserts real DOM behavior:

```js
test('PDF toolbar mirrors Typst placement while omitting unavailable actions', async () => {
  const page = await openPdfWorkspace()
  const observed = await page.evaluate(() => {
    const toolbar = document.querySelector('.pdf-workspace > .bar')
    const title = toolbar.querySelector('.bar-title')
    const titleRect = title.getBoundingClientRect()
    return {
      title: title.textContent.trim(),
      centerDelta: Math.abs(
        titleRect.left + titleRect.width / 2 - window.innerWidth / 2,
      ),
      buttons: [...toolbar.querySelectorAll('button')].map(button => button.textContent.trim()),
      status: toolbar.querySelector('.status-chip.live')?.textContent.trim(),
      hasDrawer: Boolean(document.querySelector('.pdf-drawer')),
    }
  })

  assert.ok(observed.title)
  assert.ok(observed.centerDelta <= 1)
  assert.deepEqual(observed.buttons, ['← Projects', '▶ Present'])
  assert.equal(observed.status, 'no presentation')
  assert.equal(observed.hasDrawer, false)
})
```

- [ ] **Step 8: Run the browser test against the worktree frontend**

From the worktree's `frontend/` directory, start a Vite server whose API and
WebSocket routes proxy to the existing container on port 9003:

```bash
/Users/xavier/Projects/nsw-driving-test-slot-monitor/.node/bin/node \
  --input-type=module -e '
    import { createServer } from "vite"
    import react from "@vitejs/plugin-react"
    import wasm from "vite-plugin-wasm"
    import topLevelAwait from "vite-plugin-top-level-await"
    const server = await createServer({
      configFile: false,
      root: process.cwd(),
      plugins: [react(), wasm(), topLevelAwait()],
      server: {
        host: "127.0.0.1",
        port: 4174,
        proxy: {
          "/api": "http://127.0.0.1:9003",
          "/ws": { target: "ws://127.0.0.1:9003", ws: true },
          "/pty": { target: "ws://127.0.0.1:9003", ws: true },
        },
      },
    })
    await server.listen()
    await new Promise(() => {})
  '
```

Keep that process running, then run:

```bash
VIBE_TYPST_URL=http://127.0.0.1:4174 \
  /Users/xavier/Projects/nsw-driving-test-slot-monitor/.node/bin/node \
  frontend/test/pdfWorkspaceUi.e2e.js
```

Expected: the new toolbar test and all existing PDF workspace browser tests
PASS.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/PdfWorkspace.jsx frontend/src/styles.css \
  frontend/test/pdfWorkspace.test.js frontend/test/pdfWorkspaceUi.e2e.js
git commit -m "feat: align PDF workspace toolbar with Typst"
```

### Task 2: Match Typst's icon-only synchronization controls

**Files:**
- Modify: `frontend/test/pdfWorkspaceUi.e2e.js`
- Modify: `frontend/src/PdfPreviewPane.jsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: `onFollowPresentation`, `onSendPreview`, `presentPage`, `page`, and `canSyncPresentation`.
- Produces: two buttons selected by `aria-label="Follow presentation"` and `aria-label="Send preview"`, with visible glyphs `⇤` and `⇥`.

- [ ] **Step 1: Add a failing browser assertion for icon-only controls**

Add this test:

```js
test('PDF synchronization controls match Typst icon buttons', async () => {
  const page = await openPdfWorkspace()
  const controls = await page.$$eval('.pdf-sync-controls button', buttons => buttons.map(button => ({
    text: button.textContent.trim(),
    className: button.className,
    title: button.title,
    ariaLabel: button.getAttribute('aria-label'),
  })))

  assert.deepEqual(controls.map(control => control.text), ['⇤', '⇥'])
  assert.ok(controls.every(control => control.className.includes('pb-btn')))
  assert.ok(controls.every(control => control.className.includes('icon')))
  assert.deepEqual(controls.map(control => control.ariaLabel), [
    'Follow presentation',
    'Send preview',
  ])
  assert.ok(controls.every(control => control.title))
})
```

Before changing production code, run the E2E suite against the current
worktree frontend. Expected: FAIL because the buttons still contain visible
labels and use `pdf-sync-button`.

- [ ] **Step 2: Implement the Typst button pattern**

Change the two controls in `PdfPreviewPane.jsx` to:

```jsx
<button
  className="pb-btn icon"
  type="button"
  disabled={!canSyncPresentation}
  onClick={onFollowPresentation}
  title={`Follow presentation — show page ${presentPage || 1} in Preview`}
  aria-label="Follow presentation"
>
  ⇤
</button>
<button
  className="pb-btn icon"
  type="button"
  disabled={!canSyncPresentation}
  onClick={onSendPreview}
  title={`Send Preview — make the presentation show page ${page}`}
  aria-label="Send preview"
>
  ⇥
</button>
```

Delete the now-unused `.pdf-sync-button` rules. Retain
`.pdf-sync-controls` for grouping and spacing.

- [ ] **Step 3: Update the existing synchronization behavior test**

Add this helper to `pdfWorkspaceUi.e2e.js`:

```js
async function clickButtonByAriaLabel(page, ariaLabel) {
  await page.waitForFunction(
    label => {
      const button = document.querySelector(`button[aria-label="${label}"]`)
      return button && !button.disabled
    },
    {},
    ariaLabel,
  )
  await page.click(`button[aria-label="${ariaLabel}"]`)
}
```

Replace the two text-based sync calls with:

```js
await clickButtonByAriaLabel(page, 'Follow presentation')
await clickButtonByAriaLabel(page, 'Send preview')
```

- [ ] **Step 4: Run browser tests and verify GREEN**

Run:

```bash
VIBE_TYPST_URL=http://127.0.0.1:4174 \
  /Users/xavier/Projects/nsw-driving-test-slot-monitor/.node/bin/node \
  frontend/test/pdfWorkspaceUi.e2e.js
```

Expected: all toolbar, icon, sharp-render, divider, terminal-session, and
Preview/Presentation synchronization checks PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/PdfPreviewPane.jsx frontend/src/styles.css \
  frontend/test/pdfWorkspaceUi.e2e.js
git commit -m "feat: match Typst PDF sync controls"
```

### Task 3: Build and full frontend verification

**Files:**
- Modify: `frontend/dist/index.html`
- Replace: hashed files under `frontend/dist/assets/`

**Interfaces:**
- Consumes: completed React and CSS changes from Tasks 1 and 2.
- Produces: deployable Vite assets referenced by `frontend/dist/index.html`.

- [ ] **Step 1: Run the complete frontend unit suite**

```bash
/Users/xavier/Projects/nsw-driving-test-slot-monitor/.node/bin/node \
  --test frontend/test/*.test.js
```

Expected: all tests PASS with zero failures.

- [ ] **Step 2: Build production assets with Node 22**

```bash
docker run --rm \
  -v "$PWD/frontend:/app" \
  -w /app \
  node:22-bookworm \
  npm run build
```

Expected: Vite exits 0 and writes a new JS/CSS hash pair. The existing large
chunk warning is informational.

- [ ] **Step 3: Run both focused browser suites against Vite**

```bash
VIBE_TYPST_URL=http://127.0.0.1:4174 \
  /Users/xavier/Projects/nsw-driving-test-slot-monitor/.node/bin/node \
  frontend/test/pdfWorkspaceUi.e2e.js

VIBE_TYPST_URL=http://127.0.0.1:4174 \
  /Users/xavier/Projects/nsw-driving-test-slot-monitor/.node/bin/node \
  frontend/test/pdfFilePicker.e2e.js
```

Expected: both commands exit 0.

- [ ] **Step 4: Capture and inspect a PDF workspace screenshot**

At 1440×900, verify:

- project title is centered at the same location as Typst;
- Projects and Present use Typst button styling;
- no Files & versions control exists;
- presentation status is right-aligned;
- sync controls display only `⇤` and `⇥`; and
- PDF terminal, preview, page navigation, and transcript remain visible.

- [ ] **Step 5: Commit production assets**

```bash
git add frontend/dist
git commit -m "build: update aligned PDF toolbar assets"
```

- [ ] **Step 6: Final clean-tree verification**

Run:

```bash
git diff --check
git status --short
/Users/xavier/Projects/nsw-driving-test-slot-monitor/.node/bin/node \
  --test frontend/test/*.test.js
```

Expected: the feature worktree is clean and all frontend tests PASS. The
pre-existing `?? docs/design/` remains untouched in the main checkout.
