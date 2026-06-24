"""
Vibe Typst — Control Plane

Multi-user front-end: cookie auth, workspace container lifecycle, and
transparent reverse-proxy (HTTP + WebSocket) to each user's container.

Usage:
  # Create a user:
  python main.py create-user <username> <password>
  # List users:
  python main.py list-users
  # Run the server:
  uvicorn main:app --host 0.0.0.0 --port 8090
"""

import asyncio
import hashlib
import hmac
import os
import secrets
import sqlite3
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import aiofiles
import httpx
import websockets.client
from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

# ── Config ─────────────────────────────────────────────────────────────────────

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CONTROL_DATA", HERE / "data"))
DB_PATH  = DATA_DIR / "control.db"

TCB_IMAGE      = os.environ.get("TCB_IMAGE",      "tcb-workspace:latest")
WORKSPACE_BASE = Path(os.environ.get("WORKSPACE_BASE", "/workspaces"))
PODMAN_ENV     = os.environ.get("PODMAN_ENV",     "")
BASE_PORT      = int(os.environ.get("BASE_PORT",  "9001"))
SESSION_DAYS   = 30
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)

LOGIN_HTML = HERE / "login.html"

# Hop-by-hop headers that must not be forwarded.
_HOP = frozenset([
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
])

# ── Database ───────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id         TEXT PRIMARY KEY,
            username   TEXT UNIQUE NOT NULL,
            pw_hash    TEXT NOT NULL,
            port       INTEGER UNIQUE NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL,
            expires_at REAL NOT NULL
        );
        """)

def _user_by_name(username: str) -> Optional[dict]:
    with _db() as db:
        row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None

def _user_by_id(uid: str) -> Optional[dict]:
    with _db() as db:
        row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None

def _create_user(username: str, password: str) -> dict:
    uid = secrets.token_hex(8)
    with _db() as db:
        row = db.execute("SELECT MAX(port) FROM users").fetchone()
        port = (row[0] or BASE_PORT - 1) + 1
        db.execute(
            "INSERT INTO users (id,username,pw_hash,port,created_at) VALUES (?,?,?,?,?)",
            (uid, username, _hash_pw(password), port, time.time()),
        )
    return _user_by_id(uid)

# ── Auth ───────────────────────────────────────────────────────────────────────

def _hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 260_000).hex()
    return f"{salt}${h}"

def _check_pw(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
        expected = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 260_000).hex()
        return hmac.compare_digest(h, expected)
    except Exception:
        return False

COOKIE = "tcb_session"

def _new_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    with _db() as db:
        db.execute(
            "INSERT INTO sessions (token,user_id,expires_at) VALUES (?,?,?)",
            (token, user_id, time.time() + SESSION_DAYS * 86400),
        )
    return token

def _session_user(token: str) -> Optional[dict]:
    if not token:
        return None
    with _db() as db:
        row = db.execute(
            "SELECT * FROM sessions WHERE token=? AND expires_at>?",
            (token, time.time()),
        ).fetchone()
    return _user_by_id(row["user_id"]) if row else None

def _del_session(token: str):
    with _db() as db:
        db.execute("DELETE FROM sessions WHERE token=?", (token,))

def _current_user(req) -> Optional[dict]:
    """Extract logged-in user from a Request or WebSocket (both have .cookies)."""
    return _session_user(req.cookies.get(COOKIE, ""))

# ── Orchestrator ───────────────────────────────────────────────────────────────

def _podman(*args) -> subprocess.CompletedProcess:
    cmd = f'source {PODMAN_ENV} && podman {" ".join(str(a) for a in args)}'
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=120)

def _cname(username: str) -> str:
    safe = "".join(c for c in username if c.isalnum() or c in "-_")
    return f"tcb-ws-{safe}"

def _wsdir(username: str) -> Path:
    d = WORKSPACE_BASE / username
    d.mkdir(parents=True, exist_ok=True)
    return d

def _image_exists() -> bool:
    r = _podman("image", "exists", TCB_IMAGE)
    return r.returncode == 0

def _is_running(username: str) -> bool:
    r = _podman("inspect", "--format", "{{.State.Running}}", _cname(username))
    return r.returncode == 0 and r.stdout.strip() == "true"

def _start_workspace(user: dict) -> bool:
    if not _image_exists():
        print(f"[orchestrator] image {TCB_IMAGE!r} not ready yet", file=sys.stderr)
        return False

    name = _cname(user["username"])
    port = user["port"]
    wsdir = _wsdir(user["username"])

    if _is_running(user["username"]):
        return False

    _podman("rm", "-f", name)  # clean up stopped container if any

    env_args = [
        "-e", "APP_MODE=server",
        "-e", "PORT=8080",
        "-e", "TCB_BROWSE_ROOT=/workspace",
        "-e", "RENDER_DIR=/tmp/tcb-render",
        "-e", "TCB_STATE_PATH=/workspace/.tcb/state.json",
        "-e", "HOME=/root",
    ]
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        env_args += ["-e", f"ANTHROPIC_API_KEY={api_key}"]
    r = _podman(
        "run", "-d",
        "--name", name,
        "--restart", "unless-stopped",
        "-p", f"{port}:8080",
        *env_args,
        "-v", f"{wsdir}:/workspace:Z",
        TCB_IMAGE,
    )
    if r.returncode != 0:
        print(f"[orchestrator] start failed for {user['username']}: {r.stderr}", file=sys.stderr)
        return False
    return True

async def _ensure_workspace(user: dict):
    """Start workspace if needed; wait up to 20 s for the backend to be ready."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _start_workspace, user)
    port = user["port"]
    async with httpx.AsyncClient() as c:
        for _ in range(40):
            try:
                r = await c.get(f"http://localhost:{port}/api/state", timeout=1.0)
                if r.status_code < 500:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)

# ── Proxy helpers ──────────────────────────────────────────────────────────────

_client: httpx.AsyncClient = None


async def _proxy_http(request: Request, port: int) -> Response:
    url = httpx.URL(
        f"http://localhost:{port}{request.url.path}",
        query=request.url.query.encode("utf-8") if request.url.query else b"",
    )
    fwd_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _HOP
    }
    body = await request.body()
    rp = _client.build_request(request.method, url, headers=fwd_headers, content=body)
    try:
        resp = await _client.send(rp, stream=True)
    except httpx.ConnectError:
        return HTMLResponse(
            _loading_html("Starting your workspace…",
                          "Your container is warming up. This page will refresh."),
            status_code=503,
        )
    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items() if k.lower() not in _HOP},
        background=BackgroundTask(resp.aclose),
    )


async def _proxy_ws(client_ws: WebSocket, port: int, path: str):
    """Bridge a browser WebSocket to the workspace container."""
    query = str(client_ws.url.query)
    uri = f"ws://localhost:{port}/{path}"
    if query:
        uri += f"?{query}"
    await client_ws.accept()
    try:
        async with websockets.client.connect(uri) as server_ws:
            async def c2s():
                try:
                    while True:
                        msg = await client_ws.receive()
                        if msg.get("type") == "websocket.disconnect":
                            break
                        if "bytes" in msg and msg["bytes"] is not None:
                            await server_ws.send(msg["bytes"])
                        elif "text" in msg and msg["text"] is not None:
                            await server_ws.send(msg["text"])
                except Exception:
                    pass
                finally:
                    await server_ws.close()

            async def s2c():
                try:
                    async for msg in server_ws:
                        if isinstance(msg, bytes):
                            await client_ws.send_bytes(msg)
                        else:
                            await client_ws.send_text(msg)
                except Exception:
                    pass

            await asyncio.gather(c2s(), s2c(), return_exceptions=True)
    except Exception:
        pass
    finally:
        try:
            await client_ws.close()
        except Exception:
            pass


_CARD_CSS = """
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:#0f0f1a;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{text-align:center;color:#c9c9e3;padding:2rem;max-width:440px}
.spin{width:52px;height:52px;border:4px solid rgba(124,106,247,.15);
  border-top-color:#7c6af7;border-radius:50%;animation:s 1s linear infinite;margin:0 auto 1.5rem}
@keyframes s{to{transform:rotate(360deg)}}
h1{font-size:1.4rem;margin:0 0 .5rem;font-weight:600}
p{font-size:.875rem;opacity:.55;margin:.4rem 0 0;line-height:1.5}
.note{font-size:.78rem;opacity:.35;margin-top:1rem}
"""

def _loading_html(msg: str = "Starting your workspace…",
                  sub: str = "This page will refresh automatically.",
                  note: str = "") -> str:
    return f"""<!DOCTYPE html>
<html><head><title>{msg}</title>
<meta http-equiv="refresh" content="5">
<style>{_CARD_CSS}</style></head>
<body><div class="card">
  <div class="spin"></div>
  <h1>{msg}</h1>
  <p>{sub}</p>
  {'<p class="note">' + note + '</p>' if note else ''}
</div></body></html>"""


def _error_html(msg: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>Error</title>
<style>
body{{margin:0;font-family:sans-serif;background:#0f0f1a;display:flex;align-items:center;
  justify-content:center;min-height:100vh;color:#e07070}}
.card{{text-align:center;padding:2rem}}
h1{{font-size:1.3rem;margin:0 0 .5rem}}
p{{font-size:.85rem;opacity:.6;color:#c9c9e3}}
</style></head>
<body><div class="card"><h1>Workspace error</h1><p>{msg}</p></div></body></html>"""


# ── FastAPI app ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    global _client
    init_db()
    _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
    yield
    await _client.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/_health")
async def health():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _current_user(request):
        return RedirectResponse("/")
    async with aiofiles.open(str(LOGIN_HTML)) as f:
        return HTMLResponse(await f.read())


@app.post("/login")
async def do_login(request: Request,
                   username: str = Form(...),
                   password: str = Form(...)):
    user = _user_by_name(username)
    if not user or not _check_pw(password, user["pw_hash"]):
        async with aiofiles.open(str(LOGIN_HTML)) as f:
            html = await f.read()
        html = html.replace(
            'id="error"',
            'id="error" style="display:block"',
        )
        return HTMLResponse(html, status_code=401)

    token = _new_session(user["id"])
    # kick off workspace start without blocking the login response
    asyncio.create_task(_ensure_workspace(user))

    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        COOKIE, token,
        httponly=True, samesite="lax",
        max_age=SESSION_DAYS * 86400,
    )
    return resp


@app.post("/logout")
async def do_logout(request: Request):
    token = request.cookies.get(COOKIE, "")
    if token:
        _del_session(token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ── WebSocket proxy routes (declared before the catch-all) ────────────────────

@app.websocket("/ws/{path:path}")
async def ws_room(websocket: WebSocket, path: str):
    user = _current_user(websocket)
    if not user:
        await websocket.close(code=1008)
        return
    await _proxy_ws(websocket, user["port"], f"ws/{path}")


@app.websocket("/pty")
async def ws_pty(websocket: WebSocket):
    user = _current_user(websocket)
    if not user:
        await websocket.close(code=1008)
        return
    await _proxy_ws(websocket, user["port"], "pty")


# ── Catch-all HTTP proxy ───────────────────────────────────────────────────────

@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def catch_all(request: Request, path: str):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    if not _is_running(user["username"]):
        loop = asyncio.get_event_loop()
        image_ready = await loop.run_in_executor(None, _image_exists)
        if not image_ready:
            return HTMLResponse(
                _loading_html(
                    "Building workspace image…",
                    "The workspace container image is being built on the server.",
                    "This happens once and takes about 20 minutes. "
                    "Monitor progress: tail -f /tmp/tcb-build.log on the server",
                ),
                status_code=503,
            )
        asyncio.create_task(_ensure_workspace(user))
        return HTMLResponse(
            _loading_html(
                "Starting your workspace…",
                "Your container is starting. This page will refresh automatically.",
            ),
            status_code=503,
        )

    return await _proxy_http(request, user["port"])


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="tcb-control CLI")
    sub = parser.add_subparsers(dest="cmd")

    p_cu = sub.add_parser("create-user", help="Add a new user")
    p_cu.add_argument("username")
    p_cu.add_argument("password")

    p_lu = sub.add_parser("list-users", help="List all users")

    p_pw = sub.add_parser("set-password", help="Change a user's password")
    p_pw.add_argument("username")
    p_pw.add_argument("password")

    args = parser.parse_args()
    init_db()

    if args.cmd == "create-user":
        u = _create_user(args.username, args.password)
        print(f"Created: {u['username']}  port={u['port']}")

    elif args.cmd == "list-users":
        with _db() as db:
            rows = db.execute("SELECT username,port,created_at FROM users ORDER BY port").fetchall()
        for r in rows:
            print(f"  {r['username']:20s}  port={r['port']}  created={time.ctime(r['created_at'])}")

    elif args.cmd == "set-password":
        with _db() as db:
            db.execute(
                "UPDATE users SET pw_hash=? WHERE username=?",
                (_hash_pw(args.password), args.username),
            )
        print(f"Password updated for {args.username}")

    else:
        parser.print_help()
