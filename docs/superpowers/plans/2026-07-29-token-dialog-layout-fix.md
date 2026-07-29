# Personal Access Token Dialog Layout Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore a coherent desktop token-creation form while preserving mobile stacking, then hot-update existing containers and rebuild the cold-start image.

**Architecture:** Keep the current React DOM and fix CSS Grid placement at its source. Add a browser geometry regression that checks the title row and the aligned control row, rebuild immutable frontend assets, then deploy only application code outside `/workspace`.

**Tech Stack:** React, Vite, CSS Grid, Node test runner, Puppeteer, Docker/Colima, Cloudflare-fronted FastAPI workspace UI.

## Global Constraints

- Do not modify or add the user's untracked `docs/design/` files.
- Do not modify token APIs, the control database, project files, or container mounts.
- Use the canonical public origin `https://vibetypst.yjwspace.win`.
- Preserve the existing single-column token form at viewport widths of 640 pixels or less.
- Keep all three existing workspace containers stopped after deployment, matching their initial state.

---

### Task 1: Add a Desktop Layout Regression and Fix Grid Placement

**Files:**
- Modify: `frontend/test/accountTokens.e2e.js`
- Modify: `frontend/src/styles.css:693-700`

**Interfaces:**
- Consumes: `.token-create-form`, `.token-section-title`, `input[name="token-name"]`, `select[name="token-preset"]`, `select[name="token-expiry"]`, and the submit button.
- Produces: A desktop form with one full-width title row and one aligned control row.

- [ ] **Step 1: Add the failing desktop browser test**

Add this shared request interceptor, use it from the existing mobile test, and add a desktop test
that opens the token dialog at 900×800 CSS pixels:

```js
async function installEmptyAccountMocks(page) {
  await page.setRequestInterception(true)
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname
    const responses = {
      '/whoami': { username: 'alice', role: 'user' },
      '/api/app/state': { configured: true, mode: 'server' },
      '/api/projects': { projects: [] },
      '/account/tokens': { tokens: [] },
    }
    if (responses[path]) {
      return request.respond({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(responses[path]),
      })
    }
    return request.continue()
  })
}

test('desktop token creation layout keeps its title and controls aligned', async () => {
  const page = await browser.newPage()
  await page.setViewport({ width: 900, height: 800, deviceScaleFactor: 1 })
  await installEmptyAccountMocks(page)
  await page.goto(baseUrl, { waitUntil: 'networkidle0' })
  await page.click('button[title="Account"]')
  await page.click('button[data-action="manage-tokens"]')

const geometry = await page.evaluate(() => {
  const form = document.querySelector('.token-create-form')
  const title = form.querySelector('.token-section-title').getBoundingClientRect()
  const labels = [...form.querySelectorAll('label')].map(node => node.getBoundingClientRect())
  const controls = [
    form.querySelector('input[name="token-name"]'),
    form.querySelector('select[name="token-preset"]'),
    form.querySelector('select[name="token-expiry"]'),
    form.querySelector('button[type="submit"]'),
  ].map(node => node.getBoundingClientRect())
  return {
    pageWidth: document.documentElement.scrollWidth,
    viewportWidth: document.documentElement.clientWidth,
    titleBottom: title.bottom,
    firstLabelTop: Math.min(...labels.map(rect => rect.top)),
    controlTops: controls.map(rect => rect.top),
    controlLefts: controls.map(rect => rect.left),
  }
})

assert.ok(geometry.titleBottom <= geometry.firstLabelTop)
assert.ok(Math.max(...geometry.controlTops) - Math.min(...geometry.controlTops) <= 1)
assert.deepEqual(
  [...geometry.controlLefts].sort((left, right) => left - right),
  geometry.controlLefts,
)
assert.equal(geometry.pageWidth, geometry.viewportWidth)
})
```

- [ ] **Step 2: Run the new browser test against the current production assets**

Serve `frontend/dist` on port 4173:

```bash
backend/.venv/bin/python -m http.server 4173 --bind 0.0.0.0 --directory frontend/dist
```

Run:

```bash
docker run --rm --init --cap-add=SYS_ADMIN \
  -e VIBE_TYPST_URL=http://host.docker.internal:4173 \
  -e PUPPETEER_EXECUTABLE_PATH=/home/pptruser/.cache/puppeteer/chrome-headless-shell/linux-150.0.7871.24/chrome-headless-shell-linux64/chrome-headless-shell \
  -v "$PWD/frontend":/app:ro \
  -v vibe-typst-frontend-node-modules:/app/node_modules:ro \
  -w /app ghcr.io/puppeteer/puppeteer:latest \
  node --test --test-name-pattern='desktop token creation layout' test/accountTokens.e2e.js
```

Expected: FAIL because the title occupies the first grid column and the form controls have different vertical positions.

- [ ] **Step 3: Implement the minimal CSS correction**

Update the desktop rules to:

```css
.token-create-form {
  display: grid;
  grid-template-columns: minmax(170px, 1.5fr) minmax(150px, 1fr) minmax(130px, .8fr) auto;
  gap: 12px;
  align-items: start;
}
.token-create-form > .token-section-title { grid-column: 1 / -1; margin-bottom: 0; }
.token-create-form > button { height: 38px; margin: 21px 0 0; white-space: nowrap; }
```

At the existing mobile breakpoint, reset the button to:

```css
.token-create-form > button { margin-top: 0; }
```

- [ ] **Step 4: Rebuild the frontend assets**

Run from `frontend/`:

```bash
docker run --rm \
  -v "$PWD":/app \
  -v vibe-typst-frontend-node-modules:/app/node_modules \
  -w /app node:22-bookworm npm run build
```

Expected: Vite exits 0 and `dist/index.html` references the newly hashed CSS asset.

- [ ] **Step 5: Re-run the focused desktop and mobile browser tests**

Run the full `frontend/test/accountTokens.e2e.js` suite using the Docker command from Step 2 without `--test-name-pattern`.

Expected: 3 tests pass: token lifecycle, desktop geometry, and mobile one-column layout.

- [ ] **Step 6: Commit the tested layout fix**

```bash
git add frontend/src/styles.css frontend/test/accountTokens.e2e.js frontend/dist
git commit -m "fix: align personal token creation form"
```

### Task 2: Run Regression Verification and Push

**Files:**
- Verify only: `frontend/src/`, `frontend/test/`, `frontend/dist/`

**Interfaces:**
- Consumes: The corrected production assets from Task 1.
- Produces: A tested `main` commit matching the remote branch.

- [ ] **Step 1: Run all frontend unit tests**

```bash
docker run --rm -v "$PWD/frontend":/app -w /app node:20-slim npm test
```

Expected: all frontend unit tests pass.

- [ ] **Step 2: Rebuild and confirm deterministic assets**

Repeat Task 1 Step 4, then run:

```bash
git diff --check
git status --short
```

Expected: no rebuild-only diff; the only unrelated status entry is the user's `docs/design/`.

- [ ] **Step 3: Push `main`**

```bash
git push origin main
git ls-remote origin refs/heads/main
```

Expected: the remote SHA equals local `HEAD`.

### Task 3: Cold Build and Hot-Update Existing Containers

**Files:**
- Deploy: `frontend/dist/`
- Build input: `Containerfile`

**Interfaces:**
- Consumes: The pushed frontend production assets.
- Produces: Updated `tcb-workspace:latest` and byte-identical frontend files in all current workspace containers.

- [ ] **Step 1: Record container state and workspace integrity**

Verify `tcb-ws-admin`, `tcb-ws-kangaroo`, and `tcb-ws-joelyang` are stopped. Record workspace integrity with:

```bash
find workspaces -type f | wc -l
find workspaces -type f -exec stat -f '%z' {} + |
  awk '{sum += $1} END {printf "%.0f\n", sum}'
find workspaces -type f -print0 |
  LC_ALL=C sort -z |
  xargs -0 shasum -a 256 |
  shasum -a 256
```

- [ ] **Step 2: Build the cold-start image**

```bash
docker build --platform linux/amd64 -t tcb-workspace:latest -f Containerfile .
```

Expected: image build exits 0 and the image contains the new CSS asset referenced by `frontend/dist/index.html`.

- [ ] **Step 3: Hot-copy the frontend into current containers**

For each current container:

```bash
docker cp frontend/dist/. "$CONTAINER_NAME:/app/frontend/dist/"
```

Copy `index.html` back and compare it byte-for-byte:

```bash
VERIFY_DIR="$(mktemp -d)"
docker cp "$CONTAINER_NAME:/app/frontend/dist/index.html" \
  "$VERIFY_DIR/$CONTAINER_NAME-index.html"
cmp -s frontend/dist/index.html "$VERIFY_DIR/$CONTAINER_NAME-index.html"
```

- [ ] **Step 4: Start and health-check each container**

Start all three containers and poll `/api/state` on ports 9001, 9004, and 9003. Confirm the new CSS asset exists in each container.

- [ ] **Step 5: Run the layout test against the live joelyang workspace**

Run `frontend/test/accountTokens.e2e.js` with:

```text
VIBE_TYPST_URL=http://host.docker.internal:9003
```

Expected: all three token-dialog browser tests pass against deployed assets.

- [ ] **Step 6: Restore prior container state and verify data**

Stop all three workspace containers. Recompute the workspace file count, byte count, and aggregate SHA-256 manifest.

Expected: all three values match Step 1 exactly.

- [ ] **Step 7: Verify the public site**

Confirm:

```bash
curl -fsS -o /dev/null https://vibetypst.yjwspace.win/
```

accepts the expected login redirect and the deployed asset is retrievable through Cloudflare.
