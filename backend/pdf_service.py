"""PDF validation, rendering, and atomic primary-document replacement helpers."""
import fcntl
import json
import os
import shutil
import stat
import tempfile
import threading
import uuid
import re
from contextlib import contextmanager
from pathlib import Path

import fitz


_REPLACE_LOCK = ".pdf-project-write.lock"
_JOURNAL = ".pdf-replacement-journal.json"
_TXID = re.compile(r"[0-9a-f]{32}")
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()
_LOCK_STATE = threading.local()


@contextmanager
def replacement_lock(project_dir: Path):
    """Outer project-write lock.  Lock order is this lock, then transcript's lock."""
    lock_path = Path(project_dir) / _REPLACE_LOCK
    key = str(lock_path.resolve())
    with _PROCESS_LOCKS_GUARD:
        process_lock = _PROCESS_LOCKS.setdefault(key, threading.RLock())
    with process_lock:
        depth = getattr(_LOCK_STATE, "depth", 0)
        if depth:
            _LOCK_STATE.depth = depth + 1
            try: yield
            finally: _LOCK_STATE.depth -= 1
            return
        with lock_path.open("a+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            _LOCK_STATE.depth = 1
            try:
                yield
            finally:
                _LOCK_STATE.depth = 0
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


project_write_lock = replacement_lock


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, data: dict) -> None:
    fd, raw = tempfile.mkstemp(prefix=".pdf-journal-", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = None
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _fsync_dir(path.parent)
    finally:
        if fd is not None:
            os.close(fd)
        temp.unlink(missing_ok=True)


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


def _lexical_path(root: Path, value: Path | str, label: str) -> Path:
    """Return an absolute lexical child of *root*, without resolving symlinks."""
    root = root.absolute()
    raw = Path(value)
    target = raw.absolute() if raw.is_absolute() else root / raw
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must be inside the project") from exc
    if any(part in {"", ".", ".."} for part in target.relative_to(root).parts):
        raise ValueError(f"{label} must be a lexical project path")
    return target


def _assert_no_symlink_path(root: Path, target: Path, label: str) -> None:
    """Reject a link at any path component we are about to trust."""
    current = root
    if current.is_symlink():
        raise ValueError("project directory must not be a symlink")
    for part in target.relative_to(root).parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode):
            raise ValueError(f"{label} must not be a symlink")


def _open_child(root: Path, target: Path) -> tuple[int, int, str]:
    """Open a project child while holding no-follow FDs for every directory component."""
    parts = target.relative_to(root).parts
    if not parts:
        raise ValueError("path must name a project child")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    parent_fd = root_fd
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                              dir_fd=parent_fd)
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = next_fd
        return root_fd, parent_fd, parts[-1]
    except Exception:
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)
        raise


def _copy_candidate_no_follow(root: Path, candidate: Path) -> tuple[Path, os.stat_result]:
    """Freeze the candidate inode into a durable same-directory temporary file."""
    _assert_no_symlink_path(root, candidate, "candidate")
    source_fd = temporary_fd = root_fd = parent_fd = None
    temporary: Path | None = None
    completed = False
    try:
        root_fd, parent_fd, name = _open_child(root, candidate)
        source_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size <= 0:
            raise ValueError("candidate must be a non-empty regular PDF file")
        temporary_fd, raw_temporary = tempfile.mkstemp(
            prefix=".pdf-replacement-", suffix=".pdf", dir=root
        )
        temporary = Path(raw_temporary)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(temporary_fd, view)
                view = view[written:]
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        completed = True
        return temporary, source_stat
    except OSError as exc:
        raise ValueError(f"could not safely read candidate: {exc}") from exc
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if parent_fd is not None and parent_fd != root_fd:
            os.close(parent_fd)
        if root_fd is not None:
            os.close(root_fd)
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary is not None and not completed:
            temporary.unlink(missing_ok=True)


def _prepared_render(path: Path, destination: Path) -> tuple[dict, Path]:
    """Render into an uninstalled sibling directory, leaving live output untouched."""
    info = inspect_pdf(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and (destination.is_symlink() or not destination.is_dir()):
        raise ValueError("PDF render destination must be a non-symlink directory")
    staging = Path(tempfile.mkdtemp(prefix=".pdf-replacement-render-", dir=destination.parent))
    pages = []
    try:
        with fitz.open(path) as doc:
            for number, page in enumerate(doc, start=1):
                name = f"page-{number}.png"
                page.get_pixmap().save(staging / name)
                with (staging / name).open("rb") as rendered:
                    os.fsync(rendered.fileno())
                pages.append(name)
        _fsync_dir(staging)
        return {**info, "pages": pages}, staging
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise ValueError(f"could not render PDF: {exc}") from exc


def validate_replacement_candidate(project_dir: Path, candidate: Path | str,
                                   primary: Path | str) -> Path:
    """Perform user-visible validation before creating a version snapshot.

    ``replace_primary`` repeats this work while freezing the inode; this preflight exists only
    so malformed requests return 400 without creating an otherwise unnecessary v1 tag.
    """
    root = Path(project_dir).absolute()
    primary_path = _lexical_path(root, primary, "primary")
    if primary_path != root / "document.pdf":
        raise ValueError("primary must be the exact project document.pdf")
    candidate_path = _lexical_path(root, candidate, "candidate")
    if candidate_path == primary_path:
        raise ValueError("candidate must not be document.pdf")
    stable = None
    try:
        stable, _ = _copy_candidate_no_follow(root, candidate_path)
        inspect_pdf(stable)
    finally:
        if stable is not None: stable.unlink(missing_ok=True)
    return candidate_path


def _unique_sibling(path: Path, prefix: str) -> Path:
    candidate = path.with_name(f"{prefix}{uuid.uuid4().hex}")
    while candidate.exists():
        candidate = path.with_name(f"{prefix}{uuid.uuid4().hex}")
    return candidate


class ReplacementTransaction:
    """Prepare → publish → finalize/rollback journaled PDF replacement.

    Backups remain live until the caller has durably captured v2.  Readers are required to hold
    ``project_write_lock`` so the unavoidable two-rename primary/render publish is invisible.
    """
    def __init__(self, root, candidate, primary, render_path, stable, staging, info, candidate_stat,
                 txid=None):
        self.root, self.candidate, self.primary, self.render_path = root, candidate, primary, render_path
        self.stable, self.staging, self.info, self.candidate_stat = stable, staging, info, candidate_stat
        self.txid = txid or uuid.uuid4().hex
        self.had_render = render_path.exists()
        self.primary_backup = root / f".pdf-txn-{self.txid}-primary"
        self.parked_candidate = root / f".pdf-txn-{self.txid}-candidate"
        self.render_backup = render_path.with_name(f".pdf-txn-{self.txid}-render")
        self.published = False

    @property
    def journal(self): return self.root / _JOURNAL

    def _write_journal(self, phase, tag=None):
        # No cleanup path is trusted from disk: recovery derives all paths from txid + trusted roots.
        _atomic_json(self.journal, {"schema_version": 1, "txid": self.txid, "phase": phase,
            "candidate": str(self.candidate.relative_to(self.root)), "tag": tag,
            "had_render": self.had_render})

    def publish(self):
        os.link(self.primary, self.primary_backup)
        self._write_journal("prepared")
        root_fd = parent_fd = None
        try:
            root_fd, parent_fd, name = _open_child(self.root, self.candidate)
            now = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (now.st_dev, now.st_ino) != (self.candidate_stat.st_dev, self.candidate_stat.st_ino):
                raise ValueError("candidate changed during replacement")
            self._write_journal("candidate_park_intent")
            os.replace(name, self.parked_candidate.name, src_dir_fd=parent_fd, dst_dir_fd=root_fd)
            _fsync_dir(self.root)
            self._write_journal("primary_publish_intent")
            os.replace(self.stable, self.primary); self.stable = None
            _fsync_dir(self.root)
            self._write_journal("render_backup_intent")
            if self.had_render: os.replace(self.render_path, self.render_backup)
            self._fsync_render_parent()
            self._write_journal("render_publish_intent")
            os.replace(self.staging, self.render_path); self.staging = None
            _fsync_dir(self.root); _fsync_dir(self.render_path.parent)
            self.published = True
            self._write_journal("published")
            return self.info
        except Exception:
            self.rollback()
            raise
        finally:
            if parent_fd is not None and parent_fd != root_fd: os.close(parent_fd)
            if root_fd is not None: os.close(root_fd)

    def _fsync_render_parent(self): _fsync_dir(self.render_path.parent)

    def commit_intent(self, tag): self._write_journal("commit_intent", tag)
    def mark_versioned(self, tag=None): self._write_journal("versioned", tag)

    def rollback(self):
        if self.had_render and self.render_backup.exists():
            if self.render_path.exists():
                failed = _unique_sibling(self.render_path, ".pdf-render-failed-")
                os.replace(self.render_path, failed); shutil.rmtree(failed, ignore_errors=True)
            os.replace(self.render_backup, self.render_path)
        elif not self.had_render and self.render_path.exists():
            shutil.rmtree(self.render_path, ignore_errors=True)
        if self.primary_backup.exists(): os.replace(self.primary_backup, self.primary)
        if self.parked_candidate.exists() and not self.candidate.exists(): os.replace(self.parked_candidate, self.candidate)
        _fsync_dir(self.root); self._fsync_render_parent()
        self._cleanup(best_effort=True)

    def _cleanup(self, best_effort=False):
        def clean_file(p):
            if p: p.unlink(missing_ok=True)
        def clean_dir(p):
            if p: shutil.rmtree(p, ignore_errors=best_effort)
        try:
            clean_file(self.stable); clean_dir(self.staging); clean_file(self.primary_backup)
            clean_dir(self.render_backup); clean_file(self.parked_candidate); self.journal.unlink(missing_ok=True)
            _fsync_dir(self.root); self._fsync_render_parent()
        except OSError:
            # Finalization is post-commit housekeeping; leave the journal for a future lock.
            if not best_effort: return

    def finalize(self):
        self._cleanup(best_effort=False)


def prepare_replacement(project_dir: Path, candidate: Path | str, primary: Path | str,
                        render_dir: Path | str) -> ReplacementTransaction:
    root = Path(project_dir).absolute()
    if not root.is_dir() or root.is_symlink(): raise ValueError("project directory must be a regular directory")
    primary_path = _lexical_path(root, primary, "primary")
    if primary_path != root / "document.pdf" or primary_path.is_symlink() or not primary_path.is_file():
        raise ValueError("primary must be the exact regular project document.pdf")
    candidate_path = _lexical_path(root, candidate, "candidate")
    if candidate_path == primary_path: raise ValueError("candidate must not be document.pdf")
    stable, candidate_stat = _copy_candidate_no_follow(root, candidate_path)
    try:
        info, staging = _prepared_render(stable, Path(render_dir).absolute())
        return ReplacementTransaction(root, candidate_path, primary_path, Path(render_dir).absolute(), stable, staging, info, candidate_stat)
    except Exception:
        stable.unlink(missing_ok=True); raise


def recover_pending(project_dir: Path, render_dir: Path | str | None = None) -> None:
    root = Path(project_dir).absolute(); journal = root / _JOURNAL
    if not journal.exists(): return
    try:
        data = json.loads(journal.read_text())
        if set(data) != {"schema_version", "txid", "phase", "candidate", "tag", "had_render"}:
            raise ValueError("invalid journal schema")
        if data["schema_version"] != 1 or not _TXID.fullmatch(data["txid"]):
            raise ValueError("invalid journal transaction")
        if type(data["had_render"]) is not bool:
            raise ValueError("invalid journal render state")
        candidate = _lexical_path(root, data["candidate"], "candidate")
        if candidate.name == "document.pdf" or Path(data["candidate"]).is_absolute():
            raise ValueError("invalid journal candidate")
        if render_dir is None:
            raise ValueError("trusted render directory required for recovery")
        render = Path(render_dir).absolute()
        tx = ReplacementTransaction(root, candidate, root / "document.pdf", render, None, None, {}, None, data["txid"])
        tx.had_render = data["had_render"]
        phase = data["phase"]
        if phase not in {"prepared", "candidate_park_intent", "primary_publish_intent", "render_backup_intent", "render_publish_intent", "published", "commit_intent", "versioned"}:
            raise ValueError("invalid journal phase")
        if phase in {"versioned", "commit_intent"} and data["tag"]:
            import vcs
            if any(version["tag"] == data["tag"] for version in vcs.list_versions(root)):
                tx.finalize(); return
        tx.rollback()
    except Exception:
        raise ValueError("could not recover interrupted PDF replacement")


def replace_primary(project_dir: Path, candidate: Path | str, primary: Path | str, render_dir: Path | str) -> dict:
    """Compatibility one-shot replacement; API callers retain the transaction through v2."""
    with replacement_lock(project_dir):
        recover_pending(project_dir, render_dir)
        tx = prepare_replacement(project_dir, candidate, primary, render_dir)
        try:
            result = tx.publish(); tx.mark_versioned(); tx.finalize(); return result
        except Exception:
            tx.rollback(); raise


def extract_page_text(path: Path, page_number: int) -> str:
    """Extract embedded text for one 1-based PDF page.  This never performs OCR."""
    if isinstance(page_number, bool) or not isinstance(page_number, int):
        raise ValueError("page must be a positive integer")
    info = inspect_pdf(path)
    if not 1 <= page_number <= info["page_count"]:
        raise ValueError("page must be within the document")
    try:
        with fitz.open(path) as doc:
            return doc.load_page(page_number - 1).get_text()
    except Exception as exc:
        raise ValueError(f"could not extract PDF text: {exc}") from exc
