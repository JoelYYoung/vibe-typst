"""Durable, per-page transcript storage for PDF projects."""

import fcntl
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path


_SIDECAR_NAME = "transcript.json"
_LOCK_NAME = ".pdf-transcript.lock"
_SCHEMA_VERSION = 1
_ORPHAN_KEY = re.compile(r"[1-9][0-9]*(?:#(?:[2-9]|[1-9][0-9]+))?")
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def _validate_context(pdf_name, page_count) -> None:
    if not isinstance(pdf_name, str) or pdf_name != "document.pdf":
        raise ValueError("pdf_name must be document.pdf")
    if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
        raise ValueError("page_count must be a positive integer")


def _validate_page(page, page_count, name="page") -> int:
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= page_count:
        raise ValueError(f"{name} must be a page within the document")
    return page


def _validate_record(value, location: str) -> None:
    if not isinstance(value, dict) or set(value) != {"text"} or not isinstance(value["text"], str):
        raise ValueError(f"invalid transcript entry at {location}")


def _validate_page_key(key) -> None:
    if not isinstance(key, str) or not key.isdecimal() or key != str(int(key)) or int(key) < 1:
        raise ValueError("page keys must be positive decimal strings")


def _validate_orphan_key(key) -> None:
    if not isinstance(key, str) or _ORPHAN_KEY.fullmatch(key) is None:
        raise ValueError("orphan keys must be canonical page identifiers")


def _validate_sidecar(data: object, pdf_name: str) -> dict:
    if not isinstance(data, dict) or set(data) != {"schema_version", "pdf", "pages", "orphans"}:
        raise ValueError("invalid transcript sidecar")
    if (isinstance(data["schema_version"], bool)
            or not isinstance(data["schema_version"], int)
            or data["schema_version"] != _SCHEMA_VERSION):
        raise ValueError("unsupported transcript sidecar version")
    if not isinstance(data["pdf"], str) or data["pdf"] != pdf_name:
        raise ValueError("transcript sidecar belongs to a different PDF")
    if not isinstance(data["pages"], dict) or not isinstance(data["orphans"], dict):
        raise ValueError("invalid transcript sidecar")
    for key, record in data["pages"].items():
        _validate_page_key(key)
        _validate_record(record, f"page {key}")
    for key, record in data["orphans"].items():
        _validate_orphan_key(key)
        _validate_record(record, f"orphan {key}")
    return data


def _new_sidecar(pdf_name: str) -> dict:
    return {"schema_version": _SCHEMA_VERSION, "pdf": pdf_name, "pages": {}, "orphans": {}}


def _sidecar_path(project_dir) -> Path:
    return Path(project_dir) / _SIDECAR_NAME


@contextmanager
def _transaction_lock(project_dir):
    """Lock order: project-write lock, then this transcript lock (both process-safe)."""
    from pdf_service import project_write_lock
    lock_path = Path(project_dir) / _LOCK_NAME
    lock_key = str(lock_path.resolve())
    with _PROCESS_LOCKS_GUARD:
        process_lock = _PROCESS_LOCKS.setdefault(lock_key, threading.RLock())
    with project_write_lock(project_dir):
        with process_lock:
            with lock_path.open("a+") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_sidecar(project_dir, pdf_name: str) -> tuple[Path, dict, bool]:
    path = _sidecar_path(project_dir)
    if not path.exists():
        return path, _new_sidecar(pdf_name), True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("corrupt transcript sidecar") from exc
    return path, _validate_sidecar(data, pdf_name), False


def _orphan_key(orphans: dict, page_key: str) -> str:
    if page_key not in orphans:
        return page_key
    suffix = 2
    while f"{page_key}#{suffix}" in orphans:
        suffix += 1
    return f"{page_key}#{suffix}"


def _store_orphan(orphans: dict, page_key: str, record: dict) -> None:
    orphans[_orphan_key(orphans, page_key)] = record


def _reconcile(data: dict, page_count: int) -> bool:
    pages = data["pages"]
    orphans = data["orphans"]
    changed = False
    for key in sorted(tuple(pages), key=int):
        if int(key) > page_count:
            _store_orphan(orphans, key, pages.pop(key))
            changed = True
    for page in range(1, page_count + 1):
        key = str(page)
        if key not in pages:
            pages[key] = {"text": ""}
            changed = True
    return changed


def _load_reconciled(project_dir, pdf_name: str, page_count: int) -> tuple[Path, dict, bool]:
    path, data, created = _read_sidecar(project_dir, pdf_name)
    return path, data, _reconcile(data, page_count) or created


def _atomic_write(path: Path, data: dict) -> None:
    fd = None
    temporary = None
    try:
        fd, raw_temporary = tempfile.mkstemp(prefix=".transcript-", suffix=".json", dir=path.parent)
        temporary = Path(raw_temporary)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = None
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load(project_dir, pdf_name, page_count) -> dict:
    """Return a reconciled transcript sidecar, atomically persisting any reconciliation."""
    _validate_context(pdf_name, page_count)
    with _transaction_lock(project_dir):
        path, data, changed = _load_reconciled(project_dir, pdf_name, page_count)
        if changed:
            _atomic_write(path, data)
        return data


def _validate_updates(updates, page_count: int) -> list[tuple[int, str]]:
    if not isinstance(updates, list):
        raise ValueError("updates must be a list")
    validated = []
    for update in updates:
        if not isinstance(update, dict) or set(update) != {"page", "text"}:
            raise ValueError("each update must contain only page and text")
        page = _validate_page(update["page"], page_count)
        if not isinstance(update["text"], str):
            raise ValueError("transcript text must be a string")
        validated.append((page, update["text"]))
    return validated


def _set_pages_locked(project_dir, pdf_name, page_count, validated) -> dict:
    path, data, changed = _load_reconciled(project_dir, pdf_name, page_count)
    for page, text in validated:
        record = data["pages"][str(page)]
        if record["text"] != text:
            record["text"] = text
            changed = True
    if changed:
        _atomic_write(path, data)
    return data


def set_pages(project_dir, pdf_name, page_count, updates) -> dict:
    """Replace multiple page transcripts, validating the entire batch before writing."""
    _validate_context(pdf_name, page_count)
    validated = _validate_updates(updates, page_count)
    with _transaction_lock(project_dir):
        return _set_pages_locked(project_dir, pdf_name, page_count, validated)


def set_page(project_dir, pdf_name, page_count, page, text) -> dict:
    """Replace one page transcript."""
    _validate_context(pdf_name, page_count)
    _validate_page(page, page_count)
    if not isinstance(text, str):
        raise ValueError("transcript text must be a string")
    with _transaction_lock(project_dir):
        return _set_pages_locked(project_dir, pdf_name, page_count, [(page, text)])


def _orphan_selector(orphan_page) -> str:
    if isinstance(orphan_page, bool):
        raise ValueError("orphan_page must not be a boolean")
    if isinstance(orphan_page, int):
        if orphan_page < 1:
            raise ValueError("orphan_page must be positive")
        return str(orphan_page)
    if isinstance(orphan_page, str):
        _validate_orphan_key(orphan_page)
        return orphan_page
    raise ValueError("orphan_page must identify an orphan")


def restore_orphan(project_dir, pdf_name, page_count, orphan_page, target_page) -> dict:
    """Move an orphan record back into the active page range without losing target text."""
    _validate_context(pdf_name, page_count)
    orphan_key = _orphan_selector(orphan_page)
    target_page = _validate_page(target_page, page_count, "target_page")
    with _transaction_lock(project_dir):
        path, data, _ = _load_reconciled(project_dir, pdf_name, page_count)
        try:
            orphan = data["orphans"].pop(orphan_key)
        except KeyError as exc:
            raise ValueError("orphan transcript not found") from exc
        target_key = str(target_page)
        target = data["pages"][target_key]
        if target["text"]:
            _store_orphan(data["orphans"], target_key, target)
        data["pages"][target_key] = orphan
        _atomic_write(path, data)
        return data
