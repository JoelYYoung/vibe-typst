import asyncio
import hashlib
import importlib.util
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.routing import Mount


ROOT = Path(__file__).resolve().parents[1]
CONTROL_DIR = ROOT / "control"
sys.path.insert(0, str(CONTROL_DIR))

import mcp_limits
import mcp_store
from mcp_errors import ERROR_CODES, McpServiceError
from pat_store import PatIdentity
import pat_store
from remote_mcp import PatTokenVerifier, create_remote_mcp
from workspace_gateway import WorkspaceGateway


EXPECTED_ERROR_CODES = frozenset(
    {
        "AUTH_REQUIRED",
        "TOKEN_INVALID",
        "TOKEN_EXPIRED",
        "TOKEN_REVOKED",
        "ACCOUNT_LOCKED",
        "SCOPE_DENIED",
        "RATE_LIMITED",
        "WORKSPACE_STARTING",
        "WORKSPACE_UNAVAILABLE",
        "PROJECT_NOT_FOUND",
        "PROJECT_CONTEXT_CHANGED",
        "PROJECT_HANDLE_EXPIRED",
        "REVISION_CONFLICT",
        "PATH_NOT_ALLOWED",
        "FILE_NOT_FOUND",
        "DESTINATION_EXISTS",
        "FILE_TOO_LARGE",
        "CHECKSUM_MISMATCH",
        "UPLOAD_EXPIRED",
        "UPLOAD_ALREADY_USED",
        "CAPABILITY_NOT_AVAILABLE",
        "BACKEND_ERROR",
    }
)


class _FailIfReadStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise AssertionError("oversized response body was read")
        yield b""


class _FakeProjectGateway:
    public_base_url = "https://slides.example"

    def __init__(self):
        self.projects = [
            {"id": "p1", "name": "One", "type": "typst"},
            {"id": "pdf-1", "name": "PDF", "type": "pdf"},
        ]
        self.active = None
        self.context_version = "ctx-empty"
        self.files = {"notes.md": b"old"}

    async def list_projects(self, identity):
        return [dict(project) for project in self.projects]

    async def create_typst_project(self, identity, name):
        project = {
            "id": "created",
            "name": name,
            "type": "typst",
        }
        self.projects.append(project)
        return dict(project)

    async def open_project(self, identity, project_id):
        project = next(
            (
                project
                for project in self.projects
                if project["id"] == project_id
            ),
            None,
        )
        if project is None:
            raise McpServiceError("PROJECT_NOT_FOUND", "project not found")
        self.active = project
        self.context_version = f"ctx-{project_id}"
        return {
            "ok": True,
            "project": dict(project),
            "project_id": project_id,
            "context_version": self.context_version,
        }

    async def active_context(self, identity):
        return {
            "active_project": (
                dict(self.active) if self.active is not None else None
            ),
            "project_id": (
                self.active["id"] if self.active is not None else None
            ),
            "context_version": self.context_version,
        }

    async def request(
        self, identity, method, path, json=None, timeout=30
    ):
        if method == "GET" and path == "/api/agent/files":
            return {
                "items": [{
                    "path": "notes.md",
                    "name": "notes.md",
                    "type": "file",
                    "size": len(self.files["notes.md"]),
                    "protected": False,
                }],
                "project_id": self.active["id"],
                "context_version": self.context_version,
            }
        if method == "GET" and path.startswith(
            "/api/agent/files/read?"
        ):
            content = self.files["notes.md"]
            return {
                "path": "notes.md",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "total_lines": 1,
                "shown": "1-1",
                "text": f"1: {content.decode()}",
                "truncated": False,
                "next": None,
                "download_required": False,
                "project_id": self.active["id"],
                "context_version": self.context_version,
            }
        if (
            method == "POST"
            and path == "/api/agent/files/write"
        ):
            self.files["notes.md"] = json["content"].encode()
            return {
                "path": "notes.md",
                "size": len(self.files["notes.md"]),
                "sha256": hashlib.sha256(
                    self.files["notes.md"]
                ).hexdigest(),
                "project_id": self.active["id"],
                "context_version": self.context_version,
            }
        if method == "PATCH" and path == "/api/projects/p1":
            self.active = {**self.active, "name": json["name"]}
            self.projects = [
                self.active if item["id"] == "p1" else item
                for item in self.projects
            ]
            return dict(self.active)
        if method == "POST" and path == "/api/projects/close":
            self.active = None
            self.context_version = "ctx-closed"
            return {
                "ok": True,
                "project_id": None,
                "context_version": self.context_version,
            }
        raise AssertionError(f"unexpected gateway request: {method} {path}")

    def project_web_url(self, project_id):
        return f"https://slides.example/?openProject={project_id}"


class McpErrorTest(unittest.TestCase):
    def test_structured_errors_expose_only_the_stable_public_contract(self):
        error = McpServiceError(
            "RATE_LIMITED",
            "too many requests",
            retryable=True,
            retry_after=2.5,
        )

        self.assertEqual(
            error.as_dict(),
            {
                "ok": False,
                "error": {
                    "code": "RATE_LIMITED",
                    "message": "too many requests",
                    "retryable": True,
                    "retry_after": 2.5,
                },
            },
        )
        self.assertEqual(str(error), "RATE_LIMITED: too many requests")
        self.assertEqual(ERROR_CODES, EXPECTED_ERROR_CODES)
        with self.assertRaises(ValueError):
            McpServiceError("INTERNAL_TRACEBACK", "must not escape")

    def test_retry_after_is_omitted_when_the_error_has_no_delay(self):
        body = McpServiceError(
            "PROJECT_NOT_FOUND", "project not found"
        ).as_dict()

        self.assertNotIn("retry_after", body["error"])
        self.assertFalse(body["error"]["retryable"])


class McpStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "control.db"
        mcp_store.migrate(self.db_path)
        self.identity = PatIdentity(
            token_id="token-a",
            user_id="user-a",
            username="alice",
            port=9101,
            scopes=frozenset({"projects:read"}),
            expires_at=None,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_lease_is_hashed_and_bound_to_token_user_project_and_context(self):
        public, raw = mcp_store.issue_lease(
            self.db_path,
            self.identity,
            "project-a",
            "ctx-a",
            now=100,
        )

        self.assertTrue(raw.startswith(f"vph_{public['id']}_"))
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT handle_hash FROM project_leases WHERE id=?",
                (public["id"],),
            ).fetchone()
            database_dump = "\n".join(db.iterdump())
        self.assertNotEqual(row[0], raw)
        self.assertNotIn(raw, database_dump)

        lease = mcp_store.validate_lease(
            self.db_path,
            raw,
            self.identity,
            "project-a",
            "ctx-a",
            now=101,
        )
        self.assertEqual(lease.project_id, "project-a")
        self.assertEqual(lease.context_version, "ctx-a")
        self.assertEqual(lease.expires_at, 101 + 12 * 3600)

        with self.assertRaises(McpServiceError) as changed:
            mcp_store.validate_lease(
                self.db_path,
                raw,
                self.identity,
                "project-b",
                "ctx-b",
                now=102,
            )
        self.assertEqual(changed.exception.code, "PROJECT_CONTEXT_CHANGED")

    def test_invalid_expired_and_cross_identity_handles_fail_closed(self):
        public, raw = mcp_store.issue_lease(
            self.db_path,
            self.identity,
            "project-a",
            "ctx-a",
            now=100,
        )
        other_identity = PatIdentity(
            token_id="token-b",
            user_id="user-b",
            username="bob",
            port=9102,
            scopes=self.identity.scopes,
            expires_at=None,
        )

        for candidate, identity, now in [
            ("not-a-handle", self.identity, 101),
            (raw + "tampered", self.identity, 101),
            (raw, other_identity, 101),
            (raw, self.identity, 100 + 12 * 3600),
        ]:
            with self.subTest(candidate=candidate, identity=identity.token_id):
                with self.assertRaises(McpServiceError) as caught:
                    mcp_store.validate_lease(
                        self.db_path,
                        candidate,
                        identity,
                        "project-a",
                        "ctx-a",
                        now=now,
                    )
                self.assertEqual(caught.exception.code, "PROJECT_HANDLE_EXPIRED")

        with sqlite3.connect(self.db_path) as db:
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM project_leases WHERE id=?", (public["id"],)
                ).fetchone()
            )

    def test_invalidation_removes_only_the_requested_token_or_user_leases(self):
        _, token_a = mcp_store.issue_lease(
            self.db_path, self.identity, "one", "ctx-1", now=1
        )
        second_token = PatIdentity(
            token_id="token-b",
            user_id="user-a",
            username="alice",
            port=9101,
            scopes=self.identity.scopes,
            expires_at=None,
        )
        _, token_b = mcp_store.issue_lease(
            self.db_path, second_token, "two", "ctx-2", now=1
        )
        other_user = PatIdentity(
            token_id="token-c",
            user_id="user-b",
            username="bob",
            port=9102,
            scopes=self.identity.scopes,
            expires_at=None,
        )
        _, token_c = mcp_store.issue_lease(
            self.db_path, other_user, "three", "ctx-3", now=1
        )

        mcp_store.invalidate_token_leases(self.db_path, "token-a")
        with self.assertRaises(McpServiceError):
            mcp_store.validate_lease(
                self.db_path,
                token_a,
                self.identity,
                "one",
                "ctx-1",
                now=2,
            )
        self.assertIsNotNone(
            mcp_store.validate_lease(
                self.db_path,
                token_b,
                second_token,
                "two",
                "ctx-2",
                now=2,
            )
        )

        mcp_store.invalidate_user_leases(self.db_path, "user-a")
        with self.assertRaises(McpServiceError):
            mcp_store.validate_lease(
                self.db_path,
                token_b,
                second_token,
                "two",
                "ctx-2",
                now=3,
            )
        self.assertIsNotNone(
            mcp_store.validate_lease(
                self.db_path,
                token_c,
                other_user,
                "three",
                "ctx-3",
                now=3,
            )
        )

    def test_audit_records_only_sanitized_targets_and_retention_is_90_days(self):
        now = 100 * 86400
        cutoff = now - 90 * 86400
        events = [
            mcp_store.AuditEvent(
                user_id="user-a",
                token_id="token-a",
                tool_name="write_file",
                project_id="project-a",
                targets=("slides/main.typ",),
                started_at=cutoff - 2,
                completed_at=cutoff - 1,
                outcome="ok",
                error_code=None,
                correlation_id="request-old",
            ),
            mcp_store.AuditEvent(
                user_id="user-a",
                token_id="token-a",
                tool_name="set_comment_status",
                project_id="project-a",
                targets=("comment:abc123",),
                started_at=cutoff - 1,
                completed_at=cutoff,
                outcome="error",
                error_code="REVISION_CONFLICT",
                correlation_id="request-boundary",
            ),
        ]
        for event in events:
            mcp_store.record_audit(self.db_path, event)

        with sqlite3.connect(self.db_path) as db:
            rows = db.execute(
                """
                SELECT targets, correlation_id
                FROM mcp_audit_log ORDER BY completed_at
                """
            ).fetchall()
        self.assertEqual(json.loads(rows[0][0]), ["slides/main.typ"])
        self.assertEqual(json.loads(rows[1][0]), ["comment:abc123"])

        mcp_store.issue_lease(
            self.db_path, self.identity, "project-a", "ctx-a", now=0
        )
        removed = mcp_store.sweep_expired(self.db_path, now=12 * 3600)

        self.assertEqual(removed, {"leases": 1, "audit": 0})
        retained = mcp_store.sweep_expired(self.db_path, now=now)
        self.assertEqual(retained, {"leases": 0, "audit": 1})
        with sqlite3.connect(self.db_path) as db:
            surviving = db.execute(
                "SELECT correlation_id FROM mcp_audit_log"
            ).fetchall()
        self.assertEqual(surviving, [("request-boundary",)])

        invalid = mcp_store.AuditEvent(
            user_id="user-a",
            token_id="token-a",
            tool_name="write_file",
            project_id="project-a",
            targets=("../../secret",),
            started_at=1,
            completed_at=2,
            outcome="ok",
            error_code=None,
            correlation_id="request-invalid",
        )
        with self.assertRaises(ValueError):
            mcp_store.record_audit(self.db_path, invalid)


class TokenLimiterTest(unittest.IsolatedAsyncioTestCase):
    async def test_fifth_concurrent_call_is_rejected_and_release_is_idempotent(self):
        limiter = mcp_limits.TokenLimiter(
            calls_per_minute=120, max_concurrent=4
        )
        entered = [await limiter.acquire("token-a") for _ in range(4)]

        with self.assertRaises(McpServiceError) as caught:
            await limiter.acquire("token-a")
        self.assertEqual(caught.exception.code, "RATE_LIMITED")
        self.assertTrue(caught.exception.retryable)

        entered[0].release()
        entered[0].release()
        replacement = await limiter.acquire("token-a")
        replacement.release()
        for permit in entered[1:]:
            permit.release()

    async def test_minute_window_returns_a_retry_delay_and_is_token_scoped(self):
        limiter = mcp_limits.TokenLimiter(
            calls_per_minute=2, max_concurrent=2
        )
        clock = [100.0]
        with patch.object(
            mcp_limits.time, "monotonic", side_effect=lambda: clock[0]
        ):
            first = await limiter.acquire("token-a")
            first.release()
            clock[0] = 101.0
            second = await limiter.acquire("token-a")
            second.release()
            clock[0] = 102.0
            with self.assertRaises(McpServiceError) as caught:
                await limiter.acquire("token-a")
            self.assertEqual(caught.exception.code, "RATE_LIMITED")
            self.assertEqual(caught.exception.retry_after, 58.0)

            other = await limiter.acquire("token-b")
            other.release()
            clock[0] = 160.0
            after_window = await limiter.acquire("token-a")
            after_window.release()

    async def test_permit_releases_when_async_context_exits_with_an_error(self):
        limiter = mcp_limits.TokenLimiter(
            calls_per_minute=10, max_concurrent=1
        )
        with self.assertRaisesRegex(RuntimeError, "boom"):
            async with await limiter.acquire("token-a"):
                raise RuntimeError("boom")

        permit = await limiter.acquire("token-a")
        permit.release()


class RemoteMcpProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "control.db"
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                CREATE TABLE users (
                    id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    pw_hash TEXT NOT NULL,
                    port INTEGER UNIQUE NOT NULL,
                    created_at REAL NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    locked INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            db.execute(
                """
                INSERT INTO users (
                    id, username, pw_hash, port, created_at, role, locked
                ) VALUES ('user-a', 'alice', 'unused', 9101, 1, 'user', 0)
                """
            )
        pat_store.migrate(self.db_path)
        mcp_store.migrate(self.db_path)
        _, self.editor_pat = pat_store.issue_token(
            self.db_path, "user-a", "editor", "editor", None
        )
        _, self.viewer_pat = pat_store.issue_token(
            self.db_path, "user-a", "viewer", "viewer", None
        )
        self.gateway = _FakeProjectGateway()
        self.remote = create_remote_mcp(
            self.db_path,
            self.gateway,
            "https://slides.example",
        )
        mcp_app = self.remote.streamable_http_app()

        @asynccontextmanager
        async def lifespan(app):
            async with self.remote.session_manager.run():
                yield

        combined = Starlette(
            routes=[Mount("/mcp", app=mcp_app)],
            lifespan=lifespan,
        )
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(128)
        self.port = self.socket.getsockname()[1]
        config = uvicorn.Config(
            combined,
            host="127.0.0.1",
            port=self.port,
            log_level="error",
            access_log=False,
            lifespan="on",
        )
        self.server = uvicorn.Server(config)
        self.server_task = asyncio.create_task(
            self.server.serve(sockets=[self.socket])
        )
        for _ in range(200):
            if self.server.started:
                break
            if self.server_task.done():
                await self.server_task
            await asyncio.sleep(0.01)
        self.assertTrue(self.server.started)
        self.endpoint = f"http://127.0.0.1:{self.port}/mcp"

    async def asyncTearDown(self):
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, timeout=5)
        self.socket.close()
        self._tmp.cleanup()

    async def test_pat_verifier_recovers_identity_claims(self):
        access = await PatTokenVerifier(self.db_path).verify_token(
            self.editor_pat
        )

        self.assertIsNotNone(access)
        self.assertEqual(access.subject, "user-a")
        self.assertEqual(access.client_id, access.claims["token_id"])
        self.assertEqual(access.claims["username"], "alice")
        self.assertEqual(access.claims["port"], 9101)
        self.assertIn("projects:write", access.scopes)
        self.assertIsNone(
            await PatTokenVerifier(self.db_path).verify_token("invalid")
        )

    async def test_mcp_accepts_only_bearer_pat_not_cookie_or_invalid_token(self):
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "auth-test", "version": "1"},
            },
        }
        async with httpx.AsyncClient(follow_redirects=True) as client:
            missing = await client.post(self.endpoint, json=initialize)
            invalid = await client.post(
                self.endpoint,
                json=initialize,
                headers={"Authorization": "Bearer invalid"},
            )
            cookie_only = await client.post(
                self.endpoint,
                json=initialize,
                cookies={"tcb_session": "browser-cookie"},
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(cookie_only.status_code, 401)

    async def test_official_client_exercises_project_tools_handles_and_scopes(self):
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self.editor_pat}"},
            follow_redirects=True,
        ) as http_client:
            async with streamable_http_client(
                self.endpoint, http_client=http_client
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    names = {
                        tool.name
                        for tool in (await session.list_tools()).tools
                    }
                    self.assertEqual(
                        names,
                        {
                            "list_projects",
                            "create_typst_project",
                            "open_project",
                            "get_project",
                            "rename_project",
                            "close_project",
                            "list_files",
                            "read_text_file",
                            "write_text_file",
                            "create_directory",
                            "move_file",
                            "upload_file",
                            "begin_file_upload",
                            "finish_file_upload",
                            "delete_file",
                            "list_deleted_files",
                            "restore_deleted_file",
                        },
                    )

                    listed = await session.call_tool("list_projects", {})
                    self.assertIsNotNone(
                        listed.structuredContent, repr(listed)
                    )
                    self.assertEqual(
                        listed.structuredContent["projects"][0]["id"], "p1"
                    )
                    created = await session.call_tool(
                        "create_typst_project", {"name": "Remote Deck"}
                    )
                    self.assertEqual(
                        created.structuredContent["project"]["name"],
                        "Remote Deck",
                    )
                    opened_pdf = await session.call_tool(
                        "open_project", {"project_id": "pdf-1"}
                    )
                    self.assertIn(
                        "pdf_document",
                        opened_pdf.structuredContent["capabilities"],
                    )
                    self.assertNotIn(
                        "comments",
                        opened_pdf.structuredContent["capabilities"],
                    )
                    opened = await session.call_tool(
                        "open_project", {"project_id": "p1"}
                    )
                    handle = opened.structuredContent["project_handle"]
                    self.assertTrue(handle.startswith("vph_"))
                    self.assertEqual(
                        opened.structuredContent["web_url"],
                        "https://slides.example/?openProject=p1",
                    )
                    self.assertIn(
                        "comments",
                        opened.structuredContent["capabilities"],
                    )

                    got = await session.call_tool(
                        "get_project", {"project_handle": handle}
                    )
                    self.assertEqual(
                        got.structuredContent["project"]["id"], "p1"
                    )
                    remote_files = await session.call_tool(
                        "list_files", {"project_handle": handle}
                    )
                    self.assertEqual(
                        remote_files.structuredContent["items"][0]["path"],
                        "notes.md",
                    )
                    remote_text = await session.call_tool(
                        "read_text_file",
                        {
                            "project_handle": handle,
                            "path": "notes.md",
                        },
                    )
                    written = await session.call_tool(
                        "write_text_file",
                        {
                            "project_handle": handle,
                            "path": "notes.md",
                            "content": "new",
                            "expected_sha256": (
                                remote_text.structuredContent["sha256"]
                            ),
                        },
                    )
                    self.assertTrue(written.structuredContent["ok"])
                    renamed = await session.call_tool(
                        "rename_project",
                        {
                            "project_handle": handle,
                            "name": "Renamed Remotely",
                        },
                    )
                    self.assertEqual(
                        renamed.structuredContent["project"]["name"],
                        "Renamed Remotely",
                    )
                    closed = await session.call_tool(
                        "close_project", {"project_handle": handle}
                    )
                    self.assertTrue(closed.structuredContent["ok"])
                    stale = await session.call_tool(
                        "get_project", {"project_handle": handle}
                    )
                    self.assertEqual(
                        stale.structuredContent["error"]["code"],
                        "PROJECT_CONTEXT_CHANGED",
                    )

        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self.viewer_pat}"},
            follow_redirects=True,
        ) as http_client:
            async with streamable_http_client(
                self.endpoint, http_client=http_client
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    denied = await session.call_tool(
                        "create_typst_project", {"name": "Forbidden"}
                    )
                    self.assertEqual(
                        denied.structuredContent["error"]["code"],
                        "SCOPE_DENIED",
                    )

        with sqlite3.connect(self.db_path) as db:
            audit = db.execute(
                """
                SELECT tool_name, targets, outcome, error_code
                FROM mcp_audit_log ORDER BY started_at, id
                """
            ).fetchall()
        successful_mutations = {
            row[0] for row in audit if row[2] == "ok"
        }
        self.assertEqual(
            successful_mutations,
            {
                "create_typst_project",
                "open_project",
                "write_text_file",
                "rename_project",
                "close_project",
            },
        )
        self.assertTrue(
            all("Remote Deck" not in row[1] for row in audit)
        )


class WorkspaceGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.identity = PatIdentity(
            token_id="token-a",
            user_id="user-a",
            username="alice",
            port=9101,
            scopes=frozenset({"projects:read"}),
            expires_at=None,
        )
        self.requested_urls = []
        self.requested_methods = []

        async def backend(request):
            self.requested_urls.append(str(request.url))
            self.requested_methods.append(request.method)
            if request.url.path == "/api/projects":
                if request.method == "POST":
                    return httpx.Response(
                        200,
                        json={
                            "id": "new-deck",
                            "name": "New Deck",
                            "type": "typst",
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "projects": [
                            {"id": "p1", "name": "One", "type": "typst"}
                        ]
                    },
                )
            if request.url.path == "/api/projects/p1/open":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "project": {
                            "id": "p1",
                            "name": "One",
                            "type": "typst",
                        },
                        "project_id": "p1",
                        "context_version": "ctx-1",
                    },
                )
            if request.url.path == "/api/app/state":
                return httpx.Response(
                    200,
                    json={
                        "active_project": {"id": "p1", "type": "typst"},
                        "project_id": "p1",
                        "context_version": "ctx-1",
                    },
                )
            return httpx.Response(404, json={"detail": "no such project"})

        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(backend)
        )

    async def asyncTearDown(self):
        await self.client.aclose()

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
        created = await gateway.create_typst_project(self.identity, "New Deck")
        opened = await gateway.open_project(self.identity, "p1")
        context = await gateway.active_context(self.identity)

        self.assertEqual(started, ["alice", "alice", "alice", "alice"])
        self.assertEqual(projects[0]["id"], "p1")
        self.assertEqual(created["id"], "new-deck")
        self.assertEqual(opened["project_id"], "p1")
        self.assertEqual(context["context_version"], "ctx-1")
        self.assertEqual(
            self.requested_urls,
            [
                "http://127.0.0.1:9101/api/projects",
                "http://127.0.0.1:9101/api/projects",
                "http://127.0.0.1:9101/api/projects/p1/open",
                "http://127.0.0.1:9101/api/app/state",
            ],
        )
        self.assertEqual(
            gateway.project_web_url("project / one"),
            "https://slides.example/?openProject=project%20%2F%20one",
        )

    async def test_gateway_rejects_non_api_and_absolute_urls(self):
        gateway = WorkspaceGateway(
            ensure_workspace=lambda identity: None,
            workspace_up=lambda identity: True,
            client=self.client,
            public_base_url="https://slides.example",
        )

        for path in (
            "http://attacker.invalid/api/projects",
            "//attacker.invalid/api/projects",
            "/admin/users",
        ):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    await gateway.request(self.identity, "GET", path)
        self.assertEqual(self.requested_urls, [])

    async def test_starting_and_unavailable_errors_hide_internal_ports(self):
        async def connect_failure(request):
            raise httpx.ConnectError("connect failed", request=request)

        failing_client = httpx.AsyncClient(
            transport=httpx.MockTransport(connect_failure)
        )
        self.addAsyncCleanup(failing_client.aclose)

        async def ensure(identity):
            return None

        starting = WorkspaceGateway(
            ensure_workspace=ensure,
            workspace_up=lambda identity: False,
            client=failing_client,
            public_base_url="https://slides.example",
        )
        with self.assertRaises(McpServiceError) as starting_error:
            await starting.list_projects(self.identity)
        self.assertEqual(starting_error.exception.code, "WORKSPACE_STARTING")
        self.assertNotIn("9101", str(starting_error.exception))

        unavailable = WorkspaceGateway(
            ensure_workspace=ensure,
            workspace_up=lambda identity: True,
            client=failing_client,
            public_base_url="https://slides.example",
        )
        with self.assertRaises(McpServiceError) as unavailable_error:
            await unavailable.list_projects(self.identity)
        self.assertEqual(
            unavailable_error.exception.code, "WORKSPACE_UNAVAILABLE"
        )
        self.assertNotIn("9101", str(unavailable_error.exception))

    async def test_backend_statuses_are_normalized_without_leaking_details(self):
        async def backend(request):
            if request.url.path.endswith("/missing/open"):
                return httpx.Response(
                    404,
                    json={"detail": "/private/workspaces/alice/missing"},
                )
            if request.url.path == "/api/projects":
                return httpx.Response(503, text="internal port 9101")
            return httpx.Response(500, text="traceback on port 9101")

        client = httpx.AsyncClient(transport=httpx.MockTransport(backend))
        self.addAsyncCleanup(client.aclose)
        gateway = WorkspaceGateway(
            ensure_workspace=lambda identity: None,
            workspace_up=lambda identity: True,
            client=client,
            public_base_url="https://slides.example",
        )

        with self.assertRaises(McpServiceError) as missing:
            await gateway.open_project(self.identity, "missing")
        self.assertEqual(missing.exception.code, "PROJECT_NOT_FOUND")
        self.assertNotIn("/private/", str(missing.exception))

        with self.assertRaises(McpServiceError) as starting:
            await gateway.list_projects(self.identity)
        self.assertEqual(starting.exception.code, "WORKSPACE_STARTING")
        self.assertNotIn("9101", str(starting.exception))

    async def test_agent_file_conflicts_keep_stable_revision_error(self):
        async def backend(request):
            return httpx.Response(
                409,
                json={
                    "detail": {
                        "code": "REVISION_CONFLICT",
                        "message": "private path omitted",
                        "current_sha256": "a" * 64,
                    }
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(backend))
        self.addAsyncCleanup(client.aclose)
        gateway = WorkspaceGateway(
            ensure_workspace=lambda identity: None,
            workspace_up=lambda identity: True,
            client=client,
            public_base_url="https://slides.example",
        )

        with self.assertRaises(McpServiceError) as conflict:
            await gateway.request(
                self.identity,
                "POST",
                "/api/agent/files/write",
                json={
                    "path": "notes.md",
                    "content": "new",
                    "expected_sha256": "0" * 64,
                },
            )

        self.assertEqual(conflict.exception.code, "REVISION_CONFLICT")
        self.assertNotIn("private path", str(conflict.exception))

    async def test_declared_oversized_json_is_rejected_without_reading_it(self):
        async def backend(request):
            return httpx.Response(
                200,
                headers={"Content-Length": str(9 * 1024 * 1024)},
                stream=_FailIfReadStream(),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(backend))
        self.addAsyncCleanup(client.aclose)
        gateway = WorkspaceGateway(
            ensure_workspace=lambda identity: None,
            workspace_up=lambda identity: True,
            client=client,
            public_base_url="https://slides.example",
        )

        with self.assertRaises(McpServiceError) as caught:
            await gateway.request(self.identity, "GET", "/api/state")
        self.assertEqual(caught.exception.code, "BACKEND_ERROR")


class ControlMcpStoreWiringTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        spec = importlib.util.spec_from_file_location(
            "control_main_mcp_store_test", CONTROL_DIR / "main.py"
        )
        self.control = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.control)
        self.control.DATA_DIR = Path(self._tmp.name)
        self.control.DB_PATH = self.control.DATA_DIR / "control.db"
        self.control.init_db()
        self.user = self.control._create_user("alice", "correct-horse")

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_init_migrates_mcp_tables_and_pat_revocation_deletes_leases(self):
        from pat_store import issue_token

        public, raw_pat = issue_token(
            self.control.DB_PATH,
            self.user["id"],
            "remote",
            "editor",
            None,
        )
        identity = self.control.pat_store.authenticate(
            self.control.DB_PATH, raw_pat
        )
        mcp_store.issue_lease(
            self.control.DB_PATH,
            identity,
            "project-a",
            "ctx-a",
            now=1,
        )
        session = self.control._new_session(self.user["id"])
        transport = httpx.ASGITransport(app=self.control.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={self.control.COOKIE: session},
        ) as client:
            response = await client.delete(f"/account/tokens/{public['id']}")

        self.assertEqual(response.status_code, 200, response.text)
        with sqlite3.connect(self.control.DB_PATH) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM project_leases").fetchone()[0],
                0,
            )
            self.assertIsNotNone(
                db.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='mcp_audit_log'
                    """
                ).fetchone()
            )

    async def test_control_lifespan_sweeps_once_at_startup(self):
        identity = PatIdentity(
            token_id="token-a",
            user_id=self.user["id"],
            username="alice",
            port=self.user["port"],
            scopes=frozenset({"projects:read"}),
            expires_at=None,
        )
        public, _ = mcp_store.issue_lease(
            self.control.DB_PATH,
            identity,
            "project-a",
            "ctx-a",
            now=0,
        )
        with sqlite3.connect(self.control.DB_PATH) as db:
            db.execute(
                "UPDATE project_leases SET expires_at=0 WHERE id=?",
                (public["id"],),
            )

        async with self.control.lifespan(self.control.app):
            with sqlite3.connect(self.control.DB_PATH) as db:
                remaining = db.execute(
                    "SELECT COUNT(*) FROM project_leases"
                ).fetchone()[0]

        self.assertEqual(remaining, 0)

    async def test_locking_an_account_immediately_invalidates_its_leases(self):
        admin = self.control._create_user(
            "admin", "administrative-password", "admin"
        )
        identity = PatIdentity(
            token_id="token-a",
            user_id=self.user["id"],
            username="alice",
            port=self.user["port"],
            scopes=frozenset({"projects:read"}),
            expires_at=None,
        )
        mcp_store.issue_lease(
            self.control.DB_PATH,
            identity,
            "project-a",
            "ctx-a",
            now=1,
        )
        session = self.control._new_session(admin["id"])
        transport = httpx.ASGITransport(app=self.control.app)
        with patch.object(
            self.control, "_force_user_offline", return_value=True
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                cookies={self.control.COOKIE: session},
            ) as client:
                response = await client.post(
                    f"/admin/users/{self.user['id']}/locked",
                    json={"locked": True},
                )

        self.assertEqual(response.status_code, 200, response.text)
        with sqlite3.connect(self.control.DB_PATH) as db:
            remaining = db.execute(
                "SELECT COUNT(*) FROM project_leases"
            ).fetchone()[0]
        self.assertEqual(remaining, 0)

    async def test_deleting_an_account_immediately_invalidates_its_leases(self):
        admin = self.control._create_user(
            "admin", "administrative-password", "admin"
        )
        identity = PatIdentity(
            token_id="token-a",
            user_id=self.user["id"],
            username="alice",
            port=self.user["port"],
            scopes=frozenset({"projects:read"}),
            expires_at=None,
        )
        mcp_store.issue_lease(
            self.control.DB_PATH,
            identity,
            "project-a",
            "ctx-a",
            now=1,
        )
        session = self.control._new_session(admin["id"])
        transport = httpx.ASGITransport(app=self.control.app)
        with patch.object(self.control, "_container", return_value=None):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                cookies={self.control.COOKIE: session},
            ) as client:
                response = await client.delete(
                    f"/admin/users/{self.user['id']}"
                )

        self.assertEqual(response.status_code, 200, response.text)
        with sqlite3.connect(self.control.DB_PATH) as db:
            remaining = db.execute(
                "SELECT COUNT(*) FROM project_leases"
            ).fetchone()[0]
        self.assertEqual(remaining, 0)

    async def test_new_workspace_container_publishes_only_on_loopback(self):
        workspace = Path(self._tmp.name) / "alice-workspace"
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with (
            patch.object(self.control, "_is_running", return_value=False),
            patch.object(self.control, "_container_exists", return_value=False),
            patch.object(self.control, "_image_exists", return_value=True),
            patch.object(self.control, "_wsdir", return_value=workspace),
            patch.object(
                self.control, "_container", return_value=completed
            ) as container,
        ):
            started = self.control._start_workspace(self.user)

        self.assertTrue(started)
        args = container.call_args.args
        publish_index = args.index("-p")
        mount_index = args.index("-v")
        self.assertEqual(
            args[publish_index + 1],
            f"127.0.0.1:{self.user['port']}:8080",
        )
        self.assertEqual(
            args[mount_index + 1],
            f"{workspace}:/workspace{self.control.VOLUME_SUFFIX}",
        )

    async def test_control_mounts_exact_mcp_path_before_browser_catch_all(self):
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "mount-test", "version": "1"},
            },
        }
        transport = httpx.ASGITransport(app=self.control.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            missing = await client.post("/mcp", json=initialize)
            cookie_only = await client.post(
                "/mcp",
                json=initialize,
                cookies={self.control.COOKIE: "browser-cookie"},
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(cookie_only.status_code, 401)


if __name__ == "__main__":
    unittest.main()
