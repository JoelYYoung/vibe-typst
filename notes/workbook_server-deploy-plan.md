# Workbook — Vibe Typst multi-user server, deploy to O3

Design + plan for turning the single-user Vibe Typst app into a deployable multi-user service on
the UNSW CSE server **O3** (`wp-omega-c03`, user `z5492568`, **no root**). This project
(`typst-comment-bridge-server`) is a COPY of `~/Projects/typst-comment-bridge`; the original stays
untouched.

**Status (2026-06-21):** Phase 0 + Phase 1 implemented. Deploying to O3 + Cloudflare (`vibetypst.yjwspace.win`).

## Goal
A copy of the app turned into a multi-user service: login, per-user **isolated & persistent**
workspaces, project management, file upload/download, Claude Code pre-installed (each user does
their own Claude login, which persists), and later collaborative editing.

## Feasibility verdict for O3 (probed read-only, 2026-06-20)

| Fact on O3 (`wp-omega-c03`) | Implication |
|---|---|
| **Rootless Podman 5.8.2 IS set up** at `/mnt/scratch/PAG/yjw/tools/podman` (static build + `env.sh`). Configured **single-uid + `overlay.ignore_chown_errors=true`** (standard no-subuid workaround). `podman info` → `rootless: true`, `overlay` driver, `netavark`+`pasta` net. | ✅ **Primary runtime.** Standard OCI workflow (`podman build/run -d`, volumes, networks, `--restart`, `generate systemd`). Activated by `source env.sh`. |
| Container **storage on local `/tmp/z5492568-pm`** (graphRoot/runRoot/TMPDIR; ext4, xattr-capable, ~470 GB). `env.sh` keeps podman state out of `$HOME`. | ✅ Fast, correct overlay. ⚠️ `/tmp` is host-local → images/containers **lost on host reboot** (re-pull/rebuild); user *data* lives on scratch and persists. |
| **No `subuid/subgid`** → single-uid mapping (`ignore_chown_errors`). | ⚠️ All containers run as the one UID `z5492568`. Container/network/fs isolation yes; **not a hard OS-user boundary** → residual risk for *untrusted* users. |
| **`Linger=no`**, systemd --user running | A 24/7 control plane + long-running containers need `loginctl enable-linger` (try for self) or a keep-alive; else they stop at logout. |
| **Data dir: `/mnt/scratch/PAG/yjw/<user>/`** — owned by `z5492568`, writable, NFS (86 TB free). NOT `$HOME`, NOT `PCG`. | ✅ Per-user **podman volumes / bind mounts** for persistent project data, `~/.claude`, `~/.tcb`. |
| **Apptainer 1.3.3** also present (`fakeroot`, `mksquashfs`) | Fallback runtime / building SIFs if needed. |
| **Claude Code already installed** (`~/.local/bin/claude`) + `.claude` already under scratch | ✅ Bake into the image; mount per-user `.claude` for persistent login. |
| Behind `ProxyJump cse` | Reachable inside UNSW/CSE net or via SSH tunnel; public ingress needs extra work. |

**Honest constraint:** untrusted users + no admin help. Even with working rootless Podman, the
no-subuid single-uid mapping means every container runs as the *same* Unix account (`z5492568`).
Real container/network/fs isolation, but **not a hard OS-user security boundary** — for genuinely
untrusted users it is *best-effort*, not bulletproof. The only true fix is a subuid range or root.

**Recommendation (drives the design): use the rootless Podman on O3 now, keep it portable.** The
workspace is a standard OCI image; the orchestrator talks to Podman via its env (`source
tools/podman/env.sh`). The same image + orchestrator runs unchanged on a root-capable host later
(cloud VM / O3-with-subuid) where per-UID isolation becomes a hard boundary for untrusted
production. So we start on O3 today without a rewrite; the highest-leverage future unblock stays
"get a subuid range."

## Architecture

Two tiers; the existing single-user app is reused **as-is** as the per-user workspace (we do NOT
rewrite the global state — each user gets their own container):

```
                 Browser
                    │  (one public port, cookie session)
            ┌───────▼────────┐
            │  CONTROL PLANE │  NEW. FastAPI + small React.
            │  - login/home  │  - auth (users DB, sessions)
            │  - projects    │  - project CRUD (dirs on the user volume)
            │  - file up/down│  - orchestrator: start/stop/attach a workspace
            │  - reverse-proxy  container per (user[,project]); route /api,/ws,/pty
            └───────┬────────┘
   podman run -d (rootless, via tools/podman/env.sh) — one container per user, --restart,
   volume bind to /mnt/scratch/PAG/yjw/<user>/ ; pluggable backend (podman | docker | apptainer)
            ┌───────▼─────────────────────┐   ┌──────────────────────────┐
            │ WORKSPACE CONTAINER (user A) │   │ WORKSPACE CONTAINER (B)  │  …
            │ = the current Vibe Typst app │   │  one per active user      │
            │   backend+resolver+frontend  │   │                          │
            │   +typst CLI +Claude Code     │   └──────────────────────────┘
            │ bind: /mnt/scratch/PAG/yjw/A/ →  ~/projects, ~/.tcb, ~/.claude, ~/.cache/typst
            └──────────────────────────────┘
```

- **Persistence:** each user's data lives under **`/mnt/scratch/PAG/yjw/<webapp-user>/`** (NOT the
  home dir), bind-mounted into the container (projects, `~/.tcb`, `~/.claude`, `~/.cache/typst`).
  Container is disposable; data + Claude login survive logout/restart. Satisfies "workspace
  unchanged after re-login" and "no Claude re-login while the volume exists".
- **Claude Code:** baked into the image; the user runs `claude` in the in-app terminal and logs in
  themselves; we never see/manage their credentials.

## What must change vs the current app (kept minimal)

1. **Copy** `typst-comment-bridge` → `typst-comment-bridge-server` (done; original untouched).
2. **Serve the built frontend from the backend** (production has no Vite dev server): backend
   serves `frontend/dist/` as static and handles `/api`, `/ws`, `/pty` same-origin on **one port**.
   (Today these are split via the Vite proxy in `frontend/vite.config.js`.)
3. **Containerize the workspace**: a `Containerfile` (Debian 12 base) with Python+uv, the prebuilt
   `tcb-resolver` binary, node-built `dist/`, `typst` CLI, Claude Code, fonts, and a warm typst
   package cache (`@preview/touying`, `@preview/cetz`). Build **with the rootless Podman on O3**
   (`source tools/podman/env.sh; podman build`) — or build locally and load — then `podman run -d`.
4. **New control-plane service** (`control/`): auth, projects, files, orchestrator/reverse-proxy.
5. **Orchestrator = thin wrapper over `podman`** (run/stop/ps/volume), sourcing `env.sh`; kept
   behind a small interface so `docker`/`apptainer` can be swapped for another host.

The core single-user backend (`runtime.py`, `docstore.py`, `resolver.py`, `/pty` terminal, MCP
wiring) is **reused unchanged** inside each container — that's the whole point of per-container
isolation. (See `../typst-comment-bridge/CLAUDE.md` for the single-user internals + gotchas.)

## Phased implementation

**Phase 0 — [✅ IMPLEMENTED 2026-06-21] Prove ONE workspace container on O3 with the rootless Podman.**
- Make the backend serve `dist/` + single-port (change #2).
- Write the `Containerfile`; on O3 `source /mnt/scratch/PAG/yjw/tools/podman/env.sh && podman build`.
- `podman run -d -p <port>:<port> -v /mnt/scratch/PAG/yjw/_probe:/data ...`; reach it via SSH
  tunnel; verify the FULL app in a browser: edit→preview, click-to-source, terminal, `claude` login
  + an MCP edit, PDF export, notes. **Verify O3-specific risks**: image build under
  `ignore_chown_errors`, typst package download from inside the container (network via pasta),
  host-port publish reachable over the tunnel, data persists in the mounted scratch volume after
  `podman rm` + re-run, and `loginctl enable-linger z5492568` (for 24/7). This phase decides if the
  rest is worth building.

**Phase 1 — [✅ IMPLEMENTED 2026-06-21] Control plane: auth + projects + files + orchestration.**
- Login home page + sessions + users DB (SQLite).
- Project dashboard: list/create projects (each = a dir on the user volume); "open" → ensure the
  user's workspace container is running and bound to that project → redirect to the canvas (existing UI).
- File upload (multipart → project dir) and download (stream a file). Extend the existing file browser.
- Orchestrator + reverse proxy: map session→container, start/stop/attach, route `/api,/ws,/pty`.

**Phase 2 — Persistence & lifecycle hardening.**
- Per-user scratch volumes; `--restart` + reuse-or-recreate container on login; idle auto-stop;
  resource caps (`podman run --memory/--cpus`); image storage on local `/tmp` re-seeded after host
  reboot; control-plane + container uptime via `podman generate systemd --new` + `systemctl --user
  enable` + `enable-linger` (or a keep-alive if linger is denied).

**Phase 3 — Collaboration (research spike).**
- File-level multi-edit is largely **already there** (Yjs CRDT rooms) — "share project" grants
  another user access to the same workspace's rooms. Investigate the two hard parts: (a) letting a
  collaborator into the owner's workspace container safely, and (b) **Agent sharing** — one shared
  Claude session vs per-user agent panes, the agent's cwd/auth, conflict handling. Deliver a spike +
  recommendation, not a full build.

## Open items to settle during Phase 0 (don't block the plan)
- Public ingress on O3 (SSH reverse tunnel / Cloudflare Tunnel / uni web infra) vs uni-network-only.
- `enable-linger` permission for self; if denied, a keep-alive strategy.
- Local `/tmp/z5492568-pm` image-storage durability across host reboots (re-seed images on boot).
- Final residual-isolation decision for untrusted users (accept rootless-Podman single-uid
  best-effort on O3, or move untrusted-production to a root-capable host using the same image/orchestrator).

## Verification
- **Phase 0:** scripted browser check (puppeteer over the SSH tunnel) exercising
  edit/preview/terminal/claude/PDF inside the Podman container on O3; confirm data persists after
  `podman rm` + re-`run` with the same scratch volume.
- **Phase 1:** two browser sessions as two users → each gets an isolated workspace; project
  create/open; file upload then download round-trip; confirm user A cannot reach user B's files.
- **Phase 2:** logout/login keeps a project's edits and the Claude login; idle container stops and
  re-attaches cleanly.
- **Phase 3:** two browsers editing one shared project converge via CRDT; document the agent-sharing outcome.
