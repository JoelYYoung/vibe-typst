"""Speaker-notes ("script") management: parse every `#speaker-note("...")` from the live
document, associate each with the slide it sits in, allow editing one, and export them all as
a single Markdown script. Editing routes through the same content-anchored docstore edit as
everything else, so it stays in sync with the human + Claude.
"""
import json
import os
import subprocess

import docstore
import re
import runtime

# #speaker-note("...") or speaker-note("...") — capture the (escaped) string-literal content
_NOTE_RE = re.compile(r'#?speaker-note\(\s*"((?:[^"\\]|\\.)*)"\s*\)')
# column-0 slide openers + section comments (same convention as slidemap)
_OPENER = re.compile(r"^#(slide|centered-slide|focus-slide|title-slide)\b")
_SECTION = re.compile(r"^//\s*=+\s*(.*?)\s*=+\s*$")


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _enclosing(lines: list[str], line0: int):
    slide_line = None
    for i in range(min(line0, len(lines) - 1), -1, -1):
        if _OPENER.match(lines[i]):
            slide_line = i + 1
            break
    section = None
    for i in range((slide_line - 1) if slide_line else min(line0, len(lines) - 1), -1, -1):
        m = _SECTION.match(lines[i])
        if m:
            section = m.group(1)
            break
    return slide_line, section


def slide_open_lines() -> list[int]:
    """1-based source line of every column-0 slide opener, in document order.
    Lets us map a page -> its slide opener from SOURCE (via touying's logical-slide
    label) without depending on the resolver probe, which can fail to resolve a point."""
    src = docstore.get_text() or ""
    return [i + 1 for i, ln in enumerate(src.split("\n")) if _OPENER.match(ln)]


def list_notes() -> list[dict]:
    src = docstore.get_text() or ""
    lines = src.split("\n")
    out = []
    n = 0
    for m in _NOTE_RE.finditer(src):
        # Skip matches inside a `//` line comment (e.g. the scaffold's own documentation
        # comment that mentions #speaker-note("...")) — those aren't real notes and would
        # otherwise be flagged as "renders on no slide".
        line_start = src.rfind("\n", 0, m.start()) + 1
        if "//" in src[line_start:m.start()]:
            continue
        raw = m.group(1)
        line0 = src.count("\n", 0, m.start())
        slide_line, section = _enclosing(lines, line0)
        n += 1
        out.append({
            "n": n,
            "raw": raw,                    # exact source content (used as the edit anchor)
            "text": _unescape(raw),        # human-readable
            "section": section,
            "slide_line": slide_line,
            "note_line": line0 + 1,
        })
    return out


async def update_note(raw: str, text: str) -> dict:
    """Replace one note's content. `raw` is the exact existing (escaped) source content used to
    anchor the edit; `text` is the new human-readable content (re-escaped before writing)."""
    if not raw:
        return {"ok": False, "error": "missing original note content"}
    new_raw = _escape(text)
    if new_raw == raw:
        return {"ok": True, "unchanged": True}
    return await docstore.replace_anchor(raw, new_raw)


async def create_note(slide_line: int, text: str,
                      sub_index=None, sub_total=None) -> dict:
    """Add a #speaker-note to a slide/subslide that has none.

    Subslide-aware: on a multi-subslide slide written in closure form
    (`#slide(repeat: n, self => {…})`), the note is gated to THIS subslide with
    `if self.subslide == k { speaker-note(…) }` so it doesn't repeat across the others.
    On a single-subslide slide it's a plain `#speaker-note(…)`. A content-block slide
    (`#slide[…]`) whose subslides come from `#pause` has no `self` to gate on, so the note
    necessarily repeats — we add it and return a `warning`.
    Validation: a subslide index beyond the slide's real subslide count is rejected.
    """
    if not text or not text.strip():
        return {"ok": False, "error": "empty note"}
    try:
        slide_line = int(slide_line)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad slide line"}
    try:
        k = int(sub_index) if sub_index is not None else None
        total = int(sub_total) if sub_total is not None else None
    except (TypeError, ValueError):
        k = total = None
    # A transcript cannot target a subslide the slide never renders.
    if k is not None and total is not None and k > total:
        return {"ok": False,
                "error": f"Subslide {k} doesn't exist — this slide has "
                         f"{total} subslide{'s' if total != 1 else ''}."}
    src = docstore.get_text() or ""
    lines = src.split("\n")
    idx = slide_line - 1
    if idx < 0 or idx >= len(lines):
        return {"ok": False, "error": "slide line out of range"}
    opener = lines[idx]
    base_indent = opener[: len(opener) - len(opener.lstrip())]
    stripped = opener.rstrip()
    code_ctx = stripped.endswith("{") or "=>" in stripped  # closure body vs content block
    esc = _escape(text)
    multi = bool(total and total > 1)
    warning = None
    if multi and k and code_ctx:
        # gate the note to just this subslide inside the closure body
        note = f'\n{base_indent}  if self.subslide == {k} {{ speaker-note("{esc}") }}'
    elif multi and k and not code_ctx:
        note = f'\n{base_indent}  #speaker-note("{esc}")'
        warning = ("Added, but it will repeat on all subslides: this slide is a content "
                   "block (#slide[…]) whose subslides come from #pause. For a per-subslide "
                   "transcript, write the slide as #slide(self => {…}).")
    else:
        prefix = "" if code_ctx else "#"
        note = f'\n{base_indent}  {prefix}speaker-note("{esc}")'
    cp_end_of_opener = len("\n".join(lines[: idx + 1]))  # code-point offset at end of opener line
    r = await docstore.insert_text(cp_end_of_opener, note)
    if isinstance(r, dict) and r.get("ok") and warning:
        r["warning"] = warning
    return r


# ---------------------------------------------------------- per-page (pdfpc) transcripts
# touying maps each rendered page to a speaker note in the PDF-Presenter (pdfpc) format:
# `typst query <deck> "<pdfpc-file>"` yields {pdfpcFormat, pages:[{idx,label,overlay,note}]}.
# An UNCONDITIONAL `speaker-note(...)` repeats on every subslide; a CONDITIONAL one
# (`if self.subslide == n { speaker-note(...) }`) gives a distinct per-page transcript. We use
# this as the authoritative page->note mapping (it's what touying itself computes).

def pdfpc_raw() -> str:
    """The complete `.pdfpc` file content (pdfpc-format-2 JSON) for the CURRENT deck on disk, or
    "" if it can't be produced. Caller should flush the live doc to disk first."""
    main = runtime.current_file()
    proj = runtime.project_dir()
    if not main.exists():
        return ""
    try:
        # RAYON_NUM_THREADS=1: on PID-constrained hosts (the O3 server) `typst query`
        # otherwise panics initializing rayon's thread pool ("Resource temporarily
        # unavailable"). One thread is plenty for a small metadata query.
        env = {**os.environ, "RAYON_NUM_THREADS": "1"}
        proc = subprocess.run(
            ["typst", "query", "--root", str(proj), str(main),
             "<pdfpc-file>", "--field", "value", "--one"],
            capture_output=True, text=True, cwd=str(proj), timeout=120, env=env,
        )
    except Exception:
        return ""
    out = (proc.stdout or "").strip()
    return out if (proc.returncode == 0 and out.startswith("{")) else ""


def pdfpc_pages() -> list[dict]:
    """Per-page transcripts: [{page (1-based), label, overlay, note}] in page order."""
    raw = pdfpc_raw()
    if not raw:
        return []
    try:
        pages = json.loads(raw).get("pages", [])
    except Exception:
        return []
    return [{
        "page": int(p.get("idx", 0)) + 1,        # idx is 0-based; preview pages are 1-based
        "label": p.get("label"),                  # logical slide number
        "overlay": p.get("overlay"),              # subslide index (0-based)
        "note": (p.get("note") or ""),
    } for p in pages]


def export_text() -> str:
    """Page-by-page narration for text-to-speech: each page's transcript in order, separated by a
    blank line. CONSECUTIVE duplicate notes are collapsed (a slide-level note shown across N
    subslides becomes one paragraph), so it reads cleanly. No headings/markup."""
    out, last = [], None
    for p in pdfpc_pages():
        t = (p["note"] or "").strip()
        if t and t != last:
            out.append(t)
        if t:
            last = t
    return ("\n\n".join(out) + "\n") if out else ""
