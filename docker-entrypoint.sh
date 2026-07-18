#!/usr/bin/env bash
set -e
# Ensure persistent dirs exist. The workspace is bind-mounted; agent home state is
# symlinked into it so container replacement does not discard auth/config/cache state.
WORKSPACE="${TCB_BROWSE_ROOT:-/workspace}"
mkdir -p "$WORKSPACE" /tmp/tcb-render "$(dirname "${TCB_STATE_PATH:-/workspace/.tcb/state.json}")" 2>/dev/null || true
mkdir -p "$WORKSPACE/.agent-home/claude" "$WORKSPACE/.agent-home/codex" "$WORKSPACE/.agent-home/tcb" \
         "$WORKSPACE/.agent-home/local" "$WORKSPACE/.agent-home/codex-npm" 2>/dev/null || true
# Persist the dirs the agents both CONFIG and *UPDATE* into. Claude's native auto-updater
# installs to ~/.local/bin, and codex updates via npm — both were in the ephemeral container
# layer, so the agents re-updated on every fresh container. Symlinking ~/.local (etc.) into the
# bind-mounted workspace keeps those self-updates. If an in-container dir already has content
# (e.g. a first-run install before the symlink existed), migrate it into the store once.
for pair in "$HOME/.claude:claude" "$HOME/.codex:codex" "$HOME/.tcb:tcb" "$HOME/.local:local"; do
  link="${pair%%:*}"
  target="$WORKSPACE/.agent-home/${pair#*:}"
  [ -L "$link" ] && continue
  if [ -d "$link" ] && [ -n "$(ls -A "$link" 2>/dev/null)" ]; then
    cp -a "$link/." "$target/" 2>/dev/null || true
  fi
  rm -rf "$link" 2>/dev/null || true
  ln -s "$target" "$link" 2>/dev/null || true
done
cd /app/backend
exec /app/backend/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port "${PORT:-8080}"
