import importlib.util
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx


ROOT = Path(__file__).resolve().parents[1]
CONTROL_DIR = ROOT / "control"
sys.path.insert(0, str(CONTROL_DIR))


class ProjectWorkspaceControlTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        spec = importlib.util.spec_from_file_location(
            "control_main_project_workspace_test", CONTROL_DIR / "main.py"
        )
        self.control = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.control)
        root = Path(self._tmp.name)
        self.control.DATA_DIR = root / "control"
        self.control.DB_PATH = self.control.DATA_DIR / "control.db"
        self.control.WORKSPACE_BASE = root / "workspaces"
        self.control.init_db()
        self.user = self.control._create_user("alice", "correct-horse")
        self.other = self.control._create_user("bob", "battery-staple")
        self.project_ids = ("0123456789ab", "abcdef012345")
        for project_id in self.project_ids:
            project = self.control.WORKSPACE_BASE / "alice" / project_id
            project.mkdir(parents=True)
            (project / ".vibe-typst.json").write_text(
                '{"name":"Deck","type":"typst","main_file":"main.typ"}',
                encoding="utf-8",
            )

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_each_project_gets_one_stable_isolated_port_and_state_file(self):
        first = self.control._project_workspace_for(self.user, self.project_ids[0])
        same = self.control._project_workspace_for(self.user, self.project_ids[0])
        second = self.control._project_workspace_for(self.user, self.project_ids[1])

        self.assertEqual(first["id"], same["id"])
        self.assertEqual(first["port"], same["port"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["port"], second["port"])
        later_user = self.control._create_user("carol", "one-more-password")
        self.assertGreater(later_user["port"], second["port"])
        self.assertIsNone(
            self.control._project_workspace_by_id(first["id"], self.other["id"])
        )

        calls = []

        def fake_container(*args):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "container-id\n", "")

        with (
            patch.object(self.control, "_project_workspace_running", return_value=False),
            patch.object(self.control, "_named_container_exists", return_value=False),
            patch.object(self.control, "_image_exists", return_value=True),
            patch.object(self.control, "_container", side_effect=fake_container),
        ):
            self.assertTrue(
                self.control._start_project_workspace(self.user, first)
            )

        run = next(args for args in calls if args[0] == "run")
        state = (
            f"TCB_STATE_PATH=/workspace/.tcb/project-workspaces/"
            f"{first['id']}/state.json"
        )
        self.assertIn(state, run)
        self.assertIn(f"127.0.0.1:{first['port']}:8080", run)
        self.assertNotIn("--restart", run)

    async def test_open_route_is_authenticated_owner_scoped_and_redirects(self):
        session = self.control._new_session(self.user["id"])
        transport = httpx.ASGITransport(app=self.control.app)
        with (
            patch.object(
                self.control,
                "_ensure_project_workspace",
                new=AsyncMock(return_value=None),
            ),
            patch.object(self.control, "_project_workspace_up", return_value=True),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                cookies={self.control.COOKIE: session},
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    "/project-workspaces/open",
                    params={"project_id": self.project_ids[0]},
                )

        self.assertEqual(response.status_code, 303, response.text)
        location = response.headers["location"]
        self.assertIn("workspace=", location)
        self.assertIn(f"openProject={self.project_ids[0]}", location)
        with sqlite3.connect(self.control.DB_PATH) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM project_workspaces").fetchone()[0],
                1,
            )

        anonymous = httpx.ASGITransport(app=self.control.app)
        async with httpx.AsyncClient(
            transport=anonymous, base_url="http://test"
        ) as client:
            denied = await client.get(
                "/project-workspaces/open",
                params={"project_id": self.project_ids[0]},
            )
        self.assertEqual(denied.status_code, 401)

    async def test_invalid_or_foreign_projects_are_rejected_before_container_start(self):
        with self.assertRaises(FileNotFoundError):
            self.control._project_workspace_for(self.user, "../../admin")
        with self.assertRaises(FileNotFoundError):
            self.control._project_workspace_for(self.other, self.project_ids[0])


if __name__ == "__main__":
    unittest.main()
