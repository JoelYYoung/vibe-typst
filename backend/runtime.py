"""Mutable runtime state: which supported document FILE we're working on right now.

The current file is an absolute path. Its directory is the project (tinymist --root,
comment store live there). The last-opened file is persisted globally so we reopen it
on restart. Everything (render dir, room key, store path) keys off `current_file()`.
"""
import hashlib
import json
import re
import shutil
import threading
import time
from pathlib import Path

from config import BROWSE_ROOT, DEFAULT_FILE, GLOBAL_STATE_PATH, RENDER_BASE

BACKUP_DIR = Path.home() / ".tcb" / "backups"

_lock = threading.Lock()
_state = {"file": None}


def _load() -> None:
    if _state["file"] is not None:
        return
    f = DEFAULT_FILE
    try:
        if GLOBAL_STATE_PATH.exists():
            data = json.loads(GLOBAL_STATE_PATH.read_text(encoding="utf-8"))
            cand = data.get("file")
            if cand and Path(cand).exists():
                f = Path(cand)
    except Exception:
        pass
    _state["file"] = str(Path(f).expanduser().resolve())


def current_file() -> Path:
    with _lock:
        _load()
        return Path(_state["file"])


def project_dir() -> Path:
    return current_file().parent


def current_main() -> str:
    """The file name, relative to project_dir (what we pass to typst/tinymist)."""
    return current_file().name


def document_type() -> str:
    """Return the active document kind without assuming runtime state was initialized.

    ``set_file`` keeps the state constrained to these two suffixes, while the fallback
    deliberately remains Typst for legacy/default startup state.
    """
    try:
        suffix = current_file().suffix.lower()
    except Exception:
        suffix = ""
    return "pdf" if suffix == ".pdf" else "typst"


def main_path() -> Path:
    return current_file()


def store_path() -> Path:
    """Comment store, kept right next to the opened file."""
    return project_dir() / ".slide-comments.db"


def set_file(path: str) -> str:
    p = Path(path).expanduser().resolve()
    if p.suffix.lower() not in {".typ", ".pdf"} or not p.exists() or not p.is_file():
        raise ValueError("not an existing .typ or .pdf file")
    with _lock:
        _state["file"] = str(p)
        try:
            GLOBAL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            GLOBAL_STATE_PATH.write_text(json.dumps({"file": str(p)}, indent=2), encoding="utf-8")
        except Exception:
            pass
    return str(p)


def restore_file(path: str | None) -> None:
    """Restore a prior runtime selection after an activation failure.

    This is intentionally narrower than ``set_file``: it restores a value previously
    owned by runtime, including the uninitialized ``None`` state.
    """
    with _lock:
        _state["file"] = path
        try:
            if path is None:
                GLOBAL_STATE_PATH.unlink(missing_ok=True)
            else:
                GLOBAL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                GLOBAL_STATE_PATH.write_text(json.dumps({"file": path}, indent=2), encoding="utf-8")
        except Exception:
            pass


def backup(path: str | Path | None = None, keep: int = 30, keep_local: int = 15) -> str | None:
    """No-op. The git-based version system (Save Version) supersedes the old `.backup`
    snapshots, which cluttered project folders. Kept as a stub so existing callers are
    unaffected. (Restored via Versions / git, not loose `.typ.<ts>.backup` files.)"""
    return None


def file_key(path: str | Path | None = None) -> str:
    """A stable, filesystem/URL-safe key for a file (room name + render subdir)."""
    p = Path(path) if path else current_file()
    abs_s = str(p.expanduser().resolve() if not p.is_absolute() else p)
    h = hashlib.sha1(abs_s.encode("utf-8")).hexdigest()[:10]
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", p.name)
    return f"{name}-{h}"


def render_dir(path: str | Path | None = None) -> Path:
    return RENDER_BASE / file_key(path)


# ------------------------------------------------------------------ file browser
_IGNORE_DIRS = {"node_modules", "__pycache__"}


def browse(path: str | None = None) -> dict:
    """List one directory (any directory the user can read): its subdirectories and documents
    files, plus the parent. Robust: a missing dir / non-dir / permission error returns an
    `error` field instead of raising, so a typed path can never crash the server."""
    if path and path.strip():
        try:
            base = Path(path).expanduser().resolve()
        except Exception:
            return {"error": "invalid path", "dirs": [], "files": [], "parent": None}
        if not base.exists():
            return {"error": "directory not found", "dirs": [], "files": [], "parent": None}
        if not base.is_dir():
            return {"error": "not a directory", "dirs": [], "files": [], "parent": None}
    else:
        base = project_dir()
    dirs, documents = [], []
    try:
        entries = sorted(base.iterdir(), key=lambda e: e.name.lower())
    except PermissionError:
        return {"error": "permission denied", "cwd": str(base),
                "parent": str(base.parent) if base.parent != base else None,
                "dirs": [], "files": []}
    except Exception as e:
        return {"error": str(e), "cwd": str(base), "parent": None, "dirs": [], "files": []}
    for entry in entries:
        try:
            if entry.name in _IGNORE_DIRS:
                continue
            if entry.is_dir():
                dirs.append({"name": entry.name, "path": str(entry)})
            elif entry.suffix.lower() in {".typ", ".pdf"}:
                documents.append({"name": entry.name, "path": str(entry), "size": entry.stat().st_size})
        except (PermissionError, OSError):
            continue
    parent = str(base.parent) if base.parent != base else None
    return {"cwd": str(base), "parent": parent, "dirs": dirs, "files": documents}
