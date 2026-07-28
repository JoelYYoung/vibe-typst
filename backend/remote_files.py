"""Safe ordinary-file operations for the active project.

This module deliberately excludes the active Typst source and PDF-owned state.
Those documents have dedicated CRDT/PDF services with stronger invariants.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath

import projects


MAX_TEXT_FILE_BYTES = 4 * 1024 * 1024
MAX_TEXT_RESPONSE_BYTES = 256 * 1024
MAX_TEXT_LINES = 400
_COPY_CHUNK_BYTES = 1024 * 1024


class RevisionConflict(Exception):
    def __init__(self, current_sha256: str):
        super().__init__("file revision does not match expected_sha256")
        self.current_sha256 = current_sha256


def _relative_parts(rel_path: str) -> tuple[str, ...]:
    if not isinstance(rel_path, str) or not rel_path or "\x00" in rel_path:
        raise ValueError("a non-empty relative path is required")
    path = PurePosixPath(rel_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PermissionError("path must remain inside the project")
    if any(part.startswith(".") for part in path.parts):
        raise PermissionError("hidden/private paths are not available")
    return path.parts


def _project_root(project: dict) -> Path:
    raw_path = project.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("project path is invalid")
    lexical = Path(raw_path)
    if lexical.is_symlink():
        raise PermissionError("project path may not be a symbolic link")
    root = lexical.resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError("project path is not a directory")
    return root


def _target(project: dict, rel_path: str) -> tuple[Path, Path]:
    root = _project_root(project)
    parts = _relative_parts(rel_path)
    candidate = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise PermissionError("symbolic links are not available")
        if not current.exists():
            break
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise PermissionError("path escapes project directory") from exc
    return root, candidate


def _assert_regular(target: Path) -> os.stat_result:
    try:
        info = target.lstat()
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(info.st_mode):
        raise PermissionError("symbolic links are not available")
    if stat.S_ISDIR(info.st_mode):
        raise IsADirectoryError(str(target))
    if not stat.S_ISREG(info.st_mode):
        raise PermissionError("only regular files are available")
    return info


def _open_regular(target: Path):
    _assert_regular(target)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(target, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PermissionError("only regular files are available")
        return os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with _open_regular(Path(path)) as stream:
        while chunk := stream.read(_COPY_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _active_main(project: dict, root: Path) -> Path | None:
    if project.get("type", "typst") != "typst":
        return None
    main_file = project.get("main_file")
    if not isinstance(main_file, str) or not main_file:
        raise ValueError("project main file is invalid")
    try:
        return root.joinpath(*_relative_parts(main_file))
    except PermissionError as exc:
        raise ValueError("project main file is invalid") from exc


def _protect_mutation(
    project: dict,
    root: Path,
    target: Path,
    operation: str,
    *,
    include_descendants: bool = True,
) -> None:
    active_main = _active_main(project, root)
    if active_main is not None and (
        target == active_main
        or (
            include_descendants
            and target.is_dir()
            and active_main.is_relative_to(target)
        )
    ):
        raise ValueError(f"cannot {operation} the active Typst main document")
    projects.reject_pdf_managed_mutation(
        root,
        target,
        operation,
        include_descendants=include_descendants,
    )


def _ensure_safe_parents(root: Path, target: Path) -> None:
    relative = target.parent.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise PermissionError("symbolic links are not available")
        if current.exists() and not current.is_dir():
            raise NotADirectoryError(str(current))
        if not current.exists():
            current.mkdir()


def _metadata(target: Path, root: Path) -> dict:
    info = _assert_regular(target)
    return {
        "path": target.relative_to(root).as_posix(),
        "size": info.st_size,
        "sha256": sha256_file(target),
    }


def read_text(
    project: dict,
    rel_path: str,
    offset: int = 1,
    limit: int = 120,
) -> dict:
    root, target = _target(project, rel_path)
    info = _assert_regular(target)
    digest = sha256_file(target)
    base = {
        "path": target.relative_to(root).as_posix(),
        "sha256": digest,
        "size": info.st_size,
    }
    if info.st_size > MAX_TEXT_FILE_BYTES:
        return {**base, "download_required": True}
    with _open_regular(target) as stream:
        raw = stream.read(MAX_TEXT_FILE_BYTES + 1)
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {**base, "download_required": True}

    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 1:
        raise ValueError("offset must be a positive line number")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be positive")
    limit = min(limit, MAX_TEXT_LINES)
    lines = content.splitlines()
    start_index = min(offset - 1, len(lines))
    selected: list[str] = []
    used = 0
    for index in range(start_index, min(start_index + limit, len(lines))):
        numbered = f"{index + 1}: {lines[index]}"
        encoded = numbered.encode("utf-8")
        separator_size = 1 if selected else 0
        if used + separator_size + len(encoded) > MAX_TEXT_RESPONSE_BYTES:
            if not selected:
                room = MAX_TEXT_RESPONSE_BYTES - len(
                    f"{index + 1}: ".encode("utf-8")
                )
                shortened = lines[index].encode("utf-8")[:max(room, 0)]
                while True:
                    try:
                        selected.append(
                            f"{index + 1}: {shortened.decode('utf-8')}"
                        )
                        break
                    except UnicodeDecodeError:
                        shortened = shortened[:-1]
            break
        selected.append(numbered)
        used += separator_size + len(encoded)

    shown_count = len(selected)
    end = start_index + shown_count
    truncated = end < len(lines)
    shown = (
        f"{start_index + 1}-{end}"
        if shown_count
        else f"{offset}-{offset - 1}"
    )
    return {
        **base,
        "total_lines": len(lines),
        "shown": shown,
        "text": "\n".join(selected),
        "truncated": truncated,
        "next": end + 1 if truncated else None,
        "download_required": False,
    }


def _atomic_replace_bytes(
    target: Path,
    content: bytes,
    expected_sha256: str,
) -> None:
    original = _assert_regular(target)
    current_hash = sha256_file(target)
    if current_hash != expected_sha256:
        raise RevisionConflict(current_hash)
    fd, raw_temp = tempfile.mkstemp(prefix=".remote-write-", dir=target.parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        latest = _assert_regular(target)
        if (
            latest.st_dev != original.st_dev
            or latest.st_ino != original.st_ino
            or sha256_file(target) != expected_sha256
        ):
            raise RevisionConflict(sha256_file(target))
        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)


def write_text(
    project: dict,
    rel_path: str,
    content: str,
    expected_sha256: str,
) -> dict:
    if not isinstance(content, str):
        raise ValueError("content must be text")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError("expected_sha256 is required")
    root, target = _target(project, rel_path)
    _protect_mutation(project, root, target, "write")
    _atomic_replace_bytes(target, content.encode("utf-8"), expected_sha256)
    return _metadata(target, root)


def create_directory(project: dict, rel_path: str) -> dict:
    root, target = _target(project, rel_path)
    _protect_mutation(project, root, target, "create")
    if target.exists():
        raise FileExistsError(rel_path)
    _ensure_safe_parents(root, target)
    target.mkdir()
    return {
        "path": target.relative_to(root).as_posix(),
        "name": target.name,
        "type": "dir",
    }


def _assert_safe_tree(target: Path) -> None:
    if target.is_symlink():
        raise PermissionError("symbolic links are not available")
    if target.is_dir():
        for child in target.rglob("*"):
            if child.is_symlink() or child.name.startswith("."):
                raise PermissionError(
                    "hidden/private paths and symbolic links are not available"
                )


def move_item(project: dict, old_rel: str, dest_rel: str) -> dict:
    root, source = _target(project, old_rel)
    destination_root, destination = _target(project, dest_rel)
    if destination_root != root:
        raise PermissionError("destination project does not match")
    if not source.exists():
        raise FileNotFoundError(old_rel)
    _assert_safe_tree(source)
    _protect_mutation(project, root, source, "move")
    _protect_mutation(
        project, root, destination, "move into", include_descendants=False
    )
    if project.get("type") == "pdf":
        projects._reject_pdf_tree_move(root, source)
        projects._reject_pdf_addition(root, destination.name)
    if destination.exists():
        raise FileExistsError(dest_rel)
    if source.is_dir() and destination.is_relative_to(source):
        raise ValueError("cannot move a folder into itself")
    _ensure_safe_parents(root, destination)
    os.replace(source, destination)
    result = {
        "path": destination.relative_to(root).as_posix(),
        "name": destination.name,
        "type": "dir" if destination.is_dir() else "file",
    }
    if destination.is_file():
        result.update(_metadata(destination, root))
    return result


def install_file(
    project: dict,
    staged_path: Path,
    dest_rel: str,
    overwrite: bool,
    expected_sha256: str | None,
) -> dict:
    root, destination = _target(project, dest_rel)
    _protect_mutation(
        project, root, destination, "install", include_descendants=False
    )
    projects._reject_pdf_addition(root, destination.name)
    staged_path = Path(staged_path)
    _assert_regular(staged_path)
    _ensure_safe_parents(root, destination)

    original: os.stat_result | None = None
    if destination.exists() or destination.is_symlink():
        if not overwrite:
            raise FileExistsError(dest_rel)
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            raise ValueError(
                "expected_sha256 is required when overwriting a file"
            )
        original = _assert_regular(destination)
        current_hash = sha256_file(destination)
        if current_hash != expected_sha256:
            raise RevisionConflict(current_hash)
    elif overwrite:
        raise FileNotFoundError(dest_rel)

    fd, raw_temp = tempfile.mkstemp(
        prefix=".remote-install-", dir=destination.parent
    )
    temp_path = Path(raw_temp)
    try:
        with _open_regular(staged_path) as source, os.fdopen(fd, "wb") as output:
            while chunk := source.read(_COPY_CHUNK_BYTES):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if original is not None:
            latest = _assert_regular(destination)
            latest_hash = sha256_file(destination)
            if (
                latest.st_dev != original.st_dev
                or latest.st_ino != original.st_ino
                or latest_hash != expected_sha256
            ):
                raise RevisionConflict(latest_hash)
        elif destination.exists() or destination.is_symlink():
            raise FileExistsError(dest_rel)
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)
    return {
        **_metadata(destination, root),
        "name": destination.name,
        "type": "file",
    }
