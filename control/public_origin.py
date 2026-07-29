import os
from pathlib import Path


def resolve_public_base_url(data_dir: Path, port: int) -> str:
    configured = os.environ.get("PUBLIC_BASE_URL")
    if configured is None:
        try:
            configured = (data_dir / "public-base-url").read_text(
                encoding="utf-8"
            )
        except FileNotFoundError:
            configured = f"http://localhost:{port}"
    return configured.strip().rstrip("/")
