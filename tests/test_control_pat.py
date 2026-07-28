import hashlib
import importlib.util
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
CONTROL_DIR = ROOT / "control"
sys.path.insert(0, str(CONTROL_DIR))

import pat_store


def _create_control_database(path: Path) -> tuple[str, str]:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE users (
                id         TEXT PRIMARY KEY,
                username   TEXT UNIQUE NOT NULL,
                pw_hash    TEXT NOT NULL,
                port       INTEGER UNIQUE NOT NULL,
                created_at REAL NOT NULL,
                role       TEXT NOT NULL DEFAULT 'user',
                locked     INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        db.executemany(
            """
            INSERT INTO users
                (id, username, pw_hash, port, created_at, role, locked)
            VALUES (?, ?, ?, ?, ?, 'user', 0)
            """,
            [
                ("user-a", "alice", "unused", 9101, 1.0),
                ("user-b", "bob", "unused", 9102, 2.0),
            ],
        )
    pat_store.migrate(path)
    return "user-a", "user-b"


class PatStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "control.db"
        self.user_id, self.other_user_id = _create_control_database(self.db_path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_plaintext_is_returned_once_and_only_its_hash_is_stored(self):
        public, raw = pat_store.issue_token(
            self.db_path, self.user_id, "remote-codex", "editor", None
        )

        self.assertTrue(raw.startswith(f"vbt_{public['id']}_"))
        self.assertEqual(public["name"], "remote-codex")
        self.assertEqual(public["preset"], "editor")
        self.assertNotIn("secret", public)
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT token_hash, token_prefix FROM api_tokens WHERE id=?",
                (public["id"],),
            ).fetchone()
            database_dump = "\n".join(db.iterdump())

        self.assertEqual(row[0], hashlib.sha256(raw.encode()).hexdigest())
        self.assertNotEqual(row[0], raw)
        self.assertNotIn(raw, database_dump)
        identity = pat_store.authenticate(self.db_path, raw)
        self.assertIsNotNone(identity)
        self.assertEqual(identity.user_id, self.user_id)
        self.assertEqual(identity.username, "alice")
        self.assertEqual(identity.port, 9101)
        self.assertEqual(identity.scopes, pat_store.EDITOR_SCOPES)

    def test_revoked_expired_locked_and_deleted_user_tokens_do_not_authenticate(self):
        active, active_raw = pat_store.issue_token(
            self.db_path, self.user_id, "active", "viewer", time.time() + 60
        )
        _, expired_raw = pat_store.issue_token(
            self.db_path, self.user_id, "expired", "viewer", time.time() - 1
        )

        self.assertIsNotNone(pat_store.authenticate(self.db_path, active_raw))
        self.assertIsNone(pat_store.authenticate(self.db_path, expired_raw))
        self.assertTrue(
            pat_store.revoke_token(self.db_path, self.user_id, active["id"])
        )
        self.assertIsNone(pat_store.authenticate(self.db_path, active_raw))

        _, locked_raw = pat_store.issue_token(
            self.db_path, self.other_user_id, "locked", "editor", None
        )
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE users SET locked=1 WHERE id=?", (self.other_user_id,))
        self.assertIsNone(pat_store.authenticate(self.db_path, locked_raw))

        with sqlite3.connect(self.db_path) as db:
            db.execute("DELETE FROM users WHERE id=?", (self.other_user_id,))
        self.assertIsNone(pat_store.authenticate(self.db_path, locked_raw))

    def test_listing_and_revocation_are_owner_scoped_and_hide_secrets(self):
        own, raw = pat_store.issue_token(
            self.db_path, self.user_id, "reader", "viewer", None
        )
        pat_store.issue_token(
            self.db_path, self.other_user_id, "someone-else", "editor", None
        )

        listed = pat_store.list_tokens(self.db_path, self.user_id)

        self.assertEqual([item["id"] for item in listed], [own["id"]])
        self.assertEqual(listed[0]["preset"], "viewer")
        self.assertEqual(listed[0]["scopes"], sorted(pat_store.VIEWER_SCOPES))
        self.assertNotIn("secret", listed[0])
        self.assertNotIn("token_hash", listed[0])
        self.assertNotIn(raw, repr(listed))
        self.assertFalse(
            pat_store.revoke_token(
                self.db_path, self.other_user_id, own["id"]
            )
        )
        self.assertIsNotNone(pat_store.authenticate(self.db_path, raw))

    def test_names_presets_expiry_and_token_shape_are_validated(self):
        invalid_calls = [
            ("", "viewer", None),
            ("x" * 129, "viewer", None),
            ("ok", "administrator", None),
            ("ok", "viewer", "tomorrow"),
        ]
        for name, preset, expires_at in invalid_calls:
            with self.subTest(name=name, preset=preset, expires_at=expires_at):
                with self.assertRaises(ValueError):
                    pat_store.issue_token(
                        self.db_path,
                        self.user_id,
                        name,
                        preset,
                        expires_at,
                    )

        self.assertIsNone(pat_store.authenticate(self.db_path, "not-a-token"))
        self.assertIsNone(pat_store.authenticate(self.db_path, "vbt_missing"))
        self.assertIsNone(
            pat_store.authenticate(self.db_path, "vbt_unknown_deadbeef")
        )

    def test_last_used_timestamp_is_written_at_most_once_per_minute(self):
        public, raw = pat_store.issue_token(
            self.db_path, self.user_id, "reader", "viewer", None
        )

        self.assertIsNotNone(pat_store.authenticate(self.db_path, raw, now=1000))
        with sqlite3.connect(self.db_path) as db:
            first = db.execute(
                "SELECT last_used_at FROM api_tokens WHERE id=?", (public["id"],)
            ).fetchone()[0]
        self.assertEqual(first, 1000)

        self.assertIsNotNone(pat_store.authenticate(self.db_path, raw, now=1059))
        with sqlite3.connect(self.db_path) as db:
            throttled = db.execute(
                "SELECT last_used_at FROM api_tokens WHERE id=?", (public["id"],)
            ).fetchone()[0]
        self.assertEqual(throttled, 1000)

        self.assertIsNotNone(pat_store.authenticate(self.db_path, raw, now=1060))
        with sqlite3.connect(self.db_path) as db:
            updated = db.execute(
                "SELECT last_used_at FROM api_tokens WHERE id=?", (public["id"],)
            ).fetchone()[0]
        self.assertEqual(updated, 1060)


class AccountTokenApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        spec = importlib.util.spec_from_file_location(
            "control_main_pat_test", CONTROL_DIR / "main.py"
        )
        self.control = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.control)
        self.control.DATA_DIR = Path(self._tmp.name)
        self.control.DB_PATH = self.control.DATA_DIR / "control.db"
        self.control.init_db()
        self.user = self.control._create_user("alice", "correct-horse")
        self.other = self.control._create_user("bob", "battery-staple")
        session = self.control._new_session(self.user["id"])
        transport = httpx.ASGITransport(app=self.control.app)
        self.client = httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={self.control.COOKIE: session},
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self._tmp.cleanup()

    async def test_cookie_authenticated_user_can_create_list_and_revoke_a_token(self):
        created = await self.client.post(
            "/account/tokens",
            json={"name": "remote-agent", "preset": "editor"},
        )

        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        self.assertTrue(body["secret"].startswith(f"vbt_{body['token']['id']}_"))
        self.assertEqual(body["token"]["preset"], "editor")
        self.assertEqual(created.headers.get("cache-control"), "no-store")

        listed = await self.client.get("/account/tokens")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(
            [token["id"] for token in listed.json()["tokens"]],
            [body["token"]["id"]],
        )
        self.assertNotIn("secret", listed.text)
        self.assertNotIn(body["secret"], listed.text)

        revoked = await self.client.delete(
            f"/account/tokens/{body['token']['id']}"
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertIsNone(
            pat_store.authenticate(self.control.DB_PATH, body["secret"])
        )
        missing = await self.client.delete(
            f"/account/tokens/{body['token']['id']}"
        )
        self.assertEqual(missing.status_code, 404)

    async def test_account_token_routes_reject_missing_cookie_and_bad_input(self):
        transport = httpx.ASGITransport(app=self.control.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as anonymous:
            response = await anonymous.get("/account/tokens")
        self.assertEqual(response.status_code, 401)

        invalid = await self.client.post(
            "/account/tokens",
            json={"name": "", "preset": "owner"},
        )
        self.assertEqual(invalid.status_code, 400)


if __name__ == "__main__":
    unittest.main()
