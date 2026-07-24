"""FastAPI backend for Vibe Typst.

Two runtime modes (APP_MODE env var):
  local  – single-user, no auth; projects root is user-configurable.
  server – multi-user; auth handled by the control plane; projects root fixed.

Run from this directory:
  uv run uvicorn app:app --port 8080 --reload
"""
import asyncio
import fcntl
import hashlib
import io
import json
import os
import pty
import re
import signal
import stat
import struct
import subprocess
import tempfile
import termios
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import Request as StarletteRequest

import app_config
import context
import docstore
import notes as notes_mod
import projects as projects_mod
import pdf_service
import pdf_transcript
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
_pdf_render_state = {"fingerprint": None, "version": 0}
_PDF_PAGE_NAME = re.compile(r"page-([1-9][0-9]*)\.png$")
MAX_PDF_UPLOAD_BYTES = projects_mod.MAX_PDF_UPLOAD_BYTES
_PDF_UPLOAD_CHUNK_BYTES = 1024 * 1024
_PDF_MULTIPART_OVERHEAD_BYTES = 64 * 1024
_PDF_FORM_MAX_PART_BYTES = 16 * 1024


class _PdfIngressTooLarge(MultiPartException):
    """Abort multipart parsing after the bounded ASGI receive wrapper crosses its cap."""


class _PdfIngressBoundRequest(StarletteRequest):
    """Preserve the dedicated ingress-overflow exception through Request.form()."""

    async def _get_form(self, **kwargs):
        try:
            return await super()._get_form(**kwargs)
        except StarletteHTTPException as exc:
            if isinstance(exc.__context__, _PdfIngressTooLarge):
                raise exc.__context__
            raise


def _pdf_ingress_bound_request(request: Request) -> StarletteRequest:
    """Return a request that refuses body chunks beyond the PDF plus multipart cap."""
    received = 0
    limit = MAX_PDF_UPLOAD_BYTES + _PDF_MULTIPART_OVERHEAD_BYTES

    async def bounded_receive():
        nonlocal received
        message = await request.receive()
        if message.get("type") == "http.request":
            received += len(message.get("body", b""))
            if received > limit:
                raise _PdfIngressTooLarge("PDF upload is too large")
        return message

    return _PdfIngressBoundRequest(request.scope, receive=bounded_receive)


@dataclass(frozen=True)
class _PdfIdentity:
    project: Path
    pdf: Path
    render: Path
    identity: str
    project_id: str | None
    project_name: str
    project_pdf: bool


def _project_document(info: dict) -> tuple[str, Path]:
    """Validate immutable project metadata before activating its primary document."""
    project_type = info.get("type", "typst")
    if project_type not in {"typst", "pdf"}:
        raise ValueError("unsupported project type")
    project_dir = Path(info["path"]).resolve()
    main_name = info.get("main_file")
    if not isinstance(main_name, str) or not main_name:
        raise ValueError("project main file is invalid")
    main_lexical = project_dir / main_name
    main_path = main_lexical.resolve()
    try:
        main_path.relative_to(project_dir)
    except ValueError as exc:
        raise ValueError("project main file escapes its project") from exc
    if project_type == "pdf":
        if (main_name != "document.pdf" or main_lexical.parent != project_dir
                or main_lexical.is_symlink()):
            raise ValueError("PDF project main file must be document.pdf")
        if main_path.suffix.lower() != ".pdf":
            raise ValueError("PDF project has an invalid document type")
    elif main_path.suffix.lower() != ".typ":
        raise ValueError("Typst project has an invalid main file")
    if not main_lexical.is_file():
        raise ValueError(f"main file {main_name!r} not found in project")
    return project_type, main_path


def _pdf_pages(path: Path | None = None) -> list[str]:
    directory = runtime.render_dir(path)
    if directory.is_symlink() or not directory.is_dir():
        return []
    pages = [path.name for path in directory.iterdir()
             if path.is_file() and not path.is_symlink() and _PDF_PAGE_NAME.fullmatch(path.name)]
    return sorted(pages, key=lambda name: int(_PDF_PAGE_NAME.fullmatch(name).group(1)))


def _open_pdf_render_page(name: str, pdf_path: Path | None = None):
    """Open a regular PDF page beneath a non-symlink render directory without following links."""
    if _PDF_PAGE_NAME.fullmatch(name) is None:
        return None
    directory = runtime.render_dir(pdf_path)
    if directory.is_symlink() or not directory.is_dir():
        return None
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if not all(hasattr(os, flag) for flag in required):
        return None
    dir_fd = page_fd = None
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        page_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        if not stat.S_ISREG(os.fstat(page_fd).st_mode):
            os.close(page_fd)
            page_fd = None
            return None
        return os.fdopen(page_fd, "rb", closefd=True)
    except OSError:
        if page_fd is not None:
            os.close(page_fd)
        return None
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


def _open_pdf_document(pdf_path: Path):
    """Open the active primary as a pinned regular inode without following links."""
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if not all(hasattr(os, flag) for flag in required):
        return None
    dir_fd = pdf_fd = None
    try:
        dir_fd = os.open(
            pdf_path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        pdf_fd = os.open(
            pdf_path.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=dir_fd,
        )
        if not stat.S_ISREG(os.fstat(pdf_fd).st_mode):
            os.close(pdf_fd)
            pdf_fd = None
            return None
        return os.fdopen(pdf_fd, "rb", closefd=True)
    except OSError:
        if pdf_fd is not None:
            os.close(pdf_fd)
        return None
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


def _matching_pdf_project(pdf_path: Path) -> dict | None:
    """Return immutable metadata only when ``pdf_path`` is a project's document.pdf."""
    root = app_config.get_projects_root()
    if root is None:
        return None
    root = Path(root).resolve()
    pdf_path = pdf_path.resolve()
    if pdf_path.name != "document.pdf" or pdf_path.parent.parent != root:
        return None
    try:
        info = projects_mod.get_project(pdf_path.parent.name)
        project_type, main_path = _project_document(info)
    except (FileNotFoundError, ValueError):
        return None
    return info if project_type == "pdf" and main_path == pdf_path else None


def _pdf_identity(path: Path, project: dict | None = None) -> str:
    project = project if project is not None else _active_project
    project_id = project.get("id", "") if project else ""
    return f"{project_id}:{path.resolve()}"


def _capture_pdf_identity() -> _PdfIdentity:
    """Capture the active PDF target before waiting on its cross-process lock."""
    current = runtime.current_file().resolve()
    if runtime.document_type() != "pdf" or not current.is_file():
        raise ValueError("no active PDF document")
    if _active_project is None:
        return _PdfIdentity(
            project=current.parent,
            pdf=current,
            render=runtime.render_dir(current),
            identity=_pdf_identity(current, None),
            project_id=None,
            project_name="",
            project_pdf=False,
        )
    project_type, pdf_path = _project_document(_active_project)
    if project_type != "pdf" or current != pdf_path:
        raise ValueError("active document does not match the project PDF")
    return _PdfIdentity(
        project=pdf_path.parent,
        pdf=pdf_path,
        render=runtime.render_dir(pdf_path),
        identity=_pdf_identity(pdf_path, _active_project),
        project_id=_active_project.get("id"),
        project_name=_active_project.get("name", ""),
        project_pdf=True,
    )


def _target_pdf_identity(info: dict, pdf_path: Path) -> _PdfIdentity:
    return _PdfIdentity(
        project=pdf_path.parent,
        pdf=pdf_path,
        render=runtime.render_dir(pdf_path),
        identity=_pdf_identity(pdf_path, info),
        project_id=info.get("id"),
        project_name=info.get("name", ""),
        project_pdf=True,
    )


def _revalidate_pdf_identity(expected: _PdfIdentity) -> None:
    """Fail instead of redirecting a lock waiter to a newly active project."""
    if runtime.document_type() != "pdf" or runtime.current_file().resolve() != expected.pdf:
        raise ValueError("active PDF changed while waiting")
    if expected.project_id is None:
        if _active_project is not None:
            raise ValueError("active PDF project changed while waiting")
        return
    if (_active_project is None
            or _active_project.get("type") != "pdf"
            or _active_project.get("id") != expected.project_id):
        raise ValueError("active PDF project changed while waiting")
    _, active_pdf = _project_document(_active_project)
    if active_pdf != expected.pdf:
        raise ValueError("active PDF changed while waiting")


def _record_pdf_render_version(pages: list[str], path: Path | None = None,
                               identity: str | None = None) -> int:
    digest = hashlib.sha1()
    target = path or runtime.current_file()
    digest.update((identity or _pdf_identity(target)).encode("utf-8"))
    for name in pages:
        page = runtime.render_dir(path) / name
        digest.update(name.encode("utf-8"))
        digest.update(page.read_bytes())
    fingerprint = digest.hexdigest()
    if _pdf_render_state["fingerprint"] != fingerprint:
        _pdf_render_state["fingerprint"] = fingerprint
        _pdf_render_state["version"] += 1
    return _pdf_render_state["version"]


def _pdf_render_version(path: Path | None = None, identity: str | None = None) -> int:
    pages = _pdf_pages(path)
    if not pages:
        return _pdf_render_state["version"]
    target = path or runtime.current_file()
    return _record_pdf_render_version(pages, target, identity)


def _prepare_pdf(pdf_path: Path, project_pdf: bool, identity: str) -> tuple[dict, dict | None, int]:
    """Render/reconcile before disrupting the active Typst runtime."""
    rendered = pdf_service.render_pdf(pdf_path, runtime.render_dir(pdf_path))
    transcripts = (pdf_transcript.load(pdf_path.parent, "document.pdf", rendered["page_count"])
                   if project_pdf else None)
    version = _record_pdf_render_version(rendered["pages"], pdf_path, identity)
    return rendered, transcripts, version


def _pdf_activation_response(
    rendered: dict,
    transcripts: dict | None,
    version: int,
    expected: _PdfIdentity | None = None,
) -> dict:
    target = expected or _capture_pdf_identity()
    return {
        "file": str(target.pdf), "project": str(target.project),
        "project_name": target.project_name, "mode": app_config.APP_MODE,
        "project_type": "pdf", "main": target.pdf.name,
        "selected_file": target.pdf.name, "source": "", "pages": rendered["pages"],
        "tokens": {}, "version": version, "transcripts": transcripts,
    }


def _prepare_locked_pdf(expected: _PdfIdentity, *, require_active: bool) -> tuple[dict, dict | None, int]:
    with pdf_service.project_write_lock(expected.project):
        pdf_service.recover_pending(expected.project, expected.render)
        if require_active:
            _revalidate_pdf_identity(expected)
        rendered, transcripts, version = _prepare_pdf(
            expected.pdf, expected.project_pdf, expected.identity
        )
        return rendered, transcripts, version


def _stop_typst_services_for_pdf() -> None:
    resolver.stop()
    try:
        store.close()
    except Exception:
        pass


async def _retire_typst_for_pdf() -> None:
    await docstore.stop()
    await asyncio.to_thread(_stop_typst_services_for_pdf)


async def _activate_pdf() -> dict:
    """Render a PDF without creating any Typst resolver, CRDT, comment, or workdir state."""
    expected = _capture_pdf_identity()
    rendered, transcripts, version = await asyncio.to_thread(
        _prepare_locked_pdf, expected, require_active=True
    )
    _revalidate_pdf_identity(expected)
    await _retire_typst_for_pdf()
    return _pdf_activation_response(rendered, transcripts, version, expected)


async def _activate_pdf_project(info: dict, pdf_path: Path) -> dict:
    """Prepare PDF state before publishing a switch away from a Typst project."""
    global _active_project
    expected = _target_pdf_identity(info, pdf_path)
    rendered, transcripts, version = await asyncio.to_thread(
        _prepare_locked_pdf, expected, require_active=False
    )
    previous_file = runtime._state.get("file")
    previous_project = _active_project
    try:
        runtime.set_file(str(pdf_path))
        _active_project = info
        await _retire_typst_for_pdf()
        return _pdf_activation_response(rendered, transcripts, version, expected)
    except Exception:
        _active_project = previous_project
        runtime.restore_file(previous_file)
        raise


def _has_valid_file() -> bool:
    """True if the current file exists on disk and can be worked with."""
    try:
        return runtime.current_file().exists()
    except Exception:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _active_project
    # A PDF does not start the CRDT websocket server. Typst initializes it only when needed.
    if _has_valid_file():
        if runtime.document_type() == "pdf":
            info = _matching_pdf_project(runtime.current_file())
            if info is not None:
                _active_project = info
                try:
                    await _activate_pdf()
                except Exception:
                    _active_project = None
        else:
            await docstore.start()
            store.set_path(str(runtime.store_path()))
            runtime.backup()
            await docstore.ensure_room()
            resolver.start()
    try:
        yield
    finally:
        resolver.stop()
        await docstore.stop()


app = FastAPI(title="Vibe Typst", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

_term = {"pid": None, "rows": 50, "cols": 220}  # pid + last-known client terminal size


def current_source() -> str:
    """The live document text (CRDT snapshot, falling back to disk)."""
    text = docstore.get_text()
    return text if text is not None else typst_service.read_source()


def _require_typst_mode() -> None:
    """Reject legacy source/preview functionality before it can touch Typst state."""
    if runtime.document_type() == "pdf":
        raise HTTPException(400, "endpoint is unavailable for PDF projects")


# ---------------------------------------------------------------- crdt websocket
def _yjs_admission_is_current(expected_path: Path, room: str) -> bool:
    """Whether a websocket admission still names this exact live Typst lineage."""
    return (runtime.document_type() == "typst"
            and runtime.current_file() == expected_path
            and docstore.room_name() == room
            and docstore.path_for_key(room) == expected_path)


async def _reject_stale_yjs(websocket: WebSocket) -> None:
    """A stale websocket owns only its own close, never the shared CRDT lifecycle."""
    await websocket.close(code=1008)


@app.websocket("/ws/{room}")
async def yjs_ws(websocket: WebSocket, room: str):
    if runtime.document_type() == "pdf":
        await websocket.close(code=1008)
        return
    expected_path = runtime.current_file()
    # Reject stale or invented room generations before accepting or starting CRDT work.
    if not _yjs_admission_is_current(expected_path, room):
        await _reject_stale_yjs(websocket)
        return
    active_server = await docstore.start(expected_path, room)
    if active_server is None:
        await _reject_stale_yjs(websocket)
        return
    if not _yjs_admission_is_current(expected_path, room):
        await _reject_stale_yjs(websocket)
        return
    if await docstore.ensure_room_by_key(room) is None:
        await _reject_stale_yjs(websocket)
        return
    if not _yjs_admission_is_current(expected_path, room):
        await _reject_stale_yjs(websocket)
        return
    await websocket.accept()
    # `accept()` itself awaits, so close an already-accepted socket if a final switch won.
    if not _yjs_admission_is_current(expected_path, room):
        await _reject_stale_yjs(websocket)
        return
    try:
        await active_server.serve(docstore.StarletteYChannel(websocket, room))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if not _yjs_admission_is_current(expected_path, room):
            await _reject_stale_yjs(websocket)


# ---------------------------------------------------------------- state / files
@app.get("/api/state")
def state():
    if runtime.document_type() == "pdf":
        try:
            expected = _capture_pdf_identity()
            def observe(_pdf_path, _page_count):
                pages = _pdf_pages(expected.pdf)
                return {
                    "project": str(expected.project),
                    "project_name": expected.project_name,
                    "project_type": "pdf",
                    "mode": app_config.APP_MODE,
                    "file": str(expected.pdf),
                    "main": expected.pdf.name,
                    "selected_file": expected.pdf.name,
                    "ppi": PPI,
                    "source": "",
                    "pages": pages,
                    "tokens": {},
                    "version": _pdf_render_version(expected.pdf, expected.identity),
                }
            return _locked_pdf_observation(observe, expected)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return {
        "project": str(runtime.project_dir()),
        "project_name": (_active_project or {}).get("name", ""),
        "project_type": "typst",
        "mode": app_config.APP_MODE,
        "file": str(runtime.current_file()),
        "main": runtime.current_main(),
        "room": docstore.room_name(),
        "store": str(runtime.store_path()),
        "ppi": PPI,
        "source": current_source(),
        "pages": typst_service.list_pages(),
        "tokens": typst_service.page_tokens(),
        "preview": resolver.status(),
        "workdir_ready": workdir.is_ready(),
        "external_edit_seq": docstore.external_edit_seq,
    }


@app.get("/api/render-version")
def render_version():
    if runtime.document_type() == "pdf":
        try:
            expected = _capture_pdf_identity()
            return _locked_pdf_observation(
                lambda _pdf_path, _page_count: {
                    "version": _pdf_render_version(expected.pdf, expected.identity),
                    "pages": _pdf_pages(expected.pdf),
                    "tokens": {},
                    "project_type": "pdf",
                    "error": None,
                },
                expected,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    st = resolver.status()
    return {"version": st["version"], "pages": typst_service.list_pages(),
            "tokens": typst_service.page_tokens(),
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


def _setup_workdir_and_migrate() -> dict:
    """Refresh managed agent files, then keep them out of deck version history."""
    paths = workdir.setup()
    try:
        vcs.migrate(runtime.project_dir())
    except Exception:
        pass
    return paths


async def _activate_current() -> dict:
    """Common work after the active file changes: backup, store, working-dir, room, render."""
    if runtime.document_type() == "pdf":
        return await _activate_pdf()
    runtime.backup()  # snapshot the file before touching it
    store.set_path(str(runtime.store_path()))  # follow the file's directory
    await docstore.start()
    await docstore.ensure_room()
    await docstore.flush_now()
    resolver.start()  # the Rust resolver follows the new file
    # If this working dir was already set up, refresh the managed agent instructions/config.
    # so they name the NOW-current file instead of a stale one. We only refresh an already-set-up
    # dir — never auto-create files in a fresh dir (that stays opt-in via /api/setup-workdir).
    if workdir.is_ready():
        _setup_workdir_and_migrate()
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
        "tokens": typst_service.page_tokens(),
        "preview": resolver.status(),
        "workdir_ready": workdir.is_ready(),
        "external_edit_seq": docstore.external_edit_seq,
    }


@app.post("/api/open-file")
async def open_file(request: Request):
    global _active_project
    body = await request.json()
    requested = (body or {}).get("path", "")
    try:
        requested_path = Path(requested).expanduser().resolve()
    except Exception as e:
        raise HTTPException(400, "invalid file path") from e
    if _active_project is not None and _active_project.get("type") == "pdf":
        try:
            expected = _capture_pdf_identity()
            active_pdf = await asyncio.to_thread(
                _locked_pdf_observation,
                lambda pdf_path, _page_count: pdf_path,
                expected,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if requested_path != active_pdf:
            raise HTTPException(400, "a PDF project may only open document.pdf")
    if requested_path.suffix.lower() == ".pdf":
        info = _matching_pdf_project(requested_path)
        if info is None:
            raise HTTPException(400, "open PDF files through a matching PDF project")
        try:
            return await _activate_pdf_project(info, requested_path)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    previous_file = runtime._state.get("file")
    previous_project = _active_project
    try:
        runtime.set_file(requested)
        _active_project = None
        return await _activate_current()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception:
        try:
            resolver.stop()
        except Exception:
            pass
        await docstore.stop()
        try:
            store.close()
        except Exception:
            pass
        _active_project = previous_project
        runtime.restore_file(previous_file)
        raise


@app.post("/api/new-file")
async def new_file(request: Request):
    _require_typst_mode()
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
    paths = _setup_workdir_and_migrate()
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
        # Persist agent self-updates. Codex updates via npm -> point the global prefix at a
        # PERSISTED dir (the wrapper prefers it; kept OFF the front of PATH so the MCP wrapper at
        # /usr/local/bin/codex still wins). Claude's native updater already targets ~/.local/bin,
        # which the entrypoint now persists — make sure it's on PATH so the updated build is used.
        home = os.path.expanduser("~")
        ws = os.environ.get("TCB_BROWSE_ROOT", "/workspace")
        os.environ["NPM_CONFIG_PREFIX"] = f"{ws}/.agent-home/codex-npm"
        cleaned = f"{home}/.local/bin:{cleaned}:{ws}/.agent-home/codex-npm/bin"
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
        # The PTY hit EOF — the shell exited (`exit`, Ctrl-D, or the process died). Close the
        # socket so the browser's onclose fires and it can relaunch a FRESH shell. Without this
        # the receive loop below would block forever on a dead shell and the terminal would hang.
        try:
            await websocket.close()
        except Exception:
            pass

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
    _require_typst_mode()
    return resolver.start()


@app.post("/api/preview/stop")
def preview_stop():
    _require_typst_mode()
    resolver.stop()
    return resolver.status()


@app.get("/api/preview/status")
def preview_status():
    _require_typst_mode()
    return resolver.status()


@app.post("/api/preview/resolve")
async def preview_resolve(request: Request):
    """Resolve a page coordinate (pt) to a source range, in-process via the Rust resolver."""
    _require_typst_mode()
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
    _require_typst_mode()
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
    _require_typst_mode()
    return {"notes": notes_mod.list_notes()}


@app.patch("/api/notes")
async def patch_note(request: Request):
    """Edit one speaker note. Body: {raw: <exact existing content>, text: <new content>}."""
    _require_typst_mode()
    body = await request.json() or {}
    return await notes_mod.update_note(body.get("raw", ""), body.get("text", ""))


@app.post("/api/notes")
async def create_note(request: Request):
    """Add a speaker note. Body: {slide_line, text, sub_index?, sub_total?}. When the slide
    has multiple subslides the note is gated to `sub_index` (see notes.create_note)."""
    _require_typst_mode()
    body = await request.json() or {}
    return await notes_mod.create_note(
        body.get("slide_line"), body.get("text", ""),
        body.get("sub_index"), body.get("sub_total"),
    )


@app.get("/api/slide-map")
async def slide_map():
    """Per-page presenter data: section, subslide index, and the **per-page transcript** for that
    page (authoritative, from touying's pdfpc mapping). Used by the inline notes + presenter."""
    if runtime.document_type() == "pdf":
        try:
            expected = _capture_pdf_identity()
            def observe(_pdf_path, page_count):
                transcripts = pdf_transcript.load(expected.pdf.parent, expected.pdf.name, page_count)
                page_notes = transcripts.get("pages", {})
                pages = [{"page": page, "slide_no": page, "slide_total": page_count,
                          "project_type": "pdf",
                          "note": (page_notes.get(str(page), {}) or {}).get("text", "")}
                         for page in range(1, page_count + 1)]
                orphans = [
                    {"page": int(page), "text": (entry or {}).get("text", "")}
                    for page, entry in sorted(transcripts.get("orphans", {}).items(), key=lambda item: int(item[0]))
                ]
                return {"pages": pages, "total": page_count, "orphans": orphans}
            return await asyncio.to_thread(_locked_pdf_observation, observe, expected)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    await docstore.flush_now()  # so the pdfpc query sees the latest content
    loop = asyncio.get_running_loop()
    # per-page notes (pdfpc) + source notes (for the editable raw anchor), in parallel-ish
    pdfpc = await loop.run_in_executor(None, notes_mod.pdfpc_pages)
    src_notes = notes_mod.list_notes()
    # match a page's note text to a source #speaker-note so it stays editable
    raw_by_text = {n["text"].strip(): n["raw"] for n in src_notes}
    sl_by_text = {n["text"].strip(): n["slide_line"] for n in src_notes}
    nl_by_text = {n["text"].strip(): n["note_line"] for n in src_notes}   # source line of the #speaker-note
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
            "note_line": nl_by_text.get(note.strip()),
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


def _locked_pdf_observation(operation, expected: _PdfIdentity | None = None):
    """Observe an active PDF generation while the publish pair is locked and recovered."""
    expected = expected or _capture_pdf_identity()
    with pdf_service.project_write_lock(expected.project):
        pdf_service.recover_pending(expected.project, expected.render)
        _revalidate_pdf_identity(expected)
        page_count = pdf_service.inspect_pdf(expected.pdf)["page_count"]
        return operation(expected.pdf, page_count)


async def _json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, "invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON body must be an object")
    return body


@app.get("/api/pdf/transcripts")
def get_pdf_transcripts():
    try:
        expected = _capture_pdf_identity()
        return _locked_pdf_observation(
            lambda _pdf_path, page_count: pdf_transcript.load(
                expected.project, "document.pdf", page_count
            ),
            expected,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/pdf/transcripts/{page}")
async def patch_pdf_transcript(page: str, request: Request):
    body = await _json_object(request)
    if set(body) != {"text"} or not isinstance(body.get("text"), str):
        raise HTTPException(400, "body must contain only string text")
    try:
        page_no = int(page)
        if str(page_no) != page:
            raise ValueError("page must be a positive integer")
        expected = _capture_pdf_identity()
        def patch_sync():
            return _locked_pdf_observation(
                lambda _pdf_path, page_count: pdf_transcript.set_page(
                    expected.project, "document.pdf", page_count, page_no, body["text"]
                ),
                expected,
            )
        return await asyncio.to_thread(patch_sync)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/pdf/transcripts/batch")
async def patch_pdf_transcripts(request: Request):
    body = await _json_object(request)
    if set(body) != {"updates"}:
        raise HTTPException(400, "body must contain only updates")
    try:
        expected = _capture_pdf_identity()
        def patch_sync():
            return _locked_pdf_observation(
                lambda _pdf_path, page_count: pdf_transcript.set_pages(
                    expected.project, "document.pdf", page_count, body["updates"]
                ),
                expected,
            )
        return await asyncio.to_thread(patch_sync)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/pdf/transcripts/restore")
async def restore_pdf_orphan_transcript(request: Request):
    """Restore a retained transcript entry to a current PDF page."""
    body = await _json_object(request)
    if set(body) != {"orphan_page", "target_page"}:
        raise HTTPException(400, "body must contain only orphan_page and target_page")
    try:
        expected = _capture_pdf_identity()

        def restore_sync():
            return _locked_pdf_observation(
                lambda _pdf_path, page_count: pdf_transcript.restore_orphan(
                    expected.project, "document.pdf", page_count,
                    body["orphan_page"], body["target_page"],
                ),
                expected,
            )

        return await asyncio.to_thread(restore_sync)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/pdf/replace")
async def replace_pdf(request: Request):
    """Replace the active immutable PDF with durable before/after version snapshots."""
    body = await _json_object(request)
    if set(body) != {"candidate", "message"}:
        raise HTTPException(400, "body must contain only candidate and message")
    candidate = body.get("candidate")
    message = body.get("message")
    if (not isinstance(candidate, str) or not candidate.strip()
            or not isinstance(message, str)):
        raise HTTPException(400, "candidate and message must be strings")
    candidate = candidate.strip()
    if Path(candidate).suffix.lower() != ".pdf":
        raise HTTPException(400, "candidate must be a PDF")
    try:
        expected = _capture_pdf_identity()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    before_message = f"Before PDF replacement: {message.strip() or 'save current PDF'}"
    after_message = f"Replace PDF: {message.strip() or 'install replacement PDF'}"

    def replace_sync():
        with pdf_service.project_write_lock(expected.project):
            pdf_service.recover_pending(expected.project, expected.render)
            # Revalidate after acquiring the cross-process lock: another replacement may have won.
            try:
                _revalidate_pdf_identity(expected)
                pdf_service.validate_replacement_candidate(
                    expected.project, candidate, expected.pdf
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            before_version = vcs.save_version(expected.project, before_message)
            if not before_version.get("ok"):
                detail = before_version.get("error", "could not save pre-replacement version")
                raise HTTPException(500, detail)
            transaction = None
            committed = False
            cleanup_pending = False
            try:
                transaction = pdf_service.prepare_replacement(
                    expected.project,
                    candidate,
                    expected.pdf,
                    expected.render,
                    before_tag=before_version["tag"],
                )
                rendered = transaction.publish()
                transcripts = pdf_transcript.load(
                    expected.project, "document.pdf", rendered["page_count"]
                )
                version = _record_pdf_render_version(
                    rendered["pages"], expected.pdf, expected.identity
                )
                expected_tag = vcs._next_tag(expected.project)
                transaction.commit_intent(expected_tag)
                after_version = vcs.save_version(expected.project, after_message)
                if not after_version.get("ok"):
                    detail = after_version.get("error", "could not save replacement version")
                    raise RuntimeError(detail)
                if after_version.get("tag") != expected_tag:
                    raise RuntimeError("replacement version tag did not match commit intent")
                committed = True
                try:
                    transaction.mark_versioned(expected_tag)
                    transaction.finalize()
                except Exception:
                    # v2 already names the new primary/render/transcript generation.  Cleanup is
                    # recoverable from commit_intent/versioned and must never restore v1.
                    cleanup_pending = True
            except Exception as exc:
                if committed:
                    cleanup_pending = True
                else:
                    # v1 is a complete pre-swap snapshot (primary, candidate, and sidecar).
                    # The WAL is removed only after both its generation and that snapshot are
                    # restored, so a second recovery can resume an interrupted rollback.
                    recovery = {"ok": True}
                    try:
                        if transaction is not None:
                            transaction.rollback_to_before()
                        else:
                            pdf_service.recover_pending(expected.project, expected.render)
                    except Exception as recovery_exc:
                        recovery = {"ok": False, "error": str(recovery_exc)}
                    if recovery["ok"]:
                        try:
                            restored = pdf_service.render_pdf(expected.pdf, expected.render)
                            _record_pdf_render_version(
                                restored["pages"], expected.pdf, expected.identity
                            )
                        except Exception:
                            recovery = {"ok": False, "error": "could not restore PDF render"}
                    detail = str(exc)
                    if not recovery.get("ok"):
                        detail += "; recovery failed: " + recovery.get(
                            "error", "unknown error"
                        )
                    raise HTTPException(500, detail) from exc
            return {
                "ok": True,
                "page_count": rendered["page_count"],
                "pages": rendered["pages"],
                "transcripts": transcripts,
                "version": version,
                "before_version": before_version,
                "after_version": after_version,
                "cleanup_pending": cleanup_pending,
            }
    return await asyncio.to_thread(replace_sync)


@app.get("/api/pdf/text")
def get_pdf_text(page: Optional[int] = None):
    def observe(pdf_path, page_count):
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= page_count:
            raise HTTPException(400, "page must be a page within the document")
        return {"page": page, "text": pdf_service.extract_page_text(pdf_path, page), "ocr": False}
    try:
        return _locked_pdf_observation(observe)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/locate")
async def api_locate(page: Optional[int] = None, slide: Optional[int] = None):
    """Resolve a 1-based PAGE (a rendered subslide, as counted in the preview) or SLIDE (a
    `#slide[...]` call; one slide can render as several pages) to SOURCE LINES. These are
    DIFFERENT: a page maps to its enclosing slide's opener + a subslide index; a slide spans
    opener..close and may cover several pages."""
    _require_typst_mode()
    smap = await slide_map()
    pages = smap.get("pages", [])
    if page is not None:
        row = next((r for r in pages if r.get("page") == page), None)
        if not row:
            return {"ok": False, "error": f"no page {page} (deck has {smap.get('total', 0)} pages)"}
        si = slidemap.slide_info(page) or {}
        return {"ok": True, "kind": "page", "page": page, "slide_no": row.get("slide_no"),
                "slide_line": row.get("slide_line"), "slide_end": si.get("slide_end"),
                "section": row.get("section"), "sub_index": row.get("sub_index"),
                "sub_total": row.get("sub_total"), "sub_lines": si.get("sub_lines"),
                "note_line": row.get("note_line"), "note_raw": row.get("note_raw")}
    if slide is not None:
        rows = [r for r in pages if r.get("slide_no") == slide]
        if not rows:
            return {"ok": False, "error": f"no slide {slide}"}
        first = rows[0]
        si = slidemap.slide_info(first.get("page")) or {}
        return {"ok": True, "kind": "slide", "slide_no": slide,
                "pages": [r.get("page") for r in rows],
                "slide_line": first.get("slide_line"), "slide_end": si.get("slide_end"),
                "section": first.get("section"), "sub_total": len(rows),
                "note_lines": [r.get("note_line") for r in rows if r.get("note_line")]}
    return {"ok": False, "error": "pass page= or slide= (1-based)"}


@app.get("/api/notes/export")
async def export_notes():
    """Per-page narration as one plain-text script (TTS-ready), downloadable."""
    _require_typst_mode()
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
    _require_typst_mode()
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
    _require_typst_mode()
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
    _require_typst_mode()
    body = await request.json()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, resolver.page_start, int(body["page_no"]))


@app.get("/api/source")
def get_source():
    _require_typst_mode()
    return {"source": current_source()}


@app.get("/api/document")
def get_document(file: Optional[str] = None):
    """Live document text for a file (defaults to the active file). For the MCP edit tools.
    `rev` is the room's monotonic revision — pass it back as apply_edits(base_rev=...)."""
    _require_typst_mode()
    return {"file": file or runtime.current_main(), "source": docstore.get_text(file),
            "rev": docstore.get_rev(file)}


@app.post("/api/edit")
async def edit(request: Request):
    """Apply one content-anchored edit to the live CRDT doc; broadcasts + persists."""
    _require_typst_mode()
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
    elif kind == "replace_lines":
        r = await docstore.replace_lines(op["start"], op["end"], op["new_text"], rel)
    elif kind == "insert_at_line":
        r = await docstore.insert_at_line(op["line"], op["text"], rel)
    elif kind == "apply_edits":
        r = await docstore.apply_edits(op["edits"], rel, op.get("base_rev"))
    else:
        raise HTTPException(400, f"unknown op {kind!r}")
    return r


@app.post("/api/reset-from-disk")
async def reset_from_disk():
    """Discard the in-memory CRDT state and re-seed from the .typ on disk (use after an
    external edit). Reload the browser afterward."""
    _require_typst_mode()
    r = await docstore.reset_from_disk()
    return r


@app.post("/api/compile")
async def compile_():
    # Flush the live doc to disk, then WAIT for the resolver's NEXT compile outcome so
    # Refresh reports the real result instead of a blind "success" (or a stale error).
    # We key off `seq`, which bumps on every compile whether it rendered or errored, so a
    # pre-existing error from the previous compile never short-circuits the wait.
    _require_typst_mode()
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
                "pages": typst_service.list_pages(), "tokens": typst_service.page_tokens(),
                "version": st["version"]}
    return {"ok": True, "pages": typst_service.list_pages(),
            "tokens": typst_service.page_tokens(), "version": st["version"]}


@app.get("/api/render/{name}")
def serve_render(name: str):
    if "/" in name or ".." in name:
        raise HTTPException(400, "bad name")
    if runtime.document_type() == "pdf":
        try:
            expected = _capture_pdf_identity()
            def open_page(_pdf_path, _page_count):
                if name not in _pdf_pages(expected.pdf):
                    return None
                return _open_pdf_render_page(name, expected.pdf)
            stream = _locked_pdf_observation(open_page, expected)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if stream is None:
            raise HTTPException(404)
        return StreamingResponse(stream, media_type="image/png",
                                 headers={"Cache-Control": "no-cache"},
                                 background=BackgroundTask(stream.close))
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


@app.post("/api/projects/pdf")
async def create_pdf_project(request: Request):
    """Create a PDF project from exactly one multipart upload."""
    if not app_config.is_configured():
        raise HTTPException(400, "app not configured")
    raw_content_length = request.headers.get("content-length")
    if raw_content_length:
        try:
            content_length = int(raw_content_length)
        except ValueError as exc:
            raise HTTPException(400, "invalid Content-Length") from exc
        if content_length > MAX_PDF_UPLOAD_BYTES + _PDF_MULTIPART_OVERHEAD_BYTES:
            raise HTTPException(413, "PDF upload is too large")
    bounded_request = _pdf_ingress_bound_request(request)
    try:
        form = await bounded_request.form(
            max_files=1, max_fields=1, max_part_size=_PDF_FORM_MAX_PART_BYTES,
        )
    except _PdfIngressTooLarge as exc:
        raise HTTPException(413, "PDF upload is too large") from exc
    except Exception as exc:
        raise HTTPException(400, "invalid multipart form") from exc

    items = list(form.multi_items())
    names = [value for key, value in items if key == "name" and isinstance(value, str)]
    files = [(key, value) for key, value in items if isinstance(value, StarletteUploadFile)]
    if (len(items) != 2 or len(names) != 1 or len(files) != 1
            or files[0][0] != "file"):
        for _, uploaded in files:
            await uploaded.close()
        raise HTTPException(400, "provide one name and exactly one PDF file")

    name = names[0].strip()
    file = files[0][1]
    staged_path: Path | None = None
    try:
        filename = file.filename or ""
        if not name:
            raise HTTPException(400, "name is required")
        if not filename or not filename.lower().endswith(".pdf"):
            raise HTTPException(400, "file must be a PDF")
        if file.size is not None and file.size > MAX_PDF_UPLOAD_BYTES:
            raise HTTPException(413, "PDF upload is too large")
        root = projects_mod._projects_root()
        root.mkdir(parents=True, exist_ok=True)
        fd, raw_staged_path = tempfile.mkstemp(prefix=".pdf-http-upload-", suffix=".pdf", dir=root)
        staged_path = Path(raw_staged_path)
        total = 0
        with os.fdopen(fd, "wb") as stream:
            while chunk := await file.read(_PDF_UPLOAD_CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_PDF_UPLOAD_BYTES:
                    raise HTTPException(413, "PDF upload is too large")
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            return projects_mod.create_pdf_project_from_file(
                name, filename, staged_path, max_bytes=MAX_PDF_UPLOAD_BYTES,
            )
        except FileExistsError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
        await file.close()


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
    try:
        project_type, main_path = _project_document(info)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if project_type == "pdf":
        try:
            await _activate_pdf_project(info, main_path)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        try:
            _setup_workdir_and_migrate()
        except Exception:
            pass
        return {"ok": True, "project": info}
    previous_file = runtime._state.get("file")
    previous_project = _active_project
    try:
        runtime.set_file(str(main_path))
        _active_project = info
        store.set_path(str(runtime.store_path()))
        runtime.backup()
        await docstore.start()
        await docstore.ensure_room()
        resolver.start()
    except Exception:
        try:
            resolver.stop()
        except Exception:
            pass
        await docstore.stop()
        try:
            store.close()
        except Exception:
            pass
        _active_project = previous_project
        runtime.restore_file(previous_file)
        raise
    # Auto-set-up the workdir (Claude + Codex config + enabled vibe-typst MCP server) on every
    # project open, in BOTH local and server mode — so `claude` run in the project dir finds the
    # MCP. (Local mode previously never wrote a .mcp.json, so the MCP couldn't be found.)
    try:
        _setup_workdir_and_migrate()
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
    except ValueError as e:
        raise HTTPException(400, str(e))
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
    if _active_project is not None and _active_project.get("type") == "pdf":
        if target.name == "document.pdf" or target.suffix.lower() == ".pdf":
            raise HTTPException(400, "cannot write PDF files in a PDF project")
    target.write_text(content, encoding="utf-8")
    return {"ok": True}


@app.post("/api/project/files/upload")
async def upload_file(file: UploadFile = File(...), dest: str = ""):
    """Upload a file into the project root or a selected project folder."""
    if not _has_valid_file():
        raise HTTPException(400, "no active project")
    content = await file.read()
    try:
        return projects_mod.store_upload(runtime.project_dir(), file.filename or "upload", content, dest)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/project/files/download")
def download_file(path: str):
    """Download a file from the active project directory."""
    if not _has_valid_file():
        raise HTTPException(400, "no active project")
    if runtime.document_type() == "pdf":
        try:
            expected = _capture_pdf_identity()
            requested = projects_mod._resolve_project_path(expected.project, path)
            if requested == expected.pdf:
                stream = _locked_pdf_observation(
                    lambda pdf_path, _page_count: _open_pdf_document(pdf_path),
                    expected,
                )
                if stream is None:
                    raise HTTPException(404, "file not found")
                return StreamingResponse(
                    stream,
                    media_type="application/pdf",
                    headers={
                        "Cache-Control": "no-cache",
                        "Content-Disposition": f'attachment; filename="{expected.pdf.name}"',
                    },
                    background=BackgroundTask(stream.close),
                )
        except PermissionError:
            raise HTTPException(403, "path not allowed")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
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
    if runtime.document_type() == "pdf":
        try:
            expected = _capture_pdf_identity()
            requested = projects_mod._resolve_project_path(expected.project, path)
            if requested == expected.pdf:
                stream = _locked_pdf_observation(
                    lambda pdf_path, _page_count: _open_pdf_document(pdf_path),
                    expected,
                )
                if stream is None:
                    raise HTTPException(404, "file not found")
                return StreamingResponse(
                    stream,
                    media_type="application/pdf",
                    headers={
                        "Cache-Control": "no-cache",
                        "Content-Disposition": "inline",
                    },
                    background=BackgroundTask(stream.close),
                )
        except PermissionError:
            raise HTTPException(403, "path not allowed")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
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
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))
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
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ---------------------------------------------------------------- git / vcs
@app.get("/api/git/status")
def git_status():
    if not _has_valid_file():
        return {"initialized": False, "dirty": False, "current": None}
    if runtime.document_type() == "pdf":
        try:
            expected = _capture_pdf_identity()
            return _locked_pdf_observation(
                lambda _pdf_path, _page_count: vcs.status(expected.project), expected
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return vcs.status(runtime.project_dir())


@app.get("/api/git/versions")
def git_versions():
    if not _has_valid_file():
        return []
    if runtime.document_type() == "pdf":
        try:
            expected = _capture_pdf_identity()
            return _locked_pdf_observation(
                lambda _pdf_path, _page_count: vcs.list_versions(expected.project), expected
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return vcs.list_versions(runtime.project_dir())


@app.post("/api/git/commit")
async def git_commit(request: Request):
    """Save the current state as a new version (commit + tag)."""
    if not _has_valid_file():
        raise HTTPException(400, "no active project")
    body = await request.json() or {}
    message = (body.get("message") or "").strip()
    if runtime.document_type() == "typst":
        await docstore.flush_now()  # persist in-memory edits before snapshotting
        return vcs.save_version(runtime.project_dir(), message)
    try:
        expected = _capture_pdf_identity()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    def save_pdf():
        return _locked_pdf_observation(
            lambda _pdf_path, _page_count: vcs.save_version(expected.project, message), expected
        )
    return await asyncio.to_thread(save_pdf)


@app.post("/api/git/restore")
async def git_restore(request: Request):
    """Reset the working tree to a tagged version, then reload the editor."""
    if not _has_valid_file():
        raise HTTPException(400, "no active project")
    body = await request.json() or {}
    tag = (body.get("tag") or "").strip()
    if not tag:
        raise HTTPException(400, "tag is required")
    if runtime.document_type() == "pdf":
        try:
            expected = _capture_pdf_identity()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        def restore_pdf():
            with pdf_service.project_write_lock(expected.project):
                pdf_service.recover_pending(expected.project, expected.render)
                _revalidate_pdf_identity(expected)
                prior_head = vcs._head_commit(expected.project)
                if prior_head is None or re.fullmatch(r"[0-9a-f]{40,64}", prior_head) is None:
                    return {"ok": False, "error": "could not capture current version"}, None, True
                prior_render_state = dict(_pdf_render_state)
                retained_render = pdf_service.park_render_for_restore(expected.render)
                try:
                    result = vcs.restore_version(expected.project, tag)
                    if not result["ok"]:
                        raise ValueError(result.get("error", "restore failed"))
                    rendered, transcripts, version = _prepare_pdf(
                        expected.pdf, expected.project_pdf, expected.identity
                    )
                except Exception as exc:
                    rollback_errors = []
                    restored_prior = vcs.restore_version(expected.project, prior_head)
                    if not restored_prior.get("ok"):
                        rollback_errors.append(
                            "Git rollback failed: "
                            + restored_prior.get("error", "unknown error")
                        )
                    try:
                        pdf_service.rollback_parked_render(
                            expected.render, retained_render
                        )
                    except Exception as render_exc:
                        rollback_errors.append(f"render rollback failed: {render_exc}")
                    _pdf_render_state.clear()
                    _pdf_render_state.update(prior_render_state)
                    detail = str(exc)
                    if rollback_errors:
                        detail += "; " + "; ".join(rollback_errors)
                    return {"ok": False, "error": detail}, None, not isinstance(exc, ValueError)
                pdf_service.discard_parked_render(expected.render, retained_render)
                return result, (rendered, transcripts, version), False
        result, activated, server_error = await asyncio.to_thread(restore_pdf)
        if not result["ok"]:
            raise HTTPException(
                500 if server_error else 400,
                result.get("error", "restore failed"),
            )
        _revalidate_pdf_identity(expected)
        await _retire_typst_for_pdf()
        return {"ok": True, "project_type": "pdf"}
    # Rotate the CRDT room FIRST so the soon-to-be-orphaned room can't write its
    # stale in-memory content back over the files we're about to restore.
    new_room = docstore.rotate()
    result = vcs.restore_version(runtime.project_dir(), tag)
    if not result["ok"]:
        raise HTTPException(400, result.get("error", "restore failed"))
    await docstore.start()
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
    if runtime.document_type() == "pdf":
        try:
            expected = _capture_pdf_identity()
            result = await asyncio.to_thread(
                _locked_pdf_observation,
                lambda _pdf_path, _page_count: vcs.delete_version(expected.project, tag),
                expected,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    else:
        result = vcs.delete_version(runtime.project_dir(), tag)
    if not result["ok"]:
        raise HTTPException(400, result.get("error", "delete failed"))
    return {"ok": True}


# ---------------------------------------------------------------- comments
def _require_typst_comments() -> None:
    if runtime.document_type() == "pdf":
        raise HTTPException(400, "comments are unavailable for PDF projects")


@app.get("/api/comments")
def comments(status: Optional[str] = None, file: Optional[str] = None):
    _require_typst_comments()
    return store.list_comments(status, file)


def _line_span(src: str, line1) -> list | None:
    """Code-point [from, to) of a 1-based line's content (excluding its trailing newline)."""
    if not line1:
        return None
    lines = src.split("\n")
    if not (1 <= line1 <= len(lines)):
        return None
    start = sum(len(l) + 1 for l in lines[:line1 - 1])
    return [start, start + len(lines[line1 - 1])]


@app.post("/api/comments")
async def add(request: Request):
    _require_typst_comments()
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
        # Drift-proof anchor: bind each element selection's code-point span to a pycrdt
        # StickyIndex (Yjs RelativePosition) so the comment follows the text across later
        # edits by either the human or the agent. Best-effort — never block comment create.
        if not p.get("rel_anchors"):
            sels = p.get("selections") or p.get("selection") or []
            spans = [[s["from"], s["to"]] for s in sels
                     if isinstance(s, dict) and s.get("from") is not None and s.get("to") is not None]
            # Page selections carry no from/to, so anchor the slide OPENER line (from the
            # enriched slide_info) — this gives page comments a live `location` too, instead
            # of relying on frozen line numbers baked into raw_context.
            for s in sels:
                if isinstance(s, dict) and s.get("kind") == "page":
                    span = _line_span(src, (s.get("slide") or {}).get("slide_line"))
                    if span:
                        spans.append(span)
            if spans:
                try:
                    p["rel_anchors"] = await docstore.make_rel_anchors(spans, p.get("file"))
                except BaseException:            # pycrdt can raise a BaseException-only panic
                    pass
        created.append(store.add_comment(p))
    return created


@app.get("/api/comments/{cid}/anchor")
async def comment_anchor(cid: str):
    """Resolve a comment's drift-proof StickyIndex anchors to CURRENT code-point spans and
    the live text they now cover (so the UI/agent jumps to the right place after edits)."""
    _require_typst_comments()
    c = store.get_comment(cid)
    if not c:
        raise HTTPException(404)
    rel = c.get("rel_anchors")
    if not rel:
        return {"id": cid, "spans": [], "texts": [], "lines": [], "rev": docstore.get_rev(c.get("file"))}
    spans = await docstore.resolve_rel_anchors(rel, c.get("file"))
    src = docstore.get_text(c.get("file")) or ""
    return {"id": cid, "spans": spans, "texts": [src[a:b] for a, b in spans],
            "lines": [src.count("\n", 0, a) + 1 for a, b in spans],   # 1-based line of each span start
            "rev": docstore.get_rev(c.get("file"))}


@app.patch("/api/comments/{cid}")
async def patch(cid: str, request: Request):
    _require_typst_comments()
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
    _require_typst_comments()
    c = store.get_comment(cid)
    if not c:
        raise HTTPException(404)
    return store.get_events(c["id"])


@app.post("/api/comments/{cid}/done")
async def mark_done(cid: str, request: Request):
    _require_typst_comments()
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
    _require_typst_comments()
    c = store.set_status(cid, "pending")
    if not c:
        raise HTTPException(404)
    return c


@app.delete("/api/comments/{cid}")
def delete(cid: str):
    _require_typst_comments()
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
