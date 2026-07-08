"""Manage the Rust `tcb-resolver` service — one process that does BOTH:
  - incremental compile + render the deck to per-page SVGs (replaces `typst watch`)
  - resolve a click coordinate -> source (line, col) IN-PROCESS via typst-ide
    (replaces the flaky `tinymist preview` websocket)

It speaks line-delimited JSON over stdin/stdout: we send {"id",page,x,y} resolve
requests and read {"id",ok,line,col} responses, plus {"event":"rendered",version,pages}
notifications whenever it recompiles. No sockets, no timing correlation — robust.
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


def _bin() -> Path:
    return _BIN_RELEASE if _BIN_RELEASE.exists() else _BIN_DEBUG


_proc: subprocess.Popen | None = None
# `seq` bumps on EVERY compile outcome (render OR error) so a caller can wait for the next
# result after a flush, instead of being fooled by a stale `error` from a previous compile.
_state = {"rel": None, "root": None, "version": 0, "pages": 0, "error": None, "seq": 0}
_pending: dict[int, dict] = {}
_ids = itertools.count(1)
_lock = threading.Lock()       # guards _proc + stdin writes
_plock = threading.Lock()      # guards _pending


def _alive() -> bool:
    return _proc is not None and _proc.poll() is None


def version() -> int:
    return _state["version"]


def status() -> dict:
    return {"running": _alive(), "rel": _state["rel"], "version": _state["version"],
            "pages": _state["pages"], "error": _state["error"], "seq": _state["seq"]}


def _reader(proc: subprocess.Popen):
    for line in proc.stdout:  # type: ignore[union-attr]
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("event") == "rendered":
            _state["version"] = msg.get("version", _state["version"])
            _state["pages"] = msg.get("pages", _state["pages"])
            _state["error"] = None  # a good render clears any prior compile error
            _state["seq"] += 1
        elif msg.get("event") == "compile_error":
            # keep the last-good render on screen, but record WHY it is stale so the
            # backend/UI can tell the user the source no longer compiles.
            errs = msg.get("errors") or ["compile failed"]
            _state["error"] = errs if isinstance(errs, list) else [str(errs)]
            _state["seq"] += 1
        elif "id" in msg:
            with _plock:
                slot = _pending.pop(msg["id"], None)
            if slot is not None:
                slot["result"] = msg
                slot["event"].set()


def stop() -> None:
    global _proc
    with _lock:
        if _alive():
            _proc.terminate()
            try:
                _proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _proc.kill()
        _proc = None


def start(rel: str | None = None) -> dict:
    global _proc
    rel = rel or runtime.current_main()
    cur_root = str(runtime.project_dir().resolve())
    with _lock:
        if _alive() and _state["rel"] == rel and _state["root"] == cur_root:
            return status()
        if _alive():
            _proc.terminate()
            try:
                _proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _proc.kill()
            _proc = None
        binp = _bin()
        if not binp.exists():
            _state["error"] = "tcb-resolver binary not built (cargo build --release in resolver/)"
            return status()
        root = str(runtime.project_dir().resolve())
        render_dir = str(runtime.render_dir())
        Path(render_dir).mkdir(parents=True, exist_ok=True)
        _proc = subprocess.Popen(
            [str(binp), root, rel, "serve", render_dir],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        _state.update(rel=rel, root=cur_root, version=0, pages=0, error=None)
        threading.Thread(target=_reader, args=(_proc,), daemon=True).start()
    return status()


def resolve(page_no: int, x: float, y: float, timeout: float = 2.0) -> dict:
    """Resolve a page coordinate (pt) -> source range. Synchronous (fast once compiled)."""
    if not _alive():
        start()
    rid = next(_ids)
    ev = threading.Event()
    slot = {"event": ev, "result": None}
    with _plock:
        _pending[rid] = slot
    try:
        with _lock:
            if not _alive():
                return {"ok": False, "error": "resolver not running"}
            _proc.stdin.write(json.dumps({"id": rid, "page": page_no, "x": x, "y": y}) + "\n")
            _proc.stdin.flush()
    except Exception as e:
        with _plock:
            _pending.pop(rid, None)
        return {"ok": False, "error": f"resolver write failed: {e}"}
    if not ev.wait(timeout):
        with _plock:
            _pending.pop(rid, None)
        return {"ok": False, "error": "resolver timeout"}
    r = slot["result"] or {}
    if r.get("ok"):
        return {"ok": True, "start": [r["line"], r["col"]], "end": [r["line"], r["col"]]}
    return {"ok": False, "error": "no element"}


def locate(byte_off: int, timeout: float = 2.0) -> dict:
    """Reverse of resolve(): a source UTF-8 byte offset -> the page positions where it renders.
    Returns {ok, positions:[{page, x, y}]} (one element can appear on several subslides)."""
    if not _alive():
        start()
    rid = next(_ids)
    ev = threading.Event()
    slot = {"event": ev, "result": None}
    with _plock:
        _pending[rid] = slot
    try:
        with _lock:
            if not _alive():
                return {"ok": False, "error": "resolver not running"}
            _proc.stdin.write(json.dumps({"id": rid, "cmd": "cursor", "off": int(byte_off)}) + "\n")
            _proc.stdin.flush()
    except Exception as e:
        with _plock:
            _pending.pop(rid, None)
        return {"ok": False, "error": f"resolver write failed: {e}"}
    if not ev.wait(timeout):
        with _plock:
            _pending.pop(rid, None)
        return {"ok": False, "error": "resolver timeout"}
    r = slot["result"] or {}
    return {"ok": bool(r.get("ok")), "positions": r.get("positions", [])}


def page_start(page_no: int) -> dict:
    """Resolve a page's start by probing a few points near the top."""
    for (x, y) in ((40.0, 40.0), (80.0, 60.0), (120.0, 90.0), (60.0, 140.0)):
        r = resolve(page_no, x, y)
        if r.get("ok"):
            return r
    return {"ok": False, "error": "could not locate page start"}
