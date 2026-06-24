"""MCP server exposing pending slide-edit comments AND document edits to Claude Code.

Comment status lives in the shared SQLite store (read directly). Document edits are
routed over HTTP to the running web backend, because the live document is a CRDT held
in the backend process: a separate process (this one) cannot mutate it directly, so we
POST /api/edit and the backend applies it to the Y.Doc, broadcasts to the browser, and
persists to the .typ. Edits are content-anchored (anchor_text), never line numbers.

Launch is handled automatically by the web app (a .mcp.json is generated per project).
To configure manually, see docs/deployment.md.
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("web-typst")

BACKEND = os.environ.get("TCB_BACKEND_URL", "http://127.0.0.1:8787").rstrip("/")

_GUARD = (
    "IMPORTANT: this .typ is a LIVE SHARED document (the human is editing it in a browser "
    "at the same time). NEVER use the native Read/Edit/Write/MultiEdit tools on the file "
    "directly — that writes disk behind the shared doc's back and gets clobbered. Read it "
    "ONLY via get_document / find_in_document, and edit it ONLY via replace_anchor / "
    "insert_before_anchor / insert_after_anchor / replace_range. Those route through the "
    "shared doc so the human sees your change live."
)

_LOCATE_HINT = (
    "To locate the target: run find_in_document(anchor_text) to get its line number, then "
    "get_document(offset=<that line>) to read the surrounding window. " + _GUARD
)

# Windowed reading, so a large deck never floods the context in one call.
HEAD_LINES = 120   # default window when no explicit range is given
MAX_LINES = 400    # hard cap on lines returned by a single get_document call


def _public(c: dict) -> dict:
    return {
        "id": c["id"],
        "seq": c["seq"],
        "file": c.get("file"),
        "kind": c.get("kind", "element"),
        "page": c["page"],
        "anchor_text": c["anchor_text"],
        "anchor_context": c["anchor_context"],
        "region": c.get("region"),
        "raw_context": c.get("raw_context", ""),
        "comment": c["body"],
        "status": c["status"],
        "created_at": c["created_at"],
    }


def _backend(method: str, path: str, payload: dict | None = None) -> dict:
    url = f"{BACKEND}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"backend {e.code}: {e.read().decode()[:300]}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"web backend unreachable at {BACKEND} ({e.reason}). "
                                      "Is the Typst Comment Bridge server running?"}


# ----------------------------------------------------------------- comments
@mcp.tool()
def get_pending_comments() -> list:
    """List all pending slide-edit comments, oldest first. Each has `anchor_text`, `page`,
    a `raw_context` blob, and a `comment` describing the change.
    """ + " " + _LOCATE_HINT
    return [_public(c) for c in store.list_comments("pending")]


@mcp.tool()
def get_comment(id: str) -> dict:
    """Full detail for one comment, by its `id` (hex) or `seq` (number)."""
    c = store.get_comment(id)
    return _public(c) if c else {"error": f"no comment {id!r}"}


@mcp.tool()
def mark_comment_done(id: str, note: str = "") -> dict:
    """Mark a comment done once its change is applied. `note` records what was done."""
    c = store.set_status(id, "done", note)
    return _public(c) if c else {"error": f"no comment {id!r}"}


@mcp.tool()
def mark_comment_dismissed(id: str, reason: str = "") -> dict:
    """Dismiss a comment without applying it (unclear, obsolete, or already handled)."""
    c = store.set_status(id, "dismissed", reason)
    return _public(c) if c else {"error": f"no comment {id!r}"}


@mcp.tool()
def list_all_comments() -> list:
    """List every comment regardless of status (pending, done, dismissed)."""
    return [_public(c) for c in store.list_comments()]


# ----------------------------------------------------------------- transcripts
@mcp.tool()
def get_transcripts() -> dict:
    """Per-page speaker transcripts (narration) for the deck, with each page's section + the
    source `#speaker-note(...)` text that drives it.

    Transcripts are stored INLINE in the .typ as touying `#speaker-note("...")` calls inside
    each slide — slide-level (repeats across a slide's subslides) or per-subslide via
    `if self.subslide == n { speaker-note("...") }`. They are the SAME source the human edits
    in the presenter/preview and are versioned with the deck.

    Returns {pages:[{page, section, note}], total}. To CHANGE a transcript, edit the .typ
    directly with the anchor tools (find the `speaker-note("...")` and replace_anchor on its
    text, or insert_after_anchor a new `#speaker-note("...")` as the first line of a slide
    body). IMPORTANT: when you ADD a slide, add its `#speaker-note(...)`; when you DELETE or
    REORDER slides, move/remove the matching `#speaker-note(...)` so transcripts stay aligned
    with their slides.""" + " " + _GUARD
    r = _backend("GET", "/api/slide-map")
    pages = [{"page": p.get("page"), "section": p.get("section"), "note": p.get("note") or ""}
             for p in (r.get("pages") or [])]
    return {"pages": pages, "total": r.get("total", len(pages))}


# ----------------------------------------------------------------- document edits
def _fetch_source(file: str):
    """Return (source_text, file_label) from the live backend, or (None, error_dict)."""
    r = _backend("GET", "/api/document" + (f"?file={file}" if file else ""))
    src = r.get("source")
    if not isinstance(src, str):
        return None, r
    return src, r.get("file")


@mcp.tool()
def get_document(file: str = "", offset: int = 1, limit: int = 0) -> dict:
    """Read a WINDOW of the LIVE shared document (the .typ the human is editing live), as a
    line-numbered slice — this is a pager, NOT a whole-file dump.

    DO NOT try to read the whole file at once. On a real deck that is hundreds of lines and
    just floods the context. Read only the slice you need:
      - `offset`: 1-based line to start at (default 1 = the file's head).
      - `limit`: number of lines to return (default 120, hard-capped at 400). A call that
        asks for more than 400 lines is clamped, not honored.

    Typical workflow on a large file: call find_in_document("some text") to get the line
    number of what you want, then get_document(offset=<that line>) to read around it. To
    page forward, call get_document(offset=<previous end + 1>).

    The response reports `total_lines`, the `shown` range, and `truncated`/`next` so you
    know whether more remains. Line numbers are for LOCATING and reading only — never edit
    by line number (lines shift after every edit); edit with the anchor tools using exact
    text snippets. """ + _GUARD
    src, label = _fetch_source(file)
    if src is None:
        return label  # error dict from the backend
    lines = src.split("\n")
    total = len(lines)
    start = max(1, offset)
    if start > total:
        return {"file": label, "total_lines": total, "shown": None,
                "text": "", "truncated": False,
                "note": f"offset {start} is past end of file ({total} lines)."}
    win = HEAD_LINES if limit <= 0 else min(limit, MAX_LINES)
    end = min(total, start + win - 1)
    numbered = "\n".join(f"{i:>5}\t{ln}" for i, ln in zip(range(start, end + 1), lines[start - 1:end]))
    more = end < total
    return {
        "file": label,
        "total_lines": total,
        "chars": len(src),
        "shown": f"{start}-{end}",
        "truncated": more,
        "next": (f"more below — read on with get_document(offset={end + 1})" if more else None),
        "text": numbered,
        "hint": "Read windows, not the whole file. EDIT via replace_anchor / "
                "insert_before_anchor / insert_after_anchor using exact snippets, never by "
                "line number. " + _GUARD,
    }


@mcp.tool()
def find_in_document(query: str, file: str = "", max_hits: int = 40) -> dict:
    """Search the LIVE shared document for a literal substring and return each matching
    line's number and full text — the fast way to navigate a large deck WITHOUT dumping it.

    Use this to locate an anchor or a region before reading or editing: find the line, then
    get_document(offset=<line>) to read its window, then edit with the anchor tools. The
    match is a plain case-sensitive substring (not a regex). Returns up to `max_hits`
    matches (default 40); if `matches` exceeds that, narrow the query. """ + _GUARD
    src, label = _fetch_source(file)
    if src is None:
        return label  # error dict from the backend
    lines = src.split("\n")
    hits = []
    for i, ln in enumerate(lines, 1):
        if query in ln:
            hits.append({"line": i, "text": ln})
            if len(hits) >= max(1, max_hits):
                break
    total_matches = sum(1 for ln in lines if query in ln)
    return {
        "file": label,
        "total_lines": len(lines),
        "query": query,
        "matches": total_matches,
        "shown": len(hits),
        "hits": hits,
        "hint": ("Open a hit with get_document(offset=<line>). " + _GUARD)
                if hits else "No match. Try a shorter or different substring.",
    }


@mcp.tool()
def replace_anchor(anchor: str, new_text: str, occurrence: int = 1, file: str = "") -> dict:
    """Replace `anchor` with `new_text` in the live shared document. This is the primary
    edit primitive — the equivalent of a find-and-replace anchored on exact text.

    `anchor` must be an EXACT substring copied verbatim from the document (match whitespace,
    punctuation, and the typst markup, e.g. `= My Title` or `#slide[`). It must identify a
    UNIQUE span: if it matches more than once the edit is refused, so either extend the
    anchor with surrounding text until it is unique, or set `occurrence` (1-based) to pick
    which match. To DELETE text, pass new_text="". Keep the anchor reasonably short but
    unambiguous; do not paste huge blocks. Locate anchors with find_in_document.

    Returns the backend result (ok / error). On `{ok: false}` read the error: a refusal for
    a non-unique or not-found anchor means you should re-read with get_document and retry."""
    return _backend("POST", "/api/edit", {
        "op": "replace_anchor", "anchor": anchor, "new_text": new_text,
        "occurrence": occurrence, "file": file or None,
    })


@mcp.tool()
def insert_before_anchor(anchor: str, text: str, occurrence: int = 1, file: str = "") -> dict:
    """Insert `text` immediately BEFORE `anchor` in the live shared document, without
    altering the anchor itself. Use to add a new slide, line, or block ahead of an existing
    one (e.g. a new `#slide[...]` before the current page). `anchor` is exact verbatim text
    and must be unique, or pass `occurrence` to disambiguate. Include any newline you need
    in `text` (the insert is literal — typically end `text` with "\\n" so it sits on its own
    line). Locate the anchor with find_in_document. Returns the backend ok/error result."""
    return _backend("POST", "/api/edit", {
        "op": "insert_before", "anchor": anchor, "text": text,
        "occurrence": occurrence, "file": file or None,
    })


@mcp.tool()
def insert_after_anchor(anchor: str, text: str, occurrence: int = 1, file: str = "") -> dict:
    """Insert `text` immediately AFTER `anchor` in the live shared document, without
    altering the anchor itself. Use to add a new slide, line, or block following an existing
    one (e.g. "insert a slide after this page"). `anchor` is exact verbatim text and must be
    unique, or pass `occurrence`. Include any leading newline you need in `text` (the insert
    is literal — typically start `text` with "\\n" so it begins on a fresh line). Locate the
    anchor with find_in_document. Returns the backend ok/error result."""
    return _backend("POST", "/api/edit", {
        "op": "insert_after", "anchor": anchor, "text": text,
        "occurrence": occurrence, "file": file or None,
    })


@mcp.tool()
def replace_range(from_offset: int, to_offset: int, new_text: str, file: str = "") -> dict:
    """Replace the CHARACTER range [from_offset, to_offset) with `new_text`. Offsets are
    0-based character indices into the whole document (not line numbers).

    This is an escape hatch — prefer the anchor tools, which are robust to the document
    shifting. Only use replace_range when an anchor cannot be made unique, and only with
    offsets you derived from the CURRENT document. Any prior edit invalidates offsets, so do
    a range edit LAST and never batch two of them against the same read. Returns ok/error."""
    return _backend("POST", "/api/edit", {
        "op": "replace_range", "from": from_offset, "to": to_offset,
        "new_text": new_text, "file": file or None,
    })


if __name__ == "__main__":
    mcp.run()
