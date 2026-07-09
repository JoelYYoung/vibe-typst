import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


class DocstoreExternalEditGuardTest(unittest.TestCase):
    def setUp(self):
        import docstore

        self.docstore = docstore
        self.loop = asyncio.new_event_loop()
        docstore.set_loop(self.loop)

    def tearDown(self):
        self.loop.close()
        self.docstore.set_loop(None)

    def _state(self, text_value):
        from pycrdt import Doc, Text

        doc = Doc()
        text = doc.get("source", type=Text)
        text.insert(0, text_value)
        return {
            "key": "test-room",
            "base": None,
            "doc": doc,
            "text": text,
            "path": Path(tempfile.gettempdir()) / "vibe-typst-test.typ",
            "timer": None,
            "writeback": False,
            "external_guard_old": None,
            "external_guard_new": None,
            "external_guard_until": 0,
        }

    def test_stale_client_replay_cannot_undo_external_mcp_edit(self):
        old = "#slide[old title]"
        new = "#slide[new title]"
        st = self._state(old)

        self.docstore._replace_text(st, new)
        self.docstore._guard_external_edit(st, old)

        self.docstore._replace_text(st, old)
        self.docstore._sync(st)

        self.assertEqual(str(st["text"]), new)
        self.assertEqual(self.docstore._latest[st["key"]], new)
        if st["timer"]:
            st["timer"].cancel()

    def test_guard_does_not_clobber_non_exact_concurrent_edit(self):
        old = "#slide[old title]"
        new = "#slide[new title]"
        concurrent = old + "\n// human typed"
        st = self._state(old)

        self.docstore._replace_text(st, new)
        self.docstore._guard_external_edit(st, old)

        self.docstore._replace_text(st, concurrent)
        self.docstore._sync(st)

        self.assertEqual(str(st["text"]), concurrent)
        if st["timer"]:
            st["timer"].cancel()


class RenderTokenRegressionTest(unittest.TestCase):
    def test_page_tokens_are_content_hashes_and_change_with_svg_bytes(self):
        import typst_service

        with tempfile.TemporaryDirectory() as td:
            render_dir = Path(td)
            (render_dir / "page-2.svg").write_text("<svg>two</svg>", encoding="utf-8")
            (render_dir / "page-1.svg").write_text("<svg>one</svg>", encoding="utf-8")

            with patch.object(typst_service.runtime, "render_dir", return_value=render_dir):
                self.assertEqual(typst_service.list_pages(), ["page-1.svg", "page-2.svg"])
                first = typst_service.page_tokens()
                self.assertEqual(
                    first["page-1.svg"],
                    hashlib.sha1(b"<svg>one</svg>").hexdigest()[:12],
                )

                (render_dir / "page-1.svg").write_text("<svg>changed</svg>", encoding="utf-8")
                second = typst_service.page_tokens()

        self.assertNotEqual(first["page-1.svg"], second["page-1.svg"])
        self.assertEqual(first["page-2.svg"], second["page-2.svg"])


class CodexWrapperRegressionTest(unittest.TestCase):
    def test_project_codex_config_is_loaded_and_cleared_by_directory(self):
        wrapper = ROOT / "codex-project-wrapper.sh"
        managed = """# TYPST-COMMENT-BRIDGE:BEGIN (auto-managed - edits here will be overwritten)
[mcp_servers.vibe-typst]
command = "/app/backend/.venv/bin/python"
args = ["/app/backend/mcp_server.py"]

[mcp_servers.vibe-typst.env]
COMMENT_STORE_PATH = "/workspace/example/.slide-comments.db"
TCB_BACKEND_URL = "http://127.0.0.1:8080"
# TYPST-COMMENT-BRIDGE:END
"""

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            project = root / "workspace" / "project"
            outside = root / "workspace"
            project_codex = project / ".codex"
            project_codex.mkdir(parents=True)
            outside.mkdir(exist_ok=True)
            (project_codex / "config.toml").write_text(managed, encoding="utf-8")

            fake = root / "codex-real"
            fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake.chmod(0o755)
            env = {**os.environ, "HOME": str(home), "CODEX_REAL": str(fake)}

            subprocess.run(["bash", str(wrapper), "mcp", "list"], cwd=project, env=env, check=True)
            home_cfg = home / ".codex" / "config.toml"
            self.assertIn("mcp_servers.vibe-typst", home_cfg.read_text(encoding="utf-8"))

            subprocess.run(["bash", str(wrapper), "mcp", "list"], cwd=outside, env=env, check=True)
            self.assertNotIn("mcp_servers.vibe-typst", home_cfg.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
