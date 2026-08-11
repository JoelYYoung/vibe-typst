"""Manage the Rust `tcb-resolver` service — one process per open deck that does BOTH:
  - incremental compile + render the deck to per-page SVGs (replaces `typst watch`)
  - resolve a click coordinate -> source (line, col) IN-PROCESS via typst-ide
    (replaces the flaky `tinymist preview` websocket)

It speaks line-delimited JSON over stdin/stdout: we send {"id",page,x,y} resolve
requests and read {"id",ok,line,col} responses, plus {"event":"rendered",version,pages}
notifications whenever it recompiles. No sockets, no timing correlation — robust.

There is one process PER DECK, keyed exactly like the render directory it writes into. A
single process used to follow "the active document", so opening a second project silently
stopped compiling the first one and every tab still showing it froze at its last render.
Rendered pages were always per deck on disk, so keeping each deck's compiler alive is what
makes those pages stay current and reusable by any tab that asks for them.
"""
import itertools
import json
import subprocess
import threading
from pathlib import Path

import runtime

_HERE = Path(__file__).resolve().parent
_BIN_RELEASE = _HERE.parent / "resolver" / "target" / "release" / "tcb-resolver"
_BIN_DEBUG = _HERE.parent / "resolver" / "target" / "debug" / "tcb-resolver"

# A compiler holds a whole typst world in memory, so open decks are capped and the
# least-recently-used one is retired past the cap. Its pages stay on disk and it restarts on
# the next request for that deck, so eviction costs a recompile, never correctness.
MAX_DECKS = 4


def _bin() -> Path:
    return _BIN_RELEASE if _BIN_RELEASE.exists() else _BIN_DEBUG


# deck key (same key as the render dir) -> deck record
_decks: dict[str, dict] = {}
_decks_lock = threading.Lock()
_ids = itertools.count(1)
_clock = itertools.count(1)


def _new_state() -> dict:
    # `seq` bumps on EVERY compile outcome (render OR error) so a caller can wait for the next
    # result after a flush, instead of being fooled by a stale `error` from a previous compile.
    return {"rel": None, "root": None, "version": 0, "pages": 0, "error": None, "seq": 0}


def _target(path=None) -> Path:
    """Normalize a deck's document path.

    `runtime.file_key` hashes an absolute path as given, without resolving it, so `/var/…` and
    `/private/var/…` — or any path reached through a symlink — would key the SAME document to two
    different decks and silently run two compilers over one file. Resolve first so a caller that
    passes an unresolved path still lands on the deck (and render directory) the active document
    already uses.
    """
    return Path(path).expanduser().resolve() if path is not None else runtime.current_file()


def _deck(path=None, *, create: bool = False) -> dict | None:
    """The deck record for a document (default: the active one)."""
    try:
        key = runtime.file_key(_target(path))
    except Exception:
        return None
    with _decks_lock:
        record = _decks.get(key)
        if record is None and create:
            record = {
                "key": key,
                "proc": None,
                "state": _new_state(),
                "pending": {},
                "lock": threading.Lock(),   # guards proc + stdin writes
                "plock": threading.Lock(),  # guards pending
                "used": 0,
            }
            _decks[key] = record
        if record is not None:
            record["used"] = next(_clock)
        return record


def _deck_alive(record: dict | None) -> bool:
    if record is None:
        return False
    proc = record["proc"]
    return proc is not None and proc.poll() is None


def _terminate(record: dict) -> None:
    """Stop a deck's child. Caller holds the deck lock."""
    proc = record["proc"]
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    record["proc"] = None


def _evict_beyond_cap(keep_key: str) -> None:
    """Retire least-recently-used decks so open projects cannot grow compilers without bound."""
    with _decks_lock:
        live = [
            record for record in _decks.values()
            if record["key"] != keep_key and _deck_alive(record)
        ]
        if len(live) < MAX_DECKS:
            return
        stale = sorted(live, key=lambda record: record["used"])[
            : len(live) - MAX_DECKS + 1
        ]
    for record in stale:
        with record["lock"]:
            _terminate(record)


def _alive() -> bool:
    return _deck_alive(_deck())


def version(path=None) -> int:
    return status(path)["version"]


def status(path=None) -> dict:
    record = _deck(path)
    state = record["state"] if record is not None else _new_state()
    return {"running": _deck_alive(record), "rel": state["rel"], "version": state["version"],
            "pages": state["pages"], "error": state["error"], "seq": state["seq"]}


def open_decks() -> list[str]:
    """Keys of every deck with a live compiler (diagnostics)."""
    with _decks_lock:
        return sorted(key for key, record in _decks.items() if _deck_alive(record))


def _reader(record: dict, proc: subprocess.Popen):
    state = record["state"]
    for line in proc.stdout:  # type: ignore[union-attr]
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("event") == "rendered":
            state["version"] = msg.get("version", state["version"])
            state["pages"] = msg.get("pages", state["pages"])
            state["error"] = None  # a good render clears any prior compile error
            state["seq"] += 1
        elif msg.get("event") == "compile_error":
            # keep the last-good render on screen, but record WHY it is stale so the
            # backend/UI can tell the user the source no longer compiles.
            errs = msg.get("errors") or ["compile failed"]
            state["error"] = errs if isinstance(errs, list) else [str(errs)]
            state["seq"] += 1
        elif "id" in msg:
            with record["plock"]:
                slot = record["pending"].pop(msg["id"], None)
            if slot is not None:
                slot["result"] = msg
                slot["event"].set()


def stop(path=None) -> None:
    """Stop ONE deck's compiler (default: the active document's)."""
    record = _deck(path)
    if record is None:
        return
    with record["lock"]:
        _terminate(record)


def stop_all() -> None:
    """Stop every compiler — shutdown, and the Typst retirement a PDF activation performs."""
    with _decks_lock:
        records = list(_decks.values())
    for record in records:
        with record["lock"]:
            _terminate(record)


def start(path=None) -> dict:
    """Ensure a compiler is running for a document (default: the active one)."""
    try:
        target = _target(path)
    except Exception:
        return status(path)
    rel = target.name
    cur_root = str(target.parent)
    record = _deck(target, create=True)
    if record is None:
        return status(path)
    state = record["state"]
    with record["lock"]:
        if _deck_alive(record) and state["rel"] == rel and state["root"] == cur_root:
            return status(target)
        _terminate(record)
        binp = _bin()
        if not binp.exists():
            state["error"] = "tcb-resolver binary not built (cargo build --release in resolver/)"
            return status(target)
        render_dir = str(runtime.render_dir(target))
        Path(render_dir).mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [str(binp), cur_root, rel, "serve", render_dir],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        record["proc"] = proc
        state.update(rel=rel, root=cur_root, version=0, pages=0, error=None)
        threading.Thread(target=_reader, args=(record, proc), daemon=True).start()
    _evict_beyond_cap(record["key"])
    return status(target)


def _request(record: dict, payload: dict, timeout: float) -> dict:
    """Send one line-delimited request to a deck and wait for its reply."""
    rid = next(_ids)
    ev = threading.Event()
    slot = {"event": ev, "result": None}
    with record["plock"]:
        record["pending"][rid] = slot
    try:
        with record["lock"]:
            if not _deck_alive(record):
                return {"ok": False, "error": "resolver not running"}
            record["proc"].stdin.write(json.dumps({"id": rid, **payload}) + "\n")
            record["proc"].stdin.flush()
    except Exception as e:
        with record["plock"]:
            record["pending"].pop(rid, None)
        return {"ok": False, "error": f"resolver write failed: {e}"}
    if not ev.wait(timeout):
        with record["plock"]:
            record["pending"].pop(rid, None)
        return {"ok": False, "error": "resolver timeout"}
    return slot["result"] or {}


def resolve(page_no: int, x: float, y: float, timeout: float = 2.0, path=None) -> dict:
    """Resolve a page coordinate (pt) -> source range. Synchronous (fast once compiled)."""
    if not _deck_alive(_deck(path)):
        start(path)
    record = _deck(path)
    if record is None:
        return {"ok": False, "error": "resolver not running"}
    r = _request(record, {"page": page_no, "x": x, "y": y}, timeout)
    if r.get("ok"):
        return {"ok": True, "start": [r["line"], r["col"]], "end": [r["line"], r["col"]]}
    return {"ok": False, "error": r.get("error", "no element")}


def locate(byte_off: int, timeout: float = 2.0, path=None) -> dict:
    """Reverse of resolve(): a source UTF-8 byte offset -> the page positions where it renders.
    Returns {ok, positions:[{page, x, y}]} (one element can appear on several subslides)."""
    if not _deck_alive(_deck(path)):
        start(path)
    record = _deck(path)
    if record is None:
        return {"ok": False, "error": "resolver not running"}
    r = _request(record, {"cmd": "cursor", "off": int(byte_off)}, timeout)
    return {"ok": bool(r.get("ok")), "positions": r.get("positions", [])}


def page_start(page_no: int, path=None) -> dict:
    """Resolve a page's start by probing a few points near the top."""
    for (x, y) in ((40.0, 40.0), (80.0, 60.0), (120.0, 90.0), (60.0, 140.0)):
        r = resolve(page_no, x, y, path=path)
        if r.get("ok"):
            return r
    return {"ok": False, "error": "could not locate page start"}
