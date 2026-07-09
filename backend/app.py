"""FastAPI backend for Vibe Typst.

Two runtime modes (APP_MODE env var):
  local  – single-user, no auth; projects root is user-configurable.
  server – multi-user; auth handled by the control plane; projects root fixed.

Run from this directory:
  uv run uvicorn app:app --port 8080 --reload
"""
import asyncio
import fcntl
import io
import json
import os
import pty
import signal
import struct
import subprocess
import tempfile
import termios
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import app_config
import context
import docstore
import notes as notes_mod
import projects as projects_mod
import resolver
import runtime
import slidemap
import store
import typst_service
import vcs
import workdir
from config import PPI

HERE = Path(__file__).resolve().parent

# ── active project (in-memory; cleared on restart unless runtime state persists the file) ──
_active_project: dict | None = None


def _has_valid_file() -> bool:
    """True if the current file exists on disk and can be worked with."""
    try:
        return runtime.current_file().exists()
    except Exception:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with docstore.server:
        docstore.set_loop(asyncio.get_running_loop())
        # In local mode, there may be no configured project yet → skip resolver startup.
        # The resolver is started (or restarted) when the user opens a project.
        if _has_valid_file():
            store.set_path(str(runtime.store_path()))
            runtime.backup()
            await docstore.ensure_room()
            resolver.start()
        try:
            yield
        finally:
            resolver.stop()


app = FastAPI(title="Vibe Typst", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

_term = {"pid": None, "rows": 50, "cols": 220}  # pid + last-known client terminal size


def current_source() -> str:
    """The live document text (CRDT snapshot, falling back to disk)."""
    text = docstore.get_text()
    return text if text is not None else typst_service.read_source()


# ---------------------------------------------------------------- crdt websocket
@app.websocket("/ws/{room}")
async def yjs_ws(websocket: WebSocket, room: str):
    await websocket.accept()
    await docstore.ensure_room_by_key(room)
    try:
        await docstore.server.serve(docstore.StarletteYChannel(websocket, room))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# ---------------------------------------------------------------- state / files
@app.get("/api/state")
def state():
    return {
        "project": str(runtime.project_dir()),
        "project_name": (_active_project or {}).get("name", ""),
        "mode": app_config.APP_MODE,
        "file": str(runtime.current_file()),
        "main": runtime.current_main(),
        "room": docstore.room_name(),
        "store": str(runtime.store_path()),
        "ppi": PPI,
        "source": current_source(),
        "pages": typst_service.list_pages(),
        "preview": resolver.status(),
        "workdir_ready": workdir.is_ready(),
        "external_edit_seq": docstore.external_edit_seq,
    }


@app.get("/api/render-version")
def render_version():
    st = resolver.status()
    return {"version": st["version"], "pages": typst_service.list_pages(),
            "room": docstore.room_name(), "error": st.get("error"),
            "external_edit_seq": docstore.external_edit_seq}


@app.get("/api/browse")
def browse(path: Optional[str] = None):
    return runtime.browse(path)


@app.post("/api/open-dialog")
def open_dialog():
    """Open the native macOS file picker and return the chosen .typ path."""
    script = (
        'set f to choose file with prompt "Open a Typst (.typ) file" '
        'of type {"typ", "public.plain-text"}\n'
        'POSIX path of f'
    )
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    except Exception as e:
        return {"cancelled": True, "error": str(e)}
    if r.returncode != 0:
        return {"cancelled": True}  # user cancelled (-128) or error
    return {"path": r.stdout.strip()}


_NEW_FILE_TEMPLATE = (
    "#set page(width: 16cm, height: 9cm, margin: 1.5cm)\n"
    "#set text(size: 24pt)\n\n"
    "= New Slide\n\n"
    "Content here.\n"
)


async def _activate_current() -> dict:
    """Common work after the active file changes: backup, store, working-dir, room, render."""
    runtime.backup()  # snapshot the file before touching it
    store.set_path(str(runtime.store_path()))  # follow the file's directory
    await docstore.ensure_room()
    await docstore.flush_now()
    resolver.start()  # the Rust resolver follows the new file
        # If this working dir was already set up, refresh the managed agent instructions/config.
    # so they name the NOW-current file instead of a stale one. We only refresh an already-set-up
    # dir — never auto-create files in a fresh dir (that stays opt-in via /api/setup-workdir).
    if workdir.is_ready():
        workdir.setup()
    return {
        "file": str(runtime.current_file()),
        "project": str(runtime.project_dir()),
        "project_name": (_active_project or {}).get("name", ""),
        "mode": app_config.APP_MODE,
        "main": runtime.current_main(),
        "room": docstore.room_name(),
        "store": str(runtime.store_path()),
        "source": current_source(),
        "pages": typst_service.list_pages(),
        "preview": resolver.status(),
        "workdir_ready": workdir.is_ready(),
        "external_edit_seq": docstore.external_edit_seq,
    }


@app.post("/api/open-file")
async def open_file(request: Request):
    body = await request.json()
    try:
        runtime.set_file((body or {}).get("path", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return await _activate_current()


@app.post("/api/new-file")
async def new_file(request: Request):
    body = await request.json()
    d = (body or {}).get("dir", "")
    name = (body or {}).get("name", "").strip()
    if not name.endswith(".typ"):
        name += ".typ"
    if not name or "/" in name or name.startswith("."):
        raise HTTPException(400, "invalid file name")
    target = (Path(d).expanduser() / name) if d else None
    if target is None or not target.parent.is_dir():
        raise HTTPException(400, "invalid directory")
    if target.exists():
        raise HTTPException(400, "file already exists")
    try:
        target.write_text(_NEW_FILE_TEMPLATE, encoding="utf-8")
    except Exception as e:
        raise HTTPException(400, f"could not create: {e}")
    try:
        runtime.set_file(str(target))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return await _activate_current()


@app.post("/api/setup-workdir")
def setup_workdir():
    """Create/merge Claude + Codex agent config in the current working dir (called after the user
    confirms). Not done automatically — only when the user opts in."""
    paths = workdir.setup()
    return {"ok": True, "ready": workdir.is_ready(), **paths}


# ---------------------------------------------------------------- terminal (PTY)
@app.websocket("/pty")
async def pty_ws(websocket: WebSocket):
    """A real shell over a PTY, streamed to the browser (xterm.js). Works remotely since
    the shell runs on the server. Opens in the current working directory so `claude` picks
    up the agent config there. NOTE: this is full shell access — don't expose the
    server publicly without auth."""
    await websocket.accept()
    shell = os.environ.get("SHELL", "/bin/bash")
    # Open in the DECK's directory, not HOME. Claude Code keys its sessions to the working
    # directory, so starting here means `claude`/`codex` pick up this deck's agent config.
    # AND every conversation is recorded under one consistent key — so `claude --continue`
    # reliably resumes it. (Starting at HOME scattered sessions and broke resume.)
    try:
        cwd = str(runtime.project_dir())
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
    except Exception:
        cwd = os.path.expanduser("~")
    pid, master = pty.fork()
    if pid == 0:  # child
        try:
            os.chdir(cwd)
        except Exception:
            pass
        os.environ["TERM"] = "xterm-256color"
        # Make this a CLEAN shell, decontaminated from whatever terminal app hosts this
        # server (cmux / iTerm / VS Code). Two reasons:
        #  1) their PROMPT_COMMAND names an integration FUNCTION that only exists in their own
        #     shells, so a fresh PTY errors every prompt ("_cmux_prompt_command: not found").
        #  2) they inject CLI SHIMS first on PATH (e.g. a cmux `claude` wrapper that execs
        #     into the host app). Running that wrapper from this plain PTY — which isn't the
        #     host terminal — makes tools like `claude` behave oddly (e.g. session resume not
        #     working as expected). Stripping the shims makes `claude` resolve to the real
        #     binary, so `claude --continue` / `--resume` work normally.
        #  3) if the server was launched from INSIDE a Claude Code session (or a host app's
        #     `claude` wrapper), the env carries session markers (CLAUDECODE, CLAUDE_CODE_*,
        #     CLAUDE_CODE_CHILD_SESSION, ...). A nested `claude` then thinks it is a CHILD
        #     invocation: it can READ sessions (so `--continue` finds them) but does NOT create
        #     or persist a new top-level session. Scrubbing these makes `claude` run as a fresh
        #     normal session that saves + resumes correctly.
        for var in ("PROMPT_COMMAND", "NODE_OPTIONS", "ITERM_SHELL_INTEGRATION_INSTALLED",
                    "VSCODE_SHELL_INTEGRATION"):
            os.environ.pop(var, None)
        for k in [k for k in os.environ
                  if k.startswith("CMUX_")
                  or k.startswith("CLAUDE_CODE")  # CLAUDE_CODE_ENTRYPOINT/CHILD_SESSION/SESSION_ID/EXECPATH/SSE_PORT
                  or k in ("CLAUDECODE", "CLAUDE_EFFORT")]:
            os.environ.pop(k, None)  # keep ANTHROPIC_* (auth) and CLAUDE_CONFIG_DIR untouched
        path = os.environ.get("PATH", "")
        cleaned = ":".join(p for p in path.split(":") if "cmux" not in p.lower() and p)
        if cleaned:
            os.environ["PATH"] = cleaned
        os.execvp(shell, [shell, "-l"])
        os._exit(1)
    # parent — set the PTY window size to the last known client size BEFORE bash starts
    # printing its prompt. This prevents readline from receiving a SIGWINCH with a new
    # size immediately after startup (which causes it to redraw and produce a double prompt).
    init_ws = struct.pack("HHHH", _term.get("rows", 50), _term.get("cols", 220), 0, 0)
    fcntl.ioctl(master, termios.TIOCSWINSZ, init_ws)
    _term["pid"] = pid
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def on_read():
        try:
            data = os.read(master, 65536)
        except OSError:
            data = b""
        queue.put_nowait(data)
        if not data:
            loop.remove_reader(master)

    loop.add_reader(master, on_read)

    async def sender():
        while True:
            data = await queue.get()
            if not data:
                break
            try:
                await websocket.send_bytes(data)
            except Exception:
                break

    send_task = asyncio.create_task(sender())
    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            txt = msg.get("text")
            if txt is None:
                b = msg.get("bytes")
                if b:
                    os.write(master, b)
                continue
            try:
                j = json.loads(txt)
            except Exception:
                os.write(master, txt.encode())
                continue
            if j.get("t") == "i":
                os.write(master, j["d"].encode())
            elif j.get("t") == "r":
                rows, cols = int(j["r"]), int(j["c"])
                _term["rows"], _term["cols"] = rows, cols
                ws = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(master, termios.TIOCSWINSZ, ws)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            loop.remove_reader(master)
        except Exception:
            pass
        send_task.cancel()
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
        try:
            os.close(master)
        except Exception:
            pass
        if _term.get("pid") == pid:
            _term["pid"] = None


def _proc_cwd(pid: int):
    """The shell's current working directory via /proc/{pid}/cwd (Linux; no NFS path noise)."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        pass
    # macOS / BSD fallback
    try:
        r = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                            capture_output=True, text=True, timeout=2)
        for line in r.stdout.splitlines():
            if line.startswith("n"):
                path = line[1:]
                # Strip NFS mount info that lsof appends: "/path (server:/remote/path)"
                paren = path.find(" (")
                return path[:paren] if paren != -1 else path
    except Exception:
        pass
    return None


def _agent_descendants(pid: int) -> dict:
    """Which supported agent CLIs are running under the terminal's shell."""
    try:
        r = subprocess.run(["ps", "-axo", "pid=,ppid=,command="],
                           capture_output=True, text=True, timeout=2)
    except Exception:
        return {"claude": False, "codex": False}
    children: dict[int, list[int]] = {}
    cmd: dict[int, str] = {}
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            p, pp = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(pp, []).append(p)
        cmd[p] = parts[2] if len(parts) > 2 else ""
    found = {"claude": False, "codex": False}
    stack, seen = [pid], set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for ch in children.get(cur, []):
            c = cmd.get(ch, "").lower()
            if "tcb-resolver" not in c:
                if "claude" in c:
                    found["claude"] = True
                if "codex" in c:
                    found["codex"] = True
            stack.append(ch)
    return found


@app.get("/api/terminal/info")
def terminal_info():
    """Live terminal state: the shell's cwd and whether an agent is running in it."""
    pid = _term.get("pid")
    if not pid:
        return {"cwd": None, "claude": False, "codex": False, "agent": False}
    try:
        os.kill(pid, 0)
    except OSError:
        _term["pid"] = None
        return {"cwd": None, "claude": False, "codex": False, "agent": False}
    agents = _agent_descendants(pid)
    return {"cwd": _proc_cwd(pid), **agents, "agent": agents["claude"] or agents["codex"]}


@app.post("/api/preview/start")
def preview_start():
    return resolver.start()


@app.post("/api/preview/stop")
def preview_stop():
    resolver.stop()
    return resolver.status()


@app.get("/api/preview/status")
def preview_status():
    return resolver.status()


@app.post("/api/preview/resolve")
async def preview_resolve(request: Request):
    """Resolve a page coordinate (pt) to a source range, in-process via the Rust resolver."""
    body = await request.json()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, resolver.resolve, int(body["page_no"]), float(body["x"]), float(body["y"])
    )


@app.post("/api/preview/locate")
async def preview_locate(request: Request):
    """Reverse location: a source UTF-8 byte offset -> the page positions where it renders.
    The exact caret may sit on MARKUP (`#only(3)[`, a closing `]`, the line end) which renders
    nothing, so if it misses we scan that source line for the nearest rendered position. This
    makes a caret click anywhere on a line — and a chip jump that selects a whole line — work."""
    body = await request.json()
    off = (body or {}).get("off")
    if off is None:
        raise HTTPException(400, "missing 'off'")
    off = int(off)
    loop = asyncio.get_running_loop()
    r = await loop.run_in_executor(None, resolver.locate, off)
    if r.get("ok"):
        return r
    src = docstore.get_text() or ""
    if not src:
        return r
    sb = src.encode("utf-8")
    off = max(0, min(off, len(sb)))
    cp = len(sb[:off].decode("utf-8", "ignore"))          # byte offset -> code-point offset
    lstart = src.rfind("\n", 0, cp) + 1
    lend = src.find("\n", cp)
    if lend < 0:
        lend = len(src)
    # probe across the line (a few code points apart) for the first rendered position
    step = max(1, (lend - lstart) // 30)
    for c in range(lstart, lend, step):
        rr = await loop.run_in_executor(None, resolver.locate, len(src[:c].encode("utf-8")))
        if rr.get("ok"):
            return rr
    return r


@app.get("/api/notes")
def get_notes():
    """Every speaker note in the live deck, with its slide/section."""
    return {"notes": notes_mod.list_notes()}


@app.patch("/api/notes")
async def patch_note(request: Request):
    """Edit one speaker note. Body: {raw: <exact existing content>, text: <new content>}."""
    body = await request.json() or {}
    return await notes_mod.update_note(body.get("raw", ""), body.get("text", ""))


@app.post("/api/notes")
async def create_note(request: Request):
    """Add a speaker note. Body: {slide_line, text, sub_index?, sub_total?}. When the slide
    has multiple subslides the note is gated to `sub_index` (see notes.create_note)."""
    body = await request.json() or {}
    return await notes_mod.create_note(
        body.get("slide_line"), body.get("text", ""),
        body.get("sub_index"), body.get("sub_total"),
    )


@app.get("/api/slide-map")
async def slide_map():
    """Per-page presenter data: section, subslide index, and the **per-page transcript** for that
    page (authoritative, from touying's pdfpc mapping). Used by the inline notes + presenter."""
    await docstore.flush_now()  # so the pdfpc query sees the latest content
    loop = asyncio.get_running_loop()
    # per-page notes (pdfpc) + source notes (for the editable raw anchor), in parallel-ish
    pdfpc = await loop.run_in_executor(None, notes_mod.pdfpc_pages)
    src_notes = notes_mod.list_notes()
    # match a page's note text to a source #speaker-note so it stays editable
    raw_by_text = {n["text"].strip(): n["raw"] for n in src_notes}
    sl_by_text = {n["text"].strip(): n["slide_line"] for n in src_notes}
    openers = notes_mod.slide_open_lines()   # slide opener lines, in document order
    by_page = {p["page"]: p for p in pdfpc}
    total = len(typst_service.list_pages())
    out = []
    _slide_counter = 0
    _prev_sl = object()
    for p in range(1, total + 1):
        si = slidemap.slide_info(p)
        pp = by_page.get(p, {})
        note = (pp.get("note") or "")
        # Resolve the slide opener line robustly (source-based, no resolver probe needed):
        #   1) from the page's own #speaker-note position, then
        #   2) from touying's logical-slide label -> the Nth opener, then
        #   3) the resolver probe as a last resort.
        slide_line = sl_by_text.get(note.strip()) if note else None
        if slide_line is None:
            label = pp.get("label")
            if label is not None and str(label).isdigit():
                idx = int(label) - 1
                if 0 <= idx < len(openers):
                    slide_line = openers[idx]
        if slide_line is None and si:
            slide_line = si.get("slide_line")
        # logical slide number: prefer touying's pdfpc label; else a counter that bumps each
        # time the slide opener changes (subslides of one slide share the same number).
        if slide_line != _prev_sl:
            _slide_counter += 1
            _prev_sl = slide_line
        label = pp.get("label")
        slide_no = int(label) if (label is not None and str(label).isdigit()) else _slide_counter
        out.append({
            "page": p,
            "slide_line": slide_line,
            "slide_no": slide_no,
            "section": (si.get("section") if si else None),
            "sub_index": (si.get("sub_index") if si else None),
            "sub_total": (si.get("sub_total") if si else None),
            "note": note,
            "note_raw": raw_by_text.get(note.strip()),
        })
    slide_total = max((r["slide_no"] for r in out), default=0)
    for r in out:
        r["slide_total"] = slide_total
    # Orphaned transcripts: a source #speaker-note that renders on NO page — e.g. one gated
    # to `self.subslide == k` where k exceeds the slide's real subslide count. pdfpc is the
    # ground truth (it's what touying actually renders), so anything in the source but not in
    # any rendered page note is an orphan. Only trust this when the deck compiled (pdfpc非空).
    orphans = []
    if pdfpc:
        rendered = {(pp.get("note") or "").strip() for pp in pdfpc}
        for n in src_notes:
            t = (n.get("text") or "").strip()
            if t and t not in rendered:
                orphans.append({"text": t[:80], "slide_line": n.get("slide_line")})
    return {"pages": out, "total": total, "orphans": orphans}


@app.get("/api/notes/export")
async def export_notes():
    """Per-page narration as one plain-text script (TTS-ready), downloadable."""
    from fastapi.responses import PlainTextResponse
    await docstore.flush_now()
    loop = asyncio.get_running_loop()
    txt = await loop.run_in_executor(None, notes_mod.export_text)
    name = runtime.current_file().stem + "-script.txt"
    return PlainTextResponse(txt, media_type="text/plain; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/notes/pdfpc")
async def export_pdfpc():
    """The deck's `.pdfpc` file (per-page speaker notes) that the pdfpc presenter reads directly.
    Generated natively by touying via `typst query <deck> "<pdfpc-file>"`."""
    from fastapi.responses import PlainTextResponse
    await docstore.flush_now()
    loop = asyncio.get_running_loop()
    raw = await loop.run_in_executor(None, notes_mod.pdfpc_raw)
    if not raw:
        raise HTTPException(400, "could not produce .pdfpc (deck may not compile, or has no notes)")
    name = runtime.current_file().stem + ".pdfpc"
    return PlainTextResponse(raw, media_type="application/json; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/export-pdf")
async def export_pdf():
    """Compile the CURRENT deck to PDF and return it as a download."""
    await docstore.flush_now()  # make sure disk has the latest live content
    main = runtime.current_file()
    proj = runtime.project_dir()
    out = Path(tempfile.gettempdir()) / f"{main.stem}.pdf"
    try:
        proc = subprocess.run(
            ["typst", "compile", "--root", str(proj), str(main), str(out)],
            capture_output=True, text=True, cwd=str(proj), timeout=120,
        )
    except Exception as e:
        raise HTTPException(500, f"typst not runnable: {e}")
    if proc.returncode != 0 or not out.exists():
        raise HTTPException(400, f"compile failed: {(proc.stderr or 'unknown error')[:400]}")
    return FileResponse(str(out), media_type="application/pdf", filename=f"{main.stem}.pdf",
                        headers={"Cache-Control": "no-cache"})


@app.post("/api/preview/page-start")
async def preview_page_start(request: Request):
    """Resolve the source location of a page's start (for jumping the editor there)."""
    body = await request.json()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, resolver.page_start, int(body["page_no"]))


@app.get("/api/source")
def get_source():
    return {"source": current_source()}


@app.get("/api/document")
def get_document(file: Optional[str] = None):
    """Live document text for a file (defaults to the active file). For the MCP edit tools."""
    return {"file": file or runtime.current_main(), "source": docstore.get_text(file)}


@app.post("/api/edit")
async def edit(request: Request):
    """Apply one content-anchored edit to the live CRDT doc; broadcasts + persists."""
    op = await request.json()
    kind = (op or {}).get("op")
    rel = op.get("file")
    if kind == "replace_anchor":
        r = await docstore.replace_anchor(op["anchor"], op["new_text"], rel, op.get("occurrence", 1))
    elif kind == "insert_before":
        r = await docstore.insert_relative(op["anchor"], op["text"], "before", rel, op.get("occurrence", 1))
    elif kind == "insert_after":
        r = await docstore.insert_relative(op["anchor"], op["text"], "after", rel, op.get("occurrence", 1))
    elif kind == "replace_range":
        r = await docstore.replace_range(op["from"], op["to"], op["new_text"], rel)
    elif kind == "insert_text":
        r = await docstore.insert_text(op["at"], op["text"], rel)
    else:
        raise HTTPException(400, f"unknown op {kind!r}")
    return r


@app.post("/api/reset-from-disk")
async def reset_from_disk():
    """Discard the in-memory CRDT state and re-seed from the .typ on disk (use after an
    external edit). Reload the browser afterward."""
    r = await docstore.reset_from_disk()
    return r


@app.post("/api/compile")
async def compile_():
    # Flush the live doc to disk, then WAIT for the resolver's NEXT compile outcome so
    # Refresh reports the real result instead of a blind "success" (or a stale error).
    # We key off `seq`, which bumps on every compile whether it rendered or errored, so a
    # pre-existing error from the previous compile never short-circuits the wait.
    seq0 = resolver.status()["seq"]
    await docstore.flush_now()
    waited = 0.0
    while waited < 3.0:
        st = resolver.status()
        if st["seq"] != seq0:
            break
        await asyncio.sleep(0.05)
        waited += 0.05
    st = resolver.status()
    if st.get("error"):
        return {"ok": False, "errors": st["error"],
                "pages": typst_service.list_pages(), "version": st["version"]}
    return {"ok": True, "pages": typst_service.list_pages(), "version": st["version"]}


@app.get("/api/render/{name}")
def serve_render(name: str):
    if "/" in name or ".." in name:
        raise HTTPException(400, "bad name")
    p = typst_service.render_path(name)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- app state / config
@app.get("/api/app/state")
def app_state():
    """Top-level app state: mode, configuration status, active project."""
    proj = None
    if _active_project:
        proj = _active_project
    elif _has_valid_file() and app_config.get_projects_root():
        # Recover active project from runtime state (e.g. after restart with persisted file)
        try:
            root = app_config.get_projects_root()
            f = runtime.current_file()
            if root and str(f).startswith(str(root)):
                project_id = f.parent.name
                proj = projects_mod.get_project(project_id)
        except Exception:
            pass
    return {
        "mode": app_config.APP_MODE,
        "configured": app_config.is_configured(),
        "active_project": proj,
        "editor_ready": _has_valid_file(),
    }


@app.put("/api/app/config")
async def set_app_config(request: Request):
    """Set app configuration. Currently: projects_root (local mode only)."""
    if app_config.APP_MODE != "local":
        raise HTTPException(403, "config changes are not allowed in server mode")
    body = await request.json() or {}
    projects_root = (body.get("projects_root") or "").strip()
    if not projects_root:
        raise HTTPException(400, "projects_root is required")
    try:
        p = app_config.set_projects_root(projects_root)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "projects_root": str(p)}


# ---------------------------------------------------------------- projects CRUD
@app.get("/api/projects")
def list_projects():
    if not app_config.is_configured():
        raise HTTPException(400, "app not configured — set projects_root first")
    return {"projects": projects_mod.list_projects()}


@app.post("/api/projects")
async def create_project(request: Request):
    if not app_config.is_configured():
        raise HTTPException(400, "app not configured")
    body = await request.json() or {}
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    try:
        p = projects_mod.create_project(name)
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return p


@app.patch("/api/projects/{project_id:path}")
async def rename_project(project_id: str, request: Request):
    body = await request.json() or {}
    new_name = (body.get("name") or "").strip()
    if not new_name:
        raise HTTPException(400, "name is required")
    try:
        p = projects_mod.rename_project(project_id, new_name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    # If the renamed project is the active one, update the active project state
    global _active_project
    if _active_project and _active_project.get("id") == project_id:
        _active_project = p
    return p


@app.delete("/api/projects/{project_id:path}")
def delete_project(project_id: str):
    global _active_project
    # Release every handle to the project's files FIRST (resolver process + comment-DB
    # connection) if they point into the folder we're about to delete — even if it was
    # already "closed" (active=None) but the resolver/store still hold its files. On NFS an
    # open file gets silly-renamed to .nfsXXXX instead of removed, leaving the folder
    # non-empty so the delete fails and the project lingers as an un-deletable ghost.
    try:
        proj_dir = (projects_mod._projects_root() / project_id).resolve()
        cur = runtime.current_file()
        if cur == proj_dir or proj_dir in cur.parents:
            try: resolver.stop()
            except Exception: pass
            try: store.close()
            except Exception: pass
    except Exception:
        pass
    if _active_project and _active_project.get("id") == project_id:
        _active_project = None
    try:
        projects_mod.delete_project(project_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except OSError as e:
        raise HTTPException(409, f"could not fully delete (a file is still in use): {e}")
    return {"ok": True}


@app.post("/api/projects/{project_id:path}/copy")
async def copy_project(project_id: str, request: Request):
    body = await request.json() or {}
    new_name = (body.get("name") or "").strip()
    if not new_name:
        raise HTTPException(400, "name is required")
    try:
        p = projects_mod.copy_project(project_id, new_name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    return p


@app.post("/api/projects/{project_id:path}/open")
async def open_project(project_id: str):
    """Activate a project: set its main file as the active file and start the resolver."""
    global _active_project
    try:
        info = projects_mod.get_project(project_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    main_path = Path(info["path"]) / info["main_file"]
    if not main_path.exists():
        raise HTTPException(400, f"main file {info['main_file']!r} not found in project")
    try:
        runtime.set_file(str(main_path))
    except ValueError as e:
        raise HTTPException(400, str(e))
    _active_project = info
    store.set_path(str(runtime.store_path()))
    runtime.backup()
    await docstore.ensure_room()
    resolver.start()
    # Auto-set-up the workdir (Claude + Codex config + enabled vibe-typst MCP server) on every
    # project open, in BOTH local and server mode — so `claude` run in the project dir finds the
    # MCP. (Local mode previously never wrote a .mcp.json, so the MCP couldn't be found.)
    try:
        workdir.setup()
    except Exception:
        pass
    return {"ok": True, "project": info}


@app.post("/api/projects/close")
def close_project():
    """Deactivate the current project (returns to the projects list). Releases the resolver
    process and comment-DB connection so the project's files aren't held open — otherwise a
    subsequent delete on NFS leaves .nfs* silly-rename ghosts."""
    global _active_project
    _active_project = None
    try: resolver.stop()
    except Exception: pass
    try: store.close()
    except Exception: pass
    return {"ok": True}


# ---------------------------------------------------------------- file management within project
@app.get("/api/project/files")
def project_files():
    """List all files and directories in the active project."""
    if not _has_valid_file():
        raise HTTPException(400, "no active project")
    return {"items": projects_mod.list_project_items(runtime.project_dir())}


@app.post("/api/project/files/mkdir")
async def project_mkdir(request: Request):
    """Create a directory inside the active project."""
    if not _has_valid_file():
        raise HTTPException(400, "no active project")
    body = await request.json() or {}
    rel_path = (body.get("path") or "").strip()
    if not rel_path:
        raise HTTPException(400, "path is required")
    try:
        result = projects_mod.mkdir(runtime.project_dir(), rel_path)
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return result


@app.delete("/api/project/dirs")
async def project_rmdir(request: Request):
    """Delete a directory (recursively) from the active project."""
    if not _has_valid_file():
        raise HTTPException(400, "no active project")
    body = await request.json() or {}
    rel_path = (body.get("path") or "").strip()
    if not rel_path:
        raise HTTPException(400, "path is required")
    try:
        projects_mod.rmdir(runtime.project_dir(), rel_path)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return {"ok": True}


@app.patch("/api/project/files/rename")
async def project_rename_item(request: Request):
    """Rename a file or directory inside the active project."""
    if not _has_valid_file():
        raise HTTPException(400, "no active project")
    body = await request.json() or {}
    old_rel = (body.get("from") or "").strip()
    new_name = (body.get("to") or "").strip()
    if not old_rel or not new_name:
        raise HTTPException(400, "from and to are required")
    try:
        result = projects_mod.rename_item(runtime.project_dir(), old_rel, new_name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))
    return result


@app.post("/api/project/files/move")
async def project_move_item(request: Request):
    """Move a file/folder into another folder within the active project (drag-to-move).
    Body: {from: <rel path>, dest: <dest dir rel path, '' = root>}."""
    if not _has_valid_file():
        raise HTTPException(400, "no active project")
    body = await request.json() or {}
    old_rel = (body.get("from") or "").strip()
    dest_rel = (body.get("dest") or "").strip()
    if not old_rel:
        raise HTTPException(400, "from is required")
    try:
        result = projects_mod.move_item(runtime.project_dir(), old_rel, dest_rel)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))
    return result


@app.post("/api/project/files/write")
async def write_project_file(request: Request):
    """Overwrite an existing text file inside the active project (used for .md editing)."""
    if not _has_valid_file():
        raise HTTPException(400, "no active project")
    body = await request.json() or {}
    path = (body.get("path") or "").strip()
    content = body.get("content", "")
    if not path:
        raise HTTPException(400, "path is required")
    try:
        target = projects_mod._resolve_project_path(runtime.project_dir(), path)
    except PermissionError:
        raise HTTPException(403, "path not allowed")
    if not target.exists():
        raise HTTPException(404, "file not found")
    target.write_text(content, encoding="utf-8")
    return {"ok": True}


@app.post("/api/project/files/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload a file into the active project directory."""
    if not _has_valid_file():
        raise HTTPException(400, "no active project")
    project_dir = runtime.project_dir()
    name = file.filename or "upload"
    import re as _re
    name = _re.sub(r'[\\/:*?"<>|]', "_", name)
    target = project_dir / name
    if target.exists():
        stem, suffix = target.stem, target.suffix
        i = 1
        while target.exists():
            target = project_dir / f"{stem}_{i}{suffix}"
            i += 1
    content = await file.read()
    target.write_bytes(content)
    return {"ok": True, "path": target.name, "size": len(content)}


@app.get("/api/project/files/download")
def download_file(path: str):
    """Download a file from the active project directory."""
    if not _has_valid_file():
        raise HTTPException(400, "no active project")
    project_dir = runtime.project_dir()
    try:
        target = projects_mod._resolve_project_path(project_dir, path)
    except PermissionError:
        raise HTTPException(403, "path not allowed")
    if not target.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(str(target), filename=target.name,
                        headers={"Cache-Control": "no-cache"})


_INLINE_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "webp": "image/webp",
}


@app.get("/api/project/files/view")
def view_file(path: str):
    """Serve a file inline (for in-app preview of images and PDFs)."""
    if not _has_valid_file():
        raise HTTPException(400, "no active project")
    project_dir = runtime.project_dir()
    try:
        target = projects_mod._resolve_project_path(project_dir, path)
    except PermissionError:
        raise HTTPException(403, "path not allowed")
    if not target.is_file():
        raise HTTPException(404, "file not found")
    ext = target.suffix.lstrip(".").lower()
    media_type = _INLINE_MEDIA_TYPES.get(ext, "application/octet-stream")
    return FileResponse(str(target), media_type=media_type,
                        headers={"Content-Disposition": "inline", "Cache-Control": "no-cache"})


@app.post("/api/project/files/create")
async def create_project_file(request: Request):
    """Create a new .typ file in the active project directory."""
    if not _has_valid_file():
        raise HTTPException(400, "no active project")
    body = await request.json() or {}
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    try:
        info = projects_mod.create_file(runtime.project_dir(), name)
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    return info


@app.delete("/api/project/files")
async def delete_project_file(request: Request):
    """Delete a file from the active project directory."""
    if not _has_valid_file():
        raise HTTPException(400, "no active project")
    body = await request.json() or {}
    path = (body.get("path") or "").strip()
    if not path:
        raise HTTPException(400, "path is required")
    # Prevent deleting the currently active file
    try:
        target = projects_mod._resolve_project_path(runtime.project_dir(), path)
    except PermissionError:
        raise HTTPException(403, "path not allowed")
    if target == runtime.current_file():
        raise HTTPException(400, "cannot delete the currently open file")
    try:
        projects_mod.delete_file(runtime.project_dir(), path)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


# ---------------------------------------------------------------- git / vcs
@app.get("/api/git/status")
def git_status():
    if not _has_valid_file():
        return {"initialized": False, "dirty": False, "current": None}
    return vcs.status(runtime.project_dir())


@app.get("/api/git/versions")
def git_versions():
    if not _has_valid_file():
        return []
    return vcs.list_versions(runtime.project_dir())


@app.post("/api/git/commit")
async def git_commit(request: Request):
    """Save the current state as a new version (commit + tag)."""
    if not _has_valid_file():
        raise HTTPException(400, "no active project")
    body = await request.json() or {}
    message = (body.get("message") or "").strip()
    await docstore.flush_now()  # persist in-memory edits before snapshotting
    return vcs.save_version(runtime.project_dir(), message)


@app.post("/api/git/restore")
async def git_restore(request: Request):
    """Reset the working tree to a tagged version, then reload the editor."""
    if not _has_valid_file():
        raise HTTPException(400, "no active project")
    body = await request.json() or {}
    tag = (body.get("tag") or "").strip()
    if not tag:
        raise HTTPException(400, "tag is required")
    # Rotate the CRDT room FIRST so the soon-to-be-orphaned room can't write its
    # stale in-memory content back over the files we're about to restore.
    new_room = docstore.rotate()
    result = vcs.restore_version(runtime.project_dir(), tag)
    if not result["ok"]:
        raise HTTPException(400, result.get("error", "restore failed"))
    await docstore.ensure_room()  # reseed the new room from the restored files
    resolver.start()
    return {"ok": True, "room": new_room}


@app.post("/api/git/delete")
async def git_delete(request: Request):
    if not _has_valid_file():
        raise HTTPException(400, "no active project")
    body = await request.json() or {}
    tag = (body.get("tag") or "").strip()
    if not tag:
        raise HTTPException(400, "tag is required")
    result = vcs.delete_version(runtime.project_dir(), tag)
    if not result["ok"]:
        raise HTTPException(400, result.get("error", "delete failed"))
    return {"ok": True}


# ---------------------------------------------------------------- comments
@app.get("/api/comments")
def comments(status: Optional[str] = None, file: Optional[str] = None):
    return store.list_comments(status, file)


@app.post("/api/comments")
async def add(request: Request):
    items = await request.json()
    payloads = items if isinstance(items, list) else [items]
    src = current_source()
    created = []
    for p in payloads:
        p = dict(p or {})
        p.setdefault("file", runtime.current_main())
        if p.get("selections") is not None:
            p["selection"] = p["selections"]  # store the multi-select list
        # Enrich PAGE selections with their source slide + subslide so the AI can locate them
        # (a bare page number is unanchorable: one #slide(repeat:N) yields N pages).
        for s in (p.get("selections") or p.get("selection") or []):
            if isinstance(s, dict) and s.get("kind") == "page" and s.get("slide") is None:
                try:
                    si = slidemap.slide_info(s.get("page_no"))
                    if si:
                        s["slide"] = si
                except Exception:
                    pass
        if not p.get("raw_context"):
            p["raw_context"] = context.build_raw_context(p, src)
        created.append(store.add_comment(p))
    return created


@app.patch("/api/comments/{cid}")
async def patch(cid: str, request: Request):
    fields = await request.json() or {}
    # Editing the comment's text must also update raw_context (what Claude actually reads),
    # or Claude would act on the stale instruction. We swap only the instruction block and
    # keep the original captured source snapshot.
    if "body" in fields and "raw_context" not in fields:
        cur = store.get_comment(cid)
        if cur:
            fields["raw_context"] = context.replace_instruction(cur.get("raw_context", ""), fields["body"])
    c = store.update_comment(cid, **fields)
    if not c:
        raise HTTPException(404)
    return c


@app.get("/api/comments/{cid}/events")
def comment_events(cid: str):
    c = store.get_comment(cid)
    if not c:
        raise HTTPException(404)
    return store.get_events(c["id"])


@app.post("/api/comments/{cid}/done")
async def mark_done(cid: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    c = store.set_status(cid, "done", (body or {}).get("note"))
    if not c:
        raise HTTPException(404)
    return c


@app.post("/api/comments/{cid}/reopen")
def reopen(cid: str):
    c = store.set_status(cid, "pending")
    if not c:
        raise HTTPException(404)
    return c


@app.delete("/api/comments/{cid}")
def delete(cid: str):
    return {"deleted": store.delete_comment(cid)}


# Serve the compiled Vite frontend. Must be mounted LAST so API routes take
# precedence. html=True enables SPA fallback (all unmatched paths → index.html).
#
# index.html is NOT content-hashed, so browsers must always revalidate it —
# otherwise a cached index.html keeps pointing at an old (deleted) JS/CSS bundle
# and deploys never reach the user. Hashed assets stay long-cacheable.
class _CacheAwareStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if path.endswith(".html") or path in (".", "", "index.html"):
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp


_DIST = HERE.parent / "frontend" / "dist"
if _DIST.exists():
    app.mount("/", _CacheAwareStatic(directory=str(_DIST), html=True), name="frontend")
