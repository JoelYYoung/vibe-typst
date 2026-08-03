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
        self.source = "= Old title\n#slide[\n  Body\n]\n"
        self.rev = 7
        self.comments = [{
            "id": "abcd1234",
            "seq": 1,
            "comment": "Improve the title",
            "status": "pending",
            "page": 1,
            "location": {
                "lines": [1],
                "current_text": ["= Old title"],
                "rev": 7,
            },
        }]
        self.pdf_transcripts = {
            "pages": {"1": {"text": ""}},
            "orphans": {},
        }

    async def read_bytes(self, identity, path, max_bytes):
        self.calls.append((identity.token_id, "GET_BYTES", path, None))
        return (
            b"\x89PNG\r\n\x1a\npreview",
            {
                "x-project-id": "p1",
                "x-context-version": "ctx-1",
                "x-page-count": "1",
                "content-type": "image/png",
            },
        )

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
        if method == "GET" and parsed.path == "/api/document":
            return {
                "file": "main.typ",
                "source": self.source,
                "rev": self.rev,
            }
        if method == "POST" and parsed.path == "/api/edit":
            if (
                json.get("require_single_file_typst") is True
                and any(
                    ".typ" in edit.get("text", "")
                    for edit in json.get("edits", [])
                    if isinstance(edit, dict)
                )
            ):
                return {
                    "ok": False,
                    "policy_violation": True,
                    "error": "local .typ imports/includes are forbidden",
                }
            self.source = self.source.replace("Old title", "New title")
            self.rev += 1
            return {"ok": True, "rev": self.rev, "applied": 1}
        if method == "GET" and parsed.path == "/api/locate":
            return {
                "ok": True,
                "kind": "page",
                "page": 1,
                "slide_no": 1,
                "slide_line": 2,
                "slide_end": 4,
            }
        if method == "GET" and parsed.path == "/api/slide-map":
            return {
                "pages": [{
                    "page": 1,
                    "slide_no": 1,
                    "slide_line": 2,
                    "sub_index": 1,
                    "sub_total": 1,
                    "section": "",
                    "note": "Opening",
                    "note_raw": "Opening",
                    "note_line": 2,
                }],
                "total": 1,
                "orphans": [],
            }
        if method == "GET" and parsed.path == "/api/state":
            return {
                "project_type": self.active["type"],
                "main": self.active["main_file"],
                "project_name": self.active["name"],
                "pages": ["page-1.png"],
                "version": 3,
                "generation": "pdf-generation-1",
                "project_id": "p1",
                "context_version": "ctx-1",
            }
        if method == "GET" and parsed.path == "/api/pdf/text":
            return {
                "page": int(parse_qs(parsed.query)["page"][0]),
                "text": "PDF page text",
                "ocr": False,
            }
        if method == "GET" and parsed.path == "/api/pdf/transcripts":
            return {
                **self.pdf_transcripts,
                "project_id": "p1",
                "context_version": "ctx-1",
            }
        if (
            method == "PATCH"
            and parsed.path.startswith("/api/pdf/transcripts/")
        ):
            page = parsed.path.rsplit("/", 1)[1]
            self.pdf_transcripts["pages"][page] = {
                "text": json["text"],
            }
            return {
                **self.pdf_transcripts,
                "project_id": "p1",
                "context_version": "ctx-1",
            }
        if (
            method == "POST"
            and parsed.path == "/api/pdf/transcripts/batch"
        ):
            for update in json["updates"]:
                self.pdf_transcripts["pages"][str(update["page"])] = {
                    "text": update["text"],
                }
            return {
                **self.pdf_transcripts,
                "project_id": "p1",
                "context_version": "ctx-1",
            }
        if (
            method == "POST"
            and parsed.path == "/api/agent/projects/pdf-from-upload"
        ):
            return {
                "project": {
                    "id": "pdf-new",
                    "name": json["name"],
                    "type": "pdf",
                    "main_file": "document.pdf",
                    "original_filename": json["filename"],
                },
            }
        if (
            method == "POST"
            and parsed.path == "/api/agent/pdf/replace-from-upload"
        ):
            return {
                "ok": True,
                "page_count": 1,
                "pages": ["page-1.png"],
                "transcripts": self.pdf_transcripts,
                "project_id": "p1",
                "context_version": "ctx-1",
            }
        if (
            method == "POST"
            and parsed.path == "/api/agent/export-pdf"
        ):
            return {
                "export_id": "e" * 32,
                "filename": "main.pdf",
                "size": 12,
                "sha256": "a" * 64,
                "download_path": (
                    "/api/agent/exports/" + "e" * 32
                ),
                "project_id": "p1",
                "context_version": "ctx-1",
            }
        if (
            method == "GET"
            and parsed.path == "/api/agent/comments/pending"
        ):
            return {
                "comments": [dict(item) for item in self.comments],
                "project_id": "p1",
                "context_version": "ctx-1",
            }
        if (
            method == "GET"
            and parsed.path.startswith("/api/agent/comments/")
        ):
            return {
                "comment": dict(self.comments[0]),
                "project_id": "p1",
                "context_version": "ctx-1",
            }
        if (
            method == "POST"
            and parsed.path.endswith("/done")
            and parsed.path.startswith("/api/agent/comments/")
        ):
            self.comments[0]["status"] = "done"
            return {
                "comment": dict(self.comments[0]),
                "project_id": "p1",
                "context_version": "ctx-1",
            }
        if (
            method == "POST"
            and parsed.path.endswith("/dismiss")
            and parsed.path.startswith("/api/agent/comments/")
        ):
            self.comments[0]["status"] = "dismissed"
            return {
                "comment": dict(self.comments[0]),
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
            scopes=frozenset({
                "projects:read",
                "files:read",
                "slides:read",
                "transcripts:read",
                "comments:read",
            }),
            expires_at=None,
        )
        self.editor = PatIdentity(
            token_id="editor-token",
            user_id="user-a",
            username="alice",
            port=9101,
            scopes=frozenset({
                "projects:read",
                "projects:write",
                "files:read",
                "files:write",
                "slides:read",
                "documents:write",
                "transcripts:read",
                "transcripts:write",
                "comments:read",
                "comments:write",
            }),
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

    def _receive_upload(self, begun, content):
        capability = begun["authorization"].removeprefix("Upload ")
        session = mcp_store.authorize_upload(
            self.db_path, begun["upload_id"], capability
        )
        upload_dir = (
            self.workspace_base / "alice" / ".tcb" / "uploads"
        )
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / f"{session.id}.ready").write_bytes(content)
        mcp_store.mark_upload_received(
            self.db_path, session.id, len(content)
        )

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

    async def test_typst_generic_mutations_reject_auxiliary_source(self):
        content = b"#let helper = none"
        digest = hashlib.sha256(content).hexdigest()
        cases = (
            (
                "write_text_file",
                (
                    self.editor_handle,
                    "theme.typ",
                    content.decode(),
                    hashlib.sha256(b"").hexdigest(),
                ),
            ),
            (
                "upload_file",
                (
                    self.editor_handle,
                    "slides/section.typ",
                    base64.b64encode(content).decode(),
                    len(content),
                    digest,
                    False,
                    None,
                ),
            ),
            (
                "begin_file_upload",
                (
                    self.editor_handle,
                    "components.typ",
                    "components.typ",
                    len(content),
                    digest,
                    False,
                    None,
                ),
            ),
            (
                "move_file",
                (self.editor_handle, "notes.md", "notes.typ"),
            ),
        )

        for method, args in cases:
            with self.subTest(method=method):
                result = await self._call(
                    self.editor,
                    method,
                    *args,
                )
                self.assertEqual(
                    result["error"]["code"],
                    "PATH_NOT_ALLOWED",
                )
                self.assertIn("main.typ", result["error"]["message"])

        self.assertNotIn("theme.typ", self.gateway.files)
        self.assertNotIn("slides/section.typ", self.gateway.files)
        self.assertNotIn("components.typ", self.gateway.files)
        self.assertNotIn("notes.typ", self.gateway.files)

        self.gateway.trash["2" * 32] = ("legacy.typ", content)
        restored = await self._call(
            self.editor,
            "restore_deleted_file",
            self.editor_handle,
            "2" * 32,
        )
        self.assertEqual(restored["error"]["code"], "PATH_NOT_ALLOWED")
        self.assertNotIn("legacy.typ", self.gateway.files)

        included = await self._call(
            self.editor,
            "apply_edits",
            self.editor_handle,
            [{
                "selector": {"by": "lines", "start": 1},
                "text": '#include "legacy.typ"',
            }],
            self.gateway.rev,
        )
        self.assertEqual(included["error"]["code"], "PATH_NOT_ALLOWED")
        self.assertNotIn("legacy.typ", self.gateway.source)

    async def test_typst_tools_require_main_typ_as_primary_document(self):
        self.gateway.active = {
            **self.gateway.active,
            "main_file": "deck.typ",
        }

        result = await self._call(
            self.viewer,
            "get_document",
            self.viewer_handle,
            1,
            20,
        )

        self.assertEqual(
            result["error"]["code"],
            "CAPABILITY_NOT_AVAILABLE",
        )
        self.assertIn("main.typ", result["error"]["message"])

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

    async def test_typst_document_transcript_and_comment_flow(self):
        document = await self._call(
            self.viewer,
            "get_document",
            self.viewer_handle,
            1,
            2,
        )
        self.assertEqual(document["shown"], "1-2")
        self.assertEqual(document["rev"], 7)
        found = await self._call(
            self.viewer,
            "find_in_document",
            self.viewer_handle,
            "Old title",
        )
        self.assertEqual(found["hits"][0]["line"], 1)
        located = await self._call(
            self.viewer,
            "locate",
            self.viewer_handle,
            1,
            0,
        )
        self.assertEqual(located["slide_line"], 2)
        transcripts = await self._call(
            self.viewer,
            "get_transcripts",
            self.viewer_handle,
        )
        self.assertEqual(transcripts["pages"][0]["note"], "Opening")
        preview = await self._call(
            self.viewer,
            "get_slide_preview",
            self.viewer_handle,
            1,
        )
        self.assertTrue(preview["_image_data"].startswith(b"\x89PNG"))
        exported = await self._call(
            self.viewer,
            "export_pdf",
            self.viewer_handle,
        )
        self.assertTrue(exported["download_url"].endswith(
            f"/mcp-download/{exported['download_id']}"
        ))
        self.assertTrue(
            exported["authorization"].startswith("Download ")
        )

        edited = await self._call(
            self.editor,
            "apply_edits",
            self.editor_handle,
            [{"selector": {
                "by": "anchor",
                "text": "Old title",
            }, "text": "New title"}],
            document["rev"],
        )
        self.assertTrue(edited["ok"])
        self.assertIn("New title", self.gateway.source)

        pending = await self._call(
            self.viewer,
            "get_pending_comments",
            self.viewer_handle,
        )
        self.assertEqual(pending["comments"][0]["status"], "pending")
        comment = await self._call(
            self.viewer,
            "get_comment",
            self.viewer_handle,
            "abcd1234",
        )
        self.assertEqual(comment["comment"]["seq"], 1)
        done = await self._call(
            self.editor,
            "mark_comment_done",
            self.editor_handle,
            "abcd1234",
            "updated",
        )
        self.assertEqual(done["comment"]["status"], "done")

    async def test_pdf_handle_rejects_typst_and_comment_tools(self):
        self.gateway.active = {
            **self.gateway.active,
            "type": "pdf",
            "main_file": "document.pdf",
        }

        for method, args in (
            ("get_document", (self.viewer_handle, 1, 20)),
            ("get_pending_comments", (self.viewer_handle,)),
            (
                "apply_edits",
                (self.editor_handle, [], None),
            ),
        ):
            with self.subTest(method=method):
                result = await self._call(
                    self.editor if method == "apply_edits" else self.viewer,
                    method,
                    *args,
                )
                self.assertEqual(
                    result["error"]["code"],
                    "CAPABILITY_NOT_AVAILABLE",
                )

    async def test_pdf_info_text_transcripts_preview_and_replacement(self):
        self.gateway.active = {
            **self.gateway.active,
            "type": "pdf",
            "main_file": "document.pdf",
            "original_filename": "paper.pdf",
        }

        info = await self._call(
            self.viewer, "get_pdf_info", self.viewer_handle
        )
        self.assertEqual(info["project_type"], "pdf")
        self.assertEqual(info["page_count"], 1)
        self.assertNotIn("file", info)
        text = await self._call(
            self.viewer, "get_pdf_text", self.viewer_handle, 1
        )
        self.assertEqual(text["text"], "PDF page text")
        transcripts = await self._call(
            self.viewer, "get_transcripts", self.viewer_handle
        )
        self.assertEqual(transcripts["pages"]["1"]["text"], "")
        saved = await self._call(
            self.editor,
            "set_transcript",
            self.editor_handle,
            1,
            "Opening",
        )
        self.assertEqual(saved["pages"]["1"]["text"], "Opening")
        batch = await self._call(
            self.editor,
            "set_transcripts",
            self.editor_handle,
            [{"page": 1, "text": "Batch opening"}],
        )
        self.assertEqual(
            batch["pages"]["1"]["text"], "Batch opening"
        )
        oversized = await self._call(
            self.editor,
            "set_transcripts",
            self.editor_handle,
            [
                {"page": page, "text": "x" * (220 * 1024)}
                for page in range(1, 6)
            ],
        )
        self.assertEqual(
            oversized["error"]["code"], "PATH_NOT_ALLOWED"
        )
        preview = await self._call(
            self.viewer, "get_page_preview", self.viewer_handle, 1
        )
        self.assertTrue(preview["_image_data"].startswith(b"\x89PNG"))

        candidate = b"%PDF remote replacement"
        begun = await self._call(
            self.editor,
            "begin_pdf_replacement",
            self.editor_handle,
            "candidate.pdf",
            len(candidate),
            hashlib.sha256(candidate).hexdigest(),
        )
        self._receive_upload(begun, candidate)
        replaced = await self._call(
            self.editor,
            "finish_pdf_replacement",
            self.editor_handle,
            begun["upload_id"],
            "remote update",
        )
        self.assertTrue(replaced["ok"])
        self.assertEqual(
            replaced["transcripts"]["pages"]["1"]["text"],
            "Batch opening",
        )

    async def test_pdf_project_upload_and_write_scope_enforcement(self):
        content = b"%PDF remote project"
        denied = await self._call(
            self.viewer,
            "begin_pdf_project_upload",
            "Remote paper",
            "paper.pdf",
            len(content),
            hashlib.sha256(content).hexdigest(),
        )
        self.assertEqual(denied["error"]["code"], "SCOPE_DENIED")

        begun = await self._call(
            self.editor,
            "begin_pdf_project_upload",
            "Remote paper",
            "paper.pdf",
            len(content),
            hashlib.sha256(content).hexdigest(),
        )
        self._receive_upload(begun, content)
        created = await self._call(
            self.editor,
            "finish_pdf_project_upload",
            begun["upload_id"],
        )
        self.assertEqual(created["project"]["id"], "pdf-new")
        self.assertEqual(created["project"]["type"], "pdf")

    async def test_typst_handle_rejects_pdf_only_tools(self):
        for method, args in (
            ("get_pdf_info", (self.viewer_handle,)),
            ("get_pdf_text", (self.viewer_handle, 1)),
            (
                "set_transcript",
                (self.editor_handle, 1, "not available"),
            ),
            (
                "begin_pdf_replacement",
                (
                    self.editor_handle,
                    "candidate.pdf",
                    4,
                    hashlib.sha256(b"data").hexdigest(),
                ),
            ),
        ):
            with self.subTest(method=method):
                identity = (
                    self.editor
                    if method in {"set_transcript", "begin_pdf_replacement"}
                    else self.viewer
                )
                result = await self._call(
                    identity, method, *args
                )
                self.assertEqual(
                    result["error"]["code"],
                    "CAPABILITY_NOT_AVAILABLE",
                )


if __name__ == "__main__":
    unittest.main()
