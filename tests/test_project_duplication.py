import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


class ProjectDuplicationTest(unittest.TestCase):
    def setUp(self):
        import projects
        import vcs

        self.projects = projects
        self.vcs = vcs
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "projects"
        self.root.mkdir()
        self._projects_root = patch.object(
            projects, "_projects_root", return_value=self.root.resolve()
        )
        self._projects_root.start()

    def tearDown(self):
        self._projects_root.stop()
        self._tmp.cleanup()

    @staticmethod
    def _content_tree(project: Path) -> tuple[set[str], dict[str, bytes]]:
        directories = set()
        files = {}
        for path in sorted(project.rglob("*")):
            relative = path.relative_to(project)
            if (
                ".git" in relative.parts
                or relative.as_posix() == ".vibe-typst.json"
            ):
                continue
            if path.is_dir():
                directories.add(relative.as_posix())
            elif path.is_file():
                files[relative.as_posix()] = path.read_bytes()
        return directories, files

    def test_copy_preserves_complete_working_tree_but_starts_fresh_history(self):
        source_info = self.projects.create_project("Original deck")
        source = Path(source_info["path"])
        (source / "assets" / "figures").mkdir(parents=True)
        (source / "assets" / "figures" / "plot.png").write_bytes(b"png-data")
        (source / "notes").mkdir()
        (source / "notes" / "talk.md").write_text("speaker notes\n", encoding="utf-8")
        (source / "data.csv").write_text("x,y\n1,2\n", encoding="utf-8")
        (source / "empty-folder").mkdir()
        (source / ".gitignore").write_text("scratch/\n", encoding="utf-8")

        first = self.vcs.save_version(source, "first version")
        self.assertTrue(first["ok"], first)
        (source / "main.typ").write_text("= Saved revision\n", encoding="utf-8")
        second = self.vcs.save_version(source, "second version")
        self.assertTrue(second["ok"], second)
        (source / "main.typ").write_text(
            "= Current unsaved deck\n", encoding="utf-8"
        )
        (source / "assets" / "latest.bin").write_bytes(
            b"not in a saved version"
        )

        # Nested repositories are history too, but their surrounding working files are content.
        (source / "vendor" / ".git").mkdir(parents=True)
        (source / "vendor" / ".git" / "config").write_text("history", encoding="utf-8")
        (source / "vendor" / "theme.txt").write_text("theme", encoding="utf-8")

        expected_directories, expected_files = self._content_tree(source)
        duplicate_info = self.projects.copy_project(source_info["id"], "Deck copy")
        duplicate = Path(duplicate_info["path"])

        self.assertNotEqual(duplicate_info["id"], source_info["id"])
        self.assertEqual(duplicate_info["name"], "Deck copy")
        self.assertEqual(
            self._content_tree(duplicate),
            (expected_directories, expected_files),
        )
        self.assertFalse((duplicate / ".git").exists())
        self.assertFalse((duplicate / "vendor" / ".git").exists())
        self.assertEqual(self.vcs.list_versions(duplicate), [])
        self.assertEqual(
            self.vcs.status(duplicate),
            {"initialized": False, "dirty": True, "current": None},
        )
        self.assertEqual(
            [item["tag"] for item in self.vcs.list_versions(source)],
            ["v2", "v1"],
        )

        source_meta = json.loads(
            (source / ".vibe-typst.json").read_text(encoding="utf-8")
        )
        duplicate_meta = json.loads(
            (duplicate / ".vibe-typst.json").read_text(encoding="utf-8")
        )
        self.assertEqual(source_meta["name"], "Original deck")
        self.assertEqual(duplicate_meta["name"], "Deck copy")
        self.assertNotEqual(duplicate_meta["created"], source_meta["created"])

    def test_failed_copy_does_not_publish_a_partial_project(self):
        source_info = self.projects.create_project("Original deck")
        original_ids = {path.name for path in self.root.iterdir()}

        def fail_after_creating_staging(_src, dst, **_kwargs):
            Path(dst).mkdir()
            (Path(dst) / "partial.txt").write_text("partial", encoding="utf-8")
            raise OSError("copy failed")

        with patch.object(
            self.projects.shutil,
            "copytree",
            side_effect=fail_after_creating_staging,
        ):
            with self.assertRaisesRegex(OSError, "copy failed"):
                self.projects.copy_project(source_info["id"], "Broken copy")

        self.assertEqual({path.name for path in self.root.iterdir()}, original_ids)


if __name__ == "__main__":
    unittest.main()
