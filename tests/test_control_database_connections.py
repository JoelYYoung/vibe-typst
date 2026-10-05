import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL_DIR = ROOT / "control"
sys.path.insert(0, str(CONTROL_DIR))

import mcp_store
import pat_store


class DatabaseConnectionLifecycleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "control.db"
        spec = importlib.util.spec_from_file_location(
            "control_main_database_connection_test", CONTROL_DIR / "main.py"
        )
        self.control = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.control)
        self.control.DB_PATH = self.db_path

    def tearDown(self):
        self._tmp.cleanup()

    def test_database_contexts_commit_and_close_connections(self):
        factories = {
            "control": self.control._db,
            "personal access tokens": lambda: pat_store._connect(self.db_path),
            "remote MCP": lambda: mcp_store._connect(self.db_path),
        }

        for name, factory in factories.items():
            with self.subTest(name=name):
                with factory() as db:
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS connection_probe "
                        "(source TEXT PRIMARY KEY)"
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO connection_probe(source) VALUES (?)",
                        (name,),
                    )

                with self.assertRaises(sqlite3.ProgrammingError):
                    db.execute("SELECT 1")

                with sqlite3.connect(self.db_path) as check:
                    row = check.execute(
                        "SELECT source FROM connection_probe WHERE source=?", (name,)
                    ).fetchone()
                self.assertEqual(row, (name,))


if __name__ == "__main__":
    unittest.main()
