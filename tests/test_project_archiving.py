import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import app
import projects
import vcs


class ProjectArchivingTest(unittest.TestCase):
    def setUp(self):
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
    def _content_snapshot(project_dir: Path) -> dict[str, bytes]:
        return {
            path.relative_to(project_dir).as_posix(): path.read_bytes()
            for path in project_dir.rglob("*")
            if path.is_file()
            and ".git" not in path.relative_to(project_dir).parts
            and path.name != ".vibe-typst.json"
        }

    def test_archive_and_restore_preserve_content_and_version_history(self):
        info = projects.create_project("Quarterly deck")
        project_dir = Path(info["path"])
        (project_dir / "assets").mkdir()
        (project_dir / "assets" / "chart.svg").write_text(
            "<svg />", encoding="utf-8"
        )
        saved = vcs.save_version(project_dir, "first version")
        self.assertTrue(saved["ok"], saved)
        (project_dir / "main.typ").write_text(
            "= Unsaved current work\n", encoding="utf-8"
        )
        expected_content = self._content_snapshot(project_dir)
        expected_versions = [
            (version["tag"], version["commit"], version["message"])
            for version in vcs.list_versions(project_dir)
        ]

        archived = projects.set_project_archived(info["id"], True)

        self.assertTrue(archived["archived"])
        self.assertIsNotNone(archived["archived_at"])
        self.assertEqual(projects.list_projects(), [])
        self.assertEqual(
            [project["id"] for project in projects.list_projects(archived=True)],
            [info["id"]],
        )
        self.assertEqual(self._content_snapshot(project_dir), expected_content)
        self.assertEqual(
            [
                (version["tag"], version["commit"], version["message"])
                for version in vcs.list_versions(project_dir)
            ],
            expected_versions,
        )
        self.assertTrue(vcs.status(project_dir)["dirty"])

        archived_again = projects.set_project_archived(info["id"], True)
        self.assertEqual(archived_again["archived_at"], archived["archived_at"])

        restored = projects.set_project_archived(info["id"], False)

        self.assertFalse(restored["archived"])
        self.assertIsNone(restored["archived_at"])
        self.assertEqual([project["id"] for project in projects.list_projects()], [info["id"]])
        self.assertEqual(projects.list_projects(archived=True), [])
        self.assertEqual(self._content_snapshot(project_dir), expected_content)
        self.assertEqual(
            [
                (version["tag"], version["commit"], version["message"])
                for version in vcs.list_versions(project_dir)
            ],
            expected_versions,
        )

    def test_legacy_project_is_active_and_an_archived_duplicate_is_active(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        (legacy / "main.typ").write_text("= Legacy\n", encoding="utf-8")
        (legacy / ".vibe-typst.json").write_text(
            json.dumps({"name": "Legacy", "main_file": "main.typ"}),
            encoding="utf-8",
        )

        legacy_info = projects.get_project("legacy")
        self.assertFalse(legacy_info["archived"])
        projects.set_project_archived("legacy", True)
        duplicate = projects.copy_project("legacy", "Legacy copy")

        self.assertFalse(duplicate["archived"])
        self.assertIsNone(duplicate["archived_at"])
        self.assertEqual(
            [project["id"] for project in projects.list_projects()],
            [duplicate["id"]],
        )


class ProjectArchivingApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "projects"
        self.root.mkdir()
        self._projects_root = patch.object(
            projects, "_projects_root", return_value=self.root.resolve()
        )
        self._configured = patch.object(app.app_config, "is_configured", return_value=True)
        self._projects_root.start()
        self._configured.start()
        self.info = projects.create_project("API deck")
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app.app), base_url="http://test"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self._configured.stop()
        self._projects_root.stop()
        self._tmp.cleanup()

    async def test_archive_list_open_guard_and_restore(self):
        active = await self.client.get("/api/projects")
        self.assertEqual(active.status_code, 200)
        self.assertEqual([p["id"] for p in active.json()["projects"]], [self.info["id"]])

        archived = await self.client.post(f"/api/projects/{self.info['id']}/archive")
        self.assertEqual(archived.status_code, 200)
        self.assertTrue(archived.json()["archived"])
        self.assertEqual((await self.client.get("/api/projects")).json()["projects"], [])
        archived_list = await self.client.get("/api/projects?archived=true")
        self.assertEqual(
            [p["id"] for p in archived_list.json()["projects"]], [self.info["id"]]
        )

        blocked = await self.client.post(f"/api/projects/{self.info['id']}/open")
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("restore", blocked.json()["detail"].lower())

        restored = await self.client.post(f"/api/projects/{self.info['id']}/restore")
        self.assertEqual(restored.status_code, 200)
        self.assertFalse(restored.json()["archived"])
        self.assertEqual(
            [p["id"] for p in (await self.client.get("/api/projects")).json()["projects"]],
            [self.info["id"]],
        )


if __name__ == "__main__":
    unittest.main()
