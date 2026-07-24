"""MCP server exposing pending slide-edit comments AND document edits to Claude Code.

Comment status lives in the shared SQLite store (read directly). Document edits are
routed over HTTP to the running web backend, because the live document is a CRDT held
in the backend process: a separate process (this one) cannot mutate it directly, so we
POST /api/edit and the backend applies it to the Y.Doc, broadcasts to the browser, and
persists to the .typ. Edits are content-anchored (anchor_text), never line numbers.

Launch is configured by the web app's "Run Claude" button, or add it yourself:

  {
    "mcpServers": {
      "vibe-typst": {
        "command": "uv",
        "args": ["run", "python", "/Users/joel/Projects/typst-comment-bridge/backend/mcp_server.py"],
        "env": {
          "COMMENT_STORE_PATH": "<typst-project>/.slide-comments.db",
          "TCB_BACKEND_URL": "http://127.0.0.1:8787"
        }
      }
    }
  }
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("vibe-typst")

BACKEND = os.environ.get("TCB_BACKEND_URL", "http://127.0.0.1:8787").rstrip("/")

_GUARD = (
    "IMPORTANT: this .typ is a LIVE SHARED document (the human is editing it in a browser "
    "at the same time). NEVER use the native Read/Edit/Write/MultiEdit tools on the file "
    "directly — that writes disk behind the shared doc's back and gets clobbered. Read it "
    "ONLY via get_document / find_in_document, and edit it ONLY via apply_edits (preferred, "
    "atomic batch) or the single-edit shims replace_anchor / insert_before_anchor / "
    "insert_after_anchor / replace_lines / insert_at_line / replace_range. Those route through "
    "the shared doc so the human sees your change live."
)


def _live_location(cid: str) -> dict | None:
    """The comment's CURRENT anchor position, resolved from its drift-proof StickyIndex — the
    frozen line numbers inside raw_context go stale, this does not. None if the comment has no
    resolvable anchor (e.g. a page comment)."""
    r = _backend("GET", f"/api/comments/{cid}/anchor")
    if not r.get("spans"):
        return None
    return {"lines": r.get("lines"), "current_text": r.get("texts"), "rev": r.get("rev")}

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
    a `raw_context` blob, and a `comment` describing the change. Each also carries `location`
    = {lines, current_text, rev}: the anchor's CURRENT position resolved live from the shared
    doc. TRUST `location` over the line numbers inside `raw_context` (those are frozen at
    create time and drift as the file changes). Use `location.lines` to jump straight to the
    target and `location.rev` as apply_edits(base_rev=...).
    """ + " " + _LOCATE_HINT
    out = []
    for c in store.list_comments("pending"):
        pub = _public(c)
        pub["location"] = _live_location(c["id"])
        out.append(pub)
    return out


@mcp.tool()
def get_comment(id: str) -> dict:
    """Full detail for one comment, by its `id` (hex) or `seq` (number). Includes `location`
    (live-resolved anchor position); trust it over raw_context's frozen line numbers."""
    c = store.get_comment(id)
    if not c:
        return {"error": f"no comment {id!r}"}
    pub = _public(c)
    pub["location"] = _live_location(c["id"])
    return pub


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

    Each page carries: `page`, `slide_no` (logical slide; subslides share it), `slide_line`
    (source line of the slide opener), `sub_index`/`sub_total` (which build of the slide),
    `section`, `note` (DISPLAY text — pdfpc-rendered, unescaped), `note_raw` (the EXACT source
    literal inside `#speaker-note("...")`, escaping intact), and `note_line` (its source line).
    Also returns `orphans`: source notes that render on no page (e.g. a `self.subslide == k`
    gate past the slide's real subslide count) — clean these up on delete/reorder.

    To EDIT a note, anchor on `note_raw` (verbatim source), NOT `note`, which is display-only —
    they differ on quotes/newlines/markup so searching `note` will miss. If `note_raw` is null
    or a note text repeats, address it by `note_line` with apply_edits `{"by":"lines"}`. Use
    `slide_line`/`slide_no` to keep a note with its slide when you split/insert/reorder slides
    (ADD a slide → add its `#speaker-note`; DELETE/REORDER → move/remove the matching one).
    """ + " " + _GUARD
    r = _backend("GET", "/api/slide-map")
    pages = [{"page": p.get("page"), "slide_no": p.get("slide_no"), "slide_line": p.get("slide_line"),
              "sub_index": p.get("sub_index"), "sub_total": p.get("sub_total"),
              "section": p.get("section"), "note": p.get("note") or "",
              "note_raw": p.get("note_raw"), "note_line": p.get("note_line")}
             for p in (r.get("pages") or [])]
    return {"pages": pages, "total": r.get("total", len(pages)), "orphans": r.get("orphans") or []}


# ----------------------------------------------------------------- locate
@mcp.tool()
def locate(page: int = 0, slide: int = 0) -> dict:
    """Resolve a PAGE or a SLIDE number to SOURCE LINES — the way to turn a human "fix slide 5"
    or "page 12" into an exact line range for get_document / apply_edits. Pass EXACTLY ONE of
    `page` or `slide` (1-based).

    SLIDE vs PAGE are different and must not be confused: a SLIDE is one `#slide[...]` call; it
    can render as SEVERAL PAGES (subslides via `#pause` / `self.subslide == k`). A PAGE is one
    rendered step.
    - locate(slide=N) → {slide_no, pages:[...], slide_line (opener), slide_end (closing `]`),
      section, sub_total, note_lines}.
    - locate(page=N) → {page, slide_no, slide_line, slide_end, section, sub_index, sub_total,
      sub_lines (the source lines that drive THIS page), note_line, note_raw}.

    Then edit the reported range with apply_edits `{"by":"lines", start, end}` (or read it with
    get_document(offset=slide_line)). Requires the deck to be compiling; on a compile error the
    map may be stale.""" + " " + _GUARD
    if bool(page) == bool(slide):
        return {"ok": False, "error": "pass EXACTLY ONE of page or slide (1-based)"}
    q = f"page={page}" if page else f"slide={slide}"
    return _backend("GET", f"/api/locate?{q}")


# ----------------------------------------------------------------- document edits
def _fetch_source(file: str):
    """Return (source_text, file_label, rev) from the live backend, or (None, error_dict, 0)."""
    query = f"?{urllib.parse.urlencode({'file': file})}" if file else ""
    r = _backend("GET", f"/api/document{query}")
    src = r.get("source")
    if not isinstance(src, str):
        return None, r, 0
    return src, r.get("file"), r.get("rev", 0)


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
    know whether more remains. Line numbers identify the current snapshot only; when using
    line selectors, pass the reported `rev` as `base_rev` and re-read after any conflict.
    Prefer exact anchors plus `expect` when the target text is distinctive. """ + _GUARD
    src, label, rev = _fetch_source(file)
    if src is None:
        return label  # error dict from the backend
    lines = src.split("\n")
    total = len(lines)
    start = max(1, offset)
    if start > total:
        return {"file": label, "total_lines": total, "shown": None,
                "text": "", "truncated": False, "rev": rev,
                "note": f"offset {start} is past end of file ({total} lines)."}
    win = HEAD_LINES if limit <= 0 else min(limit, MAX_LINES)
    end = min(total, start + win - 1)
    numbered = "\n".join(f"{i:>5}\t{ln}" for i, ln in zip(range(start, end + 1), lines[start - 1:end]))
    more = end < total
    return {
        "file": label,
        "total_lines": total,
        "rev": rev,
        "chars": len(src),
        "shown": f"{start}-{end}",
        "truncated": more,
        "next": (f"more below — read on with get_document(offset={end + 1})" if more else None),
        "text": numbered,
        "hint": "Read windows, not the whole file. Prefer apply_edits with exact anchors and "
                "`expect`; line selectors are supported for the current snapshot when paired "
                "with its `rev`. " + _GUARD,
    }


@mcp.tool()
def find_in_document(query: str, file: str = "", max_hits: int = 40) -> dict:
    """Search the LIVE shared document for a literal substring and return each matching
    line's number and full text — the fast way to navigate a large deck WITHOUT dumping it.

    Use this to locate an anchor or a region before reading or editing: find the line, then
    get_document(offset=<line>) to read its window, then edit with the anchor tools. The
    match is a plain case-sensitive substring (not a regex). Returns up to `max_hits`
    matches (default 40); if `matches` exceeds that, narrow the query. """ + _GUARD
    src, label, _rev = _fetch_source(file)
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
def apply_edits(edits: list, base_rev: int = 0, file: str = "") -> dict:
    """Apply a BATCH of edits to the live shared document ATOMICALLY — all succeed or none do.
    This is the most robust way to make several changes at once (e.g. "split this slide into
    two", "rename a term everywhere"): every edit is located against the SAME snapshot and they
    apply together, so no edit invalidates another's anchor and you never see a half-applied
    state. Prefer this over a sequence of single edits.

    `edits` is a list; each item is `{"selector": <Selector>, "text": <str>, "expect"?: <str>}`
    where `text` is the replacement ("" to delete) and a Selector is ONE of:
      - {"by": "anchor", "text": "<exact unique snippet>", "occurrence"?: 1,
         "side"?: "in"|"before"|"after"}   // "in" replaces the snippet; before/after insert
      - {"by": "lines", "start": <1-based>, "end"?: <1-based inclusive>}  // end omitted = insert at that line
      - {"by": "range", "from": <cp>, "to": <cp>}                          // code-point offsets (escape hatch)
    Optional `expect` is a compare-and-swap: the edit applies only if the selected span still
    equals `expect`, else the whole batch is refused as a conflict — use it to guard against the
    human editing concurrently. Pass `base_rev` = the `rev` you got from get_document.

    On success: {ok:true, rev, applied}. On conflict: {ok:false, conflict:true, index, error,
    and the live `context` around the miss} — re-read with get_document (note the new `rev`) and
    retry. Selectors are resolved against the CURRENT document, so a prior edit that removed your
    target makes the next selector fail cleanly rather than hitting the wrong place.""" + " " + _GUARD
    return _backend("POST", "/api/edit", {
        "op": "apply_edits", "edits": edits, "base_rev": base_rev or None, "file": file or None,
    })


@mcp.tool()
def replace_anchor(anchor: str, new_text: str, occurrence: int = 1, file: str = "") -> dict:
    """Replace `anchor` with `new_text` in the live shared document — single-edit shorthand for
    apply_edits with one anchor selector. For SEVERAL related changes use apply_edits instead
    (atomic, and immune to one edit invalidating the next one's anchor).

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


@mcp.tool()
def replace_lines(start: int, end: int, new_text: str, file: str = "") -> dict:
    """Replace the whole lines `start`..`end` (1-based, INCLUSIVE) with `new_text` — the same
    line numbers get_document prints. This is the PREFERRED tool for a multi-line rewrite
    (e.g. "split this slide into two", "rewrite this block"): it needs no exact anchor and no
    escaping, so it never fails with "anchor not found" over a stray whitespace or `\\\\`.

    Read the target first with get_document to get the line numbers. `new_text` may contain
    any number of lines; a trailing newline is added for you if missing. After this edit the
    line numbers shift, so re-read (get_document) before the next line-addressed edit rather
    than reusing stale numbers. Returns the backend ok/error result."""
    return _backend("POST", "/api/edit", {
        "op": "replace_lines", "start": start, "end": end,
        "new_text": new_text, "file": file or None,
    })


@mcp.tool()
def insert_at_line(line: int, text: str, file: str = "") -> dict:
    """Insert `text` so it BECOMES line `line` (1-based), pushing the current line `line` and
    everything after it down. A `line` past the end of the file appends at the end. Line
    numbers match get_document. `text` may be multiple lines; a trailing newline is added for
    you if missing. Prefer this over insert_before/after_anchor when you already know the line
    number, since it needs no exact anchor. Re-read with get_document before the next
    line-addressed edit (numbers shift). Returns the backend ok/error result."""
    return _backend("POST", "/api/edit", {
        "op": "insert_at_line", "line": line, "text": text, "file": file or None,
    })


if __name__ == "__main__":
    mcp.run()
