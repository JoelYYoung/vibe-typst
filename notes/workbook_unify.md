# Workbook — Unify to WebTypst (vibe-typst)

Merging `typst-comment-bridge` (local single-user) and `typst-comment-bridge-server` (multi-user
server) into one codebase — **WebTypst** — that supports two runtime modes.

## Decision (2026-06-21)

- **Canonical repo**: `typst-comment-bridge-server` → rename to `vibe-typst`
- **Product name**: WebTypst
- **Two modes**: `APP_MODE=local` (no auth, user-configurable projects root) and `APP_MODE=server`
  (fixed `/workspace`, auth handled by the control plane)
- **Sync audit result**: all 11 improvements from `workbook_update.md` are PRESENT except
  CommentCard editable body (front-end only; backend PATCH already there) — fix in this pass.

---

## Architecture

```
APP_MODE=local                         APP_MODE=server
  Browser                                Browser
    → backend (port 8080)                  → control plane (port 8090, auth)
        → Projects page                        → workspace container per user
        → Onboarding (first run)               → backend (port 8080, no auth needed)
        → Editor (main UI)                         → Projects page
                                                   → Editor
```

### Two-mode differences (backend)
| Feature | local | server |
|---------|-------|--------|
| Auth | None | Handled by control plane proxy |
| Projects root | `~/.vibe-typst/config.json → projects_root` | env `PROJECTS_ROOT` or `/workspace` |
| Config UI | Onboarding + settings | Hidden |
| Directory migration | UI-assisted | N/A |

### Project directory layout
```
projects_root/
└── my-project/
    ├── .vibe-typst.json    # project metadata (name, created, main_file)
    ├── main.typ            # auto-generated starter slide deck
    └── ...                 # user files
```

### Config storage (local mode)
`~/.vibe-typst/config.json`:
```json
{ "projects_root": "/Users/joel/typst-projects" }
```

---

## API additions

### App state
- `GET /api/app/state` → `{mode, configured, active_project: {id, name, path, main_file} | null}`
- `PUT /api/app/config` → `{projects_root}` (local mode only)

### Projects CRUD
- `GET /api/projects` → list all projects
- `POST /api/projects` → create (body: `{name}`)
- `PATCH /api/projects/{name}` → rename (body: `{name}`)
- `DELETE /api/projects/{name}` → delete
- `POST /api/projects/{name}/copy` → copy (body: `{new_name}`)
- `POST /api/projects/{name}/open` → open project (sets active project + opens main_file)
- `POST /api/projects/close` → back to projects list

### File management within project
- `GET /api/project/files` → list files in active project
- `POST /api/project/files/upload` → upload file (multipart)
- `GET /api/project/files/download` → download file (query: `?path=...`)
- `DELETE /api/project/files` → delete file (body: `{path}`)
- `POST /api/project/files/create` → create new .typ file (body: `{name}`)

---

## Frontend routing

`main.jsx` dispatches based on `GET /api/app/state`:
```
?project  → <Projection />               (unchanged)
configured=false, mode=local → <OnboardingPage />
active_project=null → <ProjectsPage />
active_project != null → <App />         (editor, unchanged except)
```

### App.jsx changes
- Remove "📂 Open" button + FileBrowser modal
- Add "← Projects" back button in the header
- Add `<FileManager />` panel (replaces FileBrowser, shows files in current project)

---

## Implementation progress

- [x] `notes/workbook_unify.md` — this file
- [x] Fix CommentCard editable body (confirmed already present in server branch)
- [x] `backend/app_config.py` — mode detection + config r/w
- [x] `backend/projects.py` — project CRUD + auto-generated files + backup filter
- [x] `backend/app.py` — add `/api/app/state`, `/api/projects/*`, `/api/project/files/*`
- [x] `frontend/src/OnboardingPage.jsx` — first-run setup (local mode)
- [x] `frontend/src/ProjectsPage.jsx` — project list with CRUD
- [x] `frontend/src/FileManager.jsx` — file management within project
- [x] `frontend/src/main.jsx` — routing dispatch
- [x] `frontend/src/App.jsx` — remove open button, add back button + FileManager
- [x] Rename: `vibe-typst`, `WebTypst` throughout (index.html, sample/welcome.typ)
- [x] Containerfile updated — tar-based .venv approach (avoids pids.max=1200 limits)
- [x] O3 rebuild + end-to-end test ✓ (vibetypst.yjwspace.win — projects API + editor working)

---

## Rename checklist
- [x] `frontend/index.html` title → "WebTypst"
- [x] `backend/sample/welcome.typ` → "WebTypst — Server Edition"
- [ ] O3 project dir rename: `/mnt/scratch/PAG/yjw/projects/typst-comment-bridge-server` → `vibe-typst`
- [ ] `scripts/deploy-o3.sh` remote path update
- [ ] Memory files updated

## Image build notes (O3 pids.max=1200 workaround)
Build artifacts pre-compiled on O3, COPYd into image to avoid thread/process limits:
- **Resolver**: `RUSTFLAGS='-C target-cpu=x86-64' cargo +1.95.0 build --release --jobs 32`
- **Frontend**: built on Mac, rsynced to O3
- **Python venv**: `uv sync` on O3 → `tar czf .venv.tar.gz .venv` → root of repo → COPYd + extracted with `--no-same-owner`
- Do NOT copy `.venv` dir directly (1000+ files → Podman goroutine pthread_create failures)
- `uv sync` inside container also fails (rayon thread pool creation fails under tight pids limit)
