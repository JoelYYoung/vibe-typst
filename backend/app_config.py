"""
App-level configuration: runtime mode and projects root directory.

APP_MODE=local  – single-user, no auth; projects root is user-configurable.
APP_MODE=server – multi-user; projects root is fixed (/workspace or PROJECTS_ROOT env).
"""
import json
import os
from pathlib import Path

APP_MODE: str = os.getenv("APP_MODE", "local")  # "local" | "server"

# ── config file (local mode only) ──────────────────────────────────────────
_CONFIG_DIR = Path.home() / ".vibe-typst"
_CONFIG_FILE = _CONFIG_DIR / "config.json"


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_FILE.read_text())
    except Exception:
        return {}


def _save_config(data: dict) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(json.dumps(data, indent=2))


# ── projects root ───────────────────────────────────────────────────────────

def get_projects_root() -> Path | None:
    """Return the configured projects root, or None if not yet configured."""
    if APP_MODE == "server":
        root = os.getenv("PROJECTS_ROOT", "/workspace")
        return Path(root)
    # local mode: read from config file
    cfg = _load_config()
    if "projects_root" in cfg:
        return Path(cfg["projects_root"])
    return None


def set_projects_root(path: str) -> Path:
    """Persist the projects root (local mode only)."""
    if APP_MODE != "local":
        raise RuntimeError("Cannot set projects root in server mode")
    p = Path(path).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    cfg = _load_config()
    cfg["projects_root"] = str(p)
    _save_config(cfg)
    return p


def is_configured() -> bool:
    return get_projects_root() is not None
