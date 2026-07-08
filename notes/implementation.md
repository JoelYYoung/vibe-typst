# Implementation Notes — Multi-user Server

## Phase 0 status: ✅ Ready to deploy
The workspace image was already ready before this session:
- `Containerfile` — multi-stage build (Rust resolver, Vite frontend, Debian runtime)
- Backend already serves `frontend/dist/` as static files (single port, no Vite proxy needed)
- `docker-entrypoint.sh` — starts uvicorn, sets up workspace dirs

## Phase 1 status: ✅ Implemented (2026-06-21)

### Control plane (`control/`)

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app — auth, orchestrator, reverse proxy |
| `login.html` | Dark-themed login page (pure HTML, no build step) |
| `start.sh` | O3 startup script — creates venv if absent, sets env |
| `add-user.sh` | Convenience wrapper to call `main.py create-user` |
| `pyproject.toml` | Dependency spec |

### Auth design
- Passwords: PBKDF2-SHA256 (260k iterations, 16-byte random salt) — no external bcrypt dep
- Sessions: random token (32 bytes, URL-safe) stored in SQLite; expires after 30 days
- Cookie: `tcb_session`, httpOnly, SameSite=Lax

### Orchestrator design
- Each user gets a fixed port (9001, 9002, …) assigned at account creation, stored in DB
- Container name: `tcb-ws-<sanitized-username>`
- Data dir: `/mnt/scratch/PAG/yjw/workspaces/<username>/` bind-mounted to `/workspace`
- Start: `podman run -d --restart unless-stopped -p <port>:8080 -v <wsdir>:/workspace:Z tcb-workspace:latest`
- Running check: `podman inspect --format {{.State.Running}} <name>`
- On login: `asyncio.create_task(_ensure_workspace(user))` — non-blocking start, readiness poll

### Reverse proxy design
- HTTP: httpx AsyncClient, streaming response, hop-by-hop headers stripped
- WebSocket `/ws/{path}`: bridged via `websockets.client.connect`, bidirectional c2s/s2c tasks
- WebSocket `/pty`: same bridge pattern
- Auth check: cookie lookup before any proxy; unauthenticated → 302 to /login
- Container not ready: returns 503 loading page (auto-refresh every 3s)

### Cloudflare integration
- Reuses existing tunnel `acbf4261-4a5f-4cf3-9509-4e8244eb2b45` (`yjwspace.win`)
- Adds ingress rule: `vibetypst.yjwspace.win → http://localhost:8090`
- DNS: `vibetypst CNAME acbf4261-…cfargotunnel.com` (added via `cloudflared tunnel route dns`)
- Tunnel runs as a background process on O3 (`setsid nohup … &`)

### Database schema (SQLite at `control/data/control.db`)
```sql
users(id TEXT PK, username TEXT UNIQUE, pw_hash TEXT, port INT UNIQUE, created_at REAL)
sessions(token TEXT PK, user_id TEXT, expires_at REAL)
```

## Phase 2 plan
1. Idle auto-stop: track last-activity timestamp per container; cron job every 5 min checks
   and stops containers idle > 30 min
2. Resource caps: add `--memory=4g --cpus=2` to `_start_workspace()`
3. Systemd user services: `podman generate systemd --new tcb-ws-<user>` + `systemctl --user enable`
4. Image re-seed on reboot: cron `@reboot podman load < /mnt/scratch/.../tcb-workspace.tar`

## Phase 3 plan (collaboration spike)
- Yjs CRDT rooms already work across connections (existing `docstore.py`)
- "Share project" = grant user B access to user A's workspace container
- Hard parts: (a) routing user B → user A's container (not their own), (b) Claude session sharing
- Spike before full build — see workbook_server-deploy-plan.md §Phase 3

## Known issues / gotchas
- O3 has no `subuid/subgid` → all containers run as `z5492568` — single-uid isolation
- Container images stored in `/tmp/z5492568-pm` (local, fast) — lost on host reboot; re-build needed
- macOS-only `/api/open-dialog` (osascript) silently fails in Linux container — file browser still works
- Claude Code installer in Containerfile has `|| true` — if it fails, mount a volume with pre-installed claude
