import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import remote_files


class _Request:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class RemoteFileServiceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.project_dir = self.root / "typst-project"
        self.project_dir.mkdir()
        (self.project_dir / ".vibe-typst.json").write_text(
            json.dumps({
                "name": "Deck",
                "type": "typst",
                "main_file": "main.typ",
            }),
            encoding="utf-8",
        )
        (self.project_dir / "main.typ").write_text(
            "= Protected\n", encoding="utf-8"
        )
        self.project = {
            "id": "typst-project",
            "name": "Deck",
            "type": "typst",
            "main_file": "main.typ",
            "path": str(self.project_dir),
        }

        self.outside_dir = self.root / "outside"
        self.outside_dir.mkdir()
        (self.outside_dir / "secret.txt").write_text(
            "secret", encoding="utf-8"
        )

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _sha(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def test_write_requires_current_hash_and_refuses_active_main(self):
        asset = self.project_dir / "notes.md"
        asset.write_text("old", encoding="utf-8")
        observed = self._sha(b"old")

        result = remote_files.write_text(
            self.project, "notes.md", "new", observed
        )

        self.assertEqual(result["sha256"], self._sha(b"new"))
        self.assertEqual(asset.read_text(encoding="utf-8"), "new")
        with self.assertRaises(remote_files.RevisionConflict) as conflict:
            remote_files.write_text(
                self.project, "notes.md", "lost", observed
            )
        self.assertEqual(conflict.exception.current_sha256, self._sha(b"new"))
        with self.assertRaisesRegex(ValueError, "active Typst main"):
            remote_files.write_text(
                self.project,
                "main.typ",
                "bypass",
                self._sha(b"= Protected\n"),
            )

    def test_read_is_numbered_bounded_and_hashes_the_complete_file(self):
        content = "\n".join(f"line {number}" for number in range(1, 501))
        target = self.project_dir / "notes.txt"
        target.write_text(content, encoding="utf-8")

        result = remote_files.read_text(
            self.project, "notes.txt", offset=2, limit=999
        )

        self.assertEqual(result["sha256"], self._sha(content.encode()))
        self.assertEqual(result["total_lines"], 500)
        self.assertEqual(result["shown"], "2-401")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["next"], 402)
        shown_lines = result["text"].splitlines()
        self.assertEqual(shown_lines[0], "2: line 2")
        self.assertEqual(shown_lines[-1], "401: line 401")

    def test_binary_or_large_file_requires_download_without_returning_bytes(self):
        binary = self.project_dir / "asset.bin"
        binary.write_bytes(b"\xff\xfe\x00")
        large = self.project_dir / "large.txt"
        large.write_bytes(b"x" * (4 * 1024 * 1024 + 1))

        for path in ("asset.bin", "large.txt"):
            with self.subTest(path=path):
                result = remote_files.read_text(self.project, path)
                self.assertTrue(result["download_required"])
                self.assertNotIn("text", result)
                self.assertEqual(result["size"], (
                    self.project_dir / path
                ).stat().st_size)

    def test_symlink_hidden_and_non_regular_paths_are_rejected(self):
        (self.project_dir / "escape").symlink_to(
            self.outside_dir, target_is_directory=True
        )
        (self.project_dir / ".private").write_text(
            "private", encoding="utf-8"
        )
        (self.project_dir / "folder").mkdir()

        with self.assertRaises(PermissionError):
            remote_files.read_text(self.project, "escape/secret.txt")
        with self.assertRaises(PermissionError):
            remote_files.read_text(self.project, ".private")
        with self.assertRaises(IsADirectoryError):
            remote_files.read_text(self.project, "folder")

    def test_create_directory_and_move_are_collision_safe(self):
        created = remote_files.create_directory(
            self.project, "assets/images"
        )
        self.assertEqual(created["path"], "assets/images")
        (self.project_dir / "notes.md").write_text("notes", encoding="utf-8")

        moved = remote_files.move_item(
            self.project, "notes.md", "assets/notes.md"
        )

        self.assertEqual(moved["path"], "assets/notes.md")
        self.assertTrue((self.project_dir / "assets" / "notes.md").is_file())
        with self.assertRaises(FileExistsError):
            remote_files.move_item(
                self.project, "assets/notes.md", "assets/notes.md"
            )

    def test_install_is_atomic_and_requires_explicit_matching_overwrite(self):
        staged = self.root / "upload.ready"
        staged.write_bytes(b"new image")

        installed = remote_files.install_file(
            self.project,
            staged,
            "assets/image.png",
            overwrite=False,
            expected_sha256=None,
        )

        destination = self.project_dir / "assets" / "image.png"
        self.assertEqual(destination.read_bytes(), b"new image")
        self.assertEqual(installed["sha256"], self._sha(b"new image"))
        self.assertTrue(staged.exists())

        replacement = self.root / "replacement.ready"
        replacement.write_bytes(b"replacement")
        with self.assertRaises(FileExistsError):
            remote_files.install_file(
                self.project,
                replacement,
                "assets/image.png",
                overwrite=False,
                expected_sha256=None,
            )
        with self.assertRaises(remote_files.RevisionConflict):
            remote_files.install_file(
                self.project,
                replacement,
                "assets/image.png",
                overwrite=True,
                expected_sha256=self._sha(b"wrong"),
            )

        remote_files.install_file(
            self.project,
            replacement,
            "assets/image.png",
            overwrite=True,
            expected_sha256=self._sha(b"new image"),
        )
        self.assertEqual(destination.read_bytes(), b"replacement")

    def test_pdf_managed_state_and_extra_pdfs_are_protected(self):
        project_dir = self.root / "pdf-project"
        project_dir.mkdir()
        metadata = {
            "name": "Paper",
            "type": "pdf",
            "main_file": "document.pdf",
        }
        (project_dir / ".vibe-typst.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
        (project_dir / "document.pdf").write_bytes(b"%PDF fixture")
        (project_dir / "transcript.json").write_text(
            "{}", encoding="utf-8"
        )
        (project_dir / ".pdf-project-write.lock").write_text(
            "", encoding="utf-8"
        )
        project = {
            "id": "pdf-project",
            "type": "pdf",
            "main_file": "document.pdf",
            "path": str(project_dir),
        }

        for protected in (
            "document.pdf",
            "transcript.json",
            ".vibe-typst.json",
            ".pdf-project-write.lock",
        ):
            with self.subTest(protected=protected):
                with self.assertRaises((ValueError, PermissionError)):
                    remote_files.write_text(
                        project,
                        protected,
                        "tampered",
                        self._sha((project_dir / protected).read_bytes()),
                    )

        staged = self.root / "another.ready"
        staged.write_bytes(b"%PDF another")
        with self.assertRaisesRegex(ValueError, "additional PDF"):
            remote_files.install_file(
                project,
                staged,
                "another.pdf",
                overwrite=False,
                expected_sha256=None,
            )

    def test_delete_moves_payload_outside_project_and_restore_is_collision_safe(self):
        original = self.project_dir / "assets" / "logo.svg"
        original.parent.mkdir()
        original.write_text("<svg/>", encoding="utf-8")

        deleted = remote_files.trash_item(
            self.project, "assets/logo.svg", "pat-1", now=100
        )

        self.assertFalse(original.exists())
        listed = remote_files.list_trash(self.project, now=101)
        self.assertEqual(listed[0]["id"], deleted["id"])
        self.assertEqual(listed[0]["original_path"], "assets/logo.svg")
        original.write_text("replacement", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            remote_files.restore_trash(self.project, deleted["id"])
        original.unlink()

        restored = remote_files.restore_trash(
            self.project, deleted["id"]
        )

        self.assertEqual(restored["path"], "assets/logo.svg")
        self.assertEqual(original.read_text(encoding="utf-8"), "<svg/>")
        self.assertEqual(remote_files.list_trash(self.project), [])

    def test_recursive_trash_and_thirty_day_sweep(self):
        directory = self.project_dir / "assets"
        directory.mkdir()
        (directory / "nested").mkdir()
        (directory / "nested" / "data.txt").write_text(
            "data", encoding="utf-8"
        )
        deleted = remote_files.trash_item(
            self.project, "assets", "pat-1", now=100
        )
        self.assertEqual(deleted["kind"], "directory")

        removed = remote_files.sweep_trash(
            self.root, now=100 + 30 * 86400 - 1
        )
        self.assertEqual(removed, 0)
        self.assertEqual(len(remote_files.list_trash(self.project)), 1)

        removed = remote_files.sweep_trash(
            self.root, now=100 + 30 * 86400
        )

        self.assertEqual(removed, 1)
        self.assertEqual(remote_files.list_trash(self.project), [])

    def test_trash_rejects_symlinks_main_and_pdf_managed_state(self):
        (self.project_dir / "escape").symlink_to(
            self.outside_dir, target_is_directory=True
        )
        with self.assertRaises(PermissionError):
            remote_files.trash_item(
                self.project, "escape", "pat-1"
            )
        with self.assertRaisesRegex(ValueError, "active Typst main"):
            remote_files.trash_item(
                self.project, "main.typ", "pat-1"
            )

        pdf_dir = self.root / "paper"
        pdf_dir.mkdir()
        (pdf_dir / ".vibe-typst.json").write_text(
            json.dumps({
                "name": "Paper",
                "type": "pdf",
                "main_file": "document.pdf",
            }),
            encoding="utf-8",
        )
        (pdf_dir / "document.pdf").write_bytes(b"%PDF fixture")
        pdf_project = {
            "id": "paper",
            "type": "pdf",
            "main_file": "document.pdf",
            "path": str(pdf_dir),
        }
        with self.assertRaisesRegex(ValueError, "PDF managed state"):
            remote_files.trash_item(
                pdf_project, "document.pdf", "pat-1"
            )

    def test_sweep_never_follows_a_symlinked_private_root(self):
        external = self.root / "external-private"
        entry = external / "trash" / "typst-project" / ("a" * 32)
        entry.mkdir(parents=True)
        (entry / "payload").write_text("keep", encoding="utf-8")
        (entry / "metadata.json").write_text(json.dumps({
            "id": "a" * 32,
            "project_id": "typst-project",
            "original_path": "notes.md",
            "kind": "file",
            "deleted_at": 0,
            "expires_at": 1,
            "actor_token_id": "pat-1",
        }), encoding="utf-8")
        (self.root / ".tcb").symlink_to(
            external, target_is_directory=True
        )

        self.assertEqual(remote_files.sweep_trash(self.root, now=2), 0)
        self.assertTrue((entry / "payload").exists())

    def test_failed_metadata_publication_restores_original_payload(self):
        original = self.project_dir / "notes.md"
        original.write_text("important", encoding="utf-8")

        with patch.object(
            remote_files,
            "_publish_trash_metadata",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaises(OSError):
                remote_files.trash_item(
                    self.project, "notes.md", "pat-1", now=100
                )

        self.assertEqual(original.read_text(encoding="utf-8"), "important")


class RemoteFileEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import app

        self.app = app
        self._tmp = tempfile.TemporaryDirectory()
        self.project_dir = Path(self._tmp.name) / "project"
        self.project_dir.mkdir()
        (self.project_dir / "main.typ").write_text(
            "= Main\n", encoding="utf-8"
        )
        (self.project_dir / "notes.md").write_text(
            "old", encoding="utf-8"
        )
        self.project = {
            "id": "p1",
            "name": "Deck",
            "type": "typst",
            "main_file": "main.typ",
            "path": str(self.project_dir),
        }
        self.previous_project = app._active_project
        self.previous_context = app._project_context_version
        app._active_project = self.project
        app._project_context_version = "ctx-1"
        self.runtime_patches = [
            patch.object(app.runtime, "project_dir", return_value=self.project_dir),
            patch.object(
                app.runtime,
                "current_file",
                return_value=self.project_dir / "main.typ",
            ),
        ]
        for runtime_patch in self.runtime_patches:
            runtime_patch.start()

    async def asyncTearDown(self):
        for runtime_patch in reversed(self.runtime_patches):
            runtime_patch.stop()
        self.app._active_project = self.previous_project
        self.app._project_context_version = self.previous_context
        self._tmp.cleanup()

    async def test_agent_file_routes_include_active_context(self):
        (self.project_dir / ".secret").write_text(
            "hidden", encoding="utf-8"
        )
        listed = self.app.agent_list_files()
        self.assertEqual(
            {item["path"] for item in listed["items"]},
            {"main.typ", "notes.md"},
        )
        self.assertTrue(all(
            "abs_path" not in item for item in listed["items"]
        ))

        read = self.app.agent_read_file("notes.md", offset=1, limit=10)
        self.assertEqual(read["project_id"], "p1")
        self.assertEqual(read["context_version"], "ctx-1")
        self.assertEqual(read["text"], "1: old")

        written = await self.app.agent_write_file(_Request({
            "path": "notes.md",
            "content": "new",
            "expected_sha256": hashlib.sha256(b"old").hexdigest(),
        }))
        self.assertEqual(written["project_id"], "p1")
        self.assertEqual(
            (self.project_dir / "notes.md").read_text(encoding="utf-8"),
            "new",
        )

        made = await self.app.agent_create_directory(_Request({
            "path": "assets/images",
        }))
        self.assertEqual(made["context_version"], "ctx-1")
        moved = await self.app.agent_move_file(_Request({
            "from": "notes.md",
            "to": "assets/notes.md",
        }))
        self.assertEqual(moved["path"], "assets/notes.md")

    async def test_agent_write_maps_revision_conflict_to_409(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as caught:
            await self.app.agent_write_file(_Request({
                "path": "notes.md",
                "content": "lost",
                "expected_sha256": "0" * 64,
            }))

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["code"], "REVISION_CONFLICT"
        )
        self.assertEqual(
            caught.exception.detail["current_sha256"],
            hashlib.sha256(b"old").hexdigest(),
        )

    async def test_agent_trash_routes_round_trip(self):
        deleted = await self.app.agent_delete_file(_Request({
            "path": "notes.md",
            "actor_token_id": "pat-1",
        }))
        self.assertEqual(deleted["project_id"], "p1")

        listed = self.app.agent_list_deleted_files()
        self.assertEqual(listed["items"][0]["id"], deleted["id"])

        restored = await self.app.agent_restore_deleted_file(_Request({
            "trash_id": deleted["id"],
        }))
        self.assertEqual(restored["path"], "notes.md")
        self.assertTrue((self.project_dir / "notes.md").is_file())

    async def test_agent_install_upload_accepts_only_verified_upload_id(self):
        upload_id = "a" * 32
        upload_dir = self.project_dir.parent / ".tcb" / "uploads"
        upload_dir.mkdir(parents=True)
        ready = upload_dir / f"{upload_id}.ready"
        ready.write_bytes(b"asset")

        installed = await self.app.agent_install_upload(_Request({
            "upload_id": upload_id,
            "path": "assets/logo.bin",
            "size": 5,
            "sha256": hashlib.sha256(b"asset").hexdigest(),
            "overwrite": False,
            "expected_sha256": None,
        }))

        self.assertEqual(installed["path"], "assets/logo.bin")
        self.assertFalse(ready.exists())
        self.assertEqual(
            (self.project_dir / "assets" / "logo.bin").read_bytes(),
            b"asset",
        )

        bad_id = "b" * 32
        bad = upload_dir / f"{bad_id}.ready"
        bad.write_bytes(b"bad")
        with self.assertRaises(Exception):
            await self.app.agent_install_upload(_Request({
                "upload_id": bad_id,
                "path": "assets/bad.bin",
                "size": 3,
                "sha256": "0" * 64,
                "overwrite": False,
                "expected_sha256": None,
            }))
        self.assertTrue(bad.exists())
        self.assertFalse(
            (self.project_dir / "assets" / "bad.bin").exists()
        )

    async def test_agent_comment_wrappers_return_public_live_context(self):
        stored = {
            "id": "abcd1234",
            "seq": 1,
            "file": "main.typ",
            "kind": "element",
            "page": 1,
            "anchor_text": "Title",
            "anchor_context": "= Title",
            "region": None,
            "raw_context": "context",
            "body": "Improve it",
            "status": "pending",
            "created_at": 1,
            "rel_anchors": ["private-anchor"],
        }
        location = {
            "id": "abcd1234",
            "spans": [[0, 5]],
            "texts": ["Title"],
            "lines": [1],
            "rev": 7,
        }
        with (
            patch.object(
                self.app.store,
                "list_comments",
                return_value=[stored],
            ),
            patch.object(
                self.app.store, "get_comment", return_value=stored
            ),
            patch.object(
                self.app.store,
                "set_status",
                side_effect=lambda cid, status, note: {
                    **stored,
                    "status": status,
                },
            ),
            patch.object(
                self.app,
                "comment_anchor",
                new=AsyncMock(return_value=location),
            ),
        ):
            pending = await self.app.agent_pending_comments()
            detail = await self.app.agent_comment("abcd1234")
            done = await self.app.agent_comment_done(
                "abcd1234", _Request({"note": "fixed"})
            )
            dismissed = await self.app.agent_comment_dismiss(
                "abcd1234", _Request({"note": "obsolete"})
            )

        self.assertEqual(
            pending["comments"][0]["location"]["lines"], [1]
        )
        self.assertNotIn("rel_anchors", pending["comments"][0])
        self.assertEqual(detail["comment"]["comment"], "Improve it")
        self.assertEqual(done["comment"]["status"], "done")
        self.assertEqual(dismissed["comment"]["status"], "dismissed")

    async def test_agent_export_prepares_fixed_private_download(self):
        def compile_pdf(command, **kwargs):
            Path(command[-1]).write_bytes(b"compiled pdf")
            return SimpleNamespace(returncode=0, stderr="")

        with (
            patch.object(
                self.app.docstore,
                "flush_now",
                new=AsyncMock(),
            ),
            patch.object(
                self.app.subprocess,
                "run",
                side_effect=compile_pdf,
            ),
        ):
            exported = await self.app.agent_export_pdf()

        self.assertEqual(exported["project_id"], "p1")
        self.assertRegex(exported["export_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(exported["size"], len(b"compiled pdf"))
        self.assertEqual(
            exported["sha256"],
            hashlib.sha256(b"compiled pdf").hexdigest(),
        )
        self.assertEqual(
            exported["download_path"],
            f"/api/agent/exports/{exported['export_id']}",
        )

    async def test_agent_export_discards_result_if_project_switches(self):
        other_dir = self.project_dir.parent / "other"
        other_dir.mkdir()
        (other_dir / "main.typ").write_text("= Other", encoding="utf-8")
        other = {
            "id": "p2",
            "name": "Other",
            "type": "typst",
            "main_file": "main.typ",
            "path": str(other_dir),
        }

        def compile_and_switch(command, **kwargs):
            Path(command[-1]).write_bytes(b"wrong context")
            self.app._set_active_project(other)
            return SimpleNamespace(returncode=0, stderr="")

        with (
            patch.object(
                self.app.docstore,
                "flush_now",
                new=AsyncMock(),
            ),
            patch.object(
                self.app.subprocess,
                "run",
                side_effect=compile_and_switch,
            ),
            self.assertRaises(Exception),
        ):
            await self.app.agent_export_pdf()

        exports = self.project_dir.parent / ".tcb" / "exports"
        self.assertEqual(list(exports.glob("*.pdf")), [])


if __name__ == "__main__":
    unittest.main()
