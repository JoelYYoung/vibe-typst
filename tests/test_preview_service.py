import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import preview_service


class PreviewServiceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.project_dir = self.root / "project"
        self.project_dir.mkdir()
        self.render_dir = self.root / "render"
        self.render_dir.mkdir()
        (self.project_dir / "main.typ").write_text(
            "= Main", encoding="utf-8"
        )
        self.typst_project = {
            "id": "p1",
            "type": "typst",
            "main_file": "main.typ",
            "path": str(self.project_dir),
        }

    def tearDown(self):
        self._tmp.cleanup()

    def test_typst_svg_is_converted_to_bounded_png(self):
        (self.render_dir / "page-1.svg").write_text(
            """
            <svg xmlns="http://www.w3.org/2000/svg" width="100" height="50">
              <rect width="100" height="50" fill="#334455"/>
            </svg>
            """,
            encoding="utf-8",
        )

        with patch.object(
            preview_service.runtime,
            "render_dir",
            return_value=self.render_dir,
        ):
            result = preview_service.get_page_png(
                self.typst_project, 1
            )

        self.assertEqual(result["media_type"], "image/png")
        self.assertEqual(result["page"], 1)
        self.assertTrue(result["data"].startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLessEqual(
            len(result["data"]), preview_service.MAX_PREVIEW_BYTES
        )

    def test_pdf_uses_existing_rendered_png_and_validates_page(self):
        project = {
            **self.typst_project,
            "type": "pdf",
            "main_file": "document.pdf",
        }
        (self.project_dir / "document.pdf").write_bytes(b"%PDF fixture")
        png = b"\x89PNG\r\n\x1a\nrendered"
        (self.render_dir / "page-1.png").write_bytes(png)

        with patch.object(
            preview_service.runtime,
            "render_dir",
            return_value=self.render_dir,
        ):
            result = preview_service.get_page_png(project, 1)
            with self.assertRaises(ValueError):
                preview_service.get_page_png(project, 2)

        self.assertEqual(result["data"], png)

    def test_render_symlinks_are_never_followed(self):
        outside = self.root / "outside.svg"
        outside.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"/>',
            encoding="utf-8",
        )
        (self.render_dir / "page-1.svg").symlink_to(outside)

        with (
            patch.object(
                preview_service.runtime,
                "render_dir",
                return_value=self.render_dir,
            ),
            self.assertRaises(PermissionError),
        ):
            preview_service.get_page_png(self.typst_project, 1)

    def test_agent_preview_endpoint_binds_response_to_active_context(self):
        import app

        (self.render_dir / "page-1.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>',
            encoding="utf-8",
        )
        previous_project = app._active_project
        previous_context = app._project_context_version
        app._active_project = self.typst_project
        app._project_context_version = "ctx-preview"
        try:
            with (
                patch.object(
                    app.runtime,
                    "project_dir",
                    return_value=self.project_dir,
                ),
                patch.object(
                    app.runtime,
                    "current_file",
                    return_value=self.project_dir / "main.typ",
                ),
                patch.object(
                    app.runtime,
                    "render_dir",
                    return_value=self.render_dir,
                ),
            ):
                response = app.agent_page_preview(1)
        finally:
            app._active_project = previous_project
            app._project_context_version = previous_context

        self.assertEqual(response.media_type, "image/png")
        self.assertEqual(response.headers["x-project-id"], "p1")
        self.assertEqual(
            response.headers["x-context-version"], "ctx-preview"
        )


if __name__ == "__main__":
    unittest.main()
