import base64
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from mcp.server.auth.provider import AccessToken


ROOT = Path(__file__).resolve().parents[1]
CONTROL_DIR = ROOT / "control"
sys.path.insert(0, str(CONTROL_DIR))

import mcp_store
from pat_store import PatIdentity
from remote_mcp import _RemoteProjectService


class _FileGateway:
    public_base_url = "https://slides.example"

    def __init__(self):
        self.active = {
            "id": "p1",
            "name": "Deck",
            "type": "typst",
            "main_file": "main.typ",
        }
        self.context_version = "ctx-1"
        self.files = {
            "main.typ": b"= Main\n",
            "notes.md": b"old",
        }
        self.trash = {}
        self.calls = []

    async def active_context(self, identity):
        return {
            "active_project": dict(self.active),
            "project_id": self.active["id"],
            "context_version": self.context_version,
        }

    async def request(
        self, identity, method, path, json=None, timeout=30
    ):
        self.calls.append((identity.token_id, method, path, json))
        parsed = urlsplit(path)
        if method == "GET" and parsed.path == "/api/agent/files":
            return {
                "items": [
                    {
                        "path": name,
                        "name": name,
                        "type": "file",
                        "size": len(content),
                        "protected": name == "main.typ",
                    }
                    for name, content in sorted(self.files.items())
                ],
                "project_id": "p1",
                "context_version": "ctx-1",
            }
        if method == "GET" and parsed.path == "/api/agent/files/read":
            name = parse_qs(parsed.query)["path"][0]
            content = self.files[name]
            return {
                "path": name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "total_lines": 1,
                "shown": "1-1",
                "text": f"1: {content.decode()}",
                "truncated": False,
                "next": None,
                "download_required": False,
                "project_id": "p1",
                "context_version": "ctx-1",
            }
        if method == "POST" and parsed.path == "/api/agent/files/write":
            current = self.files[json["path"]]
            if hashlib.sha256(current).hexdigest() != json["expected_sha256"]:
                raise AssertionError("test sent a stale hash")
            self.files[json["path"]] = json["content"].encode()
            return {
                "path": json["path"],
                "size": len(self.files[json["path"]]),
                "sha256": hashlib.sha256(
                    self.files[json["path"]]
                ).hexdigest(),
                "project_id": "p1",
                "context_version": "ctx-1",
            }
        if method == "POST" and parsed.path == "/api/agent/files/mkdir":
            return {
                "path": json["path"],
                "type": "dir",
                "project_id": "p1",
                "context_version": "ctx-1",
            }
        if method == "POST" and parsed.path == "/api/agent/files/move":
            self.files[json["to"]] = self.files.pop(json["from"])
            return {
                "path": json["to"],
                "type": "file",
                "project_id": "p1",
                "context_version": "ctx-1",
            }
        if (
            method == "POST"
            and parsed.path == "/api/agent/files/install-upload"
        ):
            self.files[json["path"]] = b"uploaded"
            return {
                "path": json["path"],
                "size": len(self.files[json["path"]]),
                "sha256": hashlib.sha256(
                    self.files[json["path"]]
                ).hexdigest(),
                "project_id": "p1",
                "context_version": "ctx-1",
            }
        if method == "POST" and parsed.path == "/api/agent/files/delete":
            content = self.files.pop(json["path"])
            trash_id = "1" * 32
            self.trash[trash_id] = (json["path"], content)
            return {
                "id": trash_id,
                "original_path": json["path"],
                "kind": "file",
                "project_id": "p1",
                "context_version": "ctx-1",
            }
        if method == "GET" and parsed.path == "/api/agent/files/trash":
            return {
                "items": [
                    {
                        "id": trash_id,
                        "original_path": value[0],
                        "kind": "file",
                    }
                    for trash_id, value in self.trash.items()
                ],
                "project_id": "p1",
                "context_version": "ctx-1",
            }
        if method == "POST" and parsed.path == "/api/agent/files/restore":
            name, content = self.trash.pop(json["trash_id"])
            self.files[name] = content
            return {
                "id": json["trash_id"],
                "path": name,
                "kind": "file",
                "project_id": "p1",
                "context_version": "ctx-1",
            }
        raise AssertionError(f"unexpected gateway request: {method} {path}")


class RemoteFileToolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db_path = root / "control.db"
        self.workspace_base = root / "workspaces"
        self.workspace_base.mkdir()
        mcp_store.migrate(self.db_path)
        self.gateway = _FileGateway()
        self.service = _RemoteProjectService(
            self.db_path,
            self.gateway,
            workspace_base=self.workspace_base,
        )
        self.viewer = PatIdentity(
            token_id="viewer-token",
            user_id="user-a",
            username="alice",
            port=9101,
            scopes=frozenset({"files:read"}),
            expires_at=None,
        )
        self.editor = PatIdentity(
            token_id="editor-token",
            user_id="user-a",
            username="alice",
            port=9101,
            scopes=frozenset({"files:read", "files:write"}),
            expires_at=None,
        )
        _, self.viewer_handle = mcp_store.issue_lease(
            self.db_path, self.viewer, "p1", "ctx-1"
        )
        _, self.editor_handle = mcp_store.issue_lease(
            self.db_path, self.editor, "p1", "ctx-1"
        )

    async def asyncTearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _access(identity):
        return AccessToken(
            token="not-persisted",
            client_id=identity.token_id,
            subject=identity.user_id,
            scopes=sorted(identity.scopes),
            claims={
                "token_id": identity.token_id,
                "user_id": identity.user_id,
                "username": identity.username,
                "port": identity.port,
            },
        )

    async def _call(self, identity, method, *args, **kwargs):
        with patch(
            "remote_mcp.get_access_token",
            return_value=self._access(identity),
        ):
            return await getattr(self.service, method)(*args, **kwargs)

    async def test_viewer_reads_but_cannot_write_and_editor_uses_hash(self):
        listed = await self._call(
            self.viewer, "list_files", self.viewer_handle
        )
        self.assertTrue(listed["ok"])
        read = await self._call(
            self.viewer,
            "read_text_file",
            self.viewer_handle,
            "notes.md",
            1,
            20,
        )
        current_hash = read["sha256"]

        denied = await self._call(
            self.viewer,
            "write_text_file",
            self.viewer_handle,
            "notes.md",
            "new",
            current_hash,
        )
        self.assertEqual(denied["error"]["code"], "SCOPE_DENIED")
        written = await self._call(
            self.editor,
            "write_text_file",
            self.editor_handle,
            "notes.md",
            "new",
            current_hash,
        )
        self.assertTrue(written["ok"])
        self.assertEqual(self.gateway.files["notes.md"], b"new")

    async def test_inline_upload_limit_and_staged_begin_finish(self):
        too_large = await self._call(
            self.editor,
            "upload_file",
            self.editor_handle,
            "assets/large.bin",
            base64.b64encode(b"x" * (1024 * 1024 + 1)).decode(),
            1024 * 1024 + 1,
            hashlib.sha256(b"x" * (1024 * 1024 + 1)).hexdigest(),
            False,
            None,
        )
        self.assertEqual(too_large["error"]["code"], "FILE_TOO_LARGE")

        inline = b"inline"
        uploaded = await self._call(
            self.editor,
            "upload_file",
            self.editor_handle,
            "assets/inline.bin",
            base64.b64encode(inline).decode(),
            len(inline),
            hashlib.sha256(inline).hexdigest(),
            False,
            None,
        )
        self.assertTrue(uploaded["ok"])
        self.assertIn("assets/inline.bin", self.gateway.files)

        begun = await self._call(
            self.editor,
            "begin_file_upload",
            self.editor_handle,
            "assets/image.bin",
            "image.bin",
            8,
            hashlib.sha256(b"uploaded").hexdigest(),
            False,
            None,
        )
        self.assertTrue(begun["ok"])
        self.assertNotIn(
            begun["authorization"].removeprefix("Upload "),
            begun["upload_url"],
        )
        capability = begun["authorization"].removeprefix("Upload ")
        session = mcp_store.authorize_upload(
            self.db_path, begun["upload_id"], capability
        )
        upload_dir = (
            self.workspace_base / "alice" / ".tcb" / "uploads"
        )
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / f"{session.id}.ready").write_bytes(b"uploaded")
        mcp_store.mark_upload_received(
            self.db_path, session.id, len(b"uploaded")
        )

        finished = await self._call(
            self.editor,
            "finish_file_upload",
            self.editor_handle,
            begun["upload_id"],
        )

        self.assertTrue(finished["ok"])
        self.assertIn("assets/image.bin", self.gateway.files)

    async def test_delete_list_and_restore_round_trip(self):
        deleted = await self._call(
            self.editor,
            "delete_file",
            self.editor_handle,
            "notes.md",
        )
        self.assertEqual(deleted["id"], "1" * 32)
        listed = await self._call(
            self.viewer,
            "list_deleted_files",
            self.viewer_handle,
        )
        self.assertEqual(listed["items"][0]["id"], "1" * 32)
        restored = await self._call(
            self.editor,
            "restore_deleted_file",
            self.editor_handle,
            "1" * 32,
        )
        self.assertEqual(restored["path"], "notes.md")


if __name__ == "__main__":
    unittest.main()
