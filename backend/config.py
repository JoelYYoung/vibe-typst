"""Configuration, all overridable via environment variables.

The working unit is a single .typ FILE (opened via the file browser). Its directory is
the "project": the comment store (`.slide-comments.db`) and tinymist's root live there,
right next to the file. The browser navigates the filesystem starting from BROWSE_ROOT.
"""
import os
from pathlib import Path

# The file opened on first launch (until the user opens another via the browser).
_DEFAULT_FILE = (
    "/Users/joel/Library/Mobile Documents/iCloud~md~obsidian/Documents/KNet/"
    "3.研究/13.Paper Agents/MSLI/slides/typst/msli.typ"
)
DEFAULT_FILE = Path(os.environ.get("TYPST_FILE", _DEFAULT_FILE)).expanduser()

# Where the file browser may navigate (a safety boundary). Defaults to home.
BROWSE_ROOT = Path(os.environ.get("TCB_BROWSE_ROOT", str(Path.home()))).expanduser()

PPI = int(os.environ.get("TYPST_PPI", "120"))
# Base render dir; each working file gets its own subdir under here.
RENDER_BASE = Path(os.environ.get("RENDER_DIR", "/tmp/tcb-render")).expanduser()

# Global pointer to the last-opened file (so we reopen it on restart). Unlike the
# comment store, this is app state, so it is global rather than next to the file.
GLOBAL_STATE_PATH = Path(
    os.environ.get("TCB_STATE_PATH", str(Path.home() / ".tcb" / "state.json"))
).expanduser()

# Optional hard override of the comment store path (used by the MCP server via env).
# When unset, the web backend keeps the store next to the opened file.
STORE_PATH_OVERRIDE = os.environ.get("COMMENT_STORE_PATH")
