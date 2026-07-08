#!/usr/bin/env bash
set -e
# Ensure persistent dirs exist. The workspace is bind-mounted; agent home state is
# symlinked into it so container replacement does not discard auth/config/cache state.
WORKSPACE="${TCB_BROWSE_ROOT:-/workspace}"
mkdir -p "$WORKSPACE" /tmp/tcb-render "$(dirname "${TCB_STATE_PATH:-/workspace/.tcb/state.json}")" 2>/dev/null || true
mkdir -p "$WORKSPACE/.agent-home/claude" "$WORKSPACE/.agent-home/codex" "$WORKSPACE/.agent-home/tcb" 2>/dev/null || true
for pair in "$HOME/.claude:$WORKSPACE/.agent-home/claude" "$HOME/.codex:$WORKSPACE/.agent-home/codex" "$HOME/.tcb:$WORKSPACE/.agent-home/tcb"; do
  link="${pair%%:*}"
  target="${pair#*:}"
  if [ -L "$link" ]; then
    continue
  fi
  if [ ! -e "$link" ] || { [ -d "$link" ] && [ -z "$(ls -A "$link" 2>/dev/null)" ]; }; then
    rm -rf "$link" 2>/dev/null || true
    ln -s "$target" "$link" 2>/dev/null || true
  fi
done
cd /app/backend
exec /app/backend/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port "${PORT:-8080}"
