"""Safe page-preview observation for active Typst and PDF projects."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import fitz

import runtime


MAX_PREVIEW_BYTES = 8 * 1024 * 1024
_PAGE_NAME = re.compile(r"page-([1-9][0-9]*)\.(svg|png)\Z")


def _regular_bytes(path: Path) -> bytes:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise PermissionError("rendered page may not be a symbolic link")
    if not stat.S_ISREG(info.st_mode):
        raise FileNotFoundError("rendered page is not a regular file")
    if info.st_size > MAX_PREVIEW_BYTES:
        raise ValueError("rendered page exceeds preview size limit")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise PermissionError("rendered page is not a regular file")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            data = stream.read(MAX_PREVIEW_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > MAX_PREVIEW_BYTES:
        raise ValueError("rendered page exceeds preview size limit")
    return data


def get_page_png(project: dict, page: int) -> dict:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer")
    project_type = project.get("type", "typst")
    if project_type not in {"typst", "pdf"}:
        raise ValueError("unsupported project type")
    root = Path(project.get("path", ""))
    if root.is_symlink():
        raise PermissionError("project path may not be a symbolic link")
    root = root.resolve(strict=True)
    main_name = project.get("main_file")
    if not isinstance(main_name, str) or not main_name:
        raise ValueError("project main file is invalid")
    main = (root / main_name).resolve(strict=True)
    try:
        main.relative_to(root)
    except ValueError as exc:
        raise PermissionError("project main file escapes its project") from exc

    render = Path(runtime.render_dir(main))
    if render.is_symlink() or not render.is_dir():
        raise FileNotFoundError("rendered preview is unavailable")
    suffix = "svg" if project_type == "typst" else "png"
    target = render / f"page-{page}.{suffix}"
    if target.is_symlink():
        raise PermissionError("rendered page may not be a symbolic link")
    available = {
        int(match.group(1))
        for candidate in render.iterdir()
        if (
            not candidate.is_symlink()
            and candidate.is_file()
            and (match := _PAGE_NAME.fullmatch(candidate.name)) is not None
            and match.group(2) == suffix
        )
    }
    if page not in available:
        raise ValueError(
            f"page must be within the rendered document ({len(available)} pages)"
        )
    source = _regular_bytes(target)
    if project_type == "pdf":
        png = source
    else:
        try:
            with fitz.open(stream=source, filetype="svg") as document:
                pixmap = document[0].get_pixmap(
                    matrix=fitz.Matrix(2, 2), alpha=False
                )
                png = pixmap.tobytes("png")
        except Exception as exc:
            raise ValueError("could not render SVG preview") from exc
    if len(png) > MAX_PREVIEW_BYTES:
        raise ValueError("PNG preview exceeds size limit")
    return {
        "page": page,
        "page_count": len(available),
        "media_type": "image/png",
        "data": png,
    }
