import json
import sys
import tempfile
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

    def test_corrupt_or_unsupported_sidecar_is_rejected_without_replacing_it(self):
        sidecar = self.root / "transcript.json"
        for contents in ["{ not json", json.dumps({"schema_version": 2, "pdf": "document.pdf",
                                                       "pages": {}, "orphans": {}})]:
            sidecar.write_text(contents, encoding="utf-8")
            with self.assertRaises(ValueError):
                self.transcript.load(self.root, "document.pdf", 1)
            self.assertEqual(sidecar.read_text(encoding="utf-8"), contents)

    def test_failed_atomic_write_preserves_prior_sidecar_and_cleans_temp_file(self):
        self.transcript.set_page(self.root, "document.pdf", 1, 1, "before")
        sidecar = self.root / "transcript.json"
        before = sidecar.read_bytes()

        with patch.object(self.transcript.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.transcript.set_page(self.root, "document.pdf", 1, 1, "after")

        self.assertEqual(sidecar.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".transcript-*")), [])
