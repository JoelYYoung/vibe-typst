# Deploy Log — Vibe Typst Multi-user Server

## Target
- Server: O3 (`wp-omega-c03`, user `z5492568`)
- Public URL: `https://vibetypst.yjwspace.win` via Cloudflare Tunnel
- Cloudflare tunnel ID: `acbf4261-4a5f-4cf3-9509-4e8244eb2b45` (existing, shared with llm.yjwspace.win)
- Control plane port: 8090
- Workspace containers: ports 9001+ (one per user)

---

## 2026-06-21 — Phase 0 + Phase 1 initial deploy

### What was built
- **Control plane** (`control/`) — FastAPI app that handles:
  - Cookie auth (PBKDF2-SHA256 passwords, SQLite sessions, httpOnly cookie)
  - Workspace container lifecycle (podman run/stop per user)
  - HTTP + WebSocket reverse proxy to each user's workspace container
  - Login page at `/login`
- **Workspace image** — existing `Containerfile` (Debian 12, Python+uv, typst CLI, Claude Code,
  pre-built frontend, Rust resolver). Serves the full Vibe Typst app on one port.
- **Cloudflare** — added `vibetypst.yjwspace.win` ingress rule to existing tunnel config;
  DNS CNAME added via `cloudflared tunnel route dns`.

### Deploy steps
Run from the project root:
```bash
bash scripts/deploy-o3.sh
```
This:
1. Rsyncs source to O3 at `/mnt/scratch/PAG/yjw/projects/typst-comment-bridge-server/`
2. Starts a background `podman build -t tcb-workspace:latest .` on O3
3. Creates `.venv` for the control plane and installs Python deps
4. Runs `loginctl enable-linger z5492568` (for 24/7 uptime)
5. Adds DNS record and updates `cloudflared/etc/config.yml`
6. Restarts cloudflared tunnel
7. Creates initial `admin` user
8. Starts the control plane (`control/start.sh`)

### Monitor build on O3
```bash
ssh o3 'tail -f /tmp/tcb-build.log'
```

### Add users
```bash
ssh o3 'cd /mnt/scratch/PAG/yjw/projects/typst-comment-bridge-server/control && bash add-user.sh <username> <password>'
```

### Restart control plane
```bash
ssh o3 'bash -c "kill \$(cat /mnt/scratch/PAG/yjw/projects/typst-comment-bridge-server/control/data/control.pid) && sleep 1 && bash /mnt/scratch/PAG/yjw/projects/typst-comment-bridge-server/control/start.sh >> /mnt/scratch/PAG/yjw/projects/typst-comment-bridge-server/control/data/control.log 2>&1 &"'
```

---

## Architecture on O3

```
Browser → vibetypst.yjwspace.win (Cloudflare CDN)
        → cloudflared tunnel (acbf4261…)
        → localhost:8090  (control plane, tcb-control)
        → localhost:9001  (workspace container for user A)
        → localhost:9002  (workspace container for user B)
        …
```

Each workspace container:
- Name: `tcb-ws-<username>`
- Image: `tcb-workspace:latest`
- Volume: `/mnt/scratch/PAG/yjw/workspaces/<username>` → `/workspace` inside container
- Port: `9001+` (unique per user, stored in SQLite)
- Restart policy: `unless-stopped`

---

## Status (2026-06-21)
- [x] Cloudflare DNS CNAME `vibetypst.yjwspace.win` → tunnel acbf4261 — DONE
- [x] Tunnel config updated (vibetypst.yjwspace.win → localhost:8090) — DONE
- [x] Tunnel restarted — DONE (4 connections: mel01×2, syd06, syd01)
- [x] Control plane venv installed (Python 3.11, fastapi/uvicorn/httpx/websockets) — DONE
- [x] Admin user created — DONE (username: admin, port: 9001)
- [x] Control plane running at 8090 — DONE
- [x] https://vibetypst.yjwspace.win responds (HTTP 200) — CONFIRMED
- [x] Workspace image build started (background, ~20 min for Rust + npm) — IN PROGRESS
- [ ] Image build completes — PENDING (monitor: `ssh o3 'tail -f /tmp/tcb-build.log'`)
- [ ] Test full login + workspace at https://vibetypst.yjwspace.win

## Persistence / keep-alive (linger denied, cron needs approval)
`loginctl enable-linger` was denied (sysadmin required).
The control plane and tunnel use `setsid nohup ... < /dev/null &` which survives
logout on O3 (confirmed: existing tunnel survived sessions).

To add `@reboot` cron for auto-restart after host reboot, run manually on O3:
```bash
(crontab -l 2>/dev/null; echo '# Vibe Typst keep-alive') | crontab -
(crontab -l 2>/dev/null | grep -v 'tcb-control\|tunnel-run'; \
 echo '@reboot setsid nohup bash /mnt/scratch/PAG/yjw/projects/typst-comment-bridge-server/control/start.sh >> /mnt/scratch/PAG/yjw/projects/typst-comment-bridge-server/control/data/control.log 2>&1 < /dev/null'; \
 echo '@reboot sleep 10 && bash /mnt/scratch/PAG/yjw/tools/cloudflared/tunnel-run.sh' \
) | crontab -
```

## Credentials (save securely)
- Admin login: username `admin`, password `VibeTypst2026!`
- Login URL: https://vibetypst.yjwspace.win/login

## Open items
- [x] Image build completes — DONE (2026-06-21, rebuilt with pre-built .venv strategy)
- [x] Workspace container runs — DONE (tcb-ws-admin, port 9001, HTTP 200)
- [x] Frontend serving fixed (StaticFiles mount added to app.py) — DONE
- [x] Login page responds — DONE (https://vibetypst.yjwspace.win/login → HTTP 200)
- [x] Full end-to-end login + workspace load — CONFIRMED WORKING (2026-06-21)
  - POST /login with admin credentials → 303 redirect + tcb_session cookie
  - GET / with cookie → 200, full Vite SPA HTML returned

## Pre-compiled artifact build strategy (solves crun pids.max=1200 limit)

O3 has cgroup `pids.max=1200` with ~1073 background processes, leaving only ~127 free pids.
Each `podman build` RUN step forks crun. uv's tokio on 96 cores spawns up to 96 threads.
Both cause EAGAIN ("resource temporarily unavailable") failures.

**Working approach**: pre-build ALL artifacts outside Docker, COPY everything in.
- Python `.venv`: `cd backend && /mnt/scratch/PAG/yjw/tools/uv/uv sync` (same Python 3.11.2)
- Resolver: native cargo build (RUSTFLAGS workaround — see below)
- Frontend: `npm run build` on local Mac, rsync to O3
- Entrypoint: `exec /app/backend/.venv/bin/python -m uvicorn app:app` (no uv needed in image)

This reduces Containerfile RUN steps from 6 → 3 (apt + typst + chmod/verify), keeping
crun forks well under the pids limit.

## Rust native build strategy (Docker multi-stage builds fail on O3)
The Docker multi-stage approach fails on O3 because:
1. Rootless Podman nested container builds hit `crun: resource temporarily unavailable`
2. AMD EPYC 9254 + Rust 1.95/1.96 triggers LLVM ICEs at opt-level≥1 (svgtypes, either, codex, etc.)

**Working approach**: Build resolver NATIVELY on O3 outside Docker, then COPY into image.
- Compiler: `cargo +1.95.0` (typst 0.15 requires ≥1.92; 1.96 also has ICEs)
- Profile: `[profile.release] opt-level=0` (skips LLVM optimizer, avoids ICE entirely)
- Binary is unoptimized but that's acceptable for a coord-resolver CLI

### Build history
| Attempt | Result |
|---------|--------|
| Docker multi-stage, rust:1-bookworm (1.96) | FAIL: ICE in unsafe-libyaml, zlib-rs |
| Docker multi-stage, rust:1.88 | FAIL: typst 0.15 needs ≥1.92 |
| Docker multi-stage, rust:1.95 | FAIL: crun resource unavailable + ICE in codex/toml_write |
| Native, 1.95 + opt-level=2 | FAIL: ICE in svgtypes, either |
| Native, 1.95 + opt-level=0 | FAIL: ICE in `thiserror` (coordinator panic write.rs:1929) |
| Native, 1.95 + opt-level=0 + `-C target-cpu=x86-64` | SUCCESS (25s, 114MB binary) |

**Root cause**: AMD EPYC 9254 (Zen 4) exposes AVX-512 to LLVM. Rust 1.95's LLVM version has a bug
generating AVX-512 code for certain crates. Fix: `RUSTFLAGS='-C target-cpu=x86-64'` forces
generic x86-64 baseline, disabling AVX-512 and all advanced SIMD.

**Correct native build command** (must be run on O3 each time Containerfile changes):
```bash
export CARGO_HOME=/mnt/scratch/PAG/yjw/tools/cargo
export RUSTUP_HOME=/mnt/scratch/PAG/yjw/tools/rustup
export PATH="$CARGO_HOME/bin:$PATH"
cd /mnt/scratch/PAG/yjw/projects/typst-comment-bridge-server/resolver
RUSTFLAGS='-C target-cpu=x86-64' cargo +1.95.0 build --release --jobs 32
```
- [ ] Test workspace auto-start on first login
- [ ] Test that Claude Code can be used inside workspace terminal
- [ ] Add @reboot cron for keep-alive (needs manual approval — see above)
- [ ] Phase 2: idle auto-stop + resource caps + systemd --user services

## Phase 2 plan (next)
- `podman generate systemd --new tcb-ws-<user>` for each workspace
- `systemctl --user enable` each container service
- Control plane: idle auto-stop (no activity > 30 min → `podman stop`)
- Resource caps: `--memory=4g --cpus=2` per workspace container
- Image re-seed on host reboot (cron on login or keep-alive script)
