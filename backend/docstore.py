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
from pycrdt import Channel, Text
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
    return runtime.file_key(file or runtime.current_file())


def room_name(file=None) -> str:
    base = _base_key(file)
    g = _gen.get(base, 0)
    return f"{base}~g{g}" if g else base


def _base_of(room: str) -> str:
    return room.split("~g", 1)[0]


def rotate(file=None) -> str:
    base = _base_key(file)
    _gen[base] = _gen.get(base, 0) + 1
    return room_name(file)

server = WebsocketServer(auto_clean_rooms=False, exception_handler=exception_logger)

_loop: asyncio.AbstractEventLoop | None = None
_rooms: dict[str, dict] = {}   # room_key -> state
_latest: dict[str, str] = {}   # room_key -> last text snapshot (sync-readable)
# Set to a loop timestamp whenever the active file is written, so the app can
# auto-recompile the slides after edits settle (live preview sync).
dirty_since: float | None = None


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
    if key in _rooms:
        return _rooms[key]["path"]
    if _base_of(key) == runtime.file_key(runtime.current_file()):
        return runtime.current_file()
    return None


async def ensure_room(file: str | Path | None = None, key: str | None = None) -> dict:
    path = _resolve(file)
    if key is None:
        key = room_name(file)
    if key in _rooms:
        return _rooms[key]
    room = await server.get_room(key)
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
        "path": path, "timer": None, "writeback": False, "poisoned": False,
        "last_mtime": path.stat().st_mtime if path.exists() else 0,
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
    try:
        _latest[st["key"]] = str(st["text"])
    except Exception:
        return
    if not st["writeback"]:
        _schedule(st)


async def ensure_room_by_key(key: str) -> dict | None:
    path = path_for_key(key)
    return await ensure_room(path, key=key) if path else None


def _schedule(st: dict) -> None:
    if st["timer"]:
        st["timer"].cancel()
    st["timer"] = _loop.call_later(WRITE_DELAY, _commit, st)


def _commit(st: dict) -> None:
    _flush(st)     # .typ on disk (guarded) — the disk file is the only persisted state now


def _refresh(st: dict) -> None:
    """Update the sync-readable snapshot and persist now (call right after an edit,
    on the loop, with the transaction already closed)."""
    _latest[st["key"]] = str(st["text"])
    _commit(st)


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


# --------------------------------------------------------------- edits (async, on loop)
# CRITICAL: pycrdt's Text is indexed by UTF-8 BYTE offsets (len(text) == UTF-8 byte length),
# while Python str.find()/len() work in Unicode CODE POINTS. Any multibyte char before an
# edit point (em dash "—", "▶", "·", CJK, emoji, …) makes the two diverge, so we MUST convert
# code-point offsets to byte offsets before indexing the Text — otherwise the edit lands at
# the wrong place (or panics mid-character) and corrupts the document.
def _cp_to_byte(s: str, cp: int) -> int:
    return len(s[:cp].encode("utf-8"))


def _bytelen(s: str) -> int:
    return len(s.encode("utf-8"))


def _find(s: str, needle: str, occurrence: int) -> int:
    idx, start = -1, 0
    for _ in range(max(1, occurrence)):
        idx = s.find(needle, start)
        if idx < 0:
            return -1
        start = idx + 1
    return idx


async def replace_anchor(anchor: str, new_text: str, file=None, occurrence: int = 1) -> dict:
    st = await ensure_room(file)
    text = st["text"]
    s = str(text)
    if not anchor:
        return {"ok": False, "error": "empty anchor"}
    if s.count(anchor) == 0:
        return {"ok": False, "error": "anchor not found"}
    if occurrence == 1 and s.count(anchor) > 1:
        return {"ok": False, "error": f"anchor is ambiguous ({s.count(anchor)} matches); "
                                      "pass a longer anchor or `occurrence`"}
    idx_cp = _find(s, anchor, occurrence)
    idx = _cp_to_byte(s, idx_cp)            # codepoint -> UTF-8 byte offset for pycrdt
    blen = _bytelen(anchor)
    with st["doc"].transaction():
        del text[idx:idx + blen]
        text.insert(idx, new_text)
    _refresh(st)
    return {"ok": True, "at": idx_cp, "removed": len(anchor), "inserted": len(new_text)}


async def insert_relative(anchor: str, payload: str, where: str = "after", file=None,
                          occurrence: int = 1) -> dict:
    st = await ensure_room(file)
    text = st["text"]
    s = str(text)
    if s.count(anchor) == 0:
        return {"ok": False, "error": "anchor not found"}
    if occurrence == 1 and s.count(anchor) > 1:
        return {"ok": False, "error": f"anchor ambiguous ({s.count(anchor)} matches)"}
    idx_cp = _find(s, anchor, occurrence)
    at_cp = idx_cp if where == "before" else idx_cp + len(anchor)
    at = _cp_to_byte(s, at_cp)              # codepoint -> UTF-8 byte offset
    with st["doc"].transaction():
        text.insert(at, payload)
    _refresh(st)
    return {"ok": True, "at": at_cp, "inserted": len(payload)}


async def replace_range(frm: int, to: int, new_text: str, file=None) -> dict:
    st = await ensure_room(file)
    text = st["text"]
    s = str(text)
    n = len(s)                              # validate against CODE POINTS (what Claude counts)
    if not (0 <= frm <= to <= n):
        return {"ok": False, "error": f"range out of bounds (doc len {n})"}
    frm_b = _cp_to_byte(s, frm)             # codepoint -> UTF-8 byte offset
    to_b = _cp_to_byte(s, to)
    with st["doc"].transaction():
        if to_b > frm_b:
            del text[frm_b:to_b]
        if new_text:
            text.insert(frm_b, new_text)
    _refresh(st)
    return {"ok": True, "at": frm}


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
        n = len(str(st["text"]))
        if n:
            del st["text"][0:n]
        if disk:
            st["text"].insert(0, disk)
    _refresh(st)
    return {"ok": True}


async def insert_text(at: int, payload: str, file=None) -> dict:
    st = await ensure_room(file)
    text = st["text"]
    s = str(text)
    n = len(s)                              # validate against CODE POINTS (what callers count)
    if not (0 <= at <= n):
        return {"ok": False, "error": f"position out of bounds (doc len {n})"}
    at_b = _cp_to_byte(s, at)               # codepoint -> UTF-8 byte offset for pycrdt
    with st["doc"].transaction():
        text.insert(at_b, payload)
    _refresh(st)
    return {"ok": True, "at": at}
