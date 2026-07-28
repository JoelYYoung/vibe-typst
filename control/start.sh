#!/usr/bin/env bash
# Start the Vibe Typst control plane.
# Reads SESSION_SECRET from env or generates one (persisted to CONTROL_DATA/session.secret).
set -e
CTRL_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$CTRL_DIR/.venv"
UV="${UV_BIN:-uv}"

if [ -d "$VENV" ] && ! "$VENV/bin/python" -c \
  'import sys; assert sys.version_info >= (3, 11)' >/dev/null 2>&1; then
  VENV_BACKUP="$CTRL_DIR/.venv.pre-python311.$(date +%Y%m%d%H%M%S).$$"
  mv "$VENV" "$VENV_BACKUP"
  echo "[start] Preserved incompatible virtualenv at $VENV_BACKUP"
fi

if [ ! -d "$VENV" ]; then
  echo "[start] Creating virtualenv…"
  if command -v "$UV" >/dev/null 2>&1; then
    "$UV" venv "$VENV" --python python3.11
    "$UV" pip install --python "$VENV/bin/python" \
      "fastapi>=0.115" "uvicorn[standard]>=0.34" \
      "httpx>=0.28" "aiofiles>=24.1" \
      "python-multipart>=0.0.20" "websockets>=12.0" \
      "mcp>=1.28,<2" "packaging>=24"
  else
    PYTHON_BIN="python3.11"
    if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
      if python3 -c 'import sys; assert sys.version_info >= (3, 11)' \
        >/dev/null 2>&1; then
        PYTHON_BIN="python3"
      else
        echo "[start] Python 3.11+ is required" >&2
        exit 1
      fi
    fi
    "$PYTHON_BIN" -m venv "$VENV"
    "$VENV/bin/pip" install --upgrade pip
    "$VENV/bin/pip" install \
      "fastapi>=0.115" "uvicorn[standard]>=0.34" \
      "httpx>=0.28" "aiofiles>=24.1" \
      "python-multipart>=0.0.20" "websockets>=12.0" \
      "mcp>=1.28,<2" "packaging>=24"
  fi
fi

if ! "$VENV/bin/python" -c \
  'import importlib.metadata as m; from packaging.version import Version; v=Version(m.version("mcp")); assert Version("1.28") <= v < Version("2")' \
  >/dev/null 2>&1; then
  if command -v "$UV" >/dev/null 2>&1; then
    "$UV" pip install --python "$VENV/bin/python" "mcp>=1.28,<2" "packaging>=24"
  else
    "$VENV/bin/pip" install "mcp>=1.28,<2" "packaging>=24"
  fi
fi

export PORT="${PORT:-8090}"
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-http://localhost:$PORT}"
export CONTROL_DATA="${CONTROL_DATA:-$CTRL_DIR/data}"
export PODMAN_ENV="${PODMAN_ENV:-}"
if [ -z "${WORKSPACE_BASE:-}" ]; then
  if [ "${CONTAINER_RUNTIME:-}" = "podman" ] || { [ -z "${CONTAINER_RUNTIME:-}" ] && [ -n "$PODMAN_ENV" ]; }; then
    export WORKSPACE_BASE="/workspaces"
  else
    export WORKSPACE_BASE="$(cd "$CTRL_DIR/.." && pwd)/workspaces"
  fi
else
  export WORKSPACE_BASE
fi
export CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-}"
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
