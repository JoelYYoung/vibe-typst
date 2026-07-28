import asyncio
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx


ROOT = Path(__file__).resolve().parents[1]
CONTROL_DIR = ROOT / "control"
sys.path.insert(0, str(CONTROL_DIR))

import mcp_limits
import mcp_store
from mcp_errors import ERROR_CODES, McpServiceError
from pat_store import PatIdentity


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


if __name__ == "__main__":
    unittest.main()
