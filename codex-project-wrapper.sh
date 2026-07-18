#!/usr/bin/env bash
set -euo pipefail

# Prefer a codex the user has updated into the PERSISTED npm prefix (survives container
# recreation) over the image's baked-in binary; fall back to the image default. An explicit
# CODEX_REAL (e.g. in tests) always wins.
REAL_CODEX="${CODEX_REAL:-}"
if [ -z "$REAL_CODEX" ]; then
  _persisted="${NPM_CONFIG_PREFIX:-${TCB_BROWSE_ROOT:-/workspace}/.agent-home/codex-npm}/bin/codex"
  if [ -x "$_persisted" ]; then REAL_CODEX="$_persisted"; else REAL_CODEX="/usr/local/bin/codex-real"; fi
fi
BEGIN="# TYPST-COMMENT-BRIDGE:BEGIN (auto-managed - edits here will be overwritten)"
END="# TYPST-COMMENT-BRIDGE:END"

find_project_cfg() {
  local d="$PWD"
  while [ "$d" != "/" ]; do
    if [ -f "$d/.codex/config.toml" ]; then
      printf '%s\n' "$d/.codex/config.toml"
      return 0
    fi
    d="$(dirname "$d")"
  done
  return 1
}

merge_project_mcp() {
  local project_cfg="$1"
  local home_dir="${CODEX_HOME:-$HOME/.codex}"
  local home_cfg="$home_dir/config.toml"
  mkdir -p "$home_dir"
  python3 - "$project_cfg" "$home_cfg" "$BEGIN" "$END" <<'PY'
import sys
from pathlib import Path

project_cfg, home_cfg, begin, end = sys.argv[1:]
project = Path(project_cfg).read_text(encoding="utf-8")
if begin not in project or end not in project:
    raise SystemExit(0)
section = project[project.index(begin):project.index(end) + len(end)]
path = Path(home_cfg)
existing = path.read_text(encoding="utf-8") if path.exists() else ""
if begin in existing and end in existing:
    pre = existing[:existing.index(begin)].rstrip()
    post = existing[existing.index(end) + len(end):].strip()
    parts = [p for p in (pre, section, post) if p]
    merged = "\n\n".join(parts) + "\n"
else:
    merged = (existing.rstrip() + "\n\n" if existing.strip() else "") + section + "\n"
path.write_text(merged, encoding="utf-8")
PY
}

clear_project_mcp() {
  local home_dir="${CODEX_HOME:-$HOME/.codex}"
  local home_cfg="$home_dir/config.toml"
  [ -f "$home_cfg" ] || return 0
  python3 - "$home_cfg" "$BEGIN" "$END" <<'PY'
import sys
from pathlib import Path

home_cfg, begin, end = sys.argv[1:]
path = Path(home_cfg)
existing = path.read_text(encoding="utf-8")
if begin not in existing or end not in existing:
    raise SystemExit(0)
pre = existing[:existing.index(begin)].rstrip()
post = existing[existing.index(end) + len(end):].strip()
merged = "\n\n".join(p for p in (pre, post) if p)
path.write_text((merged + "\n") if merged else "", encoding="utf-8")
PY
}

if project_cfg="$(find_project_cfg)"; then
  merge_project_mcp "$project_cfg" || true
else
  clear_project_mcp || true
fi

exec "$REAL_CODEX" "$@"
