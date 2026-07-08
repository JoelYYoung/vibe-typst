#!/usr/bin/env bash
# Full deploy to O3: rsync source, build workspace image, set up control plane,
# configure Cloudflare tunnel for vibetypst.yjwspace.win, start services.
set -euo pipefail

LOCAL_SRC="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_USER="z5492568"
REMOTE_HOST="o3"
REMOTE_BASE="/mnt/scratch/PAG/yjw/projects/typst-comment-bridge-server"
CF_DIR="/mnt/scratch/PAG/yjw/tools/cloudflared"
TUNNEL_ID="acbf4261-4a5f-4cf3-9509-4e8244eb2b45"
CONTROL_PORT=8090
HOSTNAME="vibetypst.yjwspace.win"

log() { echo "▶ $*"; }

# ── 1. Rsync source to O3 ──────────────────────────────────────────────────────
log "Syncing source to O3…"
rsync -avz --delete \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='node_modules' \
  --exclude='resolver/target' \
  --exclude='backend/.venv' \
  --exclude='control/.venv' \
  --exclude='control/data' \
  --exclude='*.pyc' \
  "$LOCAL_SRC/" \
  "${REMOTE_HOST}:${REMOTE_BASE}/"

# ── 2. Build workspace image on O3 (background — takes ~20 min for Rust build) ─
log "Starting workspace image build on O3 (background)…"
ssh "${REMOTE_HOST}" "bash -lc '
  set -e
  source /mnt/scratch/PAG/yjw/tools/podman/env.sh
  mkdir -p /tmp/z5492568-pm/tmp
  cd ${REMOTE_BASE}
  echo \"[build] Starting podman build…\"
  nohup bash -c \"
    source /mnt/scratch/PAG/yjw/tools/podman/env.sh
    cd ${REMOTE_BASE}
    podman build --ulimit nofile=65536:65536 -t tcb-workspace:latest . 2>&1 | tee /tmp/tcb-build.log
    echo Build complete
  \" > /tmp/tcb-build-outer.log 2>&1 &
  echo \"Build PID: \$!  — tail /tmp/tcb-build.log on O3 to monitor\"
'" &
BUILD_SSH_PID=$!

# ── 3. Set up control-plane virtualenv ────────────────────────────────────────
log "Setting up control plane virtualenv on O3…"
ssh "${REMOTE_HOST}" "bash -lc '
  set -e
  CTRL=${REMOTE_BASE}/control
  VENV=\$CTRL/.venv
  if [ ! -d \"\$VENV\" ]; then
    echo \"Creating venv…\"
    python3 -m venv \"\$VENV\"
    \"\$VENV/bin/pip\" install -q --upgrade pip
  fi
  echo \"Installing/updating packages…\"
  \"\$VENV/bin/pip\" install -q \\
    \"fastapi>=0.115\" \"uvicorn[standard]>=0.34\" \\
    \"httpx>=0.28\" \"aiofiles>=24.1\" \\
    \"python-multipart>=0.0.20\" \"websockets>=12.0\"
  echo \"Virtualenv ready.\"
'"

# ── 4. Enable linger so containers survive logout ─────────────────────────────
log "Enabling systemd linger for ${REMOTE_USER}…"
ssh "${REMOTE_HOST}" "loginctl enable-linger ${REMOTE_USER} 2>/dev/null && echo 'Linger enabled.' || echo 'Linger not available (will need keep-alive).'"

# ── 5. Add vibetypst DNS record via cloudflared ───────────────────────────────
log "Adding Cloudflare DNS record for ${HOSTNAME}…"
ssh "${REMOTE_HOST}" "bash -lc '
  CF=${CF_DIR}
  BIN=\$CF/bin/cloudflared
  # Check if record already exists (idempotent — cloudflared will warn but not fail)
  \"\$BIN\" tunnel route dns --overwrite-dns ${TUNNEL_ID} ${HOSTNAME} 2>&1 || true
'"

# ── 6. Update tunnel config.yml to add vibetypst ingress rule ─────────────────
log "Updating Cloudflare tunnel config for ${HOSTNAME}…"
ssh "${REMOTE_HOST}" "bash -lc '
  CONFIG=${CF_DIR}/etc/config.yml
  HOSTNAME=${HOSTNAME}
  PORT=${CONTROL_PORT}
  # Only add if not already present
  if grep -q \"\$HOSTNAME\" \"\$CONFIG\" 2>/dev/null; then
    echo \"Ingress rule for \$HOSTNAME already in config.\"
  else
    # Insert new rule before the catch-all (last line of ingress)
    python3 -c \"
import re, sys
txt = open(\\\"${CF_DIR}/etc/config.yml\\\").read()
new_rule = \\\"  - hostname: ${HOSTNAME}\\\\n    service: http://localhost:${CONTROL_PORT}\\\\n\\\"
# insert before the final catch-all service line
txt = re.sub(r\\\"(  - service: http_status:404)\\\", new_rule + r\\\"\\\\1\\\", txt)
open(\\\"${CF_DIR}/etc/config.yml\\\", \\\"w\\\").write(txt)
print(\\\"Config updated.\\\")
\\\"
  fi
  echo \"--- current config.yml ---\"
  cat \"\$CONFIG\"
'"

# ── 7. Restart cloudflared with the new config ────────────────────────────────
log "Restarting cloudflared tunnel with updated config…"
ssh "${REMOTE_HOST}" "bash -lc '
  CF=${CF_DIR}
  BIN=\$CF/bin/cloudflared
  PID_FILE=\$CF/tunnel.pid
  LOG_FILE=\$CF/tunnel.log

  # Stop existing tunnel
  if [ -f \"\$PID_FILE\" ] && kill -0 \"\$(cat \"\$PID_FILE\")\" 2>/dev/null; then
    echo \"Stopping existing tunnel (pid \$(cat \$PID_FILE))…\"
    kill \"\$(cat \"\$PID_FILE\")\" && sleep 2
  fi

  # Start with updated config
  : > \"\$LOG_FILE\"
  setsid nohup \"\$BIN\" tunnel --no-autoupdate run --config \"\$CF/etc/config.yml\" \\
    >> \"\$LOG_FILE\" 2>&1 < /dev/null &
  echo \$! > \"\$PID_FILE\"
  echo \"Tunnel restarted, pid \$(cat \$PID_FILE). Logs: \$LOG_FILE\"
'"

# ── 8. Create initial admin user (if DB doesn't exist yet) ───────────────────
log "Creating initial admin user…"
ADMIN_PASS="${ADMIN_PASS:-$(openssl rand -base64 12)}"
ssh "${REMOTE_HOST}" "bash -lc '
  set -e
  CTRL=${REMOTE_BASE}/control
  VENV=\$CTRL/.venv
  DB_PATH=\$CTRL/data/control.db
  if [ ! -f \"\$DB_PATH\" ]; then
    echo \"Creating admin user…\"
    CONTROL_DATA=\$CTRL/data \"\$VENV/bin/python\" \"\$CTRL/main.py\" create-user admin \"${ADMIN_PASS}\" || true
    echo \"Admin password: ${ADMIN_PASS}\"
  else
    echo \"DB already exists — skip admin creation.\"
  fi
'"
echo ""
echo "  ★ Admin password (save this): ${ADMIN_PASS}"
echo ""

# ── 9. Start control plane (if not already running) ──────────────────────────
log "Starting control plane on port ${CONTROL_PORT}…"
ssh "${REMOTE_HOST}" "bash -lc '
  CTRL=${REMOTE_BASE}/control
  PID_FILE=\$CTRL/data/control.pid
  LOG_FILE=\$CTRL/data/control.log

  mkdir -p \$CTRL/data
  if [ -f \"\$PID_FILE\" ] && kill -0 \"\$(cat \"\$PID_FILE\")\" 2>/dev/null; then
    echo \"Control plane already running (pid \$(cat \$PID_FILE)) — restart it? Sending SIGTERM…\"
    kill \"\$(cat \"\$PID_FILE\")\" && sleep 2
  fi

  : > \"\$LOG_FILE\"
  setsid nohup bash \"\$CTRL/start.sh\" >> \"\$LOG_FILE\" 2>&1 < /dev/null &
  echo \$! > \"\$PID_FILE\"
  echo \"Control plane started, pid \$(cat \$PID_FILE). Logs: \$LOG_FILE\"
  sleep 3
  echo \"--- tail of control.log ---\"
  tail -20 \"\$LOG_FILE\"
'"

# ── 10. Wait for build PID (informational) ───────────────────────────────────
wait $BUILD_SSH_PID 2>/dev/null || true
log "Deploy script complete."
log ""
log "  Control plane: http://o3-localhost:${CONTROL_PORT}  (via SSH tunnel)"
log "  Public URL:    https://${HOSTNAME}"
log ""
log "  To monitor image build:  ssh o3 'tail -f /tmp/tcb-build.log'"
log "  To add a user:           ssh o3 'cd ${REMOTE_BASE}/control && bash add-user.sh <user> <pass>'"
