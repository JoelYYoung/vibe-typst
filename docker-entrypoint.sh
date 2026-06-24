#!/usr/bin/env bash
set -e
# ensure persistent dirs exist (bind-mounted at runtime; created if empty)
mkdir -p "${TCB_BROWSE_ROOT:-/workspace}" /tmp/tcb-render "$(dirname "${TCB_STATE_PATH:-/workspace/.tcb/state.json}")" "$HOME/.claude" "$HOME/.tcb" 2>/dev/null || true
cd /app/backend
exec /app/backend/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port "${PORT:-8080}"
