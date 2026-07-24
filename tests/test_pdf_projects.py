import json
import sys
import tempfile
import threading
from contextlib import asynccontextmanager
import unittest
from pathlib import Path
from unittest.mock import patch


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
        self._state_path = patch.object(runtime, "GLOBAL_STATE_PATH", self.project / "state.json")
        self._state_path.start()

    async def asyncTearDown(self):
        self.runtime._state["file"] = self._previous_file
        self.app._active_project = self._previous_project
        self._state_path.stop()
        self._tmp.cleanup()

    async def _request(self, method: str, path: str, payload=None):
        transport = self.httpx.ASGITransport(app=self.app.app)
        async with self.httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, json=payload)

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
