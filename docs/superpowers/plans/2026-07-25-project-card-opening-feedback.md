# Project Card Opening Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show immediate spinner feedback on the selected project card while its open request is in flight.

**Architecture:** Add `openingProjectId` to `ProjectsPage`, pass card-local `opening` plus page-wide `interactionDisabled` into `ProjectCard`, and render an accessible overlay inside the selected card. Verify the real pending-request behavior with Puppeteer request interception.

**Tech Stack:** React 18, CSS, Node.js test runner, Puppeteer, Vite

## Global Constraints

- Applies to both Typst and PDF project cards.
- Only the selected card displays `Opening…`.
- All card open and menu actions are disabled during an open request.
- Project creation keeps its existing independent `busy` state.
- Open failures clear the overlay and keep the existing error toast.
- No backend, routing, project-data, or workspace changes.
- Preserve the pre-existing untracked `docs/design/` directory.

---

### Task 1: Add a failing pending-open browser test

**Files:**
- Create: `frontend/test/projectOpeningUi.e2e.js`

**Interfaces:**
- Consumes: `POST /api/projects/{id}/open` and the rendered `.project-card`.
- Produces: behavioral coverage for `.project-opening-overlay`, `aria-busy`, and disabled card actions.

- [ ] Write a Puppeteer test that intercepts and pauses the PDF project open
  request, clicks its card body, then asserts:

```js
assert.equal(cardState.busy, 'true')
assert.equal(cardState.overlay, 'Opening…')
assert.equal(cardState.menuDisabled, true)
assert.equal(otherCardState.overlay, null)
assert.equal(otherCardState.openDisabled, 'true')
```

- [ ] Run it against the current frontend and confirm it fails because the
  overlay and card busy state do not exist.

### Task 2: Implement card-local opening feedback

**Files:**
- Modify: `frontend/src/ProjectsPage.jsx`
- Modify: `frontend/src/styles.css`
- Test: `frontend/test/projectOpeningUi.e2e.js`

**Interfaces:**
- `ProjectCard({ project, onOpen, onRename, onDelete, onCopy, opening, interactionDisabled })`
- `openingProjectId: string | null`

- [ ] Add `openingProjectId` state to `ProjectsPage`.
- [ ] Guard `handleOpen` against a second request, set the ID before awaiting
  `api.openProject`, and clear it in `finally`.
- [ ] Pass `opening` and `interactionDisabled` to every card.
- [ ] Add `aria-busy`, disabled open/menu behavior, and this selected-card
  overlay:

```jsx
{opening && (
  <div className="project-opening-overlay" role="status" aria-live="polite">
    <span className="project-opening-spinner" aria-hidden="true" />
    <span>Opening…</span>
  </div>
)}
```

- [ ] Add absolute overlay, spinner animation, disabled-card cursor, and
  reduced-motion CSS without changing card layout.
- [ ] Run the focused browser test and verify it passes.
- [ ] Run all frontend unit tests and the existing PDF browser suites.
- [ ] Commit source and tests.

### Task 3: Build and deploy

**Files:**
- Modify: `frontend/dist/index.html`
- Replace: hashed files under `frontend/dist/assets/`

- [ ] Build production assets.
- [ ] Run the focused test against the production deployment.
- [ ] Commit assets, merge to `main`, and push the remote `main`.
- [ ] Rebuild `tcb-workspace:latest`.
- [ ] Hot-copy assets before `index.html` into `tcb-ws-joelyang` without
  stopping it.
- [ ] Verify the live pending-open overlay, container mount, and project data
  hashes, then clean temporary deployment resources.
