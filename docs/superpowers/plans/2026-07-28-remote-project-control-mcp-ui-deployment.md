# Remote Project-Control MCP UI and Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add account token management and direct project links to the web UI, document remote MCP configuration, build production assets, and deploy the complete feature without losing workspace data.

**Architecture:** The existing Projects-page account menu opens a focused PAT dialog backed by
cookie-authenticated `/account/tokens` APIs. A distinct `openProject` query parameter opens a
stable project ID after login without colliding with the existing `?project` projection window.
Deployment migrates SQLite additively, preserves workspace mounts, and verifies hashes before and
after any required process/container restart.

**Tech Stack:** React 18, Vite 5, browser Fetch API, Node test runner, Puppeteer, FastAPI control
plane, Docker/Podman, MCP Inspector/official Python client.

## Global Constraints

- The full token secret appears once after creation and is never returned by list APIs.
- The UI never writes PATs to localStorage, sessionStorage, URLs, logs, or clipboard without an
  explicit Copy button click.
- Presets are exactly Viewer and Editor.
- Expiry choices are 30 days, 90 days, one year, and no expiry.
- Revocation takes effect immediately and requires an explicit confirmation.
- Human project URLs require normal web login and never contain PATs or project handles.
- `?openProject=<stable-id>` means authenticated direct-open.
- Existing `?project` continues to mean the projection/audience window.
- Existing cookie sessions, projects, workspace mounts, local terminal, and project-local MCP
  configuration remain compatible.
- Production assets are rebuilt and committed.
- Any container recreation preserves the exact workspace bind mount and is preceded/followed by
  data hashes.
- Do not touch the existing untracked `docs/design/` directory.

---

## File Structure

- Create `frontend/src/AccountTokensDialog.jsx`: PAT create/list/revoke/copy UI.
- Create `frontend/src/tokenManagement.js`: pure expiry/preset/secret-display helpers.
- Modify `frontend/src/ProjectsPage.jsx`: account-menu entry and dialog state.
- Modify `frontend/src/api.js`: token management requests.
- Modify `frontend/src/styles.css`: dialog, token rows, secret warning, responsive layout.
- Modify `frontend/src/projectRouting.js`: parse and consume `openProject`.
- Modify `frontend/src/main.jsx`: direct-open startup flow.
- Create `frontend/test/tokenManagement.test.js`: pure UI model tests.
- Create `frontend/test/accountTokens.e2e.js`: real browser token workflow.
- Create `frontend/test/directProjectUrl.e2e.js`: authenticated direct-open routing.
- Modify `frontend/package.json`: focused E2E scripts.
- Modify `README.md`: remote MCP purpose and safe connection example.
- Modify `docs/architecture.md`: control-plane MCP component/data flow.
- Modify `docs/deployment.md`: public URL, MCP dependency, migration, verification, and rollback.
- Modify `frontend/dist/**`: production bundle output.

### Task 1: Token-management API client and pure model

**Files:**
- Create: `frontend/src/tokenManagement.js`
- Modify: `frontend/src/api.js:24-50`
- Create: `frontend/test/tokenManagement.test.js`

**Interfaces:**
- Produces:
  - `TOKEN_PRESETS`
  - `TOKEN_EXPIRIES`
  - `expiryTimestamp(choice: string, nowMs: number) -> number | null`
  - `displayTokenPrefix(token: object) -> string`
  - API functions `listAccountTokens`, `createAccountToken`, `revokeAccountToken`
- Consumes: existing JSON response/error helper in `api.js`.

- [ ] **Step 1: Write failing pure-model tests**

```javascript
test('token expiry choices resolve to exact UTC seconds', () => {
  const now = Date.UTC(2026, 6, 28)
  assert.equal(expiryTimestamp('30d', now), (now + 30 * 86400_000) / 1000)
  assert.equal(expiryTimestamp('90d', now), (now + 90 * 86400_000) / 1000)
  assert.equal(expiryTimestamp('1y', now), (now + 365 * 86400_000) / 1000)
  assert.equal(expiryTimestamp('never', now), null)
})

test('only viewer and editor presets are exposed', () => {
  assert.deepEqual(TOKEN_PRESETS.map((item) => item.value), ['viewer', 'editor'])
})
```

- [ ] **Step 2: Run and verify RED**

```bash
cd frontend && npm test -- --test-name-pattern='token'
```

Expected: missing `tokenManagement.js`.

- [ ] **Step 3: Implement exact choices and API calls**

```javascript
export const TOKEN_PRESETS = [
  { value: 'viewer', label: 'Viewer', description: 'Read projects, slides, files, transcripts, and comments.' },
  { value: 'editor', label: 'Editor', description: 'Viewer access plus project, file, document, transcript, and comment changes.' },
]

export const TOKEN_EXPIRIES = [
  { value: '30d', label: '30 days' },
  { value: '90d', label: '90 days' },
  { value: '1y', label: '1 year' },
  { value: 'never', label: 'No expiry' },
]
```

Add:

```javascript
export const listAccountTokens = () => fetch('/account/tokens').then(JD)
export const createAccountToken = (name, preset, expiresAt) =>
  fetch('/account/tokens', {
    method: 'POST',
    headers: JSONHDR,
    body: JSON.stringify({ name, preset, expires_at: expiresAt }),
  }).then(JD)
export const revokeAccountToken = (id) =>
  fetch(`/account/tokens/${encodeURIComponent(id)}`, { method: 'DELETE' }).then(JD)
```

- [ ] **Step 4: Run unit tests**

```bash
cd frontend && npm test
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/tokenManagement.js frontend/src/api.js frontend/test/tokenManagement.test.js
git commit -m "feat: add token management client"
```

### Task 2: Account token dialog

**Files:**
- Create: `frontend/src/AccountTokensDialog.jsx`
- Modify: `frontend/src/ProjectsPage.jsx:61-145`
- Modify: `frontend/src/styles.css:663-760`
- Create: `frontend/test/accountTokens.e2e.js`
- Modify: `frontend/package.json`

**Interfaces:**
- Produces `AccountTokensDialog({ onClose })`.
- Consumes token API/model from Task 1, existing toast API, and account-menu/modal style patterns.

- [ ] **Step 1: Write a failing browser workflow test**

Intercept `/whoami` and `/account/tokens` and assert:

```javascript
await page.click('button[title="Account"]')
await page.click('button[data-action="manage-tokens"]')
await page.type('input[name="token-name"]', 'remote-codex')
await page.select('select[name="token-preset"]', 'editor')
await page.select('select[name="token-expiry"]', '90d')
await page.click('button[type="submit"]')
await page.waitForSelector('[data-testid="token-secret"]')
assert.equal(
  await page.$eval('[data-testid="token-secret"]', el => el.textContent),
  'vbt_tok1_secret',
)
await page.click('button[data-action="close-token-secret"]')
assert.equal(await page.$('[data-testid="token-secret"]'), null)
```

Then reopen the dialog and assert the secret is not refetched/displayed, revoke the token with
confirmation, and verify one DELETE request.

- [ ] **Step 2: Run and verify RED**

```bash
cd frontend && node test/accountTokens.e2e.js
```

Expected: Manage tokens action/dialog absent.

- [ ] **Step 3: Build the accessible dialog**

The dialog contains:

- existing tokens with name, prefix, preset, created/expiry/last-used, and status;
- create form with exact preset/expiry choices;
- one-time secret panel with warning and explicit Copy button;
- revoke action and confirmation;
- loading, empty, request-error, and submit-busy states.

Use `role="dialog"`, `aria-modal="true"`, labelled heading, Escape close when no secret is
pending, initial focus, and focus return to the account-menu trigger. Closing clears the secret
state immediately.

- [ ] **Step 4: Add the account menu entry**

Add:

```jsx
<button
  className="usermenu-item"
  data-action="manage-tokens"
  onClick={() => { setOpen(false); setTokensOpen(true) }}
>
  Personal access tokens
</button>
```

Render `AccountTokensDialog` beside the password dialog. Do not render it in local mode where
`whoami` has no user.

- [ ] **Step 5: Style and test responsive behavior**

Reuse project modal colors and control sizes. At widths below 640px, stack token metadata/actions
and keep the secret horizontally scrollable without expanding the page.

Run:

```bash
cd frontend && node test/accountTokens.e2e.js
cd frontend && npm test
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/AccountTokensDialog.jsx frontend/src/ProjectsPage.jsx frontend/src/styles.css frontend/test/accountTokens.e2e.js frontend/package.json
git commit -m "feat: add personal token settings UI"
```

### Task 3: Authenticated direct project URL

**Files:**
- Modify: `frontend/src/projectRouting.js`
- Modify: `frontend/src/main.jsx:10-66`
- Modify: `control/main.py:539-575`
- Modify: `control/login.html:117-137`
- Modify: `frontend/test/projectCreationRouting.test.js`
- Create: `frontend/test/directProjectUrl.e2e.js`
- Modify: `frontend/package.json`

**Interfaces:**
- Produces:
  - `requestedProjectId(search: string) -> string | null`
  - `clearRequestedProject(search: string) -> string`
- Consumes: `api.openProject`, `canonicalProjectFromOpen`, and existing workspace type routing.

- [ ] **Step 1: Write failing query parser tests**

```javascript
test('openProject is distinct from the projection query', () => {
  assert.equal(requestedProjectId('?openProject=abc%20123'), 'abc 123')
  assert.equal(requestedProjectId('?project'), null)
  assert.equal(clearRequestedProject('?openProject=p1&theme=dark'), '?theme=dark')
})
```

- [ ] **Step 2: Run and verify RED**

```bash
cd frontend && npm test -- --test-name-pattern='openProject'
```

Expected: missing helpers.

- [ ] **Step 3: Implement startup direct-open**

After `/api/app/state` confirms configuration:

```javascript
const requested = requestedProjectId(location.search)
if (requested) {
  try {
    const result = await api.openProject(requested)
    const project = canonicalProjectFromOpen(result)
    setActiveProject(project)
    setView('editor')
    history.replaceState(null, '', location.pathname + clearRequestedProject(location.search))
    return
  } catch (error) {
    toast.error(error.message || 'Unable to open shared project link')
  }
}
setView('projects')
```

Keep `isProjection = new URLSearchParams(location.search).has('project')` unchanged. A failed or
unknown project lands on Projects with an error; it never falls into Projection.

- [ ] **Step 4: Preserve direct links through login**

Add `_safe_next(value)` in `control/main.py`; accept only same-origin relative paths beginning
with one `/` and reject `//`, schemes, and control characters. An unauthenticated catch-all
request redirects to:

```python
RedirectResponse("/login?" + urlencode({"next": request.url.path + query_suffix}))
```

Render the validated value into a hidden `next` field in `login.html`. `POST /login` accepts the
field, validates it again, and redirects there after successful authentication. A failed login
retains the validated hidden value, and an already-authenticated `GET /login?next=...` redirects
straight to that safe destination. Invalid/missing values redirect to `/`.

- [ ] **Step 5: Add browser tests**

Test a Typst ID routes to `App`, a PDF ID routes to `PdfWorkspace`, the address bar no longer
contains `openProject` after success, login redirects preserve the original query through the
control plane, and `?project` still renders Projection.

Run:

```bash
cd frontend && node test/directProjectUrl.e2e.js
cd frontend && npm test
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/projectRouting.js frontend/src/main.jsx control/main.py control/login.html frontend/test/projectCreationRouting.test.js frontend/test/directProjectUrl.e2e.js frontend/package.json
git commit -m "feat: open authenticated project links"
```

### Task 4: User and deployment documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/deployment.md`
- Modify: `control/start.sh`

**Interfaces:**
- Produces documented config:
  - `PUBLIC_BASE_URL=https://slides.example.com`
  - remote MCP endpoint `${PUBLIC_BASE_URL}/mcp`
  - bearer token header
- Consumes: final tool names, scopes, token UI, and deployment behavior.

- [ ] **Step 1: Add a remote-agent workflow to README**

Document:

```text
1. Sign in and create an Editor PAT under Personal access tokens.
2. Configure the remote MCP URL https://<host>/mcp.
3. Send Authorization: Bearer ${VIBE_TYPST_TOKEN}.
4. Call list_projects or create_typst_project.
5. Call open_project and retain project_handle.
6. Use document/file/comment tools.
7. Give the returned web_url to the human; they sign in normally.
8. Revoke the PAT when no longer needed.
```

Use an environment placeholder in examples; never include a real token.

- [ ] **Step 2: Update architecture and deployment configuration**

Add the control-plane MCP, PAT/lease/audit tables, upload flow, project handle invalidation, and
the absence of remote terminal/project-delete tools.

Document:

```bash
export PUBLIC_BASE_URL="https://slides.example.com"
export VIBE_TYPST_TOKEN="vbt_..."
```

and a generic client configuration:

```json
{
  "type": "streamable-http",
  "url": "https://slides.example.com/mcp",
  "headers": {
    "Authorization": "Bearer ${VIBE_TYPST_TOKEN}"
  }
}
```

State that client syntax for environment expansion varies; users must keep the token in their
client's secret store.

- [ ] **Step 3: Export `PUBLIC_BASE_URL` in start script**

Add:

```bash
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-http://localhost:$PORT}"
```

before launching uvicorn. Validate scheme/host during control startup and fail with a clear message
when production uses an invalid URL.

- [ ] **Step 4: Check docs and commit**

```bash
rg -n 'vbt_[A-Za-z0-9_-]{20,}' README.md docs control || true
git diff --check
git add README.md docs/architecture.md docs/deployment.md control/start.sh
git commit -m "docs: explain remote project MCP"
```

Expected: no real-looking long bearer secret and no whitespace errors.

### Task 5: Frontend build and complete regression suite

**Files:**
- Modify: `frontend/dist/index.html`
- Modify/Create/Delete: content-hashed files under `frontend/dist/assets/` exactly as generated by
  Vite.

**Interfaces:**
- Produces production assets containing PAT UI and direct-open routing.
- Consumes all completed foundation, operations, and UI tasks.

- [ ] **Step 1: Run frontend unit and browser tests**

```bash
cd frontend
npm test
node test/accountTokens.e2e.js
node test/directProjectUrl.e2e.js
node test/projectOpeningUi.e2e.js
node test/pdfWorkspaceUi.e2e.js
node test/comments.e2e.js
```

Expected: all pass.

- [ ] **Step 2: Run all Python tests**

```bash
control/.venv/bin/python -m unittest discover -s tests -p 'test_control_*.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_project_context.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_remote_files.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_pdf_projects.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_regressions.py' -v
```

Expected: all pass.

- [ ] **Step 3: Build production frontend**

```bash
cd frontend && npm run build
```

Expected: Vite succeeds; only the known large-chunk warning is acceptable.

- [ ] **Step 4: Verify generated asset references**

```bash
rg 'assets/index-' frontend/dist/index.html
git diff --check
```

Open the token dialog and a direct PDF/Typst link against `vite preview`, capture screenshots, and
visually inspect desktop and narrow layouts.

- [ ] **Step 5: Commit generated assets**

```bash
git add frontend/dist
git commit -m "build: update remote MCP frontend assets"
```

### Task 6: Controlled deployment and live verification

**Files:**
- No source changes unless live verification exposes a reproducible defect.
- Runtime state: control database, control process, `tcb-workspace:latest`, and existing user
  containers.

**Interfaces:**
- Produces a live `/mcp` endpoint and preserved user projects.
- Consumes all implementation commits and existing deployment procedures.

- [ ] **Step 1: Record and back up pre-deployment state**

Record:

```bash
git rev-parse HEAD
git status --short
docker ps -a --format '{{.Names}} {{.Status}}'
docker inspect tcb-ws-joelyang --format '{{json .Mounts}} {{.State.StartedAt}}'
shasum -a 256 workspaces/joelyang/5fee5cad7c5b/document.pdf \
  workspaces/joelyang/5fee5cad7c5b/transcript.json \
  workspaces/joelyang/5fee5cad7c5b/.vibe-typst.json
```

Copy `control/data/control.db` to a timestamped deployment staging directory. Do not remove the
original. Record hashes for every active user's project metadata and primary document.

- [ ] **Step 2: Build and inspect the workspace image**

```bash
docker build --platform linux/amd64 -t tcb-workspace:latest -f Containerfile .
docker image inspect tcb-workspace:latest --format '{{.Id}} {{.Architecture}} {{.Os}}'
```

Expected: build succeeds and reports `amd64 linux`.

- [ ] **Step 3: Apply the additive database migration offline against a copy**

Set `CONTROL_DATA` to the staging copy and run `init_db()`. Verify:

```sql
SELECT name FROM sqlite_master
WHERE type='table'
  AND name IN ('api_tokens','project_leases','upload_sessions','download_sessions','mcp_audit_log');
```

Expected: all five tables exist and existing users/sessions counts are unchanged.

- [ ] **Step 4: Deploy control and workspace code**

Stop only the control process long enough to replace/restart it with the new environment and
`PUBLIC_BASE_URL`. For each workspace container requiring recreation:

1. verify its exact bind-mount source and destination;
2. stop and rename the old container to a timestamped rollback name;
3. create the replacement with the same workspace mount and
   `127.0.0.1:<port>:8080`;
4. wait for `/api/state`;
5. retain the stopped rollback container until post-deployment verification passes.

Never remove or overwrite a workspace directory. Do not use `docker rm -v`.

- [ ] **Step 5: Verify live browser and MCP behavior**

Verify:

```text
login and existing Projects page
create/revoke PAT
MCP initialize and tools/list with bearer PAT
list/open existing PDF project
read 43-page PDF info and transcript
open an existing Typst project and fetch a preview
browser switch invalidates the old MCP handle
revoked PAT fails on the next request
direct openProject URLs enter the correct workspace
terminal and project-local MCP still work
```

Run the live E2E suites with `VIBE_TYPST_URL` set to the deployed control URL.

- [ ] **Step 6: Prove data and mounts are unchanged**

Re-run all pre-deployment hashes and compare byte-for-byte. Confirm each replacement container's
mount source/destination/RW flag matches its recorded value and only its host port binding changed
to loopback. Confirm PDF page count and transcript content through `/api/state` and MCP.

- [ ] **Step 7: Push and clean recoverable deployment artifacts**

Push the verified `main` commit and confirm remote SHA equals local SHA. Remove only temporary
staging containers after success. Move database/image extraction backups to the user's Trash or
documented backup location; report their paths and recoverability. Leave rollback workspace
containers until the user-authorized retention period expires.

- [ ] **Step 8: Final repository check**

```bash
git diff --check
git status --short
git log -8 --oneline --decorate
```

Expected: `main` is clean except pre-existing user-owned `docs/design/`, remote SHA matches local,
and all implementation/asset commits are present.
