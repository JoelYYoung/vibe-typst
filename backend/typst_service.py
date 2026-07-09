"""Read the .typ source and render the active deck to per-page PNGs (and SVGs).

The working file is whatever `runtime.current_main()` points at; each file renders
into its own subdir so switching files never mixes pages.
"""
import hashlib
import subprocess

import runtime
from config import PPI


def main_path():
    return runtime.main_path()


def read_source() -> str:
    return main_path().read_text(encoding="utf-8")


def write_source(text: str) -> None:
    main_path().write_text(text, encoding="utf-8")


def list_pages() -> list[str]:
    d = runtime.render_dir()
    if not d.exists():
        return []
    pages = list(d.glob("page-*.svg"))
    pages.sort(key=lambda x: int(x.stem.split("-")[1]))
    return [p.name for p in pages]


def page_tokens() -> dict[str, str]:
    """Content hashes for rendered pages, used as frontend SVG cache-busters."""
    d = runtime.render_dir()
    out = {}
    for name in list_pages():
        p = d / name
        try:
            out[name] = hashlib.sha1(p.read_bytes()).hexdigest()[:12]
        except OSError:
            pass
    return out


def render_path(name: str):
    return runtime.render_dir() / name


def compile_slides(fmt: str = "svg") -> dict:
    """Render the active file to per-page images in its render dir."""
    d = runtime.render_dir()
    d.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "svg"):
        for p in d.glob(f"page-*.{ext}"):
            try:
                p.unlink()
            except OSError:
                pass
    out = str(d / f"page-{{p}}.{fmt}")
    cmd = ["typst", "compile", runtime.current_main(), out]
    if fmt == "png":
        cmd += ["--ppi", str(PPI)]
    try:
        r = subprocess.run(
            cmd,
            cwd=str(runtime.project_dir()),
            capture_output=True,
            text=True,
            timeout=180,
        )
        return {"ok": r.returncode == 0, "stderr": r.stderr.strip(), "pages": list_pages()}
    except FileNotFoundError:
        return {"ok": False, "stderr": "`typst` not found on PATH", "pages": []}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stderr": "typst compile timed out", "pages": list_pages()}
