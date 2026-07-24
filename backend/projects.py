"""
Project management: CRUD operations on the projects root directory.

A project is a subdirectory of the projects root containing:
  .vibe-typst.json  — metadata (name, created, type, main_file)
  main.typ or document.pdf — its immutable primary document

The directory name is a short UUID hex string, decoupled from the display name.
Renaming a project only updates .vibe-typst.json — the directory path never changes.
"""
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import app_config
from pdf_service import inspect_pdf

_META_FILE = ".vibe-typst.json"
MAX_PDF_UPLOAD_BYTES = 100 * 1024 * 1024
_PDF_COPY_CHUNK_BYTES = 1024 * 1024

_STARTER_TYPST = """\
// A touying deck. Speaker transcripts live inline as #speaker-note("...") inside each
// slide, so they travel with the slide and are versioned with the source.
#import "@preview/touying:0.6.1": *
#import themes.simple: *

#show: simple-theme.with(aspect-ratio: "16-9", header: none)
#set text(size: 24pt)

#centered-slide[
  #speaker-note("Welcome. This is the speaker transcript for the title slide — edit it here, in the presenter view, or ask Claude. It is saved inline in this deck.")
  #text(size: 36pt, weight: "bold")[{title}]
  #v(0.6em)
  #text(size: 20pt, fill: gray)[Edit this deck in Vibe Typst]
]

#slide[
  #speaker-note("Transcript for the getting-started slide.")
  = Getting started

  - Edit source on the left; preview updates live on the right.
  - Click any element in the preview to jump to its source.
  - Run `claude` in the terminal for AI-assisted editing.
]
"""


# ── helpers ─────────────────────────────────────────────────────────────────

def _projects_root() -> Path:
    root = app_config.get_projects_root()
    if root is None:
        raise RuntimeError("Projects root not configured")
    return root


def _safe_name(name: str) -> str:
    """Strip characters unsafe for directory names (cross-platform)."""
    name = name.strip()
    name = re.sub(r'[\\/:*?"<>|]', "", name)
    name = re.sub(r"\s+", " ", name)
    return name[:128]


def _read_meta(project_dir: Path) -> dict:
    meta_path = project_dir / _META_FILE
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {"name": project_dir.name, "main_file": "main.typ"}


def _write_meta(project_dir: Path, meta: dict) -> None:
    (project_dir / _META_FILE).write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _project_info(project_dir: Path) -> dict:
    meta = _read_meta(project_dir)
    main_file = meta.get("main_file", "main.typ")
    if not (project_dir / main_file).exists():
        typs = sorted(project_dir.glob("*.typ"))
        main_file = typs[0].name if typs else "main.typ"
    return {
        "id": project_dir.name,
        "name": meta.get("name", project_dir.name),
        "created": meta.get("created"),
        "type": meta.get("type", "typst"),
        "main_file": main_file,
        "original_filename": meta.get("original_filename"),
        "path": str(project_dir),
    }


# ── public API ───────────────────────────────────────────────────────────────

def list_projects() -> list[dict]:
    root = _projects_root()
    root.mkdir(parents=True, exist_ok=True)
    _sweep_trash(root)  # clean up any leftover .trash-* dirs whose handles have closed
    projects = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            projects.append(_project_info(d))
    return projects


def get_project(project_id: str) -> dict:
    root = _projects_root()
    d = (root / project_id).resolve()
    if not d.is_dir() or d.parent != root:
        raise FileNotFoundError(f"Project not found: {project_id!r}")
    return _project_info(d)


def create_project(name: str) -> dict:
    name = _safe_name(name)
    if not name:
        raise ValueError("Project name cannot be empty")
    root = _projects_root()
    root.mkdir(parents=True, exist_ok=True)
    # Directory name is a 12-char UUID hex — decoupled from the display name.
    # Renaming the project only updates metadata; the directory path never changes.
    dir_id = uuid.uuid4().hex[:12]
    project_dir = root / dir_id
    while project_dir.exists():
        dir_id = uuid.uuid4().hex[:12]
        project_dir = root / dir_id
    project_dir.mkdir()
    meta = {
        "name": name,
        "created": datetime.now(timezone.utc).isoformat(),
        "main_file": "main.typ",
    }
    _write_meta(project_dir, meta)
    starter = _STARTER_TYPST.replace("{title}", name)
    (project_dir / "main.typ").write_text(starter, encoding="utf-8")
    return _project_info(project_dir)


def create_pdf_project_from_file(
    name: str, filename: str, source: Path, *, max_bytes: int = MAX_PDF_UPLOAD_BYTES,
) -> dict:
    """Create a PDF project from a staged file without loading it into memory.

    The source is copied in bounded chunks beside the projects root, parsed before a project
    becomes visible, then atomically installed under its stable internal name,
    ``document.pdf``.  The caller retains ownership of ``source``.
    """
    name = _safe_name(name)
    if not name:
        raise ValueError("Project name cannot be empty")
    if max_bytes < 1:
        raise ValueError("PDF upload size limit must be positive")

    root = _projects_root()
    root.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    staging_dir: Path | None = None
    published_dir: Path | None = None
    try:
        fd, raw_temp_path = tempfile.mkstemp(prefix=".pdf-upload-", suffix=".pdf", dir=root)
        temp_path = Path(raw_temp_path)
        total = 0
        with Path(source).open("rb") as input_stream, os.fdopen(fd, "wb") as stream:
            while chunk := input_stream.read(_PDF_COPY_CHUNK_BYTES):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"PDF upload exceeds {max_bytes} byte limit")
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        inspect_pdf(temp_path)

        dir_id = uuid.uuid4().hex[:12]
        project_dir = root / dir_id
        while project_dir.exists():
            dir_id = uuid.uuid4().hex[:12]
            project_dir = root / dir_id
        staging_dir = Path(tempfile.mkdtemp(prefix=".pdf-project-", dir=root))

        os.replace(temp_path, staging_dir / "document.pdf")
        temp_path = None
        _write_meta(staging_dir, {
            "name": name,
            "created": datetime.now(timezone.utc).isoformat(),
            "type": "pdf",
            "main_file": "document.pdf",
            "original_filename": filename,
        })
        staging_dir.rename(project_dir)
        published_dir = project_dir
        staging_dir = None
        return _project_info(project_dir)
    except Exception:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)
        if published_dir is not None:
            shutil.rmtree(published_dir, ignore_errors=True)
        raise
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def create_pdf_project(name: str, filename: str, content: bytes) -> dict:
    """Backwards-compatible bytes API for PDF project creation."""
    if len(content) > MAX_PDF_UPLOAD_BYTES:
        raise ValueError(f"PDF upload exceeds {MAX_PDF_UPLOAD_BYTES} byte limit")
    fd, raw_staging_path = tempfile.mkstemp(prefix=".pdf-bytes-upload-", suffix=".pdf")
    staging_path = Path(raw_staging_path)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return create_pdf_project_from_file(name, filename, staging_path)
    finally:
        staging_path.unlink(missing_ok=True)


def rename_project(project_id: str, new_name: str) -> dict:
    new_name = _safe_name(new_name)
    if not new_name:
        raise ValueError("Project name cannot be empty")
    root = _projects_root()
    project_dir = (root / project_id).resolve()
    if not project_dir.is_dir() or project_dir.parent != root:
        raise FileNotFoundError(f"Project not found: {project_id!r}")
    # Only update the metadata name — directory stays fixed forever.
    meta = _read_meta(project_dir)
    meta["name"] = new_name
    _write_meta(project_dir, meta)
    return _project_info(project_dir)


def _sweep_trash(root: Path) -> None:
    """Best-effort removal of leftover `.trash-*` dirs (e.g. ones that still held NFS .nfs*
    files at delete time; the handles have since closed)."""
    try:
        for t in root.glob(".trash-*"):
            shutil.rmtree(t, ignore_errors=True)
    except Exception:
        pass


def delete_project(project_id: str) -> None:
    root = _projects_root()
    d = (root / project_id).resolve()
    if not d.is_dir() or d.parent != root:
        raise FileNotFoundError(f"Project not found: {project_id!r}")
    # Rename to a HIDDEN trash name first: this leaves the project list immediately (hidden
    # dirs aren't listed) and always succeeds even if a file is still open — unlike rmdir,
    # which fails when NFS .nfs* silly-renames keep the folder non-empty. Then remove it.
    trash = root / f".trash-{project_id}-{int(time.time())}"
    try:
        d.rename(trash)
    except OSError:
        trash = d  # rename failed → delete in place
    shutil.rmtree(trash, ignore_errors=True)
    _sweep_trash(root)  # mop up any earlier trash whose handles have since been released


def copy_project(project_id: str, new_name: str) -> dict:
    new_name = _safe_name(new_name)
    if not new_name:
        raise ValueError("Project name cannot be empty")
    root = _projects_root()
    src = (root / project_id).resolve()
    if not src.is_dir() or src.parent != root:
        raise FileNotFoundError(f"Project not found: {project_id!r}")
    # New copy also gets a UUID dir name.
    dir_id = uuid.uuid4().hex[:12]
    dst = root / dir_id
    while dst.exists():
        dir_id = uuid.uuid4().hex[:12]
        dst = root / dir_id
    shutil.copytree(src, dst)
    meta = _read_meta(dst)
    meta["name"] = new_name
    meta["created"] = datetime.now(timezone.utc).isoformat()
    _write_meta(dst, meta)
    return _project_info(dst)


# ── path safety ──────────────────────────────────────────────────────────────

def _resolve_project_path(project_dir: Path, rel_path: str) -> Path:
    """Resolve a relative path inside project_dir, rejecting traversal."""
    root = project_dir.resolve()
    target = (root / rel_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise PermissionError("Path escapes project directory")
    return target


def _pdf_primary(project_dir: Path) -> Path | None:
    """Return the immutable primary only for a real PDF project."""
    root = Path(project_dir).resolve()
    meta = _read_meta(root)
    if meta.get("type") != "pdf" or meta.get("main_file") != "document.pdf":
        return None
    return root / "document.pdf"


def _reject_pdf_primary_mutation(project_dir: Path, target: Path, operation: str) -> None:
    primary = _pdf_primary(project_dir)
    if primary is None:
        return
    target = Path(target)
    if target == primary or (target.is_dir() and primary.is_relative_to(target)):
        raise ValueError(f"cannot {operation} document.pdf in a PDF project")


def _reject_pdf_addition(project_dir: Path, name: str) -> None:
    if _pdf_primary(project_dir) is not None and Path(name).suffix.lower() == ".pdf":
        raise ValueError("PDF projects may not contain additional PDF files")


def _reject_pdf_private(project_dir: Path, path: Path | str) -> None:
    if _pdf_primary(project_dir) is None:
        return
    parts = Path(path).parts
    if any(part == ".pdf-replacement-journal.json" or part.startswith(".pdf-txn-")
           or part == ".pdf-project-write.lock" for part in parts):
        raise ValueError("PDF transaction paths are private")


def _reject_pdf_tree_move(project_dir: Path, target: Path) -> None:
    if _pdf_primary(project_dir) is None:
        return
    if target.suffix.lower() == ".pdf" or (target.is_dir() and any(
            child.suffix.lower() == ".pdf" for child in target.rglob("*"))):
        raise ValueError("PDF projects may not move PDF files")


# ── file / directory listing ─────────────────────────────────────────────────

def list_project_items(project_dir: Path) -> list[dict]:
    """List all files and directories (non-hidden) inside the project, recursively."""
    items = []
    for p in sorted(project_dir.rglob("*")):
        if p.name.startswith(".") or p.name.endswith(".backup"):
            continue
        rel = str(p.relative_to(project_dir))
        if p.is_dir():
            items.append({"path": rel, "name": p.name, "type": "dir"})
        elif p.is_file():
            items.append({
                "path": rel,
                "abs_path": str(p.resolve()),
                "name": p.name,
                "type": "file",
                "size": p.stat().st_size,
                "is_typ": p.suffix == ".typ",
            })
    return items


# ── file operations ───────────────────────────────────────────────────────────

def create_file(project_dir: Path, name: str) -> dict:
    """Create a new empty .typ file inside the project (path may include subdirs)."""
    name = re.sub(r'[\\:*?"<>|]', "", name.strip())
    _reject_pdf_private(project_dir, name)
    _reject_pdf_addition(project_dir, name)
    if not name.endswith(".typ"):
        name += ".typ"
    target = _resolve_project_path(project_dir, name)
    if target.exists():
        raise FileExistsError(f"{name!r} already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")
    rel = str(target.relative_to(project_dir))
    return {"path": rel, "name": target.name, "size": 0, "is_typ": True, "type": "file",
            "abs_path": str(target.resolve())}


def delete_file(project_dir: Path, rel_path: str) -> None:
    target = _resolve_project_path(project_dir, rel_path)
    _reject_pdf_private(project_dir, rel_path)
    _reject_pdf_primary_mutation(project_dir, target, "delete")
    if not target.is_file():
        raise FileNotFoundError(f"{rel_path!r} not found")
    target.unlink()


def mkdir(project_dir: Path, rel_path: str) -> dict:
    """Create a directory (including parents) inside the project."""
    rel_path = rel_path.strip("/")
    target = _resolve_project_path(project_dir, rel_path)
    _reject_pdf_private(project_dir, rel_path)
    if target.exists():
        raise FileExistsError(f"{rel_path!r} already exists")
    target.mkdir(parents=True)
    return {"path": rel_path, "name": target.name, "type": "dir"}


def rmdir(project_dir: Path, rel_path: str) -> None:
    """Delete a directory (recursively) inside the project."""
    target = _resolve_project_path(project_dir, rel_path)
    _reject_pdf_primary_mutation(project_dir, target, "delete")
    if not target.is_dir():
        raise FileNotFoundError(f"{rel_path!r} is not a directory")
    shutil.rmtree(target)


def _available_target(target: Path, is_dir: bool = False) -> tuple[Path, bool]:
    """Return a non-existing sibling path, preserving every collision as `name_1.ext`."""
    if not target.exists():
        return target, False
    stem = target.name if is_dir else target.stem
    suffix = "" if is_dir else target.suffix
    i = 1
    candidate = target.with_name(f"{stem}_{i}{suffix}")
    while candidate.exists():
        i += 1
        candidate = target.with_name(f"{stem}_{i}{suffix}")
    return candidate, True


def store_upload(project_dir: Path, filename: str, content: bytes,
                 dest_dir_rel: str = "") -> dict:
    """Store an uploaded file in `dest_dir_rel`, keeping both files on name collisions."""
    dest_dir_rel = (dest_dir_rel or "").strip().strip("/")
    dest_dir = project_dir.resolve() if not dest_dir_rel else _resolve_project_path(project_dir, dest_dir_rel)
    if not dest_dir.is_dir():
        raise ValueError("upload destination is not a folder")
    name = re.sub(r'[\\/:*?"<>|]', "_", (filename or "upload").strip())
    if name in {"", ".", ".."}:
        name = "upload"
    _reject_pdf_private(project_dir, name)
    _reject_pdf_addition(project_dir, name)
    target, collision_renamed = _available_target(dest_dir / name)
    target.write_bytes(content)
    rel = str(target.relative_to(project_dir.resolve()))
    return {"ok": True, "path": rel, "name": target.name, "size": len(content),
            "collision_renamed": collision_renamed}


def move_item(project_dir: Path, old_rel: str, dest_dir_rel: str) -> dict:
    """Move a file or directory into another directory within the project (drag-to-move).
    `dest_dir_rel` is the target directory relative to the project root ("" = root)."""
    old_target = _resolve_project_path(project_dir, old_rel)
    _reject_pdf_private(project_dir, old_rel)
    if not old_target.exists():
        raise FileNotFoundError(f"{old_rel!r} not found")
    _reject_pdf_primary_mutation(project_dir, old_target, "move")
    _reject_pdf_tree_move(project_dir, old_target)
    dest_dir = project_dir if not dest_dir_rel else _resolve_project_path(project_dir, dest_dir_rel)
    if not dest_dir.is_dir():
        raise ValueError("destination is not a folder")
    if old_target.is_dir() and (dest_dir == old_target or str(dest_dir).startswith(str(old_target) + "/")):
        raise ValueError("cannot move a folder into itself")
    new_target = dest_dir / old_target.name
    if new_target == old_target:
        return {"path": old_rel, "name": old_target.name}  # no-op (already there)
    new_target, collision_renamed = _available_target(new_target, is_dir=old_target.is_dir())
    old_target.rename(new_target)
    rel = str(new_target.relative_to(project_dir.resolve()))
    return {"path": rel, "name": new_target.name,
            "type": "dir" if new_target.is_dir() else "file",
            "collision_renamed": collision_renamed}


def rename_item(project_dir: Path, old_rel: str, new_name: str) -> dict:
    """Rename a file or directory (new_name is just the basename, same parent dir)."""
    old_target = _resolve_project_path(project_dir, old_rel)
    _reject_pdf_private(project_dir, old_rel)
    if not old_target.exists():
        raise FileNotFoundError(f"{old_rel!r} not found")
    _reject_pdf_primary_mutation(project_dir, old_target, "rename")
    new_name_clean = re.sub(r'[\\/:*?"<>|]', "", new_name.strip())
    _reject_pdf_private(project_dir, new_name_clean)
    if not new_name_clean:
        raise ValueError("Name cannot be empty")
    _reject_pdf_addition(project_dir, new_name_clean)
    new_target = old_target.parent / new_name_clean
    _resolve_project_path(project_dir, str(new_target.relative_to(project_dir)))
    if new_target.exists() and new_target != old_target:
        raise FileExistsError(f"{new_name_clean!r} already exists")
    old_target.rename(new_target)
    rel = str(new_target.relative_to(project_dir))
    result: dict = {"path": rel, "name": new_target.name}
    if new_target.is_dir():
        result["type"] = "dir"
    else:
        result["type"] = "file"
        result["is_typ"] = new_target.suffix == ".typ"
        result["abs_path"] = str(new_target.resolve())
    return result
