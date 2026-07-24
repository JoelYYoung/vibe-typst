import asyncio
import json
import multiprocessing
import os
import shutil
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager, contextmanager
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def _one_page_pdf() -> bytes:
    """Build a minimal, structurally valid one-page PDF fixture."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
    ]
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode())
        data.extend(body)
        data.extend(b"\nendobj\n")
    xref_offset = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    data.extend(b"0000000000 65535 f \n")
    data.extend(b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets))
    data.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(data)


ONE_PAGE_PDF = _one_page_pdf()


def _pdf_bytes(label: str, pages: int = 1) -> bytes:
    """Build a distinct, genuine PDF fixture with visible embedded text."""
    import fitz

    document = fitz.open()
    for number in range(1, pages + 1):
        page = document.new_page()
        page.insert_text((72, 72), f"{label} page {number}")
    try:
        return document.tobytes()
    finally:
        document.close()


def _hold_pdf_project_lock(project: str, entered, release) -> None:
    import pdf_service

    with pdf_service.project_write_lock(Path(project)):
        entered.set()
        release.wait(5)


def _set_transcript_in_process(project: str, page: int, text: str, ready, start) -> None:
    import pdf_transcript

    ready.set()
    start.wait(5)
    pdf_transcript.set_page(Path(project), "document.pdf", 2, page, text)


def _raise_while_holding_pdf_lock(project: str, released) -> None:
    import pdf_service

    try:
        with pdf_service.project_write_lock(Path(project)):
            raise RuntimeError("forced lock body failure")
    except RuntimeError:
        released.set()


class _FakePixmap:
    def __init__(self, content: bytes):
        self.content = content

    def save(self, path: Path) -> None:
        Path(path).write_bytes(self.content)


class _FakePage:
    def __init__(self, number: int, fail: bool = False):
        self.number = number
        self.fail = fail

    def get_pixmap(self) -> _FakePixmap:
        if self.fail:
            raise RuntimeError("forced render failure")
        return _FakePixmap(f"new page {self.number}".encode())


class _FakePdf:
    is_pdf = True
    metadata = {}

    def __init__(self, pages: list[_FakePage]):
        self.pages = pages
        self.page_count = len(pages)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def __iter__(self):
        return iter(self.pages)


class PdfProjectCreationTest(unittest.TestCase):
    def setUp(self):
        import projects

        self.projects = projects
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "projects"
        self.root.mkdir()
        self._projects_root = patch.object(projects, "_projects_root", return_value=self.root.resolve())
        self._projects_root.start()

    def tearDown(self):
        self._projects_root.stop()
        self._tmp.cleanup()

    def test_legacy_project_defaults_to_typst(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        (legacy / ".vibe-typst.json").write_text(
            json.dumps({"name": "Legacy", "main_file": "main.typ"}), encoding="utf-8"
        )
        (legacy / "main.typ").write_text("= legacy", encoding="utf-8")

        self.assertEqual(self.projects.get_project("legacy")["type"], "typst")

    def test_pdf_project_requires_valid_pdf_and_uses_stable_primary_name(self):
        info = self.projects.create_pdf_project("Paper", "paper.pdf", ONE_PAGE_PDF)

        self.assertEqual(info["type"], "pdf")
        self.assertEqual(info["main_file"], "document.pdf")
        self.assertEqual(info["original_filename"], "paper.pdf")
        self.assertTrue((Path(info["path"]) / "document.pdf").exists())

    def test_invalid_pdf_leaves_no_project_directory(self):
        with self.assertRaises(ValueError):
            self.projects.create_pdf_project("Broken", "broken.pdf", b"not pdf")

        self.assertEqual(list(self.root.iterdir()), [])

    def test_pdf_project_is_not_listed_until_metadata_is_complete(self):
        write_meta = self.projects._write_meta

        def assert_unpublished(project_dir, metadata):
            self.assertEqual(self.projects.list_projects(), [])
            write_meta(project_dir, metadata)

        with patch.object(self.projects, "_write_meta", side_effect=assert_unpublished):
            self.projects.create_pdf_project("Paper", "paper.pdf", ONE_PAGE_PDF)

    def test_metadata_write_failure_leaves_no_published_or_staging_directory(self):
        with patch.object(self.projects, "_write_meta", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.projects.create_pdf_project("Paper", "paper.pdf", ONE_PAGE_PDF)

        self.assertEqual(list(self.root.iterdir()), [])

    def test_pdf_projects_keep_document_pdf_immutable_but_allow_non_pdf_assets(self):
        info = self.projects.create_pdf_project("Paper", "paper.pdf", ONE_PAGE_PDF)
        project = Path(info["path"])
        primary = project / "document.pdf"
        (project / "notes.txt").write_text("notes", encoding="utf-8")
        folder = project / "assets"
        folder.mkdir()

        for action in [
            lambda: self.projects.create_file(project, "another.pdf"),
            lambda: self.projects.store_upload(project, "another.PDF", ONE_PAGE_PDF),
            lambda: self.projects.delete_file(project, "document.pdf"),
            lambda: self.projects.rmdir(project, "."),
            lambda: self.projects.move_item(project, "document.pdf", "assets"),
            lambda: self.projects.rename_item(project, "document.pdf", "old.pdf"),
            lambda: self.projects.rename_item(project, "notes.txt", "notes.pdf"),
        ]:
            with self.assertRaises(ValueError):
                action()

        stored = self.projects.store_upload(project, "image.png", b"png", "assets")
        self.assertEqual(stored["path"], "assets/image.png")
        self.assertTrue(primary.is_file())


class PdfProjectCreationApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import app
        import httpx
        import projects

        self.app = app
        self.httpx = httpx
        self.projects = projects
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "projects"
        self.root.mkdir()
        self._configured = patch.object(app.app_config, "is_configured", return_value=True)
        self._projects_root = patch.object(projects, "_projects_root", return_value=self.root.resolve())
        self._configured.start()
        self._projects_root.start()

    async def asyncTearDown(self):
        self._projects_root.stop()
        self._configured.stop()
        self._tmp.cleanup()

    async def _post_pdf(self, *, data=None, files=None):
        transport = self.httpx.ASGITransport(app=self.app.app)
        async with self.httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/api/projects/pdf", data=data, files=files)

    async def test_valid_multipart_creation_preserves_pdf_metadata(self):
        response = await self._post_pdf(
            data={"name": "Paper"},
            files={"file": ("Original.PDF", ONE_PAGE_PDF, "application/pdf")},
        )

        self.assertEqual(response.status_code, 200, response.text)
        created = response.json()
        self.assertEqual(created["type"], "pdf")
        self.assertEqual(created["main_file"], "document.pdf")
        self.assertEqual(created["original_filename"], "Original.PDF")
        self.assertTrue((Path(created["path"]) / "document.pdf").is_file())

    async def test_invalid_or_non_pdf_uploads_are_rejected_without_a_project(self):
        for filename, content in (("broken.pdf", b"not a PDF"), ("notes.txt", ONE_PAGE_PDF)):
            with self.subTest(filename=filename):
                response = await self._post_pdf(
                    data={"name": "Paper"},
                    files={"file": (filename, content, "application/octet-stream")},
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(list(self.root.iterdir()), [])

    async def test_missing_or_extra_uploads_are_rejected_without_a_project(self):
        missing = await self._post_pdf(data={"name": "Paper"})
        self.assertEqual(missing.status_code, 400, missing.text)

        extra = await self._post_pdf(
            data={"name": "Paper"},
            files=[
                ("file", ("one.pdf", ONE_PAGE_PDF, "application/pdf")),
                ("file", ("two.pdf", ONE_PAGE_PDF, "application/pdf")),
            ],
        )
        self.assertEqual(extra.status_code, 400, extra.text)
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_upload_at_size_limit_is_created_and_oversize_is_rejected_without_residue(self):
        limit = len(ONE_PAGE_PDF)
        with patch.object(self.app, "MAX_PDF_UPLOAD_BYTES", limit):
            boundary = await self._post_pdf(
                data={"name": "At limit"},
                files={"file": ("at-limit.pdf", ONE_PAGE_PDF, "application/pdf")},
            )
        self.assertEqual(boundary.status_code, 200, boundary.text)

        shutil.rmtree(Path(boundary.json()["path"]))
        with patch.object(self.app, "MAX_PDF_UPLOAD_BYTES", limit - 1):
            oversize = await self._post_pdf(
                data={"name": "Too large"},
                files={"file": ("too-large.pdf", ONE_PAGE_PDF, "application/pdf")},
            )
        self.assertEqual(oversize.status_code, 413, oversize.text)
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_chunked_oversize_upload_without_content_length_is_rejected(self):
        boundary = "pdf-upload-boundary"
        body = b"".join([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"name\"\r\n\r\nToo large\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"too-large.pdf\"\r\n".encode(),
            b"Content-Type: application/pdf\r\n\r\n",
            ONE_PAGE_PDF,
            f"\r\n--{boundary}--\r\n".encode(),
        ])

        async def streamed_body():
            yield body[:80]
            yield body[80:]

        transport = self.httpx.ASGITransport(app=self.app.app)
        with patch.object(self.app, "MAX_PDF_UPLOAD_BYTES", len(ONE_PAGE_PDF) - 1):
            async with self.httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/projects/pdf",
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    content=streamed_body(),
                )
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(list(self.root.iterdir()), [])

class PdfRenderingTest(unittest.TestCase):
    def setUp(self):
        import pdf_service

        self.pdf_service = pdf_service
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_render_pdf_writes_a_png_for_each_page(self):
        source = self.root / "source.pdf"
        destination = self.root / "pages"
        source.write_bytes(ONE_PAGE_PDF)
        destination.mkdir()
        (destination / "stale-page.png").write_bytes(b"stale")

        result = self.pdf_service.render_pdf(source, destination)

        self.assertEqual(result["page_count"], 1)
        self.assertEqual(result["pages"], ["page-1.png"])
        self.assertTrue((destination / "page-1.png").is_file())
        self.assertEqual([path.name for path in destination.iterdir()], ["page-1.png"])

    def test_failed_render_preserves_existing_destination_and_leaves_no_staging_residue(self):
        source = self.root / "source.pdf"
        destination = self.root / "pages"
        source.write_bytes(ONE_PAGE_PDF)
        destination.mkdir()
        (destination / "page-1.png").write_bytes(b"old page one")
        (destination / "page-2.png").write_bytes(b"old page two")
        before = {path.name: path.read_bytes() for path in destination.iterdir()}
        fake_pdf = _FakePdf([_FakePage(1), _FakePage(2, fail=True)])

        with patch.object(self.pdf_service.fitz, "open", return_value=fake_pdf):
            with self.assertRaises(ValueError):
                self.pdf_service.render_pdf(source, destination)

        after = {path.name: path.read_bytes() for path in destination.iterdir()}
        self.assertEqual(after, before)
        self.assertEqual([path for path in self.root.iterdir() if path.name.startswith(".")], [])

    def test_backup_cleanup_failure_restores_previous_destination_and_removes_hidden_residue(self):
        source = self.root / "source.pdf"
        destination = self.root / "pages"
        source.write_bytes(ONE_PAGE_PDF)
        destination.mkdir()
        (destination / "page-1.png").write_bytes(b"old page one")
        (destination / "page-2.png").write_bytes(b"old page two")
        before = {path.name: path.read_bytes() for path in destination.iterdir()}
        original_rmtree = self.pdf_service.shutil.rmtree
        failed_backup_cleanup = False

        def fail_first_backup_cleanup(path, *args, **kwargs):
            nonlocal failed_backup_cleanup
            if Path(path).name.startswith(".pdf-render-backup-") and not failed_backup_cleanup:
                failed_backup_cleanup = True
                raise OSError("forced backup cleanup failure")
            return original_rmtree(path, *args, **kwargs)

        with patch.object(self.pdf_service.shutil, "rmtree", side_effect=fail_first_backup_cleanup):
            with self.assertRaises(ValueError):
                self.pdf_service.render_pdf(source, destination)

        after = {path.name: path.read_bytes() for path in destination.iterdir()}
        self.assertEqual(after, before)
        self.assertEqual([path for path in self.root.iterdir() if path.name.startswith(".")], [])


class PdfReplacementTest(unittest.TestCase):
    """Replacement is prepare-then-swap: bad input cannot disturb live pages."""

    def setUp(self):
        import pdf_service

        self.pdf_service = pdf_service
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()
        self.primary = self.root / "document.pdf"
        self.old_pdf = _pdf_bytes("old")
        self.new_pdf = _pdf_bytes("new")
        self.primary.write_bytes(self.old_pdf)
        self.candidate = self.root / "replacement.pdf"
        self.render_dir = Path(self._tmp.name) / "render"
        self.render_dir.mkdir()
        (self.render_dir / "page-1.png").write_bytes(b"old render")

    def tearDown(self):
        self._tmp.cleanup()

    def test_invalid_candidate_never_changes_primary_render_or_candidate(self):
        self.candidate.write_bytes(b"not a PDF")
        primary_before = self.primary.read_bytes()
        render_before = {p.name: p.read_bytes() for p in self.render_dir.iterdir()}
        candidate_before = self.candidate.read_bytes()

        with self.assertRaises(ValueError):
            self.pdf_service.replace_primary(self.root, self.candidate, self.primary,
                                             self.render_dir)

        self.assertEqual(self.primary.read_bytes(), primary_before)
        self.assertEqual({p.name: p.read_bytes() for p in self.render_dir.iterdir()}, render_before)
        self.assertEqual(self.candidate.read_bytes(), candidate_before)
        self.assertEqual([p.name for p in self.root.iterdir()
                          if p.name.startswith(".") and "write.lock" not in p.name], [])

    def test_success_consumes_candidate_and_replaces_every_render_page(self):
        self.candidate.write_bytes(self.new_pdf)

        result = self.pdf_service.replace_primary(self.root, self.candidate, self.primary,
                                                  self.render_dir)

        self.assertEqual(result["page_count"], 1)
        self.assertFalse(self.candidate.exists())
        self.assertEqual(self.primary.read_bytes(), self.new_pdf)
        self.assertEqual([p.name for p in self.render_dir.iterdir()], ["page-1.png"])
        self.assertEqual([p.name for p in self.root.iterdir()
                          if p.name.startswith(".") and "write.lock" not in p.name], [])

    def test_direct_helper_cleanup_failure_keeps_committed_new_generation(self):
        self.candidate.write_bytes(self.new_pdf)
        real_remove = self.pdf_service._remove_path
        failed = False

        def fail_old_render_cleanup(path):
            nonlocal failed
            if Path(path).name.endswith("-old-render") and not failed:
                failed = True
                raise OSError("old render cleanup fault")
            return real_remove(path)

        with patch.object(
            self.pdf_service,
            "_remove_path",
            side_effect=fail_old_render_cleanup,
        ):
            result = self.pdf_service.replace_primary(
                self.root, self.candidate, self.primary, self.render_dir
            )

        self.assertTrue(failed)
        self.assertTrue(result["cleanup_pending"])
        self.assertEqual(self.primary.read_bytes(), self.new_pdf)
        self.assertFalse(self.candidate.exists())
        self.assertNotEqual((self.render_dir / "page-1.png").read_bytes(), b"old render")
        journal = self.root / ".pdf-replacement-journal.json"
        self.assertEqual(json.loads(journal.read_text(encoding="utf-8"))["phase"], "committed")

        self.pdf_service.recover_pending(self.root, self.render_dir)

        self.assertEqual(self.primary.read_bytes(), self.new_pdf)
        self.assertFalse(self.candidate.exists())
        self.assertNotEqual((self.render_dir / "page-1.png").read_bytes(), b"old render")
        self.assertFalse(journal.exists())
        self.assertEqual([
            path.name for path in self.root.iterdir()
            if path.name.startswith(".pdf-") and not path.name.endswith(".lock")
        ], [])

    def test_preparing_journal_is_durable_before_candidate_copy(self):
        self.candidate.write_bytes(self.new_pdf)
        observed = {}

        def fail_copy(*_args, **_kwargs):
            journal = self.root / ".pdf-replacement-journal.json"
            observed["journal"] = json.loads(journal.read_text(encoding="utf-8"))
            raise RuntimeError("copy fault fired")

        with patch.object(self.pdf_service, "_copy_candidate_no_follow", side_effect=fail_copy):
            with self.assertRaisesRegex(RuntimeError, "copy fault fired"):
                self.pdf_service.prepare_replacement(
                    self.root, self.candidate, self.primary, self.render_dir
                )

        self.assertEqual(observed["journal"]["phase"], "preparing")
        self.assertEqual(observed["journal"]["candidate"], "replacement.pdf")
        self.assertEqual(type(observed["journal"]["had_render"]), bool)
        self.pdf_service.recover_pending(self.root, self.render_dir)
        self.assertEqual(self.primary.read_bytes(), self.old_pdf)
        self.assertEqual(self.candidate.read_bytes(), self.new_pdf)

    def test_every_prepare_and_publish_crash_boundary_recovers_old_generation(self):
        boundaries = {
            "stable_created", "render_staging_created", "render_prepared",
            "primary_backup_intent", "primary_backup_created",
            "candidate_park_intent", "candidate_parked",
            "primary_publish_intent", "primary_published",
            "render_backup_intent", "render_backed_up",
            "render_publish_intent", "render_published",
        }
        fired = set()

        for boundary in sorted(boundaries):
            with self.subTest(boundary=boundary):
                self.primary.write_bytes(self.old_pdf)
                self.candidate.write_bytes(self.new_pdf)
                shutil.rmtree(self.render_dir, ignore_errors=True)
                self.render_dir.mkdir()
                (self.render_dir / "page-1.png").write_bytes(b"old render")

                class SimulatedDeath(BaseException):
                    pass

                def crash(point):
                    if point == boundary:
                        fired.add(point)
                        raise SimulatedDeath(point)

                with self.assertRaises(SimulatedDeath):
                    transaction = self.pdf_service.prepare_replacement(
                        self.root, self.candidate, self.primary, self.render_dir,
                        fault_hook=crash,
                    )
                    transaction.publish()

                self.pdf_service.recover_pending(self.root, self.render_dir)
                self.assertEqual(self.primary.read_bytes(), self.old_pdf)
                self.assertEqual(self.candidate.read_bytes(), self.new_pdf)
                self.assertEqual((self.render_dir / "page-1.png").read_bytes(), b"old render")
                self.assertFalse((self.root / ".pdf-replacement-journal.json").exists())

        self.assertEqual(fired, boundaries)

    def test_interrupted_replacement_with_no_prior_render_removes_new_render(self):
        self.candidate.write_bytes(self.new_pdf)
        shutil.rmtree(self.render_dir)
        transaction = self.pdf_service.prepare_replacement(
            self.root, self.candidate, self.primary, self.render_dir
        )
        transaction.publish()

        self.pdf_service.recover_pending(self.root, self.render_dir)

        self.assertFalse(self.render_dir.exists())
        self.assertEqual(self.primary.read_bytes(), self.old_pdf)
        self.assertEqual(self.candidate.read_bytes(), self.new_pdf)

    def test_corrupt_or_forged_journal_fails_closed_without_touching_external_path(self):
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"outside")
        journal = self.root / ".pdf-replacement-journal.json"
        journal.write_text(json.dumps({
            "schema_version": 1,
            "txid": "a" * 32,
            "phase": "preparing",
            "candidate": "replacement.pdf",
            "had_render": True,
            "cleanup": str(sentinel),
        }), encoding="utf-8")

        with self.assertRaises(ValueError):
            self.pdf_service.recover_pending(self.root, self.render_dir)

        self.assertEqual(sentinel.read_bytes(), b"outside")
        self.assertTrue(journal.exists())

    def test_partial_and_noncanonical_journals_fail_closed(self):
        journal = self.root / ".pdf-replacement-journal.json"
        invalid = [
            "{",
            json.dumps({
                "schema_version": 1, "txid": "b" * 32, "phase": "preparing",
                "candidate": "replacement.pdf",
            }),
            json.dumps({
                "schema_version": 1, "txid": "b" * 32, "phase": "preparing",
                "candidate": "nested/../replacement.pdf", "had_render": True,
            }),
            json.dumps({
                "schema_version": 1, "txid": "b" * 32, "phase": "published",
                "candidate": "replacement.pdf", "had_render": True,
                "expected_tag": "v2",
            }),
        ]
        primary_before = self.primary.read_bytes()
        render_before = (self.render_dir / "page-1.png").read_bytes()

        for contents in invalid:
            with self.subTest(contents=contents):
                journal.write_text(contents, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.pdf_service.recover_pending(self.root, self.render_dir)
                self.assertEqual(self.primary.read_bytes(), primary_before)
                self.assertEqual((self.render_dir / "page-1.png").read_bytes(), render_before)
                self.assertEqual(journal.read_text(encoding="utf-8"), contents)

    def test_symlinked_candidate_component_cannot_redirect_recovery_outside_project(self):
        nested = self.root / "nested"
        nested.mkdir()
        candidate = nested / "replacement.pdf"
        candidate.write_bytes(self.new_pdf)
        transaction = self.pdf_service.prepare_replacement(
            self.root, candidate, self.primary, self.render_dir
        )
        transaction.publish()
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"outside")
        nested.rmdir()
        nested.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(ValueError):
            self.pdf_service.recover_pending(self.root, self.render_dir)

        self.assertEqual(sentinel.read_bytes(), b"outside")
        self.assertFalse((outside / "replacement.pdf").exists())
        self.assertTrue((self.root / ".pdf-replacement-journal.json").exists())

    def test_replacement_lock_reentrancy_is_scoped_to_each_project(self):
        second = Path(self._tmp.name) / "second-project"
        second.mkdir()
        entered = multiprocessing.Event()
        release = multiprocessing.Event()

        process = multiprocessing.Process(
            target=_hold_pdf_project_lock, args=(str(second), entered, release)
        )
        process.start()
        self.assertTrue(entered.wait(3))
        acquired = threading.Event()

        def nested_lock():
            with self.pdf_service.project_write_lock(self.root):
                with self.pdf_service.project_write_lock(second):
                    acquired.set()

        thread = threading.Thread(target=nested_lock)
        thread.start()
        time.sleep(0.2)
        self.assertFalse(acquired.is_set())
        release.set()
        thread.join(3)
        process.join(3)
        self.assertTrue(acquired.is_set())
        self.assertFalse(thread.is_alive())
        self.assertEqual(process.exitcode, 0)

    def test_interrupted_published_transaction_recovers_old_pair_and_candidate(self):
        self.candidate.write_bytes(self.new_pdf)
        old_render = {p.name: p.read_bytes() for p in self.render_dir.iterdir()}

        with self.pdf_service.project_write_lock(self.root):
            transaction = self.pdf_service.prepare_replacement(
                self.root, self.candidate, self.primary, self.render_dir
            )
            transaction.publish()  # simulate process death before v2/finalize
            self.assertTrue((self.root / ".pdf-replacement-journal.json").exists())
            self.pdf_service.recover_pending(self.root, self.render_dir)

        self.assertTrue(self.candidate.exists())
        self.assertEqual(self.primary.read_bytes(), self.old_pdf)
        self.assertEqual({p.name: p.read_bytes() for p in self.render_dir.iterdir()}, old_render)
        self.assertFalse((self.root / ".pdf-replacement-journal.json").exists())


class PdfTranscriptTest(unittest.TestCase):
    def setUp(self):
        import pdf_transcript

        self.transcript = pdf_transcript
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_new_sidecar_has_one_blank_entry_per_page(self):
        data = self.transcript.load(self.root, "document.pdf", 2)

        self.assertEqual(data["pages"], {"1": {"text": ""}, "2": {"text": ""}})

    def test_shrink_moves_text_to_orphans_without_loss(self):
        self.transcript.set_page(self.root, "document.pdf", 3, 3, "closing")

        data = self.transcript.load(self.root, "document.pdf", 2)

        self.assertEqual(data["orphans"]["3"]["text"], "closing")

    def test_batch_validation_is_atomic(self):
        before = self.transcript.load(self.root, "document.pdf", 2)

        with self.assertRaises(ValueError):
            self.transcript.set_pages(self.root, "document.pdf", 2, [
                {"page": 1, "text": "valid"}, {"page": 9, "text": "invalid"}
            ])

        self.assertEqual(self.transcript.load(self.root, "document.pdf", 2), before)

    def test_restore_orphan_moves_its_text_to_an_active_page(self):
        self.transcript.set_page(self.root, "document.pdf", 3, 3, "closing")
        self.transcript.load(self.root, "document.pdf", 2)

        data = self.transcript.restore_orphan(self.root, "document.pdf", 2, 3, 1)

        self.assertEqual(data["pages"]["1"]["text"], "closing")
        self.assertNotIn("3", data["orphans"])

    def test_orphan_collision_preserves_every_text_with_a_stable_suffix(self):
        self.transcript.set_page(self.root, "document.pdf", 3, 3, "first")
        self.transcript.load(self.root, "document.pdf", 2)
        self.transcript.set_page(self.root, "document.pdf", 3, 3, "second")

        data = self.transcript.load(self.root, "document.pdf", 2)

        self.assertEqual(data["orphans"]["3"], {"text": "first"})
        self.assertEqual(data["orphans"]["3#2"], {"text": "second"})

    def test_invalid_and_malformed_batches_leave_the_sidecar_unchanged(self):
        before = self.transcript.load(self.root, "document.pdf", 2)
        invalid_updates = [
            ({"page": 1, "text": "not a list"},),
            ([{"page": 1, "text": "valid"}, {"page": True, "text": "bad"}],),
            ([{"page": 1, "text": "valid"}, {"page": 2}],),
            ([{"page": 1, "text": 42}],),
            ([{"page": 1, "text": "valid", "extra": "bad"}],),
        ]

        for (updates,) in invalid_updates:
            with self.assertRaises(ValueError):
                self.transcript.set_pages(self.root, "document.pdf", 2, updates)
            self.assertEqual(self.transcript.load(self.root, "document.pdf", 2), before)

    def test_invalid_pdf_name_and_page_count_are_rejected(self):
        for pdf_name, page_count in [("", 1), (None, 1), ("document.pdf", 0),
                                     ("document.pdf", True)]:
            with self.assertRaises(ValueError):
                self.transcript.load(self.root, pdf_name, page_count)

    def test_only_the_stable_document_pdf_name_is_accepted_by_every_operation(self):
        self.transcript.load(self.root, "document.pdf", 2)
        sidecar = self.root / "transcript.json"
        before = sidecar.read_bytes()

        for number, pdf_name in enumerate(
                ["other.pdf", " document.pdf", "document.pdf ", Path("document.pdf")], start=1):
            fresh_project = self.root / f"fresh-{number}"
            fresh_project.mkdir()
            with self.assertRaises(ValueError):
                self.transcript.load(fresh_project, pdf_name, 2)
            with self.assertRaises(ValueError):
                self.transcript.load(self.root, pdf_name, 2)
            with self.assertRaises(ValueError):
                self.transcript.set_page(self.root, pdf_name, 2, 1, "changed")
            with self.assertRaises(ValueError):
                self.transcript.set_pages(self.root, pdf_name, 2, [{"page": 1, "text": "changed"}])
            with self.assertRaises(ValueError):
                self.transcript.restore_orphan(self.root, pdf_name, 2, 3, 1)
            self.assertEqual(sidecar.read_bytes(), before)

    def test_corrupt_or_unsupported_sidecar_is_rejected_without_replacing_it(self):
        sidecar = self.root / "transcript.json"
        for contents in ["{ not json", json.dumps({"schema_version": 2, "pdf": "document.pdf",
                                                       "pages": {}, "orphans": {}})]:
            sidecar.write_text(contents, encoding="utf-8")
            with self.assertRaises(ValueError):
                self.transcript.load(self.root, "document.pdf", 1)
            self.assertEqual(sidecar.read_text(encoding="utf-8"), contents)

    def test_mismatched_pdf_sidecar_fails_closed_without_replacing_it(self):
        sidecar = self.root / "transcript.json"
        contents = json.dumps({"schema_version": 1, "pdf": "other.pdf",
                               "pages": {"1": {"text": "old"}}, "orphans": {}})
        sidecar.write_text(contents, encoding="utf-8")

        with self.assertRaises(ValueError):
            self.transcript.load(self.root, "document.pdf", 1)

        self.assertEqual(sidecar.read_text(encoding="utf-8"), contents)

    def test_orphan_keys_must_be_canonical_and_bad_sidecars_remain_unchanged(self):
        sidecar = self.root / "transcript.json"
        for bad_key in ["0", "01", "orphan", "3#1", "3#0", "3#02", "3#x", "3#2#3"]:
            contents = json.dumps({"schema_version": 1, "pdf": "document.pdf",
                                   "pages": {"1": {"text": ""}},
                                   "orphans": {bad_key: {"text": "saved"}}})
            sidecar.write_text(contents, encoding="utf-8")
            with self.assertRaises(ValueError):
                self.transcript.load(self.root, "document.pdf", 1)
            self.assertEqual(sidecar.read_text(encoding="utf-8"), contents)

        valid = {"schema_version": 1, "pdf": "document.pdf", "pages": {"1": {"text": ""}},
                 "orphans": {"3#2": {"text": "saved"}}}
        sidecar.write_text(json.dumps(valid), encoding="utf-8")
        self.assertEqual(self.transcript.load(self.root, "document.pdf", 1), valid)

    def test_failed_atomic_write_preserves_prior_sidecar_and_cleans_temp_file(self):
        self.transcript.set_page(self.root, "document.pdf", 1, 1, "before")
        sidecar = self.root / "transcript.json"
        before = sidecar.read_bytes()

        with patch.object(self.transcript.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.transcript.set_page(self.root, "document.pdf", 1, 1, "after")

        self.assertEqual(sidecar.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".transcript-*")), [])

    def test_restore_preserves_existing_target_text_as_an_orphan(self):
        self.transcript.set_page(self.root, "document.pdf", 3, 3, "closing")
        self.transcript.load(self.root, "document.pdf", 2)
        self.transcript.set_page(self.root, "document.pdf", 2, 1, "opening")

        data = self.transcript.restore_orphan(self.root, "document.pdf", 2, 3, 1)

        self.assertEqual(data["pages"]["1"], {"text": "closing"})
        self.assertEqual(data["orphans"]["1"], {"text": "opening"})

    def test_invalid_or_missing_restore_selectors_leave_the_sidecar_unchanged(self):
        self.transcript.set_page(self.root, "document.pdf", 3, 3, "closing")
        self.transcript.load(self.root, "document.pdf", 2)
        sidecar = self.root / "transcript.json"
        before = sidecar.read_bytes()

        for selector in [0, True, "", "3#1", "missing", 9]:
            with self.assertRaises(ValueError):
                self.transcript.restore_orphan(self.root, "document.pdf", 2, selector, 1)
            self.assertEqual(sidecar.read_bytes(), before)

    def test_repeated_shrink_expand_shrink_retains_every_nonblank_transcript(self):
        self.transcript.set_pages(self.root, "document.pdf", 4, [
            {"page": 3, "text": "third"}, {"page": 4, "text": "fourth"},
        ])
        self.transcript.load(self.root, "document.pdf", 2)
        self.transcript.set_pages(self.root, "document.pdf", 4, [
            {"page": 3, "text": "third again"}, {"page": 4, "text": "fourth again"},
        ])

        data = self.transcript.load(self.root, "document.pdf", 2)

        self.assertEqual(data["orphans"], {
            "3": {"text": "third"}, "4": {"text": "fourth"},
            "3#2": {"text": "third again"}, "4#2": {"text": "fourth again"},
        })

    def test_concurrent_different_page_updates_preserve_both_texts(self):
        self.transcript.load(self.root, "document.pdf", 2)
        first_write_started = threading.Event()
        second_started = threading.Event()
        original_write = self.transcript._atomic_write
        write_count = 0
        count_lock = threading.Lock()
        errors = []

        def pause_first_write(path, data):
            nonlocal write_count
            with count_lock:
                write_count += 1
                is_first_write = write_count == 1
            if is_first_write:
                first_write_started.set()
                self.assertTrue(second_started.wait(timeout=3))
            original_write(path, data)

        def update(page, text, started=None):
            try:
                if started is not None:
                    started.set()
                self.transcript.set_page(self.root, "document.pdf", 2, page, text)
            except Exception as exc:  # captured so assertion failures reach this test thread
                errors.append(exc)

        with patch.object(self.transcript, "_atomic_write", side_effect=pause_first_write):
            first = threading.Thread(target=update, args=(1, "one"))
            first.start()
            self.assertTrue(first_write_started.wait(timeout=3))
            second = threading.Thread(target=update, args=(2, "two", second_started))
            second.start()
            first.join(timeout=3)
            second.join(timeout=3)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.transcript.load(self.root, "document.pdf", 2)["pages"], {
            "1": {"text": "one"}, "2": {"text": "two"},
        })

    def test_separate_process_updates_share_the_project_and_transcript_locks(self):
        self.transcript.load(self.root, "document.pdf", 2)
        start = multiprocessing.Event()
        first_ready = multiprocessing.Event()
        second_ready = multiprocessing.Event()
        first = multiprocessing.Process(
            target=_set_transcript_in_process,
            args=(str(self.root), 1, "one", first_ready, start),
        )
        second = multiprocessing.Process(
            target=_set_transcript_in_process,
            args=(str(self.root), 2, "two", second_ready, start),
        )
        first.start()
        second.start()
        self.assertTrue(first_ready.wait(3))
        self.assertTrue(second_ready.wait(3))
        start.set()
        first.join(5)
        second.join(5)

        self.assertEqual(first.exitcode, 0)
        self.assertEqual(second.exitcode, 0)
        self.assertEqual(self.transcript.load(self.root, "document.pdf", 2)["pages"], {
            "1": {"text": "one"}, "2": {"text": "two"},
        })

    def test_project_lock_is_released_after_body_exception(self):
        released = multiprocessing.Event()
        failed = multiprocessing.Process(
            target=_raise_while_holding_pdf_lock, args=(str(self.root), released)
        )
        failed.start()
        self.assertTrue(released.wait(3))
        failed.join(3)
        self.assertEqual(failed.exitcode, 0)

        acquired = multiprocessing.Event()
        allow_exit = multiprocessing.Event()
        follower = multiprocessing.Process(
            target=_hold_pdf_project_lock,
            args=(str(self.root), acquired, allow_exit),
        )
        follower.start()
        self.assertTrue(acquired.wait(3))
        allow_exit.set()
        follower.join(3)
        self.assertEqual(follower.exitcode, 0)


class PdfRuntimeApiTest(unittest.IsolatedAsyncioTestCase):
    """PDF projects deliberately bypass Typst's CRDT/resolver/comment lifecycle."""

    async def asyncSetUp(self):
        import app
        import httpx
        import runtime

        self.app = app
        self.httpx = httpx
        self.runtime = runtime
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "project"
        self.project.mkdir()
        self.pdf = self.project / "document.pdf"

        # A genuine PyMuPDF PDF (including embedded text), not a renderer mock.
        import fitz
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Embedded PDF text")
        doc.save(self.pdf)
        doc.close()

        self.info = {"id": "pdf-project", "name": "Paper", "path": str(self.project),
                     "type": "pdf", "main_file": "document.pdf"}
        self._previous_file = runtime._state.get("file")
        self._previous_project = app._active_project
        self._previous_pdf_render_state = dict(app._pdf_render_state)
        self._state_path = patch.object(runtime, "GLOBAL_STATE_PATH", self.project / "state.json")
        self._state_path.start()

    async def asyncTearDown(self):
        if hasattr(self.app.docstore, "stop"):
            await self.app.docstore.stop()
        self.runtime._state["file"] = self._previous_file
        self.app._active_project = self._previous_project
        self.app._pdf_render_state.clear()
        self.app._pdf_render_state.update(self._previous_pdf_render_state)
        shutil.rmtree(self.runtime.render_dir(self.pdf), ignore_errors=True)
        self._state_path.stop()
        self._tmp.cleanup()

    async def _request(self, method: str, path: str, payload=None):
        transport = self.httpx.ASGITransport(app=self.app.app)
        async with self.httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, json=payload)

    async def _open_pdf(self, info=None):
        with patch.object(self.app.projects_mod, "get_project", return_value=info or self.info):
            return await self.app.open_project((info or self.info)["id"])

    def _write_pdf(self, path: Path, pages: int) -> None:
        import fitz

        document = fitz.open()
        for number in range(pages):
            page = document.new_page()
            page.insert_text((72, 72), f"PDF page {number + 1}")
        document.save(path)
        document.close()

    async def _prepare_shrinking_replacement(self):
        self.pdf.unlink()
        self._write_pdf(self.pdf, 2)
        candidate = self.project / "replacement.pdf"
        self._write_pdf(candidate, 1)
        await self._open_pdf()
        patched = await self._request("PATCH", "/api/pdf/transcripts/2", {
            "text": "page two"
        })
        self.assertEqual(patched.status_code, 200, patched.text)
        return {
            "candidate": candidate,
            "primary": self.pdf.read_bytes(),
            "candidate_bytes": candidate.read_bytes(),
            "render": {
                path.name: path.read_bytes()
                for path in self.runtime.render_dir().iterdir()
            },
        }

    async def _assert_pre_v2_replacement_recovered(self, before):
        recovered = await self._request("GET", "/api/state")
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()["pages"], ["page-1.png", "page-2.png"])
        self.assertEqual(self.pdf.read_bytes(), before["primary"])
        self.assertEqual(before["candidate"].read_bytes(), before["candidate_bytes"])
        self.assertEqual({
            path.name: path.read_bytes()
            for path in self.runtime.render_dir().iterdir()
        }, before["render"])
        transcripts = (await self._request("GET", "/api/pdf/transcripts")).json()
        self.assertEqual(transcripts["pages"]["2"], {"text": "page two"})
        status = self.app.vcs.status(self.project)
        self.assertEqual(status["current"], "v1")
        self.assertFalse(status["dirty"])
        self.assertFalse((self.project / ".pdf-replacement-journal.json").exists())
        self.assertEqual([
            path.name for path in self.project.iterdir()
            if path.name.startswith(".pdf-") and not path.name.endswith(".lock")
        ], [])

    async def test_interruption_after_transcript_reconcile_restores_v1_transcript_and_git(self):
        before = await self._prepare_shrinking_replacement()

        class SimulatedDeath(BaseException):
            pass

        with patch.object(
            self.app,
            "_record_pdf_render_version",
            side_effect=SimulatedDeath("after transcript reconcile"),
        ):
            with self.assertRaises(SimulatedDeath):
                await self._request("POST", "/api/pdf/replace", {
                    "candidate": "replacement.pdf", "message": "shrink"
                })

        await self._assert_pre_v2_replacement_recovered(before)

    async def test_interruption_after_commit_intent_without_v2_restores_complete_v1(self):
        before = await self._prepare_shrinking_replacement()
        real_save = self.app.vcs.save_version
        calls = 0

        class SimulatedDeath(BaseException):
            pass

        def die_on_second_save(project, message):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise SimulatedDeath("after commit intent")
            return real_save(project, message)

        with patch.object(self.app.vcs, "save_version", side_effect=die_on_second_save):
            with self.assertRaises(SimulatedDeath):
                await self._request("POST", "/api/pdf/replace", {
                    "candidate": "replacement.pdf", "message": "shrink"
                })

        await self._assert_pre_v2_replacement_recovered(before)

    async def test_interruption_after_new_commit_before_tag_restores_head_and_v1(self):
        before = await self._prepare_shrinking_replacement()
        real_save = self.app.vcs.save_version
        calls = 0

        class SimulatedDeath(BaseException):
            pass

        def commit_then_die(project, message):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_save(project, message)
            self.assertEqual(self.app.vcs._run(["add", "-A"], project)[2], 0)
            self.assertEqual(
                self.app.vcs._run(["commit", "-m", "untagged replacement"], project)[2],
                0,
            )
            raise SimulatedDeath("after commit before tag")

        with patch.object(self.app.vcs, "save_version", side_effect=commit_then_die):
            with self.assertRaises(SimulatedDeath):
                await self._request("POST", "/api/pdf/replace", {
                    "candidate": "replacement.pdf", "message": "shrink"
                })

        self.assertNotEqual(
            self.app.vcs._head_commit(self.project),
            self.app.vcs.list_versions(self.project)[0]["commit"],
        )
        await self._assert_pre_v2_replacement_recovered(before)

    async def test_pdf_replacement_saves_before_after_versions_and_orphans_shrunk_text(self):
        self.pdf.unlink()
        self._write_pdf(self.pdf, 2)
        candidate = self.project / "replacement.pdf"
        self._write_pdf(candidate, 1)
        await self._open_pdf()
        self.assertEqual((await self._request("PATCH", "/api/pdf/transcripts/2", {
            "text": "page two"
        })).status_code, 200)

        replaced = await self._request("POST", "/api/pdf/replace", {
            "candidate": "replacement.pdf", "message": "shorter revision"
        })

        self.assertEqual(replaced.status_code, 200, replaced.text)
        result = replaced.json()
        self.assertTrue(result["before_version"]["ok"])
        self.assertTrue(result["after_version"]["ok"])
        self.assertEqual(result["page_count"], 1)
        self.assertEqual(result["transcripts"]["orphans"]["2"], {"text": "page two"})
        self.assertFalse(candidate.exists())
        self.assertEqual([entry["tag"] for entry in self.app.vcs.list_versions(self.project)], ["v2", "v1"])
        tree, _, rc = self.app.vcs._run(
            ["ls-tree", "-r", "--name-only", "v2"], self.project
        )
        self.assertEqual(rc, 0)
        self.assertFalse(any(name.startswith(".pdf-") for name in tree.splitlines()), tree)
        self.assertFalse(self.app.vcs.status(self.project)["dirty"])

    async def test_mark_versioned_failure_after_v2_keeps_committed_new_generation(self):
        candidate = self.project / "replacement.pdf"
        self._write_pdf(candidate, 2)
        await self._open_pdf()

        with patch.object(
            self.app.pdf_service.ReplacementTransaction,
            "mark_versioned",
            side_effect=OSError("mark fault fired"),
        ) as mark_versioned:
            replaced = await self._request("POST", "/api/pdf/replace", {
                "candidate": "replacement.pdf", "message": "two pages"
            })

        self.assertEqual(replaced.status_code, 200, replaced.text)
        mark_versioned.assert_called_once_with("v2")
        self.assertEqual(self.app.pdf_service.inspect_pdf(self.pdf)["page_count"], 2)
        self.assertEqual(sorted(p.name for p in self.runtime.render_dir().iterdir()), [
            "page-1.png", "page-2.png",
        ])
        self.assertFalse(candidate.exists())
        self.assertEqual([v["tag"] for v in self.app.vcs.list_versions(self.project)], ["v2", "v1"])
        # A later locked observation recognizes commit_intent + v2 and finishes cleanup.
        status = await self._request("GET", "/api/git/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertFalse(status.json()["dirty"])
        self.assertFalse((self.project / ".pdf-replacement-journal.json").exists())

    async def test_finalize_failure_after_v2_returns_committed_success_and_recovers_cleanup(self):
        candidate = self.project / "replacement.pdf"
        self._write_pdf(candidate, 2)
        await self._open_pdf()
        real_finalize = self.app.pdf_service.ReplacementTransaction.finalize
        calls = 0

        def fail_once(transaction):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("finalize fault fired")
            return real_finalize(transaction)

        with patch.object(
            self.app.pdf_service.ReplacementTransaction,
            "finalize",
            autospec=True,
            side_effect=fail_once,
        ):
            replaced = await self._request("POST", "/api/pdf/replace", {
                "candidate": "replacement.pdf", "message": "two pages"
            })

        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertEqual(calls, 1)
        self.assertEqual(self.app.pdf_service.inspect_pdf(self.pdf)["page_count"], 2)
        self.assertEqual([v["tag"] for v in self.app.vcs.list_versions(self.project)], ["v2", "v1"])
        self.assertTrue((self.project / ".pdf-replacement-journal.json").exists())
        self.assertEqual((await self._request("GET", "/api/state")).status_code, 200)
        self.assertFalse((self.project / ".pdf-replacement-journal.json").exists())

    async def test_all_pdf_observation_and_version_routes_take_the_common_lock(self):
        await self._open_pdf()
        saved = await self._request("POST", "/api/git/commit", {"message": "first"})
        self.assertEqual(saved.status_code, 200, saved.text)
        calls = []
        real_lock = self.app.pdf_service.project_write_lock

        @contextmanager
        def observed_lock(project):
            calls.append(Path(project).resolve())
            with real_lock(project):
                yield

        routes = [
            ("GET", "/api/state", None),
            ("GET", "/api/render-version", None),
            ("GET", "/api/slide-map", None),
            ("GET", "/api/pdf/text?page=1", None),
            ("GET", "/api/render/page-1.png", None),
            ("GET", "/api/pdf/transcripts", None),
            ("GET", "/api/git/status", None),
            ("GET", "/api/git/versions", None),
            ("POST", "/api/git/delete", {"tag": "v1"}),
        ]
        with patch.object(self.app.pdf_service, "project_write_lock", new=observed_lock):
            for method, route, payload in routes:
                before = len(calls)
                response = await self._request(method, route, payload)
                self.assertEqual(response.status_code, 200, (route, response.text))
                self.assertGreater(len(calls), before, route)
                self.assertTrue(all(path == self.project.resolve() for path in calls[before:]))

    async def test_reader_waits_for_replacement_pair_and_observes_new_generation(self):
        candidate = self.project / "replacement.pdf"
        self._write_pdf(candidate, 2)
        await self._open_pdf()
        primary_published = threading.Event()
        release_publish = threading.Event()
        original_fault = self.app.pdf_service.ReplacementTransaction._fault

        def gate_publish(transaction, boundary):
            original_fault(transaction, boundary)
            if boundary == "primary_published":
                primary_published.set()
                self.assertTrue(release_publish.wait(5))

        with patch.object(
            self.app.pdf_service.ReplacementTransaction,
            "_fault",
            autospec=True,
            side_effect=gate_publish,
        ):
            replace_task = asyncio.create_task(self._request("POST", "/api/pdf/replace", {
                "candidate": "replacement.pdf", "message": "two pages"
            }))
            self.assertTrue(await asyncio.to_thread(primary_published.wait, 3))
            reader_task = asyncio.create_task(self._request("GET", "/api/state"))
            await asyncio.sleep(0.1)
            self.assertFalse(reader_task.done())
            release_publish.set()
            replaced, observed = await asyncio.gather(replace_task, reader_task)

        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertEqual(observed.status_code, 200, observed.text)
        self.assertEqual(observed.json()["pages"], ["page-1.png", "page-2.png"])

    async def test_pdf_download_and_view_wait_for_replacement_and_stream_new_inode(self):
        candidate = self.project / "replacement.pdf"
        self._write_pdf(candidate, 2)
        expected_bytes = candidate.read_bytes()
        await self._open_pdf()
        primary_published = threading.Event()
        release_publish = threading.Event()
        original_fault = self.app.pdf_service.ReplacementTransaction._fault

        def gate_publish(transaction, boundary):
            original_fault(transaction, boundary)
            if boundary == "primary_published":
                primary_published.set()
                self.assertTrue(release_publish.wait(5))

        with patch.object(
            self.app.pdf_service.ReplacementTransaction,
            "_fault",
            autospec=True,
            side_effect=gate_publish,
        ):
            replacing = asyncio.create_task(self._request("POST", "/api/pdf/replace", {
                "candidate": "replacement.pdf", "message": "two pages"
            }))
            self.assertTrue(await asyncio.to_thread(primary_published.wait, 3))
            download = asyncio.create_task(self._request(
                "GET", "/api/project/files/download?path=document.pdf"
            ))
            view = asyncio.create_task(self._request(
                "GET", "/api/project/files/view?path=document.pdf"
            ))
            await asyncio.sleep(0.1)
            self.assertFalse(download.done())
            self.assertFalse(view.done())
            release_publish.set()
            replaced, downloaded, viewed = await asyncio.gather(
                replacing, download, view
            )

        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(viewed.status_code, 200, viewed.text)
        self.assertEqual(downloaded.content, expected_bytes)
        self.assertEqual(viewed.content, expected_bytes)
        self.assertIn("attachment", downloaded.headers["content-disposition"])
        self.assertEqual(viewed.headers["content-disposition"], "inline")

    async def test_stale_pdf_download_waiter_returns_400_after_project_switch(self):
        await self._open_pdf()
        second = Path(self._tmp.name) / "second-download"
        second.mkdir()
        second_pdf = second / "document.pdf"
        self._write_pdf(second_pdf, 2)
        second_info = {
            "id": "second-download-project",
            "name": "Second download",
            "path": str(second),
            "type": "pdf",
            "main_file": "document.pdf",
        }
        entered = multiprocessing.Event()
        release = multiprocessing.Event()
        holder = multiprocessing.Process(
            target=_hold_pdf_project_lock,
            args=(str(self.project), entered, release),
        )
        holder.start()
        self.assertTrue(entered.wait(3))
        attempted = threading.Event()
        real_lock = self.app.pdf_service.project_write_lock

        @contextmanager
        def observed_lock(project):
            if Path(project).resolve() == self.project.resolve():
                attempted.set()
            with real_lock(project):
                yield

        try:
            with patch.object(
                self.app.pdf_service,
                "project_write_lock",
                new=observed_lock,
            ):
                stale = asyncio.create_task(self._request(
                    "GET", "/api/project/files/download?path=document.pdf"
                ))
                self.assertTrue(await asyncio.to_thread(attempted.wait, 3))
                await self._open_pdf(second_info)
            release.set()
            response = await asyncio.wait_for(stale, 5)
        finally:
            release.set()
            holder.join(3)

        self.assertEqual(holder.exitcode, 0)
        self.assertEqual(response.status_code, 400, response.text)

    async def test_stale_page_patch_revalidates_after_shrink_without_blocking_event_loop(self):
        self.pdf.unlink()
        self._write_pdf(self.pdf, 2)
        candidate = self.project / "replacement.pdf"
        self._write_pdf(candidate, 1)
        await self._open_pdf()
        self.assertEqual((await self._request("PATCH", "/api/pdf/transcripts/2", {
            "text": "page two"
        })).status_code, 200)
        primary_published = threading.Event()
        release_publish = threading.Event()
        original_fault = self.app.pdf_service.ReplacementTransaction._fault

        def gate_publish(transaction, boundary):
            original_fault(transaction, boundary)
            if boundary == "primary_published":
                primary_published.set()
                self.assertTrue(release_publish.wait(5))

        with patch.object(
            self.app.pdf_service.ReplacementTransaction,
            "_fault",
            autospec=True,
            side_effect=gate_publish,
        ):
            replacement = asyncio.create_task(self._request("POST", "/api/pdf/replace", {
                "candidate": "replacement.pdf", "message": "shrink"
            }))
            self.assertTrue(await asyncio.to_thread(primary_published.wait, 3))
            stale_patch = asyncio.create_task(self._request(
                "PATCH", "/api/pdf/transcripts/2", {"text": "stale update"}
            ))
            heartbeat = asyncio.create_task(asyncio.sleep(0.05, result="alive"))
            self.assertEqual(await asyncio.wait_for(heartbeat, 1), "alive")
            self.assertFalse(stale_patch.done())
            release_publish.set()
            replaced, patched = await asyncio.gather(replacement, stale_patch)

        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertEqual(patched.status_code, 400, patched.text)
        transcripts = (await self._request("GET", "/api/pdf/transcripts")).json()
        self.assertNotIn("2", transcripts["pages"])
        self.assertEqual(transcripts["orphans"]["2"], {"text": "page two"})

    async def test_pdf_restore_holds_lock_through_git_render_and_reconcile_in_worker(self):
        candidate = self.project / "replacement.pdf"
        self._write_pdf(candidate, 2)
        await self._open_pdf()
        replaced = await self._request("POST", "/api/pdf/replace", {
            "candidate": "replacement.pdf", "message": "two pages"
        })
        self.assertEqual(replaced.status_code, 200, replaced.text)
        render_entered = threading.Event()
        release_render = threading.Event()
        real_render = self.app.pdf_service.render_pdf

        def gated_render(path, destination):
            render_entered.set()
            self.assertTrue(release_render.wait(5))
            return real_render(path, destination)

        with patch.object(self.app.pdf_service, "render_pdf", side_effect=gated_render):
            restore = asyncio.create_task(self._request(
                "POST", "/api/git/restore", {"tag": "v1"}
            ))
            self.assertTrue(await asyncio.to_thread(render_entered.wait, 3))
            reader = asyncio.create_task(self._request("GET", "/api/state"))
            heartbeat = asyncio.create_task(asyncio.sleep(0.05, result="alive"))
            self.assertEqual(await asyncio.wait_for(heartbeat, 1), "alive")
            await asyncio.sleep(0.05)
            self.assertFalse(reader.done())
            release_render.set()
            restored, observed = await asyncio.gather(restore, reader)

        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(observed.status_code, 200, observed.text)
        self.assertEqual(observed.json()["pages"], ["page-1.png"])
        self.assertEqual(
            (await self._request("GET", "/api/pdf/transcripts")).json()["pages"],
            {"1": {"text": ""}},
        )

    async def _prepare_versioned_two_page_pdf(self):
        candidate = self.project / "replacement.pdf"
        self._write_pdf(candidate, 2)
        await self._open_pdf()
        replaced = await self._request("POST", "/api/pdf/replace", {
            "candidate": "replacement.pdf", "message": "two pages"
        })
        self.assertEqual(replaced.status_code, 200, replaced.text)
        patched = await self._request("PATCH", "/api/pdf/transcripts/2", {
            "text": "current page two"
        })
        self.assertEqual(patched.status_code, 200, patched.text)
        saved = await self._request("POST", "/api/git/commit", {
            "message": "current transcript"
        })
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["tag"], "v3")
        return {
            "primary": self.pdf.read_bytes(),
            "render": {
                path.name: path.read_bytes()
                for path in self.runtime.render_dir().iterdir()
            },
            "transcript": (self.project / "transcript.json").read_bytes(),
        }

    async def _assert_failed_restore_kept_current_generation(self, before):
        self.assertEqual(self.pdf.read_bytes(), before["primary"])
        self.assertEqual({
            path.name: path.read_bytes()
            for path in self.runtime.render_dir().iterdir()
        }, before["render"])
        self.assertEqual(
            (self.project / "transcript.json").read_bytes(),
            before["transcript"],
        )
        status = self.app.vcs.status(self.project)
        self.assertEqual(status["current"], "v3")
        self.assertFalse(status["dirty"])
        transcripts = (await self._request("GET", "/api/pdf/transcripts")).json()
        self.assertEqual(transcripts["pages"]["2"], {"text": "current page two"})
        self.assertEqual(
            list(self.runtime.render_dir().parent.glob(".pdf-restore-*")),
            [],
        )

    async def test_pdf_restore_failure_after_git_reset_rolls_back_before_reader_unblocks(self):
        before = await self._prepare_versioned_two_page_pdf()
        refresh_entered = threading.Event()
        release_refresh = threading.Event()

        def fail_after_reset(*_args, **_kwargs):
            refresh_entered.set()
            self.assertTrue(release_refresh.wait(5))
            raise RuntimeError("refresh after reset failed")

        with patch.object(self.app, "_prepare_pdf", side_effect=fail_after_reset):
            restoring = asyncio.create_task(self._request(
                "POST", "/api/git/restore", {"tag": "v1"}
            ))
            self.assertTrue(await asyncio.to_thread(refresh_entered.wait, 3))
            reader = asyncio.create_task(self._request("GET", "/api/state"))
            await asyncio.sleep(0.05)
            self.assertFalse(reader.done())
            release_refresh.set()
            restored, observed = await asyncio.gather(
                restoring, reader, return_exceptions=True
            )

        self.assertFalse(isinstance(restored, BaseException), restored)
        self.assertEqual(restored.status_code, 500, restored.text)
        self.assertFalse(isinstance(observed, BaseException), observed)
        self.assertEqual(observed.status_code, 200, observed.text)
        self.assertEqual(observed.json()["pages"], ["page-1.png", "page-2.png"])
        await self._assert_failed_restore_kept_current_generation(before)

    async def test_pdf_restore_failure_after_render_install_restores_prior_render_and_git(self):
        before = await self._prepare_versioned_two_page_pdf()
        real_render = self.app.pdf_service.render_pdf

        def install_then_fail(path, destination):
            real_render(path, destination)
            raise RuntimeError("render install failed")

        with patch.object(
            self.app.pdf_service,
            "render_pdf",
            side_effect=install_then_fail,
        ):
            try:
                restored = await self._request(
                    "POST", "/api/git/restore", {"tag": "v1"}
                )
            except BaseException as exc:
                restored = exc

        self.assertFalse(isinstance(restored, BaseException), restored)
        self.assertEqual(restored.status_code, 500, restored.text)
        await self._assert_failed_restore_kept_current_generation(before)

    async def test_contended_pdf_open_uses_worker_and_common_lock(self):
        entered = multiprocessing.Event()
        release = multiprocessing.Event()
        holder = multiprocessing.Process(
            target=_hold_pdf_project_lock,
            args=(str(self.project), entered, release),
        )
        holder.start()
        self.assertTrue(entered.wait(3))

        opening = asyncio.create_task(self._open_pdf())
        heartbeat = asyncio.create_task(asyncio.sleep(0.05, result="alive"))
        self.assertEqual(await asyncio.wait_for(heartbeat, 1), "alive")
        self.assertFalse(opening.done())
        release.set()
        opened = await asyncio.wait_for(opening, 5)
        holder.join(3)

        self.assertTrue(opened["ok"])
        self.assertEqual(holder.exitcode, 0)

    async def test_lock_waiter_revalidates_identity_instead_of_redirecting_to_new_project(self):
        await self._open_pdf()
        second = Path(self._tmp.name) / "second"
        second.mkdir()
        second_pdf = second / "document.pdf"
        self._write_pdf(second_pdf, 2)
        second_info = {
            "id": "second-project", "name": "Second", "path": str(second),
            "type": "pdf", "main_file": "document.pdf",
        }
        entered = multiprocessing.Event()
        release = multiprocessing.Event()
        holder = multiprocessing.Process(
            target=_hold_pdf_project_lock,
            args=(str(self.project), entered, release),
        )
        holder.start()
        self.assertTrue(entered.wait(3))
        attempted = threading.Event()
        real_lock = self.app.pdf_service.project_write_lock

        @contextmanager
        def observed_lock(project):
            if Path(project).resolve() == self.project.resolve():
                attempted.set()
            with real_lock(project):
                yield

        with patch.object(self.app.pdf_service, "project_write_lock", new=observed_lock):
            stale_state = asyncio.create_task(self._request("GET", "/api/state"))
            self.assertTrue(await asyncio.to_thread(attempted.wait, 3))
            await self._open_pdf(second_info)
            release.set()
            response = await asyncio.wait_for(stale_state, 5)
        holder.join(3)

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("changed while waiting", response.text)
        self.assertEqual(self.runtime.current_file(), second_pdf.resolve())
        self.assertEqual(holder.exitcode, 0)
        shutil.rmtree(self.runtime.render_dir(second_pdf), ignore_errors=True)

    async def test_pdf_open_renders_safe_pages_and_skips_typst_lifecycle(self):
        self.assertEqual(self.runtime.set_file(str(self.pdf)), str(self.pdf.resolve()))
        self.assertEqual(self.runtime.document_type(), "pdf")
        self.runtime._state["file"] = self._previous_file

        with (
            patch.object(self.app.projects_mod, "get_project", return_value=self.info),
            patch.object(self.app.resolver, "start") as resolver_start,
            patch.object(self.app.resolver, "stop") as resolver_stop,
            patch.object(self.app.docstore, "ensure_room") as ensure_room,
            patch.object(self.app.docstore, "flush_now") as flush_now,
            patch.object(self.app.store, "set_path") as set_store_path,
            patch.object(self.app.workdir, "setup") as setup_workdir,
        ):
            opened = await self.app.open_project("pdf-project")

        self.assertTrue(opened["ok"])
        resolver_start.assert_not_called()
        resolver_stop.assert_called_once()
        ensure_room.assert_not_called()
        flush_now.assert_not_called()
        set_store_path.assert_not_called()
        setup_workdir.assert_called_once()
        self.assertTrue((self.runtime.render_dir() / "page-1.png").is_file())

        state = (await self._request("GET", "/api/state")).json()
        self.assertEqual(state["project_type"], "pdf")
        self.assertEqual(state["source"], "")
        self.assertEqual(state["pages"], ["page-1.png"])
        self.assertEqual(state["main"], "document.pdf")
        self.assertNotIn("room", state)
        self.assertNotIn("store", state)
        self.assertEqual((await self._request("GET", "/api/comments")).status_code, 400)

        version = (await self._request("GET", "/api/render-version")).json()
        self.assertEqual(version["pages"], ["page-1.png"])
        self.assertIn("version", version)
        self.assertNotIn("room", version)

        served = await self._request("GET", "/api/render/page-1.png")
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.headers["content-type"], "image/png")
        self.assertEqual((await self._request("GET", "/api/render/page-2.png")).status_code, 404)
        self.assertEqual((await self._request("GET", "/api/render/../document.pdf")).status_code, 404)

        slide_map = (await self._request("GET", "/api/slide-map")).json()
        self.assertEqual(slide_map["pages"], [{"page": 1, "slide_no": 1, "slide_total": 1,
                                                 "project_type": "pdf"}])
        self.assertEqual(slide_map["orphans"], [])

    async def test_pdf_transcripts_and_embedded_text_validate_all_input(self):
        with patch.object(self.app.projects_mod, "get_project", return_value=self.info):
            await self.app.open_project("pdf-project")

        transcripts = await self._request("GET", "/api/pdf/transcripts")
        self.assertEqual(transcripts.status_code, 200)
        self.assertEqual(transcripts.json()["pages"], {"1": {"text": ""}})

        patched = await self._request("PATCH", "/api/pdf/transcripts/1", {"text": "Narration"})
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.json()["pages"]["1"], {"text": "Narration"})
        self.assertEqual((await self._request("PATCH", "/api/pdf/transcripts/0", {"text": "bad"})).status_code, 400)
        self.assertEqual((await self._request("PATCH", "/api/pdf/transcripts/1", {"text": 7})).status_code, 400)

        bad_batch = await self._request("POST", "/api/pdf/transcripts/batch", {
            "updates": [{"page": 1, "text": "would write"}, {"page": 2, "text": "bad"}]
        })
        self.assertEqual(bad_batch.status_code, 400)
        self.assertEqual((await self._request("GET", "/api/pdf/transcripts")).json()["pages"]["1"],
                         {"text": "Narration"})

        text = await self._request("GET", "/api/pdf/text?page=1")
        self.assertEqual(text.status_code, 200)
        self.assertEqual(text.json(), {"page": 1, "text": "Embedded PDF text\n", "ocr": False})
        self.assertEqual((await self._request("GET", "/api/pdf/text?page=0")).status_code, 400)

    async def test_open_file_rejects_unbound_pdf_without_mutating_runtime(self):
        before_file = self.runtime.current_file()
        before_project = self.app._active_project
        opened = await self._request("POST", "/api/open-file", {"path": str(self.pdf)})

        self.assertEqual(opened.status_code, 400)
        self.assertEqual(self.runtime.current_file(), before_file)
        self.assertIs(self.app._active_project, before_project)

    async def test_lifespan_recovers_verified_persisted_pdf_without_typst_startup(self):
        @asynccontextmanager
        async def fake_docstore_server():
            yield

        self.runtime._state["file"] = str(self.pdf.resolve())
        self.app._active_project = None
        with (
            patch.object(self.app.docstore, "server", fake_docstore_server()),
            patch.object(self.app.app_config, "get_projects_root", return_value=self.project.parent),
            patch.object(self.app.projects_mod, "get_project", return_value=self.info),
            patch.object(self.app.docstore, "ensure_room") as ensure_room,
            patch.object(self.app.resolver, "start") as resolver_start,
            patch.object(self.app.store, "set_path") as set_store_path,
        ):
            async with self.app.lifespan(self.app.app):
                self.assertEqual(self.app._active_project, self.info)
                self.assertTrue((self.runtime.render_dir() / "page-1.png").is_file())
                self.assertTrue((self.project / "transcript.json").is_file())

        ensure_room.assert_not_called()
        resolver_start.assert_not_called()
        set_store_path.assert_not_called()

    async def test_project_type_mismatch_fails_closed_and_typst_stays_typst(self):
        bad_info = {**self.info, "main_file": "other.pdf"}
        with patch.object(self.app.projects_mod, "get_project", return_value=bad_info):
            with self.assertRaises(self.app.HTTPException) as exc:
                await self.app.open_project("pdf-project")
        self.assertEqual(exc.exception.status_code, 400)

        typ = self.project / "main.typ"
        typ.write_text("= Typst", encoding="utf-8")
        typ_info = {"id": "typst-project", "name": "Typst", "path": str(self.project),
                    "type": "typst", "main_file": "main.typ"}
        with (
            patch.object(self.app.projects_mod, "get_project", return_value=typ_info),
            patch.object(self.app.docstore, "ensure_room") as ensure_room,
            patch.object(self.app.resolver, "start") as resolver_start,
            patch.object(self.app.workdir, "setup", return_value={}),
            patch.object(self.app.vcs, "migrate"),
        ):
            await self.app.open_project("typst-project")
        self.assertEqual(self.runtime.document_type(), "typst")
        ensure_room.assert_called_once()
        resolver_start.assert_called_once()

    async def test_failed_pdf_switch_leaves_active_typst_runtime_and_services_intact(self):
        typ = self.project / "main.typ"
        typ.write_text("= Typst", encoding="utf-8")
        typ_info = {"id": "typst-project", "name": "Typst", "path": str(self.project),
                    "type": "typst", "main_file": "main.typ"}
        self.runtime.set_file(str(typ))
        self.app._active_project = typ_info

        with (
            patch.object(self.app.projects_mod, "get_project", return_value=self.info),
            patch.object(self.app.pdf_service, "render_pdf", side_effect=ValueError("render failed")),
            patch.object(self.app.resolver, "stop") as resolver_stop,
            patch.object(self.app.store, "close") as store_close,
        ):
            with self.assertRaises(self.app.HTTPException) as exc:
                await self.app.open_project("pdf-project")

        self.assertEqual(exc.exception.status_code, 400)
        self.assertEqual(self.runtime.current_file(), typ.resolve())
        self.assertEqual(self.app._active_project, typ_info)
        resolver_stop.assert_not_called()
        store_close.assert_not_called()

    async def test_pdf_mode_rejects_every_typst_only_endpoint_before_side_effects(self):
        await self._open_pdf()
        routes = [
            ("POST", "/api/preview/start", None),
            ("POST", "/api/preview/stop", None),
            ("GET", "/api/preview/status", None),
            ("POST", "/api/preview/resolve", {"page_no": 1, "x": 1, "y": 1}),
            ("POST", "/api/preview/locate", {"off": 0}),
            ("GET", "/api/notes", None),
            ("PATCH", "/api/notes", {"raw": "", "text": ""}),
            ("POST", "/api/notes", {"slide_line": 1, "text": ""}),
            ("GET", "/api/locate?page=1", None),
            ("GET", "/api/notes/export", None),
            ("GET", "/api/notes/pdfpc", None),
            ("POST", "/api/export-pdf", None),
            ("POST", "/api/preview/page-start", {"page_no": 1}),
            ("GET", "/api/source", None),
            ("GET", "/api/document", None),
            ("POST", "/api/edit", {"op": "insert_text", "at": 0, "text": "bad"}),
            ("POST", "/api/reset-from-disk", None),
            ("POST", "/api/compile", None),
        ]
        with (
            patch.object(self.app.resolver, "start") as resolver_start,
            patch.object(self.app.resolver, "stop") as resolver_stop,
            patch.object(self.app.docstore, "flush_now", new=AsyncMock()) as flush_now,
            patch.object(self.app.docstore, "get_text") as get_text,
            patch.object(self.app.workdir, "setup", return_value={}) as setup_workdir,
            patch.object(self.app.notes_mod, "list_notes") as list_notes,
        ):
            for method, path, payload in routes:
                self.assertEqual((await self._request(method, path, payload)).status_code, 400, path)
            setup = await self._request("POST", "/api/setup-workdir")
            self.assertEqual(setup.status_code, 200, setup.text)
            setup_workdir.assert_called_once()
        resolver_start.assert_not_called()
        resolver_stop.assert_not_called()
        flush_now.assert_not_called()
        get_text.assert_not_called()
        list_notes.assert_not_called()

    async def test_pdf_project_primary_symlink_is_rejected(self):
        target = self.project / "target.pdf"
        target.write_bytes(self.pdf.read_bytes())
        self.pdf.unlink()
        self.pdf.symlink_to(target.name)

        with patch.object(self.app.projects_mod, "get_project", return_value=self.info):
            with self.assertRaises(self.app.HTTPException) as exc:
                await self.app.open_project("pdf-project")
        self.assertEqual(exc.exception.status_code, 400)

    async def test_symlinked_render_page_is_neither_listed_nor_served(self):
        await self._open_pdf()
        page = self.runtime.render_dir() / "page-1.png"
        outside = self.project / "outside.png"
        outside.write_bytes(page.read_bytes())
        page.unlink()
        page.symlink_to(outside)

        self.assertEqual((await self._request("GET", "/api/state")).json()["pages"], [])
        self.assertEqual((await self._request("GET", "/api/render/page-1.png")).status_code, 404)

    async def test_identical_pdf_pages_in_distinct_projects_advance_render_version(self):
        second = Path(self._tmp.name) / "second"
        second.mkdir()
        second_pdf = second / "document.pdf"
        shutil.copy2(self.pdf, second_pdf)
        second_info = {"id": "second-project", "name": "Second", "path": str(second),
                       "type": "pdf", "main_file": "document.pdf"}
        await self._open_pdf()
        first_version = (await self._request("GET", "/api/render-version")).json()["version"]
        await self._open_pdf(second_info)
        second_version = (await self._request("GET", "/api/render-version")).json()["version"]

        self.assertNotEqual(first_version, second_version)
        shutil.rmtree(self.runtime.render_dir(second_pdf), ignore_errors=True)

    async def test_late_pdf_fingerprint_failure_does_not_publish_or_stop_typst(self):
        typ = self.project / "main.typ"
        typ.write_text("= Typst", encoding="utf-8")
        typ_info = {"id": "typst-project", "name": "Typst", "path": str(self.project),
                    "type": "typst", "main_file": "main.typ"}
        self.runtime.set_file(str(typ))
        self.app._active_project = typ_info
        with (
            patch.object(self.app.projects_mod, "get_project", return_value=self.info),
            patch.object(self.app, "_record_pdf_render_version", side_effect=RuntimeError("hash failed")),
            patch.object(self.app.resolver, "stop") as resolver_stop,
            patch.object(self.app.store, "close") as store_close,
        ):
            with self.assertRaises(RuntimeError):
                await self.app.open_project("pdf-project")
        self.assertEqual(self.runtime.current_file(), typ.resolve())
        self.assertEqual(self.app._active_project, typ_info)
        resolver_stop.assert_not_called()
        store_close.assert_not_called()

    async def test_failed_typst_switch_restores_previous_pdf_and_stops_typst_service(self):
        await self._open_pdf()
        with (
            patch.object(self.app.projects_mod, "get_project", return_value={
                "id": "typst-project", "name": "Typst", "path": str(self.project),
                "type": "typst", "main_file": "main.typ",
            }),
            patch.object(self.app.docstore, "ensure_room", new=AsyncMock()),
            patch.object(self.app.resolver, "start", side_effect=RuntimeError("start failed")),
            patch.object(self.app.resolver, "stop") as resolver_stop,
            patch.object(self.app.store, "set_path"),
            patch.object(self.app.store, "close") as store_close,
        ):
            (self.project / "main.typ").write_text("= Typst", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                await self.app.open_project("typst-project")
        self.assertEqual(self.runtime.document_type(), "pdf")
        self.assertEqual(self.app._active_project, self.info)
        self.assertFalse(self.app.docstore.is_running())
        resolver_stop.assert_called()
        store_close.assert_called()

    async def test_pdf_versions_skip_crdt_flush_rotate_and_typst_startup(self):
        await self._open_pdf()
        with (
            patch.object(self.app.docstore, "flush_now", new=AsyncMock()) as flush_now,
            patch.object(self.app.docstore, "rotate") as rotate,
            patch.object(self.app.docstore, "ensure_room", new=AsyncMock()) as ensure_room,
            patch.object(self.app.resolver, "start") as resolver_start,
            patch.object(self.app.vcs, "save_version", return_value={"ok": True}),
            patch.object(self.app.vcs, "restore_version", return_value={"ok": True}),
            patch.object(self.app.vcs, "_head_commit", return_value="a" * 40),
            patch.object(self.app, "_activate_pdf", new=AsyncMock(return_value={})),
        ):
            self.assertEqual((await self._request("POST", "/api/git/commit", {"message": "v1"})).status_code, 200)
            self.assertEqual((await self._request("POST", "/api/git/restore", {"tag": "v1"})).status_code, 200)
        flush_now.assert_not_called()
        rotate.assert_not_called()
        ensure_room.assert_not_called()
        resolver_start.assert_not_called()

    async def test_persisted_pdf_lifespan_never_enters_crdt_server_or_sets_loop(self):
        class ProbeServer:
            entered = False

            async def __aenter__(self):
                self.entered = True
                return self

            async def __aexit__(self, *_args):
                return False

        probe = ProbeServer()
        self.runtime._state["file"] = str(self.pdf.resolve())
        self.app._active_project = None
        with (
            patch.object(self.app.docstore, "server", probe),
            patch.object(self.app.docstore, "set_loop") as set_loop,
            patch.object(self.app.app_config, "get_projects_root", return_value=self.project.parent),
            patch.object(self.app.projects_mod, "get_project", return_value=self.info),
        ):
            async with self.app.lifespan(self.app.app):
                self.assertFalse(probe.entered)
                set_loop.assert_not_called()

    async def test_crdt_lifecycle_is_lazy_restarts_after_pdf_and_clears_old_rooms(self):
        await self._open_pdf()
        self.assertFalse(self.app.docstore.is_running())
        typ = self.project / "main.typ"
        typ.write_text("= Typst", encoding="utf-8")
        typ_info = {"id": "typst-project", "name": "Typst", "path": str(self.project),
                    "type": "typst", "main_file": "main.typ"}
        await self._open_pdf(typ_info)
        self.assertTrue(self.app.docstore.is_running())
        self.assertTrue(self.app.docstore._rooms)
        first_server = self.app.docstore.server

        await self._open_pdf()
        self.assertFalse(self.app.docstore.is_running())
        self.assertEqual(self.app.docstore._rooms, {})

        await self._open_pdf(typ_info)
        self.assertTrue(self.app.docstore.is_running())
        self.assertTrue(self.app.docstore._rooms)
        self.assertIsNot(self.app.docstore.server, first_server)

    async def test_pdf_transition_retires_crdt_generation_and_rejects_stale_room_updates(self):
        """A pre-PDF Yjs update cannot merge into the new disk-seeded Typst lineage."""
        typ = self.project / "main.typ"
        typ.write_text("hello", encoding="utf-8")
        typ_info = {"id": "typst-project", "name": "Typst", "path": str(self.project),
                    "type": "typst", "main_file": "main.typ"}
        await self._open_pdf(typ_info)
        old_key = self.app.docstore.room_name()
        base = self.app.docstore._base_key()
        old_update = self.app.docstore._rooms[old_key]["doc"].get_update()
        self.assertTrue(old_update)

        # A retired poisoned room can coexist with the live one; stopping must advance its
        # shared base only once, rather than once per room.
        future_key = f"{base}~g999"
        await self.app.docstore.ensure_room(typ, key=future_key)
        before_generation = self.app.docstore._gen.get(base, 0)

        await self._open_pdf()
        self.assertFalse(self.app.docstore.is_running())
        self.assertEqual(self.app.docstore._rooms, {})
        self.assertEqual(self.app.docstore._gen[base], before_generation + 1)

        await self._open_pdf(typ_info)
        current_key = self.app.docstore.room_name()
        current = self.app.docstore._rooms[current_key]
        self.assertNotEqual(current_key, old_key)
        self.assertEqual(str(current["text"]), "hello")
        self.assertIsNone(await self.app.docstore.ensure_room_by_key(old_key))
        self.assertIsNone(await self.app.docstore.ensure_room_by_key(future_key))

        class StaleWebSocket:
            accepted = False
            closed = None
            received = False

            async def accept(self):
                self.accepted = True

            async def close(self, code):
                self.closed = code

            async def receive_bytes(self):
                self.received = True
                return old_update

        stale_socket = StaleWebSocket()
        with patch.object(self.app.docstore, "start", new=AsyncMock(
                side_effect=AssertionError("stale room must be rejected before CRDT startup"))):
            await self.app.yjs_ws(stale_socket, old_key)
        self.assertEqual(stale_socket.closed, 1008)
        self.assertFalse(stale_socket.accepted)
        self.assertFalse(stale_socket.received)
        self.assertEqual(str(self.app.docstore._rooms[current_key]["text"]), "hello")
        self.assertEqual(typ.read_text(encoding="utf-8"), "hello")

    async def test_yjs_start_race_with_pdf_switch_cleans_the_new_crdt_lifecycle(self):
        typ = self.project / "main.typ"
        typ.write_text("= Typst", encoding="utf-8")
        typ_info = {"id": "typst-project", "name": "Typst", "path": str(self.project),
                    "type": "typst", "main_file": "main.typ"}
        await self._open_pdf(typ_info)
        key = self.app.docstore.room_name()

        class RaceWebSocket:
            accepted = False
            closed = None
            received = False

            async def accept(self):
                self.accepted = True

            async def close(self, code):
                self.closed = code

            async def receive_bytes(self):
                self.received = True
                return b"stale update"

        real_start = self.app.docstore.start

        async def switch_to_pdf_then_start(*args, **kwargs):
            await self._open_pdf()
            return await real_start(*args, **kwargs)

        socket = RaceWebSocket()
        with patch.object(self.app.docstore, "start", new=switch_to_pdf_then_start):
            await self.app.yjs_ws(socket, key)

        self.assertEqual(self.runtime.document_type(), "pdf")
        self.assertEqual(socket.closed, 1008)
        self.assertFalse(socket.accepted)
        self.assertFalse(socket.received)
        self.assertFalse(self.app.docstore.is_running())
        self.assertEqual(self.app.docstore._rooms, {})

    async def test_yjs_ensure_race_with_pdf_switch_never_accepts_the_old_socket(self):
        typ = self.project / "main.typ"
        typ.write_text("= Typst", encoding="utf-8")
        typ_info = {"id": "typst-project", "name": "Typst", "path": str(self.project),
                    "type": "typst", "main_file": "main.typ"}
        await self._open_pdf(typ_info)
        key = self.app.docstore.room_name()

        class RaceWebSocket:
            accepted = False
            closed = None
            received = False

            async def accept(self):
                self.accepted = True

            async def close(self, code):
                self.closed = code

            async def receive_bytes(self):
                self.received = True
                return b"stale update"

        real_ensure = self.app.docstore.ensure_room_by_key

        async def ensure_then_switch_to_pdf(room):
            state = await real_ensure(room)
            await self._open_pdf()
            return state

        socket = RaceWebSocket()
        with patch.object(self.app.docstore, "ensure_room_by_key", new=ensure_then_switch_to_pdf):
            await self.app.yjs_ws(socket, key)

        self.assertEqual(self.runtime.document_type(), "pdf")
        self.assertEqual(socket.closed, 1008)
        self.assertFalse(socket.accepted)
        self.assertFalse(socket.received)
        self.assertFalse(self.app.docstore.is_running())
        self.assertEqual(self.app.docstore._rooms, {})

    async def test_start_discards_fresh_server_when_pdf_wins_during_enter(self):
        typ = self.project / "main.typ"
        typ.write_text("= Typst", encoding="utf-8")
        typ_info = {"id": "typst-project", "name": "Typst", "path": str(self.project),
                    "type": "typst", "main_file": "main.typ"}
        await self._open_pdf(typ_info)
        await self.app.docstore.stop()
        entered = asyncio.Event()
        release_enter = asyncio.Event()

        class GatedServer:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                entered.set()
                await release_enter.wait()
                return self

            async def __aexit__(self, *_args):
                return False

        with patch.object(self.app.docstore, "WebsocketServer", GatedServer):
            start_task = asyncio.create_task(self.app.docstore.start())
            await entered.wait()
            pdf_task = asyncio.create_task(self._open_pdf())
            while self.runtime.document_type() != "pdf":
                await asyncio.sleep(0)
            release_enter.set()
            started = await start_task
            await pdf_task

        self.assertIsNone(started)
        self.assertFalse(self.app.docstore.is_running())
        self.assertEqual(self.app.docstore._rooms, {})

    async def test_delayed_stale_a_cleanup_cannot_stop_active_typst_b(self):
        typ_a = self.project / "a.typ"
        typ_b = self.project / "b.typ"
        typ_a.write_text("= A", encoding="utf-8")
        typ_b.write_text("= B", encoding="utf-8")
        info_a = {"id": "typst-a", "name": "A", "path": str(self.project),
                  "type": "typst", "main_file": "a.typ"}
        info_b = {"id": "typst-b", "name": "B", "path": str(self.project),
                  "type": "typst", "main_file": "b.typ"}
        await self._open_pdf(info_a)
        key_a = self.app.docstore.room_name()
        start_entered = asyncio.Event()
        release_start = asyncio.Event()
        stale_stop_entered = asyncio.Event()
        release_stale_stop = asyncio.Event()
        real_start = self.app.docstore.start
        real_stop = self.app.docstore.stop
        stale_task = None

        class RaceWebSocket:
            accepted = False
            closed = None
            received = False

            async def accept(self):
                self.accepted = True

            async def close(self, code):
                self.closed = code

            async def receive_bytes(self):
                self.received = True
                return b"stale update"

        async def gate_start(*args, **kwargs):
            if asyncio.current_task() is stale_task:
                start_entered.set()
                await release_start.wait()
            return await real_start(*args, **kwargs)

        async def gate_stale_stop():
            if asyncio.current_task() is stale_task:
                stale_stop_entered.set()
                await release_stale_stop.wait()
            await real_stop()

        stale_socket = RaceWebSocket()
        with (
            patch.object(self.app.docstore, "start", new=gate_start),
            patch.object(self.app.docstore, "stop", new=gate_stale_stop),
        ):
            stale_task = asyncio.create_task(self.app.yjs_ws(stale_socket, key_a))
            await start_entered.wait()
            await self._open_pdf()
            release_start.set()
            while not stale_task.done() and not stale_stop_entered.is_set():
                await asyncio.sleep(0)
            await self._open_pdf(info_b)
            key_b = self.app.docstore.room_name()
            self.assertTrue(self.app.docstore.is_running())
            self.assertIn(key_b, self.app.docstore._rooms)
            release_stale_stop.set()
            await stale_task

        self.assertEqual(stale_socket.closed, 1008)
        self.assertFalse(stale_socket.accepted)
        self.assertFalse(stale_socket.received)
        self.assertEqual(self.runtime.current_file(), typ_b.resolve())
        self.assertTrue(self.app.docstore.is_running())
        self.assertIn(key_b, self.app.docstore._rooms)

        b_socket = RaceWebSocket()
        with patch.object(self.app.docstore.server, "serve", new=AsyncMock()):
            await self.app.yjs_ws(b_socket, key_b)
        self.assertTrue(b_socket.accepted)
        self.assertFalse(b_socket.received)

    async def test_active_typst_lifecycle_serves_non_active_file_rooms(self):
        main = self.project / "main.typ"
        other = self.project / "appendix.typ"
        main.write_text("= Main", encoding="utf-8")
        other.write_text("= Appendix", encoding="utf-8")
        info = {"id": "typst-project", "name": "Typst", "path": str(self.project),
                "type": "typst", "main_file": "main.typ"}
        await self._open_pdf(info)

        room = await self.app.docstore.ensure_room(other)
        self.assertIsNotNone(room)
        self.assertEqual(room["key"], self.app.docstore.room_name(other))
        edited = await self.app.docstore.apply_edits([
            {"selector": {"by": "anchor", "text": "Appendix"}, "text": "Appendix A"},
        ], other)
        self.assertTrue(edited["ok"], edited)
        self.assertEqual(other.read_text(encoding="utf-8"), "= Appendix A")

    async def test_pdf_render_directory_symlink_is_not_listed_or_served(self):
        await self._open_pdf()
        render_dir = self.runtime.render_dir()
        external = self.project / "external-render"
        external.mkdir()
        (external / "page-1.png").write_bytes((render_dir / "page-1.png").read_bytes())
        render_dir.rename(self.project / "real-render")
        render_dir.symlink_to(external, target_is_directory=True)

        self.assertEqual((await self._request("GET", "/api/state")).json()["pages"], [])
        self.assertEqual((await self._request("GET", "/api/render/page-1.png")).status_code, 404)

    async def test_pdf_render_stream_keeps_opened_inode_after_path_swap(self):
        await self._open_pdf()
        page = self.runtime.render_dir() / "page-1.png"
        original = page.read_bytes()
        stream = self.app._open_pdf_render_page("page-1.png")
        self.assertIsNotNone(stream)
        page.unlink()
        page.write_bytes(b"replacement")
        try:
            self.assertEqual(stream.read(), original)
        finally:
            stream.close()


class PdfMcpTest(unittest.TestCase):
    """The PDF terminal bridge is deliberately independent from Typst/comment state."""

    def setUp(self):
        import app
        import httpx
        import pdf_mcp_server
        import runtime

        self.app = app
        self.httpx = httpx
        self.mcp = pdf_mcp_server
        self.runtime = runtime
        self.loop = asyncio.new_event_loop()
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "PDF project & notes"
        self.project.mkdir()
        self.pdf = self.project / "document.pdf"
        self._write_pdf(self.pdf, 2)
        self.info = {"id": "pdf-mcp", "name": "PDF", "path": str(self.project),
                     "type": "pdf", "main_file": "document.pdf"}
        self.previous_file = runtime._state.get("file")
        self.previous_project = app._active_project
        self.state_path = patch.object(runtime, "GLOBAL_STATE_PATH", self.project / "state.json")
        self.state_path.start()
        self.original_backend = self.mcp._backend
        self.mcp._backend = self._backend_shim
        self.loop.run_until_complete(self._open_pdf())

    def tearDown(self):
        self.mcp._backend = self.original_backend
        self.loop.run_until_complete(self.app.docstore.stop())
        self.runtime._state["file"] = self.previous_file
        self.app._active_project = self.previous_project
        shutil.rmtree(self.runtime.render_dir(self.pdf), ignore_errors=True)
        self.state_path.stop()
        self._tmp.cleanup()
        self.loop.close()

    def _write_pdf(self, path: Path, pages: int) -> None:
        import fitz

        document = fitz.open()
        for number in range(pages):
            page = document.new_page()
            page.insert_text((72, 72), f"PDF MCP page {number + 1}")
        document.save(path)
        document.close()

    async def _open_pdf(self):
        with patch.object(self.app.projects_mod, "get_project", return_value=self.info):
            await self.app.open_project(self.info["id"])

    def _backend_shim(self, method, path, payload=None):
        return self.loop.run_until_complete(self._route(method, path, payload))

    async def _route(self, method, path, payload):
        transport = self.httpx.ASGITransport(app=self.app.app)
        async with self.httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.request(method, path, json=payload)
            body = response.json()
            if response.status_code >= 400:
                return {"ok": False, "error": f"backend {response.status_code}: {body}"}
            return body

    def test_pdf_tools_use_only_pdf_http_api_with_encoded_inputs(self):
        self.assertEqual(self.mcp.get_pdf_info()["project_type"], "pdf")
        self.assertEqual(self.mcp.get_pdf_text(1), {
            "page": 1, "text": "PDF MCP page 1\n", "ocr": False,
        })
        self.assertEqual(self.mcp.get_transcripts()["pages"]["1"], {"text": ""})
        self.assertEqual(self.mcp.set_transcript(1, "One")["pages"]["1"], {"text": "One"})
        self.assertEqual(
            self.mcp.set_transcripts([{"page": 1, "text": "Batch one"},
                                      {"page": 2, "text": "Batch two"}])["pages"]["2"],
            {"text": "Batch two"},
        )

        candidate = self.project / "candidate & #1.pdf"
        self._write_pdf(candidate, 1)
        replaced = self.mcp.replace_pdf(candidate.name, "replace & preserve")
        self.assertTrue(replaced["ok"])
        self.assertEqual(replaced["page_count"], 1)
        self.assertEqual(self.mcp.list_orphan_transcripts(), {
            "orphans": {"2": {"text": "Batch two"}},
        })
        restored = self.mcp.restore_orphan_transcript("2", 1)
        self.assertEqual(restored["pages"]["1"], {"text": "Batch two"})
        self.assertEqual(restored["orphans"]["1"], {"text": "Batch one"})

    def test_pdf_mcp_returns_backend_errors_and_encodes_query_values(self):
        calls = []

        def failing_backend(method, path, payload=None):
            calls.append((method, path, payload))
            return {"ok": False, "error": "backend 400: invalid input"}

        with patch.object(self.mcp, "_backend", new=failing_backend):
            self.assertEqual(self.mcp.get_pdf_text("1 & #"), {
                "ok": False, "error": "backend 400: invalid input",
            })
            self.assertEqual(self.mcp.replace_pdf("/tmp/a & #.pdf", "message"), {
                "ok": False, "error": "backend 400: invalid input",
            })
        self.assertEqual(calls, [
            ("GET", "/api/pdf/text?page=1+%26+%23", None),
            ("POST", "/api/pdf/replace", {"candidate": "/tmp/a & #.pdf", "message": "message"}),
        ])

    def test_pdf_workdir_uses_pdf_server_and_preserves_typst_and_user_content(self):
        import workdir

        (self.project / "AGENTS.md").write_text("# User rules\n", encoding="utf-8")
        (self.project / ".mcp.json").write_text(json.dumps({"mcpServers": {
            "other": {"command": "other"},
        }}), encoding="utf-8")
        typst_section = workdir._section("main.typ")
        self.assertEqual(self.runtime.set_file(str(self.pdf)), str(self.pdf.resolve()))
        workdir.setup(8123)

        mcp_config = json.loads((self.project / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(mcp_config["mcpServers"]["other"], {"command": "other"})
        pdf_server = mcp_config["mcpServers"][workdir.MCP_NAME]
        self.assertEqual(pdf_server["args"], [str(workdir._PDF_MCP_SERVER)])
        self.assertEqual(pdf_server["env"], {"TCB_BACKEND_URL": "http://127.0.0.1:8123"})
        agents = (self.project / "AGENTS.md").read_text(encoding="utf-8")
        codex = (self.project / ".codex" / "config.toml").read_text(encoding="utf-8")
        self.assertIn("# User rules", agents)
        self.assertIn("native PDF utilities", agents)
        self.assertIn("replace_pdf", agents)
        self.assertIn("must not overwrite, delete, or move `document.pdf`", agents)
        for forbidden in ("apply_edits", "get_pending_comments", "touying", "Typst", "COMMENT_STORE_PATH"):
            self.assertNotIn(forbidden, agents + codex)
        self.assertTrue(workdir.is_ready())
        pdf_outputs = {
            path: path.read_bytes() for path in (
                self.project / ".mcp.json",
                self.project / "AGENTS.md",
                self.project / ".codex" / "config.toml",
            )
        }
        workdir.setup(8123)
        self.assertEqual({path: path.read_bytes() for path in pdf_outputs}, pdf_outputs)

        typ = self.project / "main.typ"
        typ.write_text("= Typst", encoding="utf-8")
        self.runtime.set_file(str(typ))
        self.assertFalse(workdir.is_ready())
        workdir.setup(8123)
        self.assertEqual(workdir._section("main.typ"), typst_section)
        typst_config = json.loads((self.project / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(typst_config["mcpServers"][workdir.MCP_NAME]["args"], [str(workdir._MCP_SERVER)])
        self.assertIn("COMMENT_STORE_PATH", typst_config["mcpServers"][workdir.MCP_NAME]["env"])
        self.assertTrue(workdir.is_ready())
