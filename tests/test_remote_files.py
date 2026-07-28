import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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


if __name__ == "__main__":
    unittest.main()
