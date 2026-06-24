"""Read the .typ source and render the active deck to per-page PNGs (and SVGs).

The working file is whatever `runtime.current_main()` points at; each file renders
into its own subdir so switching files never mixes pages.
"""
import hashlib
import subprocess

import runtime
from config import PPI

# Per-page content token cache, keyed by absolute SVG path -> (mtime_ns, size, token).
# Lets page_tokens() skip re-hashing a file whose mtime+size haven't moved (cheap on a
# 300ms poll). Keyed by full path so two projects' identically-named page-1.svg never alias.
_token_cache: dict[str, tuple] = {}


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


def render_path(name: str):
    return runtime.render_dir() / name


def page_tokens() -> dict[str, str]:
    """Per-page CONTENT token used to cache-bust each preview <img> URL individually.

    The token is a short hash of the page's SVG bytes. So:
      - a page whose content is unchanged keeps the SAME token -> SAME URL -> the browser
        serves it from cache (no refetch, no wasted bytes);
      - a page that actually changed gets a NEW token -> NEW URL -> the browser fetches it.
    This replaces the old single global render-version counter, which forced EVERY page to
    refetch on any edit and (because it reset per project) collided across projects.
    Hashing is memoised by (mtime, size) so we don't re-read big SVGs on every poll.
    """
    d = runtime.render_dir()
    if not d.exists():
        return {}
    out: dict[str, str] = {}
    for p in d.glob("page-*.svg"):
        try:
            stt = p.stat()
        except OSError:
            continue
        key = str(p)
        cached = _token_cache.get(key)
        if cached and cached[0] == stt.st_mtime_ns and cached[1] == stt.st_size:
            out[p.name] = cached[2]
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        tok = hashlib.blake2b(data, digest_size=6).hexdigest()  # 12 hex chars, plenty
        _token_cache[key] = (stt.st_mtime_ns, stt.st_size, tok)
        out[p.name] = tok
    return out


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
