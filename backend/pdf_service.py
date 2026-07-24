"""PDF validation, rendering, and atomic primary-document replacement helpers."""
import fcntl
import hashlib
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
_VERSION_TAG = re.compile(r"v[1-9][0-9]*")
_PHASES = {
    "preparing",
    "prepared",
    "primary_backup_intent",
    "candidate_park_intent",
    "primary_publish_intent",
    "render_backup_intent",
    "render_publish_intent",
    "published",
    "committed",
    "commit_intent",
    "versioned",
}
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
        depths = getattr(_LOCK_STATE, "depths", None)
        if depths is None:
            depths = {}
            _LOCK_STATE.depths = depths
        depth = depths.get(key, 0)
        if depth:
            depths[key] = depth + 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        with lock_path.open("a+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


project_write_lock = replacement_lock


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_fd(fd: int) -> None:
    os.fsync(fd)


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


def _copy_candidate_no_follow(
    root: Path,
    candidate: Path,
    destination: Path | None = None,
    fault_hook=None,
) -> tuple[Path, os.stat_result, str]:
    """Freeze the candidate inode into a durable same-directory temporary file."""
    _assert_no_symlink_path(root, candidate, "candidate")
    source_fd = temporary_fd = root_fd = parent_fd = None
    temporary = destination
    completed = False
    cleanup_on_error = False
    content_digest = hashlib.sha256()
    try:
        root_fd, parent_fd, name = _open_child(root, candidate)
        source_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size <= 0:
            raise ValueError("candidate must be a non-empty regular PDF file")
        if temporary is None:
            temporary_fd, raw_temporary = tempfile.mkstemp(
                prefix=".pdf-replacement-", suffix=".pdf", dir=root
            )
            temporary = Path(raw_temporary)
        else:
            temporary_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        if fault_hook is not None:
            fault_hook("stable_created")
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            content_digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(temporary_fd, view)
                view = view[written:]
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        _fsync_dir(root)
        completed = True
        return temporary, source_stat, content_digest.hexdigest()
    except OSError as exc:
        cleanup_on_error = True
        raise ValueError(f"could not safely read candidate: {exc}") from exc
    except Exception:
        cleanup_on_error = True
        raise
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if parent_fd is not None and parent_fd != root_fd:
            os.close(parent_fd)
        if root_fd is not None:
            os.close(root_fd)
        if temporary_fd is not None:
            os.close(temporary_fd)
        # Ordinary preparation failures are cleaned immediately.  BaseException is used by
        # crash tests to model process death, so its deterministic partial file is retained for
        # recovery just as it would be after SIGKILL.
        if temporary is not None and not completed and cleanup_on_error:
            temporary.unlink(missing_ok=True)


def _inspect_candidate_no_follow(root: Path, candidate: Path) -> dict:
    """Validate a candidate inode without creating any pre-WAL filesystem artifact."""
    _assert_no_symlink_path(root, candidate, "candidate")
    source_fd = root_fd = parent_fd = None
    try:
        root_fd, parent_fd, name = _open_child(root, candidate)
        source_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size <= 0:
            raise ValueError("candidate must be a non-empty regular PDF file")
        content = bytearray()
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            content.extend(chunk)
        try:
            with fitz.open(stream=bytes(content), filetype="pdf") as document:
                if not document.is_pdf or document.page_count < 1:
                    raise ValueError("PDF must contain at least one page")
                return {
                    "page_count": document.page_count,
                    "metadata": dict(document.metadata or {}),
                }
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"invalid PDF: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"could not safely read candidate: {exc}") from exc
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if parent_fd is not None and parent_fd != root_fd:
            os.close(parent_fd)
        if root_fd is not None:
            os.close(root_fd)


def _prepared_render(
    path: Path,
    destination: Path,
    staging: Path | None = None,
    fault_hook=None,
) -> tuple[dict, Path]:
    """Render into an uninstalled sibling directory, leaving live output untouched."""
    info = inspect_pdf(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and (destination.is_symlink() or not destination.is_dir()):
        raise ValueError("PDF render destination must be a non-symlink directory")
    if staging is None:
        staging = Path(tempfile.mkdtemp(prefix=".pdf-replacement-render-", dir=destination.parent))
    else:
        staging.mkdir()
    _fsync_dir(destination.parent)
    if fault_hook is not None:
        fault_hook("render_staging_created")
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
        if fault_hook is not None:
            fault_hook("render_prepared")
        return {**info, "pages": pages}, staging
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        _fsync_dir(destination.parent)
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
    candidate_path, _ = _validated_candidate_relative(root, candidate)
    if candidate_path == primary_path:
        raise ValueError("candidate must not be document.pdf")
    _inspect_candidate_no_follow(root, candidate_path)
    return candidate_path


def _remove_path(path: Path) -> None:
    """Remove one transaction artifact without ever following a symlink."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def park_render_for_restore(render_dir: Path | str) -> Path | None:
    """Temporarily retain the current render so a failed Git restore can roll back."""
    destination = Path(render_dir).absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        return None
    _require_directory(destination, "current PDF render")
    backup = destination.with_name(f".pdf-restore-{uuid.uuid4().hex}-old-render")
    os.replace(destination, backup)
    _fsync_dir(destination.parent)
    return backup


def rollback_parked_render(render_dir: Path | str, backup: Path | None) -> None:
    """Remove any newly rendered target and atomically restore the retained render."""
    destination = Path(render_dir).absolute()
    if destination.exists() or destination.is_symlink():
        _remove_path(destination)
    if backup is not None:
        _require_directory(backup, "retained PDF render")
        os.replace(backup, destination)
    _fsync_dir(destination.parent)


def discard_parked_render(render_dir: Path | str, backup: Path | None) -> None:
    """Discard a retained render after a successful restore."""
    if backup is not None:
        _remove_path(backup)
    _fsync_dir(Path(render_dir).absolute().parent)


def _require_regular(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ValueError(f"{label} is missing") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"{label} must be a regular file")


def _require_directory(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ValueError(f"{label} is missing") from exc
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        raise ValueError(f"{label} must be a non-symlink directory")


def _validated_candidate_relative(root: Path, candidate: Path | str) -> tuple[Path, str]:
    raw = Path(candidate)
    if raw.is_absolute():
        target = _lexical_path(root, raw, "candidate")
        relative = target.relative_to(root)
    else:
        relative = raw
        target = _lexical_path(root, raw, "candidate")
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("candidate must be a lexical project path")
    canonical = relative.as_posix()
    if canonical == "document.pdf":
        raise ValueError("candidate must not be document.pdf")
    return target, canonical


class ReplacementTransaction:
    """Prepare → publish → finalize/rollback journaled PDF replacement.

    Backups remain live until the caller has durably captured v2.  Readers are required to hold
    ``project_write_lock`` so the unavoidable two-rename primary/render publish is invisible.
    """
    def __init__(
        self,
        root: Path,
        candidate: Path,
        candidate_relative: str,
        primary: Path,
        render_path: Path,
        txid: str,
        had_render: bool,
        before_tag: str | None = None,
        info: dict | None = None,
        candidate_stat: os.stat_result | None = None,
        candidate_digest: str | None = None,
        fault_hook=None,
    ):
        self.root = root
        self.candidate = candidate
        self.candidate_relative = candidate_relative
        self.primary = primary
        self.render_path = render_path
        self.txid = txid
        self.had_render = had_render
        self.before_tag = before_tag
        self.info = info or {}
        self.candidate_stat = candidate_stat
        self.candidate_digest = candidate_digest
        self.fault_hook = fault_hook
        self.phase = "preparing"
        self.expected_tag = None
        self.stable = root / f".pdf-txn-{self.txid}-stable.pdf"
        self.primary_backup = root / f".pdf-txn-{self.txid}-old-primary"
        self.parked_candidate = root / f".pdf-txn-{self.txid}-candidate"
        self.staging = render_path.with_name(f".pdf-txn-{self.txid}-render-staging")
        self.render_backup = render_path.with_name(f".pdf-txn-{self.txid}-old-render")
        self.failed_render = render_path.with_name(f".pdf-txn-{self.txid}-failed-render")
        self.published = False

    @property
    def journal(self) -> Path:
        return self.root / _JOURNAL

    def _fault(self, boundary: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(boundary)

    def _write_journal(self, phase: str, expected_tag: str | None = None) -> None:
        if phase not in _PHASES:
            raise ValueError("invalid replacement phase")
        if expected_tag is not None and _VERSION_TAG.fullmatch(expected_tag) is None:
            raise ValueError("invalid expected version tag")
        if self.before_tag is not None and _VERSION_TAG.fullmatch(self.before_tag) is None:
            raise ValueError("invalid pre-replacement version tag")
        # No cleanup path is trusted from disk: recovery derives all paths from txid + trusted roots.
        data = {
            "schema_version": 1,
            "txid": self.txid,
            "phase": phase,
            "candidate": self.candidate_relative,
            "had_render": self.had_render,
        }
        if expected_tag is not None:
            data["expected_tag"] = expected_tag
        if self.before_tag is not None:
            data["before_tag"] = self.before_tag
        _atomic_json(self.journal, data)
        self.phase = phase
        self.expected_tag = expected_tag

    def publish(self) -> dict:
        self._write_journal("primary_backup_intent")
        self._fault("primary_backup_intent")
        os.link(self.primary, self.primary_backup, follow_symlinks=False)
        _fsync_dir(self.root)
        self._fault("primary_backup_created")
        root_fd = parent_fd = candidate_fd = None
        try:
            self._write_journal("candidate_park_intent")
            self._fault("candidate_park_intent")
            root_fd, parent_fd, name = _open_child(self.root, self.candidate)
            candidate_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            now = os.fstat(candidate_fd)
            path_now = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (self.candidate_stat is None
                    or (now.st_dev, now.st_ino)
                    != (self.candidate_stat.st_dev, self.candidate_stat.st_ino)
                    or (path_now.st_dev, path_now.st_ino)
                    != (now.st_dev, now.st_ino)
                    or self.candidate_digest is None):
                raise ValueError("candidate changed during replacement")
            os.replace(name, self.parked_candidate.name, src_dir_fd=parent_fd, dst_dir_fd=root_fd)
            _fsync_fd(parent_fd)
            _fsync_dir(self.root)
            self._fault("candidate_parked")
            self._write_journal("primary_publish_intent")
            self._fault("primary_publish_intent")
            parked_now = os.stat(
                self.parked_candidate.name,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            digest = hashlib.sha256()
            while True:
                chunk = os.read(candidate_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            if ((parked_now.st_dev, parked_now.st_ino)
                    != (now.st_dev, now.st_ino)
                    or digest.hexdigest() != self.candidate_digest):
                raise ValueError("candidate changed during replacement")
            _require_regular(self.stable, "stable candidate")
            os.replace(self.stable, self.primary)
            _fsync_dir(self.root)
            self._fault("primary_published")
            self._write_journal("render_backup_intent")
            self._fault("render_backup_intent")
            if self.had_render:
                _require_directory(self.render_path, "existing render")
                os.replace(self.render_path, self.render_backup)
            self._fsync_render_parent()
            self._fault("render_backed_up")
            self._write_journal("render_publish_intent")
            self._fault("render_publish_intent")
            _require_directory(self.staging, "staged render")
            os.replace(self.staging, self.render_path)
            self._fsync_render_parent()
            self._fault("render_published")
            self.published = True
            self._write_journal("published")
            return self.info
        except Exception:
            self.rollback_to_before()
            raise
        finally:
            if candidate_fd is not None:
                os.close(candidate_fd)
            if parent_fd is not None and parent_fd != root_fd:
                os.close(parent_fd)
            if root_fd is not None:
                os.close(root_fd)

    def _fsync_render_parent(self) -> None:
        _fsync_dir(self.render_path.parent)

    def commit_intent(self, tag: str) -> None:
        self._write_journal("commit_intent", tag)

    def mark_committed(self) -> None:
        """Durably commit a direct-helper replacement before fallible cleanup."""
        self._write_journal("committed")

    def mark_versioned(self, tag: str) -> None:
        self._write_journal("versioned", tag)

    def _restore_candidate(self) -> None:
        try:
            _require_regular(self.parked_candidate, "parked candidate")
        except ValueError:
            if not self.parked_candidate.exists():
                return
            raise
        root_fd = parent_fd = None
        try:
            root_fd, parent_fd, name = _open_child(self.root, self.candidate)
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("candidate path is occupied during recovery")
            os.replace(
                self.parked_candidate.name,
                name,
                src_dir_fd=root_fd,
                dst_dir_fd=parent_fd,
            )
            _fsync_fd(parent_fd)
            _fsync_dir(self.root)
        finally:
            if parent_fd is not None and parent_fd != root_fd:
                os.close(parent_fd)
            if root_fd is not None:
                os.close(root_fd)

    def _rollback_generation(self) -> None:
        # Validate and restore the nested candidate path first.  If an untrusted component was
        # replaced by a symlink, fail closed before changing either live generation.
        if self.parked_candidate.exists() or self.parked_candidate.is_symlink():
            self._restore_candidate()
        if self.had_render:
            if self.render_backup.exists():
                _require_directory(self.render_backup, "old render")
                if self.render_path.exists() or self.render_path.is_symlink():
                    os.replace(self.render_path, self.failed_render)
                os.replace(self.render_backup, self.render_path)
                _remove_path(self.failed_render)
        elif self.phase in {
            "render_publish_intent", "published", "commit_intent", "versioned"
        }:
            _remove_path(self.render_path)
        if self.primary_backup.exists():
            _require_regular(self.primary_backup, "old primary")
            os.replace(self.primary_backup, self.primary)
        _fsync_dir(self.root)
        self._fsync_render_parent()

    def rollback(self) -> None:
        self._rollback_generation()
        self._cleanup()

    def rollback_to_before(self) -> None:
        """Restore the complete v1 generation before deleting the resumable WAL."""
        self._rollback_generation()
        if self.before_tag is not None:
            import vcs
            restored = vcs.restore_version(self.root, self.before_tag)
            if not restored.get("ok"):
                raise ValueError(
                    "could not restore pre-replacement version: "
                    + restored.get("error", "unknown error")
                )
        self._cleanup()

    def _cleanup(self) -> None:
        # The journal is deliberately removed last.  Any cleanup failure leaves a recoverable
        # record whose phase says whether to rollback or finish the committed generation.
        for path in (
            self.stable,
            self.staging,
            self.primary_backup,
            self.render_backup,
            self.failed_render,
            self.parked_candidate,
        ):
            _remove_path(path)
        _fsync_dir(self.root)
        self._fsync_render_parent()
        self.journal.unlink(missing_ok=True)
        _fsync_dir(self.root)

    def finalize(self) -> None:
        self._cleanup()


def prepare_replacement(
    project_dir: Path,
    candidate: Path | str,
    primary: Path | str,
    render_dir: Path | str,
    *,
    before_tag: str | None = None,
    fault_hook=None,
) -> ReplacementTransaction:
    root = Path(project_dir).absolute()
    if not root.is_dir() or root.is_symlink():
        raise ValueError("project directory must be a regular directory")
    primary_path = _lexical_path(root, primary, "primary")
    if primary_path != root / "document.pdf" or primary_path.is_symlink() or not primary_path.is_file():
        raise ValueError("primary must be the exact regular project document.pdf")
    candidate_path, candidate_relative = _validated_candidate_relative(root, candidate)
    render_path = Path(render_dir).absolute()
    render_path.parent.mkdir(parents=True, exist_ok=True)
    if render_path.exists():
        _require_directory(render_path, "PDF render destination")
        had_render = True
    else:
        had_render = False
    txid = uuid.uuid4().hex
    transaction = ReplacementTransaction(
        root,
        candidate_path,
        candidate_relative,
        primary_path,
        render_path,
        txid,
        had_render,
        before_tag=before_tag,
        fault_hook=fault_hook,
    )
    transaction._write_journal("preparing")
    try:
        stable, candidate_stat, candidate_digest = _copy_candidate_no_follow(
            root,
            candidate_path,
            transaction.stable,
            fault_hook=fault_hook,
        )
        info, staging = _prepared_render(
            stable,
            render_path,
            transaction.staging,
            fault_hook=fault_hook,
        )
        transaction.stable = stable
        transaction.staging = staging
        transaction.info = info
        transaction.candidate_stat = candidate_stat
        transaction.candidate_digest = candidate_digest
        transaction._write_journal("prepared")
        return transaction
    except Exception:
        transaction.rollback_to_before()
        raise


def recover_pending(project_dir: Path, render_dir: Path | str | None = None) -> None:
    root = Path(project_dir).absolute()
    journal = root / _JOURNAL
    if not journal.exists():
        return
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
        required = {"schema_version", "txid", "phase", "candidate", "had_render"}
        optional = {"expected_tag", "before_tag"}
        if (not isinstance(data, dict)
                or not required.issubset(data)
                or not set(data).issubset(required | optional)):
            raise ValueError("invalid journal schema")
        if (type(data["schema_version"]) is not int or data["schema_version"] != 1
                or not isinstance(data["txid"], str)
                or not _TXID.fullmatch(data["txid"])):
            raise ValueError("invalid journal transaction")
        if type(data["had_render"]) is not bool:
            raise ValueError("invalid journal render state")
        if not isinstance(data["candidate"], str) or Path(data["candidate"]).is_absolute():
            raise ValueError("invalid journal candidate")
        candidate, candidate_relative = _validated_candidate_relative(root, data["candidate"])
        if candidate_relative != data["candidate"]:
            raise ValueError("journal candidate is not canonical")
        if render_dir is None:
            raise ValueError("trusted render directory required for recovery")
        render = Path(render_dir).absolute()
        phase = data["phase"]
        if not isinstance(phase, str) or phase not in _PHASES:
            raise ValueError("invalid journal phase")
        expected_tag = data.get("expected_tag")
        if phase in {"commit_intent", "versioned"}:
            if not isinstance(expected_tag, str) or _VERSION_TAG.fullmatch(expected_tag) is None:
                raise ValueError("invalid journal expected tag")
        elif "expected_tag" in data:
            raise ValueError("unexpected journal version tag")
        before_tag = data.get("before_tag")
        if before_tag is not None:
            if not isinstance(before_tag, str) or _VERSION_TAG.fullmatch(before_tag) is None:
                raise ValueError("invalid journal pre-replacement tag")
        tx = ReplacementTransaction(
            root,
            candidate,
            candidate_relative,
            root / "document.pdf",
            render,
            data["txid"],
            data["had_render"],
            before_tag=before_tag,
        )
        tx.phase = phase
        tx.expected_tag = expected_tag
        if phase in {"versioned", "commit_intent"}:
            import vcs
            if vcs.version_exists(root, expected_tag):
                tx.finalize()
                return
        elif phase == "committed":
            tx.finalize()
            return
        tx.rollback_to_before()
    except Exception:
        raise ValueError("could not recover interrupted PDF replacement")


def replace_primary(project_dir: Path, candidate: Path | str, primary: Path | str, render_dir: Path | str) -> dict:
    """Compatibility one-shot replacement; API callers retain the transaction through v2."""
    with replacement_lock(project_dir):
        recover_pending(project_dir, render_dir)
        tx = prepare_replacement(project_dir, candidate, primary, render_dir)
        try:
            result = tx.publish()
            tx.mark_committed()
        except Exception:
            tx.rollback()
            raise
        try:
            tx.finalize()
        except Exception:
            return {**result, "cleanup_pending": True}
        return {**result, "cleanup_pending": False}


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
