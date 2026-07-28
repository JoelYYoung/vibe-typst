import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import app


class _Request:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class ProjectContextVersionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.projects = {}
        for project_id in ("alpha", "beta"):
            project_dir = root / project_id
            project_dir.mkdir()
            main = project_dir / "main.typ"
            main.write_text(f"= {project_id.title()}\n", encoding="utf-8")
            self.projects[project_id] = {
                "id": project_id,
                "name": project_id.title(),
                "path": str(project_dir),
                "type": "typst",
                "main_file": "main.typ",
            }

        self.previous_file = app.runtime._state.get("file")
        self.previous_project = app._active_project
        self.previous_context = app._project_context_version
        app.runtime._state["file"] = None
        app._active_project = None
        app._project_context_version = "test-start-context"

    async def asyncTearDown(self):
        app.runtime._state["file"] = self.previous_file
        app._active_project = self.previous_project
        app._project_context_version = self.previous_context
        self._tmp.cleanup()

    @staticmethod
    def _set_runtime_file(path):
        app.runtime._state["file"] = str(Path(path).resolve())
        return app.runtime._state["file"]

    async def test_same_project_open_is_idempotent_and_switch_rotates_context(self):
        with (
            patch.object(
                app.projects_mod,
                "get_project",
                side_effect=lambda project_id: self.projects[project_id],
            ),
            patch.object(
                app.runtime, "set_file", side_effect=self._set_runtime_file
            ),
            patch.object(app.store, "set_path") as set_store_path,
            patch.object(app.runtime, "backup") as backup,
            patch.object(app.docstore, "start", new=AsyncMock()) as start,
            patch.object(
                app.docstore, "ensure_room", new=AsyncMock()
            ) as ensure_room,
            patch.object(app.resolver, "start") as resolver_start,
            patch.object(app.workdir, "setup", return_value={}),
            patch.object(app.vcs, "migrate"),
        ):
            first = await app.open_project("alpha")
            first_context = first["context_version"]

            start.reset_mock()
            ensure_room.reset_mock()
            resolver_start.reset_mock()
            set_store_path.reset_mock()
            backup.reset_mock()
            second = await app.open_project("alpha")

            self.assertEqual(second["context_version"], first_context)
            start.assert_not_awaited()
            ensure_room.assert_not_awaited()
            resolver_start.assert_not_called()
            set_store_path.assert_not_called()
            backup.assert_not_called()

            switched = await app.open_project("beta")

        self.assertNotEqual(switched["context_version"], first_context)
        self.assertEqual(switched["project"]["id"], "beta")
        self.assertEqual(app._active_context()["project_id"], "beta")

    async def test_close_rotates_context_and_clears_active_project(self):
        app._set_active_project(self.projects["alpha"])
        opened_context = app._active_context()["context_version"]

        with (
            patch.object(app.resolver, "stop"),
            patch.object(app.store, "close"),
        ):
            closed = app.close_project()

        self.assertTrue(closed["ok"])
        self.assertIsNone(closed["project_id"])
        self.assertNotEqual(closed["context_version"], opened_context)
        self.assertEqual(app._active_context(), {
            "project_id": None,
            "context_version": closed["context_version"],
        })

    async def test_active_rename_keeps_context_and_active_delete_rotates_it(self):
        app._set_active_project(self.projects["alpha"])
        original_context = app._active_context()["context_version"]
        renamed = {**self.projects["alpha"], "name": "Renamed"}

        with patch.object(
            app.projects_mod, "rename_project", return_value=renamed
        ):
            result = await app.rename_project("alpha", _Request({"name": "Renamed"}))

        self.assertEqual(result["name"], "Renamed")
        self.assertEqual(
            app._active_context()["context_version"], original_context
        )
        self.assertEqual(app._active_project["name"], "Renamed")

        with (
            patch.object(app.projects_mod, "delete_project"),
            patch.object(
                app.projects_mod,
                "_projects_root",
                return_value=Path(self._tmp.name),
            ),
            patch.object(
                app.runtime,
                "current_file",
                return_value=Path(self._tmp.name) / "unrelated.typ",
            ),
        ):
            result = app.delete_project("alpha")

        self.assertTrue(result["ok"])
        self.assertIsNone(app._active_context()["project_id"])
        self.assertNotEqual(
            app._active_context()["context_version"], original_context
        )

    async def test_typst_state_and_app_state_expose_the_same_context(self):
        app._set_active_project(self.projects["alpha"])
        context = app._active_context()
        main = Path(self.projects["alpha"]["path"]) / "main.typ"
        app.runtime._state["file"] = str(main)

        with (
            patch.object(app.runtime, "document_type", return_value="typst"),
            patch.object(app.runtime, "project_dir", return_value=main.parent),
            patch.object(app.runtime, "current_file", return_value=main),
            patch.object(app.runtime, "current_main", return_value="main.typ"),
            patch.object(
                app.runtime,
                "store_path",
                return_value=main.parent / ".slide-comments.db",
            ),
            patch.object(app.docstore, "room_name", return_value="room"),
            patch.object(app, "current_source", return_value="= Alpha"),
            patch.object(app.typst_service, "list_pages", return_value=[]),
            patch.object(app.typst_service, "page_tokens", return_value={}),
            patch.object(app.resolver, "status", return_value={"version": 0}),
            patch.object(app.workdir, "is_ready", return_value=True),
            patch.object(app.app_config, "is_configured", return_value=True),
        ):
            editor_state = app.state()
            shell_state = app.app_state()

        self.assertEqual(editor_state["project_id"], "alpha")
        self.assertEqual(editor_state["context_version"], context["context_version"])
        self.assertEqual(shell_state["project_id"], "alpha")
        self.assertEqual(shell_state["context_version"], context["context_version"])

    async def test_app_state_recovery_publishes_the_persisted_project(self):
        info = self.projects["alpha"]
        main = Path(info["path"]) / "main.typ"
        app.runtime._state["file"] = str(main)

        with (
            patch.object(app, "_has_valid_file", return_value=True),
            patch.object(
                app.app_config,
                "get_projects_root",
                return_value=Path(self._tmp.name),
            ),
            patch.object(app.app_config, "is_configured", return_value=True),
            patch.object(app.runtime, "current_file", return_value=main),
            patch.object(app.projects_mod, "get_project", return_value=info),
        ):
            recovered = app.app_state()

        self.assertEqual(recovered["active_project"]["id"], "alpha")
        self.assertEqual(recovered["project_id"], "alpha")
        self.assertEqual(app._active_project["id"], "alpha")
        self.assertEqual(
            recovered["context_version"],
            app._active_context()["context_version"],
        )


if __name__ == "__main__":
    unittest.main()
