# Remote Project-Control MCP Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Personal Access Tokens, safe active-project leases, and an authenticated Streamable HTTP MCP endpoint with project discovery and lifecycle tools.

**Architecture:** The control plane owns PAT verification, scopes, leases, rate limits, audit records, and the `/mcp` transport. It starts the authenticated user's existing container and delegates project operations to the workspace backend. The backend exposes an opaque `context_version`; every MCP project handle is bound to it so a browser project switch invalidates stale calls.

**Tech Stack:** Python 3.11+, FastAPI/Starlette, MCP Python SDK `>=1.28,<2`, SQLite, httpx, unittest, existing Docker/Podman workspace orchestration.

## Global Constraints

- Remote transport is MCP Streamable HTTP over HTTPS at `/mcp`.
- The MCP endpoint accepts `Authorization: Bearer <PAT>` only; browser cookies do not authenticate MCP calls.
- PATs belong to existing users, are shown once, and are stored only as SHA-256 hashes.
- Initial presets are exactly `viewer` and `editor`; no project-delete, terminal, or trash-purge scope exists.
- Workspace container ports must be bound to `127.0.0.1`.
- One user has one active project; a project handle is bound to `user + PAT + project_id + context_version`.
- Opening the already active project is idempotent; opening another project, closing, or restarting the backend invalidates old handles.
- Handles expire after 12 hours without use.
- Initial limits are 120 calls/minute/token and four concurrent calls/token.
- Audit records retain identifiers and outcomes, never tokens or request bodies.
- Do not touch the existing untracked `docs/design/` directory.

---

## File Structure

- Create `control/pat_store.py`: PAT schema, issuance, listing, revocation, and bearer authentication.
- Create `control/mcp_store.py`: project-lease and sanitized audit persistence.
- Create `control/mcp_errors.py`: stable structured service errors and MCP conversion.
- Create `control/mcp_limits.py`: in-memory per-token rate/concurrency limiter.
- Create `control/workspace_gateway.py`: async user-container startup and JSON request adapter.
- Create `control/remote_mcp.py`: FastMCP construction, authentication verifier, project tools, and scope checks.
- Modify `control/main.py`: migrations, account-token HTTP APIs, MCP mount/lifespan, and loopback container ports.
- Modify `control/pyproject.toml` and `control/start.sh`: pin and install MCP SDK 1.x.
- Modify `backend/app.py`: active project ID/context version and idempotent project open.
- Create `tests/test_control_pat.py`: PAT persistence and account integration tests.
- Create `tests/test_project_context.py`: backend context-version tests.
- Create `tests/test_control_mcp_foundation.py`: leases, gateway, limits, and real MCP protocol tests.

### Task 1: PAT persistence and account APIs

**Files:**
- Create: `control/pat_store.py`
- Modify: `control/main.py:64-102`
- Modify: `control/main.py:585-625`
- Test: `tests/test_control_pat.py`

**Interfaces:**
- Produces:
  - `VIEWER_SCOPES: frozenset[str]`
  - `EDITOR_SCOPES: frozenset[str]`
  - `PatIdentity(token_id: str, user_id: str, username: str, port: int, scopes: frozenset[str], expires_at: float | None)`
  - `migrate(db_path: Path) -> None`
  - `issue_token(db_path: Path, user_id: str, name: str, preset: str, expires_at: float | None) -> tuple[dict, str]`
  - `list_tokens(db_path: Path, user_id: str) -> list[dict]`
  - `revoke_token(db_path: Path, user_id: str, token_id: str) -> bool`
  - `authenticate(db_path: Path, raw_token: str, now: float | None = None) -> PatIdentity | None`
- Consumes: the existing `users` table in `control.db`.

- [ ] **Step 1: Write failing PAT store tests**

```python
class PatStoreTest(unittest.TestCase):
    def test_plaintext_is_returned_once_and_only_hash_is_stored(self):
        public, raw = pat_store.issue_token(
            self.db_path, self.user_id, "remote-codex", "editor", None
        )
        self.assertTrue(raw.startswith(f"vbt_{public['id']}_"))
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT token_hash, token_prefix FROM api_tokens WHERE id=?",
                (public["id"],),
            ).fetchone()
        self.assertNotIn(raw, row)
        self.assertEqual(
            pat_store.authenticate(self.db_path, raw).user_id,
            self.user_id,
        )

    def test_revoked_expired_and_locked_tokens_do_not_authenticate(self):
        public, raw = pat_store.issue_token(
            self.db_path, self.user_id, "short", "viewer", time.time() + 60
        )
        self.assertIsNotNone(pat_store.authenticate(self.db_path, raw))
        self.assertTrue(pat_store.revoke_token(self.db_path, self.user_id, public["id"]))
        self.assertIsNone(pat_store.authenticate(self.db_path, raw))
```

- [ ] **Step 2: Run the PAT tests and verify RED**

Run:

```bash
backend/.venv/bin/python -m unittest discover -s tests -p 'test_control_pat.py' -v
```

Expected: import failure for `pat_store`.

- [ ] **Step 3: Implement the PAT schema and high-entropy token flow**

Implement these tables and constants in `control/pat_store.py`:

```python
VIEWER_SCOPES = frozenset({
    "projects:read", "files:read", "slides:read",
    "transcripts:read", "comments:read",
})
EDITOR_SCOPES = VIEWER_SCOPES | frozenset({
    "projects:write", "files:write", "documents:write",
    "transcripts:write", "comments:write",
})

CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    token_prefix TEXT NOT NULL,
    scopes TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL,
    last_used_at REAL,
    revoked_at REAL
)
```

Use `secrets.token_urlsafe(32)` for the bearer secret and
`hashlib.sha256(raw.encode()).hexdigest()` for lookup. Validate names as non-empty and at most
128 characters. Accept only `viewer` or `editor`. Parse the public ID before hashing so malformed
tokens fail without a table scan. Join `api_tokens` to `users` during authentication and reject
locked/missing users. Throttle `last_used_at` writes to at most once per minute.

- [ ] **Step 4: Add cookie-authenticated token management APIs**

Add routes before the catch-all in `control/main.py`:

```python
@app.get("/account/tokens")
async def account_tokens(request: Request):
    user = _require_user(request)
    return {"tokens": pat_store.list_tokens(DB_PATH, user["id"])}

@app.post("/account/tokens")
async def account_create_token(request: Request):
    user = _require_user(request)
    body = await request.json() or {}
    public, raw = pat_store.issue_token(
        DB_PATH, user["id"], body.get("name", ""),
        body.get("preset", "viewer"), body.get("expires_at"),
    )
    return {"token": public, "secret": raw}

@app.delete("/account/tokens/{token_id}")
async def account_revoke_token(token_id: str, request: Request):
    user = _require_user(request)
    if not pat_store.revoke_token(DB_PATH, user["id"], token_id):
        raise HTTPException(404, "token not found")
    return {"ok": True}
```

Call `pat_store.migrate(DB_PATH)` from `init_db()`. Add no admin endpoint that returns token
secrets.

- [ ] **Step 5: Run focused and existing control-adjacent tests**

Run:

```bash
backend/.venv/bin/python -m unittest discover -s tests -p 'test_control_pat.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_regressions.py' -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add control/pat_store.py control/main.py tests/test_control_pat.py
git commit -m "feat: add personal access tokens"
```

### Task 2: Backend active-project context version

**Files:**
- Modify: `backend/app.py:35-55`
- Modify: `backend/app.py:537-610`
- Modify: `backend/app.py:1588-1630`
- Modify: `backend/app.py:1735-1865`
- Test: `tests/test_project_context.py`

**Interfaces:**
- Produces:
  - `_active_context() -> dict[str, str | None]`
  - `_set_active_project(project: dict | None) -> None`
  - `/api/state` fields `project_id` and `context_version`
  - project-open response fields `project` and `context_version`
- Consumes: existing `_active_project`, type-aware `_activate_pdf_project`, Typst activation, and
  project close.

- [ ] **Step 1: Write failing context-version tests**

```python
class ProjectContextVersionTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_project_open_is_idempotent_and_switch_rotates_context(self):
        first = await self.app.open_project("alpha")
        first_context = first["context_version"]
        second = await self.app.open_project("alpha")
        self.assertEqual(second["context_version"], first_context)
        switched = await self.app.open_project("beta")
        self.assertNotEqual(switched["context_version"], first_context)

    async def test_close_rotates_and_clears_active_project(self):
        opened = await self.app.open_project("alpha")
        self.app.close_project()
        state = self.app.app_state()
        self.assertIsNone(state["project_id"])
        self.assertNotEqual(state["context_version"], opened["context_version"])
```

Patch resolver/docstore/PDF activation in the fixture so the test exercises project-state logic
without starting subprocesses.

- [ ] **Step 2: Run the context tests and verify RED**

Run:

```bash
backend/.venv/bin/python -m unittest discover -s tests -p 'test_project_context.py' -v
```

Expected: missing `context_version`.

- [ ] **Step 3: Add one setter for all active-project transitions**

At module load:

```python
_active_project: dict | None = None
_project_context_version = secrets.token_urlsafe(24)

def _active_context() -> dict:
    return {
        "project_id": (_active_project or {}).get("id"),
        "context_version": _project_context_version,
    }

def _set_active_project(project: dict | None) -> None:
    global _active_project, _project_context_version
    before = (_active_project or {}).get("id")
    after = (project or {}).get("id")
    if before != after:
        _project_context_version = secrets.token_urlsafe(24)
    _active_project = project
```

Replace direct `_active_project = ...` assignments in open, close, active rename, active delete,
and activation rollback. Preserve context during a rename and when reopening the same ID.

- [ ] **Step 4: Return context fields consistently**

Merge `_active_context()` into:

- Typst `/api/state`;
- PDF `/api/state`;
- `/api/app/state`;
- successful Typst and PDF project-open responses;
- project-close response.

When `/api/app/state` recovers a persisted active project after restart, call
`_set_active_project(proj)` before returning it, so later state calls agree on the ID.

- [ ] **Step 5: Make same-project open truly idempotent**

Before restarting resolver/docstore/PDF services, detect an already-valid active project:

```python
if (_active_project or {}).get("id") == project_id and runtime.current_file() == main_path:
    return {
        "ok": True,
        "project": info,
        "context_version": _project_context_version,
    }
```

Do not skip activation if the persisted runtime path does not match the project metadata.

- [ ] **Step 6: Run focused and backend regression tests**

Run:

```bash
backend/.venv/bin/python -m unittest discover -s tests -p 'test_project_context.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_pdf_projects.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_regressions.py' -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add backend/app.py tests/test_project_context.py
git commit -m "feat: version active project contexts"
```

### Task 3: Lease, audit, error, and limit primitives

**Files:**
- Create: `control/mcp_store.py`
- Create: `control/mcp_errors.py`
- Create: `control/mcp_limits.py`
- Modify: `control/main.py:64-102`
- Test: `tests/test_control_mcp_foundation.py`

**Interfaces:**
- Produces:
  - `McpServiceError(code, message, retryable=False, retry_after=None)`
  - `issue_lease(db_path, identity, project_id, context_version, now=None) -> tuple[dict, str]`
  - `validate_lease(db_path, raw_handle, identity, project_id, context_version, now=None) -> Lease`
  - `invalidate_token_leases(db_path, token_id) -> None`
  - `invalidate_user_leases(db_path, user_id) -> None`
  - `record_audit(db_path, AuditEvent) -> None`
  - `TokenLimiter(calls_per_minute=120, max_concurrent=4)`
- Consumes: `pat_store.PatIdentity`.

- [ ] **Step 1: Write failing lease and limiter tests**

```python
def test_lease_is_bound_to_token_user_project_and_context(self):
    public, raw = mcp_store.issue_lease(
        self.db_path, self.identity, "project-a", "ctx-a", now=100
    )
    lease = mcp_store.validate_lease(
        self.db_path, raw, self.identity, "project-a", "ctx-a", now=101
    )
    self.assertEqual(lease.project_id, "project-a")
    with self.assertRaisesRegex(McpServiceError, "PROJECT_CONTEXT_CHANGED"):
        mcp_store.validate_lease(
            self.db_path, raw, self.identity, "project-b", "ctx-b", now=102
        )

async def test_limiter_rejects_fifth_concurrent_call(self):
    limiter = TokenLimiter(calls_per_minute=120, max_concurrent=4)
    entered = [await limiter.acquire("token-a") for _ in range(4)]
    with self.assertRaisesRegex(McpServiceError, "RATE_LIMITED"):
        await limiter.acquire("token-a")
    for permit in entered:
        permit.release()
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
backend/.venv/bin/python -m unittest discover -s tests -p 'test_control_mcp_foundation.py' -v
```

Expected: modules do not exist.

- [ ] **Step 3: Implement lease and audit tables**

Use hashed, random 32-byte handles and these tables:

```sql
CREATE TABLE IF NOT EXISTS project_leases (
    id TEXT PRIMARY KEY,
    handle_hash TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    context_version TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_used_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_audit_log (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    project_id TEXT,
    targets TEXT NOT NULL,
    started_at REAL NOT NULL,
    completed_at REAL NOT NULL,
    outcome TEXT NOT NULL,
    error_code TEXT,
    correlation_id TEXT NOT NULL
);
```

Set lease expiry to `now + 12 * 3600` on successful validation. Delete expired rows during
validation and periodic cleanup. Serialize only relative paths/comment IDs in `targets`.

- [ ] **Step 4: Implement structured errors and limiter**

`McpServiceError.as_dict()` must return:

```python
{
    "ok": False,
    "error": {
        "code": self.code,
        "message": self.message,
        "retryable": self.retryable,
        **({"retry_after": self.retry_after} if self.retry_after is not None else {}),
    },
}
```

Implement a token-bucket window using `time.monotonic()` and an `asyncio.Lock`. The permit is an
async-context-compatible object whose `release()` is idempotent. Never persist raw handles or
request bodies.

Define and test the complete first-version code set:

```text
AUTH_REQUIRED TOKEN_INVALID TOKEN_EXPIRED TOKEN_REVOKED ACCOUNT_LOCKED
SCOPE_DENIED RATE_LIMITED WORKSPACE_STARTING WORKSPACE_UNAVAILABLE
PROJECT_NOT_FOUND PROJECT_CONTEXT_CHANGED PROJECT_HANDLE_EXPIRED
REVISION_CONFLICT PATH_NOT_ALLOWED FILE_NOT_FOUND DESTINATION_EXISTS
FILE_TOO_LARGE CHECKSUM_MISMATCH UPLOAD_EXPIRED UPLOAD_ALREADY_USED
CAPABILITY_NOT_AVAILABLE BACKEND_ERROR
```

- [ ] **Step 5: Implement retention cleanup**

Add `sweep_expired(db_path, now)` to delete expired leases and audit rows whose
`completed_at < now - 90 * 86400`. Cover both boundaries in tests. The control lifespan starts a
cancellable task that runs this sweep every 60 seconds and runs it once at startup.

- [ ] **Step 6: Wire migrations and run tests**

Call `mcp_store.migrate(DB_PATH)` from `control.main.init_db()`.
Update token revocation to call `invalidate_token_leases(DB_PATH, token_id)` in the same request
after the owner-scoped token row is revoked.

Run:

```bash
backend/.venv/bin/python -m unittest discover -s tests -p 'test_control_mcp_foundation.py' -v
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add control/mcp_store.py control/mcp_errors.py control/mcp_limits.py control/main.py tests/test_control_mcp_foundation.py
git commit -m "feat: add MCP leases and limits"
```

### Task 4: Workspace gateway and context validation

**Files:**
- Create: `control/workspace_gateway.py`
- Modify: `control/main.py:188-330`
- Test: `tests/test_control_mcp_foundation.py`

**Interfaces:**
- Produces:
  - `WorkspaceGateway(ensure_workspace, workspace_up, client, public_base_url)`
  - `async list_projects(identity) -> list[dict]`
  - `async create_typst_project(identity, name) -> dict`
  - `async open_project(identity, project_id) -> dict`
  - `async active_context(identity) -> dict`
  - `async request(identity, method, path, json=None, timeout=30) -> dict`
- Consumes: `PatIdentity.port`, control `_ensure_workspace`, and the backend JSON APIs.

- [ ] **Step 1: Add failing gateway tests with httpx MockTransport**

```python
async def test_gateway_starts_workspace_then_calls_only_identity_port(self):
    started = []
    async def ensure(identity):
        started.append(identity.username)
    gateway = WorkspaceGateway(
        ensure_workspace=ensure,
        workspace_up=lambda identity: False,
        client=self.client,
        public_base_url="https://slides.example",
    )
    projects = await gateway.list_projects(self.identity)
    self.assertEqual(started, ["alice"])
    self.assertEqual(projects[0]["id"], "p1")
    self.assertEqual(self.requested_urls, ["http://127.0.0.1:9101/api/projects"])
```

Also test 503/connection errors become `WORKSPACE_STARTING` or `WORKSPACE_UNAVAILABLE` without
exposing port numbers.

- [ ] **Step 2: Run and verify RED**

Run:

```bash
backend/.venv/bin/python -m unittest discover -s tests -p 'test_control_mcp_foundation.py' -v
```

Expected: missing `WorkspaceGateway`.

- [ ] **Step 3: Implement gateway request normalization**

Use only URLs constructed from the authenticated `identity.port`:

```python
url = f"http://127.0.0.1:{identity.port}{path}"
```

Do not accept a host, port, or absolute URL from tool arguments. Bound JSON responses and map
backend 400/404/409 statuses to stable MCP errors. Return a web URL using:

```python
f"{public_base_url}/?openProject={quote(project_id, safe='')}"
```

- [ ] **Step 4: Bind future workspace containers to loopback**

Change the container run argument in `_start_workspace` from:

```python
"-p", f"{port}:8080",
```

to:

```python
"-p", f"127.0.0.1:{port}:8080",
```

Add a command-construction test that asserts the exact binding and unchanged workspace mount.
Do not recreate existing containers in this task.

- [ ] **Step 5: Run focused tests**

Run:

```bash
backend/.venv/bin/python -m unittest discover -s tests -p 'test_control_mcp_foundation.py' -v
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add control/workspace_gateway.py control/main.py tests/test_control_mcp_foundation.py
git commit -m "feat: add authenticated workspace gateway"
```

### Task 5: Streamable HTTP MCP and project tools

**Files:**
- Create: `control/remote_mcp.py`
- Modify: `control/main.py:43-62`
- Modify: `control/main.py:510-780`
- Modify: `control/pyproject.toml`
- Modify: `control/start.sh:8-30`
- Test: `tests/test_control_mcp_foundation.py`

**Interfaces:**
- Produces:
  - `PatTokenVerifier(TokenVerifier)`
  - `create_remote_mcp(db_path, gateway, public_base_url) -> FastMCP`
  - tools `list_projects`, `create_typst_project`, `open_project`, `get_project`,
    `rename_project`, and `close_project`
- Consumes: PAT store, lease store, limiter, gateway, and stable error model.

- [ ] **Step 1: Pin and install the MCP dependency**

Add:

```toml
"mcp>=1.28,<2",
```

to `control/pyproject.toml` and both new-environment install branches in `control/start.sh`.
Because `start.sh` currently skips all dependency installation when `.venv` already exists, add
an idempotent version check after that block:

```bash
if ! "$VENV/bin/python" -c \
  'import importlib.metadata as m; from packaging.version import Version; v=Version(m.version("mcp")); assert Version("1.28") <= v < Version("2")' \
  >/dev/null 2>&1; then
  if command -v "$UV" >/dev/null 2>&1; then
    "$UV" pip install --python "$VENV/bin/python" "mcp>=1.28,<2" "packaging>=24"
  else
    "$VENV/bin/pip" install "mcp>=1.28,<2" "packaging>=24"
  fi
fi
```

Install `packaging>=24` with the base control dependencies so the version check is available.
Sync the existing development environment:

```bash
uv pip install --python control/.venv/bin/python "mcp>=1.28,<2" "packaging>=24"
```

Verify:

```bash
control/.venv/bin/python -c 'import importlib.metadata; print(importlib.metadata.version("mcp"))'
```

Expected: a `1.x` version at least `1.28`.

- [ ] **Step 2: Write a failing authenticated protocol test**

Start the combined ASGI app on an ephemeral loopback port and use the official client:

```python
async with streamable_http_client(
    f"http://127.0.0.1:{port}/mcp",
    http_client=httpx.AsyncClient(
        headers={"Authorization": f"Bearer {self.raw_pat}"}
    ),
) as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        self.assertIn("open_project", names)
        opened = await session.call_tool(
            "open_project", {"project_id": "p1"}
        )
        self.assertIn("project_handle", opened.structuredContent)
```

Also assert missing/invalid bearer tokens fail and Viewer cannot call a write tool.

- [ ] **Step 3: Implement the verifier and scope helper**

Create `PatTokenVerifier.verify_token()`:

```python
identity = pat_store.authenticate(self.db_path, token)
if identity is None:
    return None
return AccessToken(
    token=token,
    client_id=identity.token_id,
    subject=identity.user_id,
    scopes=sorted(identity.scopes),
    expires_at=int(identity.expires_at) if identity.expires_at else None,
    claims={
        "token_id": identity.token_id,
        "user_id": identity.user_id,
        "username": identity.username,
        "port": identity.port,
    },
)
```

Tool functions recover identity through `get_access_token()`, reconstruct a `PatIdentity`, enforce
the exact required scope, acquire a limiter permit, call the gateway, and record one sanitized
audit event in `finally`.

- [ ] **Step 4: Construct a stateless JSON FastMCP server**

Use:

```python
FastMCP(
    "vibe-typst-projects",
    token_verifier=PatTokenVerifier(db_path),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(public_base_url),
        resource_server_url=AnyHttpUrl(f"{public_base_url}/mcp"),
        required_scopes=[],
    ),
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[public_host, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[public_base_url, "http://127.0.0.1:*", "http://localhost:*"],
    ),
)
```

`PUBLIC_BASE_URL` defaults to `http://localhost:${PORT}` and must not end in `/`.

- [ ] **Step 5: Implement project tools and handle validation**

`open_project` calls the gateway, issues a lease, and returns:

```python
{
    "ok": True,
    "project": project,
    "project_handle": raw_handle,
    "capabilities": capabilities_for(project["type"]),
    "web_url": f"{public_base_url}/?openProject={project['id']}",
}
```

All other project-scoped tools validate `project_handle` against a fresh backend
`project_id/context_version`. `close_project` closes only if the handle is current.

- [ ] **Step 6: Mount MCP before the catch-all and run its lifespan**

Create the FastMCP object after config and helper definitions. In the existing control lifespan:

```python
async with remote_mcp.session_manager.run():
    yield
```

Register:

```python
app.mount("/mcp", remote_mcp.streamable_http_app())
```

before `@app.api_route("/{path:path}", ...)`.

- [ ] **Step 7: Run protocol, auth, backend, and syntax tests**

Run:

```bash
control/.venv/bin/python -m unittest discover -s tests -p 'test_control_pat.py' -v
control/.venv/bin/python -m unittest discover -s tests -p 'test_control_mcp_foundation.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_project_context.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_regressions.py' -v
control/.venv/bin/python -m py_compile control/main.py control/pat_store.py control/mcp_store.py control/mcp_errors.py control/mcp_limits.py control/workspace_gateway.py control/remote_mcp.py
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add control/remote_mcp.py control/main.py control/pyproject.toml control/start.sh tests/test_control_mcp_foundation.py
git commit -m "feat: expose project control MCP"
```

### Task 6: Foundation verification checkpoint

**Files:**
- No planned source modifications.

**Interfaces:**
- Produces: a remotely callable, PAT-authenticated MCP with safe project handles.
- Consumes: all earlier foundation tasks.

- [ ] **Step 1: Run the complete Python suite**

```bash
control/.venv/bin/python -m unittest discover -s tests -v
```

If the control environment lacks backend-only packages, run the split equivalent:

```bash
control/.venv/bin/python -m unittest discover -s tests -p 'test_control_*.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_project_context.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_pdf_projects.py' -v
backend/.venv/bin/python -m unittest discover -s tests -p 'test_regressions.py' -v
```

Expected: all pass.

- [ ] **Step 2: Exercise a real local MCP round trip**

Run the protocol fixture, which starts the combined control ASGI app on an ephemeral loopback
port against a temporary control database/workspace:

```bash
control/.venv/bin/python -m unittest discover -s tests -p 'test_control_mcp_foundation.py' -v
```

The `test_real_streamable_http_round_trip` case issues a PAT and calls `tools/list`,
`list_projects`, and `open_project`. It asserts the returned web URL contains
`openProject=<stable-id>` and no PAT/handle.

- [ ] **Step 3: Verify repository hygiene**

```bash
git diff --check
git status --short
```

Expected: only intentional plan/implementation changes; `docs/design/` remains untouched.
