# Deployment Guide

Two supported modes: **local** (single user, Mac) and **server** (multi-user, Linux + Podman + Cloudflare).

---

## Local Deployment (Mac)

### Prerequisites

| Tool | How to install |
|------|---------------|
| Python 3.11+ | `brew install python` |
| [uv](https://docs.astral.sh/uv/) | `brew install uv` |
| Node.js 20+ | `brew install node` |
| [Typst](https://typst.app) | `brew install typst` |
| Rust / cargo | `curl https://sh.rustup.rs -sSf \| sh` |

### 1. Build the resolver

The resolver is a small Rust binary that watches `.typ` files and renders SVG pages incrementally.

```bash
cd resolver
cargo build --release
# binary → resolver/target/release/tcb-resolver
```

### 2. Install Python dependencies

```bash
cd backend
uv sync
```

### 3. Install frontend dependencies

```bash
cd frontend
npm install
```

### 4. Run (development)

Open three terminals:

```bash
# Terminal 1 — backend (port 8787 by default)
cd backend
uv run uvicorn app:app --port 8787 --reload

# Terminal 2 — frontend dev server (port 5180)
cd frontend
npm run dev

# Terminal 3 — (optional) build the frontend for production
cd frontend
npm run build      # output goes to frontend/dist/
```

Open http://localhost:5180.

### 5. Run as a LaunchAgent (background, auto-start on login)

Build the frontend first (`npm run build`), then create a plist. The backend serves the built
frontend from `frontend/dist/` when `APP_MODE` is not set or is `local`:

```xml
<!-- ~/Library/LaunchAgents/com.vibe-typst.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>       <string>com.vibe-typst</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/vibe-typst/backend/.venv/bin/uvicorn</string>
    <string>app:app</string>
    <string>--port</string> <string>8787</string>
  </array>
  <key>WorkingDirectory</key> <string>/path/to/vibe-typst/backend</string>
  <key>RunAtLoad</key>        <true/>
  <key>KeepAlive</key>        <true/>
  <key>StandardOutPath</key>  <string>/tmp/vibe-typst.log</string>
  <key>StandardErrorPath</key><string>/tmp/vibe-typst.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.vibe-typst.plist
```

Open http://localhost:8787.

### Local data locations

| What | Where |
|------|-------|
| Projects | `~/Documents/vibe-typst/` (default) or `TYPST_WORKSPACE` env |
| Comment database | `<project>/.slide-comments.db` |
| Resolver binary | `resolver/target/release/tcb-resolver` |

---

## Server Deployment (Linux + Podman + Cloudflare)

This mode runs each user's workspace in an isolated Podman container managed by a lightweight
**control plane** (`control/main.py`). A Cloudflare tunnel exposes the control plane to the
internet without opening firewall ports.

### Architecture

```
internet
  └── Cloudflare tunnel → control plane (port 8090)
                              ├── /login   static auth page
                              ├── /api/*   container lifecycle, user mgmt
                              └── /{path}  reverse-proxy → user's container (port 9001+)
                                               └── Vibe Typst app (FastAPI + resolver)
```

### Prerequisites on the server

- Linux with **rootless Podman** (≥ 4.x)
- Python 3.11+, `uv`
- [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
- A Cloudflare tunnel already created (note its tunnel ID)
- Your own domain with Cloudflare DNS

### 1. Rsync source to the server

```bash
rsync -avz --delete \
  --exclude='.venv' --exclude='__pycache__' \
  --exclude='node_modules' --exclude='resolver/target' \
  --exclude='control/data' --exclude='*.pyc' \
  ./ user@server:/path/to/vibe-typst/
```

> `--exclude='.venv.tar.gz'` and `--exclude='node-claude.tar.gz'` if those are built on
> the server — `rsync --delete` would otherwise wipe them.

### 2. Build the workspace image

```bash
# On the server — stop any running containers first (PID pressure)
podman build -t tcb-workspace:latest /path/to/vibe-typst/
```

The `Containerfile` bundles the backend, pre-built frontend, and resolver binary.
Build takes ~20 min on first run (Rust compile). Subsequent builds are incremental.

> **PID pressure:** if the server has a low `pids.max` (e.g. 1200), stop workspace
> containers and the control plane before building — each running container consumes ~20–30
> extra PIDs and the build step that executes inside a temp container will fail with
> `resource temporarily unavailable`.

### 3. Set up the control plane

```bash
cd /path/to/vibe-typst/control
bash start.sh          # creates .venv, generates SESSION_SECRET, starts on port 8090
```

Environment variables read by `start.sh` / `main.py`:

| Variable | Default | Purpose |
|----------|---------|---------|
| `PORT` | `8090` | Control plane listen port |
| `CONTROL_DATA` | `control/data/` | SQLite DB, session secret, logs |
| `WORKSPACE_BASE` | `/workspaces` | Where per-user workspace dirs live |
| `PODMAN_ENV` | — | Path to a shell script that sets Podman env vars (rootless) |
| `TCB_IMAGE` | `tcb-workspace:latest` | Workspace image name |
| `BASE_PORT` | `9001` | First port for workspace containers |
| `SESSION_SECRET` | auto-generated | Cookie signing secret (persisted to `data/session.secret`) |
| `IDLE_STOP_SECONDS` | `1800` | Stop a user's workspace and clear sessions after this many idle seconds; set `0` to disable |
| `IDLE_SWEEP_SECONDS` | `60` | How often the control plane scans for idle workspaces |
| `ANTHROPIC_API_KEY` | — | Passed into workspace containers for Claude |

### 4. Create the first admin user

```bash
cd /path/to/vibe-typst/control
bash add-user.sh admin <your-password>
# or: python main.py create-user admin <your-password> --role admin
```

After that, use the **Admin panel** in the web UI to invite additional users. There is no
public signup.

### Idle shutdown

By default, the control plane treats a user as active when it sees authenticated HTTP or
WebSocket traffic. If a workspace is idle for 30 minutes (`IDLE_STOP_SECONDS=1800`), the
control plane:

1. stops that user's container with `podman stop`;
2. clears that user's sessions so the next browser access returns to `/login`;
3. leaves the user's workspace directory and stopped container intact.

The next successful login starts the existing stopped container again when possible, preserving
its writable layer. To keep workspaces running indefinitely, set `IDLE_STOP_SECONDS=0` before
starting `control/start.sh`.

### 5. Configure Cloudflare tunnel

Add an ingress rule to your tunnel's `config.yml`:

```yaml
ingress:
  - hostname: your-domain.example.com
    service: http://localhost:8090
  - service: http_status:404
```

```bash
cloudflared tunnel route dns <tunnel-id> your-domain.example.com
# then restart cloudflared
```

### 6. Keep services alive

The control plane has no systemd unit by default. Keep it alive with `setsid nohup`:

```bash
setsid nohup bash /path/to/vibe-typst/control/start.sh \
  >> /path/to/vibe-typst/control/data/control.log 2>&1 &
```

Or set up a systemd user service with `loginctl enable-linger <user>`.

### Hot-deploying code changes

When you update source files without rebuilding the image:

```bash
# 1. Copy new backend to the running container
podman cp ./backend/. tcb-ws-<user>:/app/backend/

# 2. Copy new frontend bundle
podman exec tcb-ws-<user> bash -c 'rm -rf /app/frontend/dist && mkdir -p /app/frontend/dist'
podman cp ./frontend/dist/. tcb-ws-<user>:/app/frontend/dist/

# 3. Restart the container (preserves writable layer)
podman restart tcb-ws-<user>

# 4. Bake into the image so new containers get the update
podman commit tcb-ws-<user> tcb-workspace:latest
```

> **Never commit a container that has an active Claude login** (`/root/.claude`).
> Strip it first: `podman exec tcb-ws-<user> rm -rf /root/.claude /root/.claude.json`

### Restarting the control plane safely

To restart (pick up a `main.py` edit):

```bash
# Kill by PORT, not by process name — killing by pattern in an ssh command
# can match the ssh process itself and abort mid-restart.
pid=$(ss -tlnp | grep ":8090 " | grep -o "pid=[0-9]*" | cut -d= -f2)
kill "$pid"
sleep 1
setsid nohup bash /path/to/vibe-typst/control/start.sh \
  >> /path/to/vibe-typst/control/data/control.log 2>&1 &
```

### Server data locations

| What | Where |
|------|-------|
| User DB + sessions | `control/data/control.db` |
| Session signing key | `control/data/session.secret` |
| Per-user workspaces | `$WORKSPACE_BASE/<username>/` |
| Control plane log | `control/data/control.log` |
| Container image | `tcb-workspace:latest` (Podman local store) |
