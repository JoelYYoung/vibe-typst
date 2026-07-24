"""PDF validation and rendering helpers."""
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
    """Render each page of a validated PDF as a PNG in ``destination``."""
    info = inspect_pdf(path)
    destination.mkdir(parents=True, exist_ok=True)
    pages = []
    try:
        with fitz.open(path) as doc:
            for number, page in enumerate(doc, start=1):
                name = f"page-{number}.png"
                page.get_pixmap().save(destination / name)
                pages.append(name)
    except Exception as exc:
        raise ValueError(f"could not render PDF: {exc}") from exc
    return {**info, "pages": pages}
