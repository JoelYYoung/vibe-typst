import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CONTROL_DIR = ROOT / "control"
sys.path.insert(0, str(CONTROL_DIR))

from public_origin import resolve_public_base_url


class PublicBaseUrlTest(unittest.TestCase):
    def test_start_script_does_not_shadow_the_data_file_fallback(self):
        script = (CONTROL_DIR / "start.sh").read_text(encoding="utf-8")

        self.assertNotIn(
            'PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-http://localhost:$PORT}"',
            script,
        )

    def test_data_file_supplies_public_origin_when_environment_is_absent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "public-base-url").write_text(
                "https://vibetypst.yjwspace.win/\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ):
                os.environ.pop("PUBLIC_BASE_URL", None)
                resolved = resolve_public_base_url(data_dir, 8090)

        self.assertEqual(resolved, "https://vibetypst.yjwspace.win")

    def test_environment_takes_precedence_over_data_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "public-base-url").write_text(
                "https://file.example\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"PUBLIC_BASE_URL": "https://environment.example/"},
            ):
                resolved = resolve_public_base_url(data_dir, 8090)

        self.assertEqual(resolved, "https://environment.example")

    def test_localhost_remains_the_default_without_other_configuration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ):
                os.environ.pop("PUBLIC_BASE_URL", None)
                resolved = resolve_public_base_url(Path(temp_dir), 8123)

        self.assertEqual(resolved, "http://localhost:8123")


if __name__ == "__main__":
    unittest.main()
