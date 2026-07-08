#!/usr/bin/env bash
# Start the Vibe Typst control plane DETACHED from any terminal/SSH session, idempotently.
# Re-run any time to relaunch; no-ops if 8090 is already listening.
D="$(cd "$(dirname "$0")" && pwd)"
if ss -tln 2>/dev/null | grep -q ":8090 "; then echo "already running on :8090"; exit 0; fi
mkdir -p "$D/data"
if command -v setsid >/dev/null 2>&1; then
  setsid nohup bash "$D/start.sh" > "$D/data/control.log" 2>&1 < /dev/null &
else
  nohup bash "$D/start.sh" > "$D/data/control.log" 2>&1 < /dev/null &
fi
echo "control plane launched detached (log: $D/data/control.log)"
