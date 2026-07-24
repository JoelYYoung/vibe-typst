"""PDF validation and rendering helpers."""
import shutil
import tempfile
import uuid
from pathlib import Path

import fitz


def inspect_pdf(path: Path) -> dict:
    """Return PDF metadata after verifying that ``path`` is a non-empty PDF document."""
    try:
        with fitz.open(path) as doc:
            if not doc.is_pdf or doc.page_count < 1:
                raise ValueError("PDF must contain at least one page")
            return {"page_count": doc.page_count, "metadata": dict(doc.metadata or {})}
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"invalid PDF: {exc}") from exc


def render_pdf(path: Path, destination: Path) -> dict:
    """Render each page of a validated PDF as PNGs, replacing ``destination`` atomically."""
    info = inspect_pdf(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not destination.is_dir():
        raise ValueError("PDF render destination must be a directory")

    staging_dir = Path(tempfile.mkdtemp(prefix=".pdf-render-", dir=destination.parent))
    backup_dir: Path | None = None
    installed_destination = False
    pages = []
    try:
        with fitz.open(path) as doc:
            for number, page in enumerate(doc, start=1):
                name = f"page-{number}.png"
                page.get_pixmap().save(staging_dir / name)
                pages.append(name)
        if destination.exists():
            backup_dir = destination.with_name(f".pdf-render-backup-{uuid.uuid4().hex}")
            while backup_dir.exists():
                backup_dir = destination.with_name(f".pdf-render-backup-{uuid.uuid4().hex}")
            destination.rename(backup_dir)
        staging_dir.rename(destination)
        installed_destination = True
        staging_dir = None
        if backup_dir is not None:
            shutil.rmtree(backup_dir)
            backup_dir = None
    except Exception as exc:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)
        if backup_dir is not None:
            if installed_destination:
                failed_destination = destination.with_name(
                    f".pdf-render-failed-{uuid.uuid4().hex}"
                )
                while failed_destination.exists():
                    failed_destination = destination.with_name(
                        f".pdf-render-failed-{uuid.uuid4().hex}"
                    )
                destination.rename(failed_destination)
                backup_dir.rename(destination)
                backup_dir = None
                shutil.rmtree(failed_destination, ignore_errors=True)
            elif not destination.exists():
                backup_dir.rename(destination)
                backup_dir = None
        raise ValueError(f"could not render PDF: {exc}") from exc
    return {**info, "pages": pages}
