import asyncio
import json
import shutil
import sys
import tempfile
import threading
from contextlib import asynccontextmanager
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
        setup_workdir.assert_not_called()
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
            ("POST", "/api/setup-workdir", None),
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
            patch.object(self.app.workdir, "setup") as setup_workdir,
            patch.object(self.app.notes_mod, "list_notes") as list_notes,
        ):
            for method, path, payload in routes:
                self.assertEqual((await self._request(method, path, payload)).status_code, 400, path)
        resolver_start.assert_not_called()
        resolver_stop.assert_not_called()
        flush_now.assert_not_called()
        get_text.assert_not_called()
        setup_workdir.assert_not_called()
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
