#!/usr/bin/env bash
# Start the Vibe Typst control plane.
# Reads SESSION_SECRET from env or generates one (persisted to CONTROL_DATA/session.secret).
set -e
CTRL_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$CTRL_DIR/.venv"
UV="${UV_BIN:-uv}"

if [ ! -d "$VENV" ]; then
  echo "[start] Creating virtualenv…"
  "$UV" venv "$VENV" --python python3.11
  "$UV" pip install --python "$VENV/bin/python" \
    "fastapi>=0.115" "uvicorn[standard]>=0.34" \
    "httpx>=0.28" "aiofiles>=24.1" \
    "python-multipart>=0.0.20" "websockets>=12.0"
fi

export PORT="${PORT:-8090}"
export CONTROL_DATA="${CONTROL_DATA:-$CTRL_DIR/data}"
export WORKSPACE_BASE="${WORKSPACE_BASE:-/workspaces}"
export PODMAN_ENV="${PODMAN_ENV:-}"
export TCB_IMAGE="${TCB_IMAGE:-tcb-workspace:latest}"
export BASE_PORT="${BASE_PORT:-9001}"

if [ -z "$SESSION_SECRET" ]; then
  SECRET_FILE="$CONTROL_DATA/session.secret"
  mkdir -p "$CONTROL_DATA"
  if [ ! -f "$SECRET_FILE" ]; then
    python3 -c "import secrets; print(secrets.token_hex(32))" > "$SECRET_FILE"
    chmod 600 "$SECRET_FILE"
    echo "[start] Generated new SESSION_SECRET → $SECRET_FILE"
  fi
  export SESSION_SECRET="$(cat "$SECRET_FILE")"
fi

echo "[start] Control plane starting on port $PORT …"
cd "$CTRL_DIR"
exec "$VENV/bin/uvicorn" main:app --host 0.0.0.0 --port "$PORT"
