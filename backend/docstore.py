"""The shared document layer: one CRDT (Yjs/pycrdt) room per .typ file.

The Y.Doc is the single source of truth for a file's text. Browsers edit it over a
WebSocket (y-websocket protocol); Claude edits it via HTTP -> the FastAPI process ->
this module (same process as the room, which is required: a separate process cannot
touch the in-memory server). Every edit, whoever makes it, is broadcast to all
connected browsers automatically and debounced-persisted to the .typ on disk.

Verified against pycrdt 0.14.1 / pycrdt-websocket 0.16.3 (import path `pycrdt.websocket`).
"""
import asyncio
import sys
from pathlib import Path

from anyio import Lock
from pycrdt import Assoc, Channel, StickyIndex, Text
from pycrdt.websocket import WebsocketServer, exception_logger

import runtime

# Shared-type key. MUST equal the frontend's ydoc.getText(TEXT_KEY).
TEXT_KEY = "source"
WRITE_DELAY = 0.2  # seconds to debounce Doc -> .typ writes (then typst watch ~12ms)
# Per-file Yjs state is persisted here so a restart RESUMES THE SAME CRDT lineage.
# This is critical: re-seeding a fresh doc from the .typ on every restart creates a NEW
# lineage, and a browser reconnecting with the old lineage makes Yjs *concatenate* the
# two identical copies (O + O = OO) instead of deduping — which once ballooned a deck to
# 512x. Same lineage merges cleanly.
CACHE_DIR = Path.home() / ".tcb" / "docs"


def _sidecar(key: str) -> Path:
    return CACHE_DIR / f"{key}.ydoc"


# Room generation per file. On corruption we ROTATE (bump the generation) so the room
# name changes; the frontend reconnects with a FRESH Yjs doc, orphaning the poisoned
# lineage (a stale browser tab can't keep re-merging its corruption into a live room).
_gen: dict[str, int] = {}


def _base_key(file=None) -> str:
    # Derive the room identity from the SAME absolute path `_resolve` writes to on disk.
    # `runtime.file_key` resolves a *relative* path (e.g. MCP's "main.typ") against the
    # process CWD, while `_resolve` resolves it against the project dir — so passing the
    # raw arg here made a relative-path MCP edit land in a DIFFERENT room than the
    # browser's absolute-path room, yet both flushed to the same file (the "split-brain
    # room" revert bug). Resolving first collapses them to one room.
    return runtime.file_key(_resolve(file))


def room_name(file=None) -> str:
    base = _base_key(file)
    g = _gen.get(base, 0)
    return f"{base}~g{g}" if g else base


def rotate(file=None) -> str:
    base = _base_key(file)
    _gen[base] = _gen.get(base, 0) + 1
    return room_name(file)

server: WebsocketServer | None = None
_lifecycle_lock = Lock()

_loop: asyncio.AbstractEventLoop | None = None
_rooms: dict[str, dict] = {}   # room_key -> state
_latest: dict[str, str] = {}   # room_key -> last text snapshot (sync-readable)
# Set to a loop timestamp whenever the active file is written, so the app can
# auto-recompile the slides after edits settle (live preview sync).
dirty_since: float | None = None
external_edit_seq: int = 0


def is_running() -> bool:
    """Whether this process currently owns a live CRDT websocket server."""
    return server is not None


async def start() -> WebsocketServer:
    """Start one fresh server on demand. A stopped WebsocketServer is never reused."""
    global server
    async with _lifecycle_lock:
        if server is not None:
            return server
        fresh = WebsocketServer(auto_clean_rooms=False, exception_handler=exception_logger)
        try:
            await fresh.__aenter__()
            set_loop(asyncio.get_running_loop())
        except Exception:
            try:
                await fresh.__aexit__(None, None, None)
            except Exception:
                pass
            raise
        server = fresh
        return fresh


async def stop() -> None:
    """Cancel every room/timer and discard the server before a PDF transition or shutdown."""
    global server, _loop, dirty_since
    async with _lifecycle_lock:
        active = server
        server = None
        stale = list(_rooms.values())
        # Every lineage retired by a PDF transition needs a new room key on its next Typst
        # activation. A base can have multiple stale generations, but it advances exactly
        # once: otherwise the next valid key would depend on incidental stale-room count.
        for base in {st.get("base") for st in stale if st.get("base") is not None}:
            _gen[base] = _gen.get(base, 0) + 1
        _rooms.clear()
        _latest.clear()
        _loop = None
        dirty_since = None
        for st in stale:
            st["closed"] = True
            timer = st.get("timer")
            if timer:
                timer.cancel()
            try:
                st["doc"].unobserve(st.get("sub"))
            except Exception:
                pass
        if active is not None:
            try:
                await active.__aexit__(None, None, None)
            except RuntimeError:
                # A test/deliberate partial lifecycle may provide an unstarted server.
                pass


def _resolve(file: str | Path | None) -> Path:
    """An absolute .typ path: the current file by default, else `file` (relative paths
    are resolved against the current project dir)."""
    if not file:
        return runtime.current_file()
    p = Path(file)
    if not p.is_absolute():
        p = runtime.project_dir() / p
    return p.expanduser().resolve()


# --------------------------------------------------------------- starlette bridge
class StarletteYChannel(Channel):
    """Adapts a Starlette WebSocket to the pycrdt Channel protocol."""

    def __init__(self, ws, path: str):
        self._ws = ws
        self._path = path
        self._lock = Lock()

    @property
    def path(self) -> str:
        return self._path

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self.recv()
        except Exception:
            raise StopAsyncIteration()

    async def send(self, message: bytes) -> None:
        async with self._lock:
            await self._ws.send_bytes(message)

    async def recv(self) -> bytes:
        return bytes(await self._ws.receive_bytes())


# --------------------------------------------------------------- room lifecycle
def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def path_for_key(key: str) -> Path | None:
    # Only the currently issued generation may address the active file. In particular, an
    # old browser reconnect must not recreate its retired lineage, and an invented future
    # generation must never become a route to the current document.
    current = runtime.current_file()
    base = runtime.file_key(current)
    return current if key == _cur_room(base) else None


async def ensure_room(file: str | Path | None = None, key: str | None = None) -> dict:
    path = _resolve(file)
    if key is None:
        key = room_name(file)
    if key in _rooms:
        return _rooms[key]
    active = await start()
    room = await active.get_room(key)
    doc = room.ydoc
    text = doc.get(TEXT_KEY, type=Text)
    # Restore the persisted CRDT lineage if we have one; only seed from the .typ the
    # very first time (no sidecar yet). NEVER re-insert text into a doc that clients sync.
    # Seed a freshly-created room from the DISK .typ — the single source of truth (it's what
    # the resolver renders and the version system saves). We deliberately DO NOT persist or
    # restore a CRDT sidecar (.ydoc): a stale sidecar, or a stale browser tab merging back into
    # the room, is exactly what made the editor load and CYCLE through historical titles when
    # switching projects. The in-memory room handles the live session; every cold load trusts
    # disk. (rooms already in `_rooms` are returned earlier, so `text` is empty here.)
    disk = path.read_text(encoding="utf-8") if path.exists() else ""
    if disk and str(text) != disk:
        with doc.transaction():
            if len(text):
                del text[0:len(text)]   # len(text) = UTF-8 byte length — correct for CJK
            text.insert(0, disk)
    _latest[key] = str(text)
    st = {
        "key": key, "base": _base_key(file), "room": room, "doc": doc, "text": text,
        "path": path, "timer": None, "writeback": False, "poisoned": False, "rev": 0,
        "last_mtime": path.stat().st_mtime if path.exists() else 0,
        "external_guard_old": None, "external_guard_new": None, "external_guard_until": 0,
    }

    def on_change(_event, st=st):
        # Observe at the DOC level, not the Text level: in this pycrdt build a
        # type-level observer fires only for local edits, while a doc-level observer
        # fires for both local and remote (websocket apply_update) changes. The
        # callback runs *inside* the transaction, so reading str(text) here would open
        # a nested transaction — defer the snapshot + persist onto the loop.
        if _loop is not None:
            _loop.call_soon_threadsafe(_sync, st)

    st["sub"] = doc.observe(on_change)
    _rooms[key] = st
    return st


def _sync(st: dict) -> None:
    if st.get("closed"):
        return
    try:
        current = str(st["text"])
    except Exception:
        return
    if _loop is not None and _loop.time() < st.get("external_guard_until", 0):
        if current == st.get("external_guard_old") and st.get("external_guard_new") is not None:
            _replace_text(st, st["external_guard_new"])
            _latest[st["key"]] = st["external_guard_new"]
            _schedule(st)
            return
    previous = _latest.get(st["key"])
    if previous is not None and current != previous:
        # Browser/websocket mutations arrive here without passing through apply_edits(). Count
        # each distinct synchronized snapshot so document revisions describe every writer.
        # apply_edits refreshes _latest before its deferred observer runs, avoiding a double bump.
        st["rev"] = st.get("rev", 0) + 1
    _latest[st["key"]] = current
    if not st["writeback"]:
        _schedule(st)


async def ensure_room_by_key(key: str) -> dict | None:
    path = path_for_key(key)
    return await ensure_room(path, key=key) if path else None


def _schedule(st: dict) -> None:
    if st.get("closed") or _loop is None:
        return
    if st["timer"]:
        st["timer"].cancel()
    st["timer"] = _loop.call_later(WRITE_DELAY, _commit, st)


def _commit(st: dict) -> None:
    if st.get("closed"):
        return
    _flush(st)     # .typ on disk (guarded) — the disk file is the only persisted state now


def _refresh(st: dict) -> None:
    """Update the sync-readable snapshot and persist now (call right after an edit,
    on the loop, with the transaction already closed)."""
    _latest[st["key"]] = str(st["text"])
    _commit(st)


def _replace_text(st: dict, new_text: str) -> None:
    text = st["text"]
    with st["doc"].transaction():
        n = len(text)
        if n:
            del text[0:n]
        if new_text:
            text.insert(0, new_text)


def _guard_external_edit(st: dict, old_text: str) -> None:
    """Briefly reject an exact stale-client replay of the pre-edit document."""
    if _loop is None:
        return
    st["external_guard_old"] = old_text
    st["external_guard_new"] = str(st["text"])
    st["external_guard_until"] = _loop.time() + 5.0


def _persist(st: dict) -> None:
    try:
        side = _sidecar(st["key"])
        side.parent.mkdir(parents=True, exist_ok=True)
        tmp = side.with_suffix(".tmp")
        tmp.write_bytes(st["doc"].get_update())
        tmp.replace(side)
    except Exception:
        pass


def _cur_room(base: str) -> str:
    g = _gen.get(base, 0)
    return f"{base}~g{g}" if g else base


def _flush(st: dict) -> None:
    global dirty_since
    try:
        if st.get("closed"):
            return
        base = st.get("base")
        if base is not None and st["key"] != _cur_room(base):
            return  # orphaned room (rotated away after a poison) — never touches disk
        new = str(st["text"])
        p = st["path"]
        cur = p.read_text(encoding="utf-8") if p.exists() else ""
        if cur == new:
            return  # no-op write
        # Circuit breaker (corruption guard) — only for CATASTROPHIC corruption, kept very
        # conservative so it NEVER fires on a real edit (which would wrongly discard work and
        # rotate the room, wiping undo). Two signals that only a whole-lineage merge produces:
        #   - the document's opening line now appears MORE times than before (dup'd header)
        #   - the file MORE THAN DOUBLED in a single write
        # (We deliberately do NOT use line-tail-duplication heuristics here: a slide deck
        #  reuses boilerplate lines everywhere, so any such heuristic false-positives on
        #  legitimate inserts/restores — which is exactly what broke insert + undo before.)
        if len(cur) > 200:
            first = next((ln for ln in cur.splitlines() if ln.strip()), "")
            sig_dup = len(first) >= 8 and new.count(first) > cur.count(first)
            size_blow = len(new) > len(cur) * 1.5 + 4096
            if sig_dup or size_blow:
                print(f"[docstore] BLOCKED write to {p.name}: {len(cur)}->{len(new)} chars "
                      f"(sig_dup={sig_dup} size_blow={size_blow}). "
                      f"File left untouched.", file=sys.stderr, flush=True)
                # Poisoned (a stale client merged a corrupted lineage). ROTATE the room:
                # the frontend will reconnect with a fresh doc and the bad lineage is
                # orphaned. Never persist the corrupted lineage.
                st["poisoned"] = True
                try:
                    _sidecar(st["key"]).unlink()
                except FileNotFoundError:
                    pass
                if base is not None:
                    _gen[base] = _gen.get(base, 0) + 1
                    print(f"[docstore] rotated room for {p.name} -> gen {_gen[base]}",
                          file=sys.stderr, flush=True)
                return
        # atomic write (tmp + replace) so a crash mid-write can't truncate the file
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(new, encoding="utf-8")
        tmp.replace(p)
        st["last_mtime"] = p.stat().st_mtime
        # flag for the app's auto-recompile loop (only for the active file)
        if _loop is not None and str(p) == str(runtime.current_file()):
            dirty_since = _loop.time()
    except Exception:
        pass


async def flush_now(file: str | Path | None = None) -> None:
    st = await ensure_room(file)
    _flush(st)


def get_text(file: str | Path | None = None) -> str | None:
    """Sync snapshot of the live document (safe to call from sync endpoints). Reads the
    CURRENT room (post-rotation), never an orphaned/poisoned one; falls back to disk."""
    key = room_name(file)
    if key in _latest:
        return _latest[key]
    path = _resolve(file)
    return path.read_text(encoding="utf-8") if path.exists() else None


def _mark_external_edit() -> int:
    """Monotonic counter for edits initiated outside the browser editor.

    Browser edits normally arrive over the websocket and already update CodeMirror. MCP/API
    edits also broadcast through Yjs, but exposing this counter gives the frontend a cheap
    polling fallback: if a websocket update is missed, remounting the editor resyncs it from
    the authoritative backend room.
    """
    global external_edit_seq
    external_edit_seq += 1
    return external_edit_seq


# --------------------------------------------------------------- edits (async, on loop)
# CRITICAL: pycrdt's Text is indexed by UTF-8 BYTE offsets (len(text) == UTF-8 byte length),
# while Python str.find()/len() work in Unicode CODE POINTS. Any multibyte char before an
# edit point (em dash "—", "▶", "·", CJK, emoji, …) makes the two diverge, so we MUST convert
# code-point offsets to byte offsets before indexing the Text — otherwise the edit lands at
# the wrong place (or panics mid-character) and corrupts the document.
def _cp_to_byte(s: str, cp: int) -> int:
    return len(s[:cp].encode("utf-8"))


def _byte_to_cp(s: str, b: int) -> int:
    """Inverse of _cp_to_byte: a pycrdt UTF-8 byte index -> a Unicode code-point offset."""
    return len(s.encode("utf-8")[:b].decode("utf-8", "ignore"))


def _bytelen(s: str) -> int:
    return len(s.encode("utf-8"))


def _line_starts(s: str) -> list[int]:
    """Code-point offset of the start of each 1-based line. len() == number of lines the
    way get_document counts them (a trailing newline yields a final empty line N+1), so
    these indices line up with what the agent sees in get_document output."""
    offs = [0]
    for i, ch in enumerate(s):
        if ch == "\n":
            offs.append(i + 1)
    return offs


def _find(s: str, needle: str, occurrence: int) -> int:
    idx, start = -1, 0
    for _ in range(max(1, occurrence)):
        idx = s.find(needle, start)
        if idx < 0:
            return -1
        start = idx + 1
    return idx


# ============================================================= unified edit primitive
# Every mutation is `del[span] + insert(text)`; only HOW the span is named varies. So there
# is ONE operation — apply_edits — over a tagged-union Selector, applied as an atomic,
# all-or-nothing BATCH with an optional per-edit compare-and-swap (`expect`). This is what
# makes consecutive AND within-batch edits safe: every selector in a batch resolves against
# the SAME snapshot and they apply together, so no edit invalidates another's anchor, and a
# stale `expect` yields a clean conflict instead of a wrong-place edit. Selectors:
#   {"by":"anchor","text":..,"occurrence":1,"side":"in"|"before"|"after"}
#   {"by":"lines","start":i,"end":j?}   # end omitted => insertion point at the start of line i
#   {"by":"range","from":a,"to":b}      # code-point offsets (escape hatch)
# The legacy single-edit tools below are thin sugar over this one primitive.
def _resolve_selector(sel: dict, s: str):
    """Resolve a selector against snapshot `s` -> (from_cp, to_cp, kind, err). Insertion
    points return from_cp == to_cp."""
    kind = (sel or {}).get("by")
    if kind == "anchor":
        anchor = sel.get("text") or sel.get("anchor") or ""
        occ = sel.get("occurrence", 1)
        if isinstance(occ, bool) or not isinstance(occ, int):
            return None, None, kind, "occurrence must be an integer >= 1"
        if not isinstance(anchor, str):
            return None, None, kind, "anchor must be a string"
        if not anchor:
            return None, None, kind, "empty anchor"
        n = s.count(anchor)
        if n == 0:
            return None, None, kind, "anchor not found"
        if not 1 <= occ <= n:
            return None, None, kind, f"occurrence out of bounds ({n} matches)"
        if occ == 1 and n > 1:
            return None, None, kind, (f"anchor is ambiguous ({n} matches); pass a longer "
                                      "anchor or `occurrence`")
        idx = _find(s, anchor, occ)
        side = sel.get("side", "in")
        if not isinstance(side, str):
            return None, None, kind, "anchor side must be a string"
        if side not in {"in", "before", "after"}:
            return None, None, kind, f"unknown anchor side {side!r}"
        if idx < 0:
            return None, None, kind, "anchor occurrence not found"
        if side == "before":
            return idx, idx, kind, None
        if side == "after":
            return idx + len(anchor), idx + len(anchor), kind, None
        return idx, idx + len(anchor), kind, None            # "in" = replace the anchor span
    if kind == "lines":
        offs = _line_starts(s)
        total = len(offs)
        start = sel.get("start")
        end = sel.get("end")
        if isinstance(start, bool) or not isinstance(start, int):
            return None, None, kind, "line must be an integer >= 1"
        if end is not None and (isinstance(end, bool) or not isinstance(end, int)):
            return None, None, kind, "line end must be an integer"
        if start < 1:
            return None, None, kind, "line must be >= 1"
        if end is None:                                      # insertion point
            at = offs[start - 1] if start <= total else len(s)   # a line past EOF appends
            return at, at, kind, None
        if not (start <= end <= total):
            return None, None, kind, f"line range out of bounds (doc has {total} lines)"
        frm = offs[start - 1]
        to = offs[end] if end < total else len(s)
        return frm, to, kind, None
    if kind == "range":
        frm = sel.get("from")
        to = sel.get("to")
        n = len(s)
        if (isinstance(frm, bool) or not isinstance(frm, int)
                or isinstance(to, bool) or not isinstance(to, int)):
            return None, None, kind, "range offsets must be integers"
        if not (0 <= frm <= to <= n):
            return None, None, kind, f"range out of bounds (doc len {n})"
        return frm, to, kind, None
    return None, None, kind, f"unknown selector {kind!r}"


def _neighborhood(s: str, frm: int, to: int, pad: int = 80) -> str:
    """A slice of the live text around [frm,to) so a conflicting caller can re-aim in one round."""
    return s[max(0, frm - pad):min(len(s), to + pad)]


def _selector_neighborhood(s: str, selector: dict) -> str:
    """Best-effort live context even when a malformed selector has no resolvable span."""
    kind = selector.get("by")
    if kind == "anchor":
        anchor = selector.get("text") or selector.get("anchor")
        if isinstance(anchor, str) and anchor:
            at = s.find(anchor)
            if at >= 0:
                return _neighborhood(s, at, at + len(anchor))
    elif kind == "lines":
        start = selector.get("start")
        if isinstance(start, int) and not isinstance(start, bool):
            offsets = _line_starts(s)
            at = offsets[min(max(start - 1, 0), len(offsets) - 1)]
            return _neighborhood(s, at, at)
    elif kind == "range":
        frm = selector.get("from")
        to = selector.get("to")
        if all(isinstance(value, int) and not isinstance(value, bool) for value in (frm, to)):
            a = min(max(frm, 0), len(s))
            b = min(max(to, a), len(s))
            return _neighborhood(s, a, b)
    return s[:160]


def _line_newline_fixup(kind: str, s: str, frm: int, to: int, txt: str) -> str:
    """Keep whole-line structure for `lines` edits: a line replacement/insertion ends on its
    own line (and an append past EOF starts on one)."""
    if kind != "lines" or not txt:
        return txt
    if to > frm:                                             # replacing whole lines
        if s[frm:to].endswith("\n") and not txt.endswith("\n"):
            txt += "\n"
    else:                                                    # insertion point
        if frm >= len(s) and s and not s.endswith("\n"):
            txt = "\n" + txt
        if not txt.endswith("\n"):
            txt += "\n"
    return txt


def _selector_of(e: dict) -> dict:
    return e.get("selector") or {k: e[k] for k in
                                 ("by", "text", "anchor", "occurrence", "side",
                                  "start", "end", "from", "to") if k in e}


def _spans_overlap(a1: int, b1: int, a2: int, b2: int) -> bool:
    """Whether two snapshot edit spans conflict.

    Multiple insertions at one point are ordered and valid. An insertion at the start or inside
    a replaced span is ambiguous and rejected; an insertion at its end is an adjacent edit.
    """
    point1 = a1 == b1
    point2 = a2 == b2
    if point1 and point2:
        return False
    if point1:
        return a2 <= a1 < b2
    if point2:
        return a1 <= a2 < b1
    return max(a1, a2) < min(b1, b2)


async def apply_edits(edits: list, file=None, base_rev: int | None = None) -> dict:
    """Apply a BATCH of edits atomically (all-or-nothing) against the current room text. Each
    edit = {"selector": <Selector>, "text": str, "expect"?: str}. On any unresolved selector,
    `expect` mismatch, or intra-batch overlap, NOTHING is applied and a conflict (with the live
    neighborhood + current `rev`) is returned so the caller can re-read and retry. `base_rev`
    is advisory (reported back as `rebased`); real safety comes from per-edit `expect`."""
    st = await ensure_room(file)
    text = st["text"]
    s = str(text)
    cur_rev = st.get("rev", 0)
    if not isinstance(edits, list):
        return {"ok": False, "conflict": True, "error": "edits must be a list",
                "rev": cur_rev, "room": st["key"]}
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict):
            return {"ok": False, "conflict": True, "index": i,
                    "error": "each edit must be an object", "rev": cur_rev, "room": st["key"]}
        selector = edit.get("selector")
        if selector is not None and not isinstance(selector, dict):
            return {"ok": False, "conflict": True, "index": i,
                    "error": "selector must be an object", "rev": cur_rev, "room": st["key"]}
        if not isinstance(edit.get("text", ""), str):
            return {"ok": False, "conflict": True, "index": i,
                    "error": "replacement text must be a string",
                    "rev": cur_rev, "room": st["key"]}
        if "expect" in edit and not isinstance(edit["expect"], str):
            return {"ok": False, "conflict": True, "index": i,
                    "error": "expect must be a string",
                    "rev": cur_rev, "room": st["key"]}

    stale = base_rev is not None and base_rev != cur_rev
    resolved = []                                            # (from_cp, to_cp, txt, idx)
    for i, e in enumerate(edits or []):
        selector = _selector_of(e)
        frm, to, kind, err = _resolve_selector(selector, s)
        if err:
            return {"ok": False, "conflict": True, "index": i, "error": err,
                    "context": _selector_neighborhood(s, selector),
                    "rev": cur_rev, "room": st["key"]}
        if stale and kind in {"lines", "range"} and "expect" not in e:
            return {"ok": False, "conflict": True, "index": i,
                    "error": "stale base_rev requires expect for positional selectors",
                    "context": _neighborhood(s, frm, to),
                    "rev": cur_rev, "room": st["key"]}
        expect = e.get("expect")
        if expect is not None and s[frm:to] != expect:
            return {"ok": False, "conflict": True, "index": i, "error": "expect mismatch",
                    "found": s[frm:to], "context": _neighborhood(s, frm, to),
                    "rev": cur_rev, "room": st["key"]}
        txt = _line_newline_fixup(kind, s, frm, to, e.get("text", "") or "")
        # Deleting the last line of a file with NO trailing newline: also consume the
        # preceding newline, else a phantom empty line is left behind (e.g. "a\nb\nc" -> "a\nb").
        if (kind == "lines" and not txt and not s.endswith("\n")
                and to >= len(s) and frm > 0 and s[frm - 1] == "\n"):
            frm -= 1
        resolved.append((frm, to, txt, i))
    ordered = sorted(resolved, key=lambda r: (r[0], r[1]))
    for (a1, b1, *_), (a2, b2, *_) in zip(ordered, ordered[1:]):
        if _spans_overlap(a1, b1, a2, b2):
            return {"ok": False, "conflict": True, "error": "overlapping edits in batch",
                    "rev": cur_rev, "room": st["key"]}
    if resolved:
        # Apply highest-offset-first so the earlier (lower-offset) byte positions stay valid.
        # For edits at the SAME offset, apply LAST input first so their text ends up in input
        # order (each insert pushes prior ones right) — else two inserts at one point swap.
        with st["doc"].transaction():
            for frm, to, txt, _idx in sorted(resolved, key=lambda r: (r[0], r[3]), reverse=True):
                fb = _cp_to_byte(s, frm)
                tb = _cp_to_byte(s, to)
                if tb > fb:
                    del text[fb:tb]
                if txt:
                    text.insert(fb, txt)
        st["rev"] = cur_rev + 1
        _guard_external_edit(st, s)
        _refresh(st)
    return {"ok": True, "rev": st["rev"], "applied": len(resolved),
            "rebased": stale,
            "external_edit_seq": _mark_external_edit(), "room": st["key"]}


def get_rev(file=None) -> int:
    st = _rooms.get(room_name(file))
    return st.get("rev", 0) if st else 0


# --------------------------------------------------------------- single-edit sugar
async def replace_anchor(anchor: str, new_text: str, file=None, occurrence: int = 1) -> dict:
    return await apply_edits([{"selector": {"by": "anchor", "text": anchor,
                              "occurrence": occurrence, "side": "in"}, "text": new_text}], file)


async def insert_relative(anchor: str, payload: str, where: str = "after", file=None,
                          occurrence: int = 1) -> dict:
    return await apply_edits([{"selector": {"by": "anchor", "text": anchor,
                              "occurrence": occurrence, "side": where}, "text": payload}], file)


async def replace_range(frm: int, to: int, new_text: str, file=None) -> dict:
    return await apply_edits([{"selector": {"by": "range", "from": frm, "to": to},
                               "text": new_text}], file)


async def reset_from_disk(file=None) -> dict:
    """Discard the CRDT lineage and re-seed from the .typ on disk (for picking up an
    external edit). Safe only when called deliberately — it starts a NEW lineage, so any
    still-connected browser should reload the page afterward."""
    path = _resolve(file)
    key = runtime.file_key(path)
    st = _rooms.get(key)
    disk = path.read_text(encoding="utf-8") if path.exists() else ""
    if st is None:
        try:
            _sidecar(key).unlink()
        except FileNotFoundError:
            pass
        await ensure_room(path)
        return {"ok": True}
    with st["doc"].transaction():
        old = str(st["text"])
        n = len(st["text"])
        if n:
            del st["text"][0:n]
        if disk:
            st["text"].insert(0, disk)
    _guard_external_edit(st, old)
    _refresh(st)
    return {"ok": True, "external_edit_seq": _mark_external_edit(), "room": st["key"]}


async def insert_text(at: int, payload: str, file=None) -> dict:
    return await apply_edits([{"selector": {"by": "range", "from": at, "to": at},
                               "text": payload}], file)


# ------------------------------------------------------------- line-addressed edits
# Content anchors are fragile once the agent has made a prior edit (the copied snippet goes
# stale — see notes/workbook-bugfix-0709.md). Line-addressed edits sidestep quoting and
# escaping entirely: the agent edits by the same 1-based line numbers get_document prints,
# and each call is applied atomically to the CURRENT room text (no offset drift between
# calls). Lines are 1-based and inclusive, matching get_document.
async def replace_lines(start: int, end: int, new_text: str, file=None) -> dict:
    return await apply_edits([{"selector": {"by": "lines", "start": start, "end": end},
                               "text": new_text}], file)


async def insert_at_line(line: int, payload: str, file=None) -> dict:
    """Insert `payload` so it BECOMES line `line` (pushing the old line `line` down). A
    `line` past the end appends at EOF."""
    return await apply_edits([{"selector": {"by": "lines", "start": line}, "text": payload}], file)


# ------------------------------------------------------------- drift-proof anchors
# A comment's anchor stored as a code-point span goes stale the moment ANYONE (human or
# agent) edits above it. A pycrdt StickyIndex (the Yjs RelativePosition) is bound to the
# CHARACTER's CRDT identity, not its offset, so it follows the text across inserts/deletes
# by either party and converges on every replica. We store its JSON at comment-create time
# and resolve it back to a live span on read.
def _safe_sticky_index(text, s: str, byte_off: int, prefer):
    """Create a StickyIndex without tripping pycrdt's Rust panic. `Assoc.AFTER` has no
    successor at end-of-document (or on an empty doc) and panics there — and that panic is a
    BaseException that slips past `except Exception`, so it would 500 comment creation. Bind
    with `Assoc.BEFORE` at/after EOF, and belt-and-suspenders catch BaseException."""
    blen = _bytelen(s)
    off = max(0, min(byte_off, blen))
    assoc = Assoc.BEFORE if off >= blen else prefer
    try:
        return text.sticky_index(off, assoc)
    except BaseException:
        return None


async def make_rel_anchors(spans, file=None) -> list:
    """spans: [[from_cp, to_cp], ...] -> [{from, to}, ...] StickyIndex JSON, drift-proof."""
    st = await ensure_room(file)
    text = st["text"]
    s = str(text)
    out = []
    with st["doc"].transaction():
        for span in spans or []:
            frm = max(0, min(int(span[0]), len(s)))
            to = max(0, min(int(span[1]), len(s)))
            si_from = _safe_sticky_index(text, s, _cp_to_byte(s, frm), Assoc.AFTER)
            si_to = _safe_sticky_index(text, s, _cp_to_byte(s, to), Assoc.BEFORE)
            if si_from is None or si_to is None:
                continue
            out.append({"from": si_from.to_json(), "to": si_to.to_json()})
    return out


async def resolve_rel_anchors(rel, file=None) -> list:
    """Inverse of make_rel_anchors: [{from, to}, ...] -> current [[from_cp, to_cp], ...]."""
    st = await ensure_room(file)
    text = st["text"]
    s = str(text)
    spans = []
    with st["doc"].transaction() as txn:
        for a in rel or []:
            try:
                si_from = StickyIndex.from_json(a["from"], text)
                si_to = StickyIndex.from_json(a["to"], text)
                fb = si_from.get_index(txn)
                tb = si_to.get_index(txn)
            except BaseException:
                continue
            if fb is None or tb is None:
                continue
            if fb > tb:                      # a caret/zero-width anchor can cross after edits
                fb, tb = tb, fb
            spans.append([_byte_to_cp(s, fb), _byte_to_cp(s, tb)])
    return spans
