import hashlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI


ROOT = Path(__file__).resolve().parents[1]
CONTROL_DIR = ROOT / "control"
sys.path.insert(0, str(CONTROL_DIR))

import mcp_store
import mcp_transfer
from mcp_errors import McpServiceError
from pat_store import PatIdentity


class TransferStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db_path = root / "control.db"
        self.workspace_base = root / "workspaces"
        self.workspace_base.mkdir()
        mcp_store.migrate(self.db_path)
        self.identity = PatIdentity(
            token_id="token-a",
            user_id="user-a",
            username="alice",
            port=9101,
            scopes=frozenset({"files:write"}),
            expires_at=None,
        )
        self.other_identity = PatIdentity(
            token_id="token-b",
            user_id="user-b",
            username="bob",
            port=9102,
            scopes=frozenset({"files:write"}),
            expires_at=None,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _begin(self, **overrides):
        arguments = {
            "kind": "file",
            "project_id": "p1",
            "destination": "assets/a.png",
            "size": 4,
            "sha256": hashlib.sha256(b"data").hexdigest(),
            "filename": "a.png",
            "now": 100,
        }
        arguments.update(overrides)
        return mcp_store.begin_upload(
            self.db_path, self.identity, **arguments
        )

    def test_upload_capability_is_hashed_one_time_and_bound_to_metadata(self):
        public, capability = self._begin()
        session = mcp_store.authorize_upload(
            self.db_path,
            public["id"],
            capability,
            now=public["created_at"] + 1,
        )
        self.assertEqual(session.destination, "assets/a.png")
        self.assertEqual(session.size, 4)

        with sqlite3.connect(self.db_path) as db:
            dump = "\n".join(db.iterdump())
        self.assertNotIn(capability, dump)

        mcp_store.mark_upload_received(
            self.db_path, session.id, 4, now=102
        )
        completed = mcp_store.complete_upload(
            self.db_path, session.id, self.identity, now=103
        )
        self.assertEqual(completed.state, "finishing")
        with self.assertRaisesRegex(
            McpServiceError, "UPLOAD_ALREADY_USED"
        ):
            mcp_store.complete_upload(
                self.db_path, session.id, self.identity, now=104
            )

    def test_upload_rejects_wrong_secret_owner_expiry_and_third_live_session(self):
        first, first_capability = self._begin()
        with self.assertRaisesRegex(McpServiceError, "UPLOAD_EXPIRED"):
            mcp_store.authorize_upload(
                self.db_path,
                first["id"],
                first_capability,
                now=first["expires_at"],
            )
        with self.assertRaisesRegex(McpServiceError, "UPLOAD_EXPIRED"):
            mcp_store.authorize_upload(
                self.db_path, first["id"], "wrong", now=101
            )

        second, _ = self._begin(now=101, destination="b.png")
        with self.assertRaisesRegex(McpServiceError, "RATE_LIMITED"):
            self._begin(now=102, destination="c.png")

        mcp_store.mark_upload_received(
            self.db_path, second["id"], 4, now=103
        )
        with self.assertRaisesRegex(McpServiceError, "UPLOAD_EXPIRED"):
            mcp_store.complete_upload(
                self.db_path,
                second["id"],
                self.other_identity,
                now=104,
            )

    def test_download_capability_is_hashed_bound_and_claimed_once(self):
        public, capability = mcp_store.begin_download(
            self.db_path,
            self.identity,
            "p1",
            "/api/project/files/download?path=notes.pdf",
            filename="notes.pdf",
            size=12,
            sha256="a" * 64,
            now=100,
        )

        claimed = mcp_store.claim_download(
            self.db_path,
            public["id"],
            capability,
            now=101,
        )

        self.assertEqual(claimed.backend_path, (
            "/api/project/files/download?path=notes.pdf"
        ))
        self.assertEqual(claimed.state, "streaming")
        with self.assertRaisesRegex(
            McpServiceError, "UPLOAD_ALREADY_USED"
        ):
            mcp_store.claim_download(
                self.db_path, public["id"], capability, now=102
            )
        with sqlite3.connect(self.db_path) as db:
            self.assertNotIn(capability, "\n".join(db.iterdump()))

    def test_transfer_sweep_removes_only_expired_exact_payloads(self):
        expired, _ = self._begin(now=0, destination="expired.bin")
        live, _ = self._begin(now=100, destination="live.bin")
        upload_dir = (
            self.workspace_base / "alice" / ".tcb" / "uploads"
        )
        upload_dir.mkdir(parents=True)
        expired_file = upload_dir / f"{expired['id']}.ready"
        live_file = upload_dir / f"{live['id']}.ready"
        unrelated = upload_dir / "unrelated.ready"
        expired_file.write_bytes(b"expired")
        live_file.write_bytes(b"live")
        unrelated.write_bytes(b"keep")

        result = mcp_store.sweep_transfer_sessions(
            self.db_path, self.workspace_base, now=950
        )

        self.assertEqual(result["uploads"], 1)
        self.assertFalse(expired_file.exists())
        self.assertTrue(live_file.exists())
        self.assertTrue(unrelated.exists())
        with sqlite3.connect(self.db_path) as db:
            ids = {
                row[0]
                for row in db.execute("SELECT id FROM upload_sessions")
            }
        self.assertEqual(ids, {live["id"]})


class UploadRouteTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db_path = root / "control.db"
        self.workspace_base = root / "workspaces"
        self.workspace_base.mkdir()
        mcp_store.migrate(self.db_path)
        self.identity = PatIdentity(
            token_id="token-a",
            user_id="user-a",
            username="alice",
            port=9101,
            scopes=frozenset({"files:write"}),
            expires_at=None,
        )
        app = FastAPI()
        app.include_router(mcp_transfer.create_transfer_router(
            self.db_path,
            self.workspace_base,
            gateway=None,
        ))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://slides.example",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self._tmp.cleanup()

    def _begin(self, content: bytes, **overrides):
        arguments = {
            "kind": "file",
            "project_id": "p1",
            "destination": "assets/a.bin",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "filename": "a.bin",
        }
        arguments.update(overrides)
        return mcp_store.begin_upload(
            self.db_path, self.identity, **arguments
        )

    async def test_put_streams_to_ready_file_and_is_not_reusable(self):
        public, capability = self._begin(b"data")

        response = await self.client.put(
            f"/mcp-upload/{public['id']}",
            content=b"data",
            headers={"Authorization": f"Upload {capability}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        ready = (
            self.workspace_base
            / "alice"
            / ".tcb"
            / "uploads"
            / f"{public['id']}.ready"
        )
        self.assertEqual(ready.read_bytes(), b"data")
        reused = await self.client.put(
            f"/mcp-upload/{public['id']}",
            content=b"data",
            headers={"Authorization": f"Upload {capability}"},
        )
        self.assertEqual(reused.status_code, 409)

    async def test_put_rejects_short_long_bad_hash_and_cleans_partial_files(self):
        cases = [
            (b"abc", {"size": 4}, 400),
            (b"abcde", {"size": 4}, 413),
            (b"abcd", {"sha256": "0" * 64}, 400),
        ]
        for number, (content, overrides, status) in enumerate(cases):
            with self.subTest(number=number):
                public, capability = self._begin(
                    content, destination=f"{number}.bin", **overrides
                )
                response = await self.client.put(
                    f"/mcp-upload/{public['id']}",
                    content=content,
                    headers={
                        "Authorization": f"Upload {capability}"
                    },
                )
                self.assertEqual(response.status_code, status)
                upload_dir = (
                    self.workspace_base / "alice" / ".tcb" / "uploads"
                )
                self.assertFalse(
                    (upload_dir / f"{public['id']}.part").exists()
                )
                self.assertFalse(
                    (upload_dir / f"{public['id']}.ready").exists()
                )

    async def test_put_requires_upload_authorization(self):
        public, _ = self._begin(b"data")

        missing = await self.client.put(
            f"/mcp-upload/{public['id']}", content=b"data"
        )
        bearer = await self.client.put(
            f"/mcp-upload/{public['id']}",
            content=b"data",
            headers={"Authorization": "Bearer not-an-upload"},
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(bearer.status_code, 401)


class _DownloadGateway:
    def __init__(self):
        self.paths = []

    @asynccontextmanager
    async def stream_response(self, identity, path):
        self.paths.append((identity.user_id, identity.port, path))
        yield httpx.Response(
            200,
            content=b"exported pdf",
            headers={
                "Content-Type": "application/pdf",
                "Content-Length": "12",
            },
        )


class DownloadRouteTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db_path = root / "control.db"
        self.workspace_base = root / "workspaces"
        self.workspace_base.mkdir()
        mcp_store.migrate(self.db_path)
        self.identity = PatIdentity(
            token_id="token-a",
            user_id="user-a",
            username="alice",
            port=9101,
            scopes=frozenset({"files:read"}),
            expires_at=None,
        )
        self.gateway = _DownloadGateway()
        app = FastAPI()
        app.include_router(mcp_transfer.create_transfer_router(
            self.db_path, self.workspace_base, self.gateway
        ))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://slides.example",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self._tmp.cleanup()

    async def test_download_proxies_only_stored_path_and_is_one_time(self):
        public, capability = mcp_store.begin_download(
            self.db_path,
            self.identity,
            "p1",
            "/api/export-pdf",
            filename="deck.pdf",
            size=12,
            sha256=hashlib.sha256(b"exported pdf").hexdigest(),
        )

        response = await self.client.get(
            f"/mcp-download/{public['id']}",
            headers={"Authorization": f"Download {capability}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"exported pdf")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("deck.pdf", response.headers["content-disposition"])
        self.assertEqual(
            self.gateway.paths,
            [("user-a", 9101, "/api/export-pdf")],
        )
        reused = await self.client.get(
            f"/mcp-download/{public['id']}",
            headers={"Authorization": f"Download {capability}"},
        )
        self.assertEqual(reused.status_code, 409)


if __name__ == "__main__":
    unittest.main()
