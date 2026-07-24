#!/usr/bin/env bash
set -e
# Ensure persistent dirs exist. The workspace is bind-mounted; agent home state is
# symlinked into it so container replacement does not discard auth/config/cache state.
WORKSPACE="${TCB_BROWSE_ROOT:-/workspace}"
if ! mkdir -p "$WORKSPACE" /tmp/tcb-render \
    "$(dirname "${TCB_STATE_PATH:-/workspace/.tcb/state.json}")" \
    "$WORKSPACE/.agent-home/claude" "$WORKSPACE/.agent-home/codex" \
    "$WORKSPACE/.agent-home/tcb" "$WORKSPACE/.agent-home/local" \
    "$WORKSPACE/.agent-home/codex-npm"; then
  echo "[entrypoint] cannot create persistent workspace directories" >&2
  exit 1
fi
# Persist the dirs the agents both CONFIG and *UPDATE* into. Claude's native auto-updater
# installs to ~/.local/bin, and codex updates via npm — both were in the ephemeral container
# layer, so the agents re-updated on every fresh container. Symlinking ~/.local (etc.) into the
# bind-mounted workspace keeps those self-updates. If an in-container dir already has content
# (e.g. a first-run install before the symlink existed), migrate it into the store once.
for pair in "$HOME/.claude:claude" "$HOME/.codex:codex" "$HOME/.tcb:tcb" "$HOME/.local:local"; do
  link="${pair%%:*}"
  target="$WORKSPACE/.agent-home/${pair#*:}"
  if [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ]; then
    continue
  fi
  if [ -d "$link" ] && [ -n "$(ls -A "$link" 2>/dev/null)" ]; then
    if ! cp -a "$link/." "$target/"; then
      echo "[entrypoint] failed to preserve $link; original left untouched" >&2
      exit 1
    fi
  fi
  backup="${link}.vibe-migrate-$$"
  pending="${link}.vibe-link-$$"
  if [ -e "$link" ] || [ -L "$link" ]; then
    if ! mv "$link" "$backup"; then
      echo "[entrypoint] failed to stage $link for persistent linking" >&2
      exit 1
    fi
  fi
  if ! ln -s "$target" "$pending" || ! mv "$pending" "$link"; then
    rm -f "$pending" 2>/dev/null || true
    if [ -e "$backup" ] || [ -L "$backup" ]; then
      mv "$backup" "$link" 2>/dev/null || true
    fi
    echo "[entrypoint] failed to link $link to persistent storage" >&2
    exit 1
  fi
  if [ -e "$backup" ] || [ -L "$backup" ]; then
    rm -rf "$backup" 2>/dev/null || true
  fi
done
cd /app/backend
exec /app/backend/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port "${PORT:-8080}"
