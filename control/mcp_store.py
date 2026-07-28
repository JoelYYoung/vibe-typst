"""Persistence for opaque project leases and sanitized MCP audit events."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from mcp_errors import ERROR_CODES, McpServiceError
from pat_store import PatIdentity


LEASE_IDLE_SECONDS = 12 * 3600
AUDIT_RETENTION_SECONDS = 90 * 86400
UPLOAD_TTL_SECONDS = 15 * 60
DOWNLOAD_TTL_SECONDS = 5 * 60
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_LIVE_UPLOADS_PER_TOKEN = 2


@dataclass(frozen=True)
class Lease:
    id: str
    user_id: str
    token_id: str
    project_id: str
    context_version: str
    created_at: float
    last_used_at: float
    expires_at: float


@dataclass(frozen=True)
class AuditEvent:
    user_id: str
    token_id: str
    tool_name: str
    project_id: str | None
    targets: tuple[str, ...]
    started_at: float
    completed_at: float
    outcome: str
    error_code: str | None
    correlation_id: str


@dataclass(frozen=True)
class UploadSession:
    id: str
    user_id: str
    token_id: str
    username: str
    kind: str
    project_id: str
    destination: str
    size: int
    sha256: str
    filename: str
    state: str
    created_at: float
    expires_at: float
    received_at: float | None
    received_size: int | None
    error_code: str | None


@dataclass(frozen=True)
class DownloadSession:
    id: str
    user_id: str
    token_id: str
    username: str
    port: int
    project_id: str
    backend_path: str
    filename: str
    size: int | None
    sha256: str | None
    state: str
    created_at: float
    expires_at: float
    consumed_at: float | None


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    return connection


def migrate(db_path: Path) -> None:
    with _connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS project_leases (
                id              TEXT PRIMARY KEY,
                handle_hash     TEXT NOT NULL UNIQUE,
                user_id         TEXT NOT NULL,
                token_id        TEXT NOT NULL,
                project_id      TEXT NOT NULL,
                context_version TEXT NOT NULL,
                created_at      REAL NOT NULL,
                last_used_at    REAL NOT NULL,
                expires_at      REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS project_leases_token
                ON project_leases(token_id);
            CREATE INDEX IF NOT EXISTS project_leases_user
                ON project_leases(user_id);
            CREATE INDEX IF NOT EXISTS project_leases_expiry
                ON project_leases(expires_at);

            CREATE TABLE IF NOT EXISTS mcp_audit_log (
                id             TEXT PRIMARY KEY,
                user_id        TEXT NOT NULL,
                token_id       TEXT NOT NULL,
                tool_name      TEXT NOT NULL,
                project_id     TEXT,
                targets        TEXT NOT NULL,
                started_at     REAL NOT NULL,
                completed_at   REAL NOT NULL,
                outcome        TEXT NOT NULL,
                error_code     TEXT,
                correlation_id TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS mcp_audit_completed
                ON mcp_audit_log(completed_at);

            CREATE TABLE IF NOT EXISTS upload_sessions (
                id            TEXT PRIMARY KEY,
                capability_hash TEXT NOT NULL,
                user_id       TEXT NOT NULL,
                token_id      TEXT NOT NULL,
                username      TEXT NOT NULL,
                kind          TEXT NOT NULL,
                project_id    TEXT NOT NULL,
                destination   TEXT NOT NULL,
                size          INTEGER NOT NULL,
                sha256        TEXT NOT NULL,
                filename      TEXT NOT NULL,
                state         TEXT NOT NULL,
                created_at    REAL NOT NULL,
                expires_at    REAL NOT NULL,
                received_at   REAL,
                received_size INTEGER,
                error_code    TEXT
            );
            CREATE INDEX IF NOT EXISTS upload_sessions_token_state
                ON upload_sessions(token_id, state, expires_at);
            CREATE INDEX IF NOT EXISTS upload_sessions_expiry
                ON upload_sessions(expires_at);

            CREATE TABLE IF NOT EXISTS download_sessions (
                id              TEXT PRIMARY KEY,
                capability_hash TEXT NOT NULL,
                user_id         TEXT NOT NULL,
                token_id        TEXT NOT NULL,
                username        TEXT NOT NULL,
                port            INTEGER NOT NULL,
                project_id      TEXT NOT NULL,
                backend_path    TEXT NOT NULL,
                filename        TEXT NOT NULL,
                size            INTEGER,
                sha256          TEXT,
                state           TEXT NOT NULL,
                created_at      REAL NOT NULL,
                expires_at      REAL NOT NULL,
                consumed_at     REAL
            );
            CREATE INDEX IF NOT EXISTS download_sessions_expiry
                ON download_sessions(expires_at);
            """
        )


def _required_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is required")
    return value


def issue_lease(
    db_path: Path,
    identity: PatIdentity,
    project_id: str,
    context_version: str,
    now: float | None = None,
) -> tuple[dict, str]:
    project_id = _required_text(project_id, "project_id")
    context_version = _required_text(context_version, "context_version")
    issued_at = time.time() if now is None else float(now)
    lease_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    raw_handle = f"vph_{lease_id}_{secret}"
    handle_hash = hashlib.sha256(raw_handle.encode()).hexdigest()
    expires_at = issued_at + LEASE_IDLE_SECONDS

    with _connect(db_path) as db:
        db.execute(
            """
            INSERT INTO project_leases (
                id, handle_hash, user_id, token_id, project_id,
                context_version, created_at, last_used_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lease_id,
                handle_hash,
                identity.user_id,
                identity.token_id,
                project_id,
                context_version,
                issued_at,
                issued_at,
                expires_at,
            ),
        )
    return (
        {
            "id": lease_id,
            "project_id": project_id,
            "created_at": issued_at,
            "last_used_at": issued_at,
            "expires_at": expires_at,
        },
        raw_handle,
    )


def _handle_id(raw_handle: str) -> str | None:
    if not isinstance(raw_handle, str):
        return None
    parts = raw_handle.split("_", 2)
    if len(parts) != 3 or parts[0] != "vph" or not parts[1] or not parts[2]:
        return None
    return parts[1]


def _expired_handle() -> McpServiceError:
    return McpServiceError(
        "PROJECT_HANDLE_EXPIRED",
        "project handle is invalid or expired",
    )


def _lease_from_row(row: sqlite3.Row) -> Lease:
    return Lease(
        id=row["id"],
        user_id=row["user_id"],
        token_id=row["token_id"],
        project_id=row["project_id"],
        context_version=row["context_version"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        expires_at=row["expires_at"],
    )


def validate_lease(
    db_path: Path,
    raw_handle: str,
    identity: PatIdentity,
    project_id: str,
    context_version: str,
    now: float | None = None,
) -> Lease:
    lease_id = _handle_id(raw_handle)
    if lease_id is None:
        raise _expired_handle()
    used_at = time.time() if now is None else float(now)
    candidate_hash = hashlib.sha256(raw_handle.encode()).hexdigest()

    with _connect(db_path) as db:
        row = db.execute(
            "SELECT * FROM project_leases WHERE id=?", (lease_id,)
        ).fetchone()
        if (
            row is None
            or row["user_id"] != identity.user_id
            or row["token_id"] != identity.token_id
            or not hmac.compare_digest(row["handle_hash"], candidate_hash)
        ):
            raise _expired_handle()
        if row["expires_at"] <= used_at:
            db.execute("DELETE FROM project_leases WHERE id=?", (lease_id,))
            db.commit()
            raise _expired_handle()
        if (
            row["project_id"] != project_id
            or row["context_version"] != context_version
        ):
            db.execute("DELETE FROM project_leases WHERE id=?", (lease_id,))
            db.commit()
            raise McpServiceError(
                "PROJECT_CONTEXT_CHANGED",
                "the active project context changed; open the project again",
            )

        expires_at = used_at + LEASE_IDLE_SECONDS
        db.execute(
            """
            UPDATE project_leases
            SET last_used_at=?, expires_at=?
            WHERE id=?
            """,
            (used_at, expires_at, lease_id),
        )
        updated = dict(row)
        updated["last_used_at"] = used_at
        updated["expires_at"] = expires_at
    return _lease_from_row(updated)


def invalidate_token_leases(db_path: Path, token_id: str) -> None:
    with _connect(db_path) as db:
        db.execute("DELETE FROM project_leases WHERE token_id=?", (token_id,))
        db.execute("DELETE FROM upload_sessions WHERE token_id=?", (token_id,))
        db.execute("DELETE FROM download_sessions WHERE token_id=?", (token_id,))


def invalidate_user_leases(db_path: Path, user_id: str) -> None:
    with _connect(db_path) as db:
        db.execute("DELETE FROM project_leases WHERE user_id=?", (user_id,))
        db.execute("DELETE FROM upload_sessions WHERE user_id=?", (user_id,))
        db.execute("DELETE FROM download_sessions WHERE user_id=?", (user_id,))


def _validated_sha256(value: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("sha256 is required")
    normalized = value.lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("sha256 must be a hexadecimal SHA-256 digest")
    return normalized


def _validated_size(
    value: int | None, *, optional: bool = False
) -> int | None:
    if optional and value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise ValueError("size must be a non-negative integer")
    if value > MAX_UPLOAD_BYTES:
        raise McpServiceError("FILE_TOO_LARGE", "file is too large")
    return value


def _validated_filename(value: str) -> str:
    value = _required_text(value, "filename")
    if (
        len(value) > 255
        or "\x00" in value
        or "/" in value
        or "\\" in value
        or value in {".", ".."}
    ):
        raise ValueError("filename is invalid")
    return value


def _validated_transfer_text(value: str, label: str) -> str:
    value = _required_text(value, label)
    if len(value) > 1024 or "\x00" in value or "\\" in value:
        raise ValueError(f"{label} is invalid")
    return value


def _upload_from_row(row: sqlite3.Row) -> UploadSession:
    return UploadSession(
        id=row["id"],
        user_id=row["user_id"],
        token_id=row["token_id"],
        username=row["username"],
        kind=row["kind"],
        project_id=row["project_id"],
        destination=row["destination"],
        size=row["size"],
        sha256=row["sha256"],
        filename=row["filename"],
        state=row["state"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        received_at=row["received_at"],
        received_size=row["received_size"],
        error_code=row["error_code"],
    )


def _download_from_row(row: sqlite3.Row) -> DownloadSession:
    return DownloadSession(
        id=row["id"],
        user_id=row["user_id"],
        token_id=row["token_id"],
        username=row["username"],
        port=row["port"],
        project_id=row["project_id"],
        backend_path=row["backend_path"],
        filename=row["filename"],
        size=row["size"],
        sha256=row["sha256"],
        state=row["state"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        consumed_at=row["consumed_at"],
    )


def _private_transfer_error() -> McpServiceError:
    return McpServiceError(
        "UPLOAD_EXPIRED", "transfer capability is invalid or expired"
    )


def begin_upload(
    db_path: Path,
    identity: PatIdentity,
    kind: str,
    project_id: str,
    destination: str,
    size: int,
    sha256: str,
    filename: str,
    now: float | None = None,
) -> tuple[dict, str]:
    if kind not in {"file", "pdf_project", "pdf_replacement"}:
        raise ValueError("upload kind is invalid")
    project_id = (
        _validated_transfer_text(project_id, "project_id")
        if project_id
        else ""
    )
    destination = _validated_transfer_text(destination, "destination")
    validated_size = _validated_size(size)
    validated_hash = _validated_sha256(sha256)
    filename = _validated_filename(filename)
    created_at = time.time() if now is None else float(now)
    expires_at = created_at + UPLOAD_TTL_SECONDS
    upload_id = secrets.token_hex(16)
    capability = secrets.token_urlsafe(32)
    capability_hash = hashlib.sha256(capability.encode()).hexdigest()

    with _connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        live = db.execute(
            """
            SELECT COUNT(*) FROM upload_sessions
            WHERE token_id=? AND state IN ('pending','received','finishing')
              AND expires_at>?
            """,
            (identity.token_id, created_at),
        ).fetchone()[0]
        if live >= MAX_LIVE_UPLOADS_PER_TOKEN:
            raise McpServiceError(
                "RATE_LIMITED",
                "too many active upload sessions",
                retryable=True,
                retry_after=1.0,
            )
        db.execute(
            """
            INSERT INTO upload_sessions (
                id, capability_hash, user_id, token_id, username, kind,
                project_id, destination, size, sha256, filename, state,
                created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                upload_id,
                capability_hash,
                identity.user_id,
                identity.token_id,
                identity.username,
                kind,
                project_id,
                destination,
                validated_size,
                validated_hash,
                filename,
                created_at,
                expires_at,
            ),
        )
    return (
        {
            "id": upload_id,
            "kind": kind,
            "project_id": project_id or None,
            "destination": destination,
            "size": validated_size,
            "sha256": validated_hash,
            "filename": filename,
            "created_at": created_at,
            "expires_at": expires_at,
        },
        capability,
    )


def authorize_upload(
    db_path: Path,
    upload_id: str,
    capability: str,
    now: float | None = None,
) -> UploadSession:
    checked_at = time.time() if now is None else float(now)
    if not isinstance(upload_id, str) or not isinstance(capability, str):
        raise _private_transfer_error()
    candidate_hash = hashlib.sha256(capability.encode()).hexdigest()
    with _connect(db_path) as db:
        row = db.execute(
            "SELECT * FROM upload_sessions WHERE id=?", (upload_id,)
        ).fetchone()
    if (
        row is None
        or row["expires_at"] <= checked_at
        or not hmac.compare_digest(
            row["capability_hash"], candidate_hash
        )
    ):
        raise _private_transfer_error()
    if row["state"] != "pending":
        raise McpServiceError(
            "UPLOAD_ALREADY_USED", "upload capability was already used"
        )
    return _upload_from_row(row)


def mark_upload_received(
    db_path: Path,
    upload_id: str,
    received_size: int,
    now: float | None = None,
) -> UploadSession:
    received_at = time.time() if now is None else float(now)
    with _connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM upload_sessions WHERE id=?", (upload_id,)
        ).fetchone()
        if (
            row is None
            or row["expires_at"] <= received_at
            or row["state"] != "pending"
        ):
            raise McpServiceError(
                "UPLOAD_ALREADY_USED", "upload capability was already used"
            )
        if received_size != row["size"]:
            raise ValueError("received size does not match upload metadata")
        db.execute(
            """
            UPDATE upload_sessions
            SET state='received', received_at=?, received_size=?
            WHERE id=?
            """,
            (received_at, received_size, upload_id),
        )
        updated = dict(row)
        updated.update({
            "state": "received",
            "received_at": received_at,
            "received_size": received_size,
        })
    return _upload_from_row(updated)


def fail_upload(
    db_path: Path, upload_id: str, error_code: str
) -> None:
    if error_code not in ERROR_CODES:
        raise ValueError("invalid upload error code")
    with _connect(db_path) as db:
        db.execute(
            """
            UPDATE upload_sessions
            SET state='failed', error_code=?
            WHERE id=? AND state IN ('pending','received','finishing')
            """,
            (error_code, upload_id),
        )


def complete_upload(
    db_path: Path,
    upload_id: str,
    identity: PatIdentity,
    now: float | None = None,
) -> UploadSession:
    completed_at = time.time() if now is None else float(now)
    with _connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM upload_sessions WHERE id=?", (upload_id,)
        ).fetchone()
        if (
            row is None
            or row["expires_at"] <= completed_at
            or row["user_id"] != identity.user_id
            or row["token_id"] != identity.token_id
        ):
            raise _private_transfer_error()
        if row["state"] != "received":
            raise McpServiceError(
                "UPLOAD_ALREADY_USED", "upload capability was already used"
            )
        db.execute(
            "UPDATE upload_sessions SET state='finishing' WHERE id=?",
            (upload_id,),
        )
        updated = dict(row)
        updated["state"] = "finishing"
    return _upload_from_row(updated)


def finish_upload(db_path: Path, upload_id: str) -> None:
    with _connect(db_path) as db:
        changed = db.execute(
            """
            UPDATE upload_sessions SET state='complete'
            WHERE id=? AND state='finishing'
            """,
            (upload_id,),
        ).rowcount
        if changed != 1:
            raise McpServiceError(
                "UPLOAD_ALREADY_USED", "upload capability was already used"
            )


def begin_download(
    db_path: Path,
    identity: PatIdentity,
    project_id: str,
    backend_path: str,
    *,
    filename: str,
    size: int | None = None,
    sha256: str | None = None,
    now: float | None = None,
) -> tuple[dict, str]:
    project_id = _validated_transfer_text(project_id, "project_id")
    backend_path = _validated_transfer_text(backend_path, "backend_path")
    if not backend_path.startswith("/api/") or "://" in backend_path:
        raise ValueError("backend_path must be an /api/ path")
    filename = _validated_filename(filename)
    validated_size = _validated_size(size, optional=True)
    validated_hash = _validated_sha256(sha256, optional=True)
    created_at = time.time() if now is None else float(now)
    expires_at = created_at + DOWNLOAD_TTL_SECONDS
    download_id = secrets.token_hex(16)
    capability = secrets.token_urlsafe(32)
    capability_hash = hashlib.sha256(capability.encode()).hexdigest()
    with _connect(db_path) as db:
        db.execute(
            """
            INSERT INTO download_sessions (
                id, capability_hash, user_id, token_id, username, port,
                project_id, backend_path, filename, size, sha256, state,
                created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                download_id,
                capability_hash,
                identity.user_id,
                identity.token_id,
                identity.username,
                identity.port,
                project_id,
                backend_path,
                filename,
                validated_size,
                validated_hash,
                created_at,
                expires_at,
            ),
        )
    return (
        {
            "id": download_id,
            "project_id": project_id,
            "filename": filename,
            "size": validated_size,
            "sha256": validated_hash,
            "created_at": created_at,
            "expires_at": expires_at,
        },
        capability,
    )


def claim_download(
    db_path: Path,
    download_id: str,
    capability: str,
    now: float | None = None,
) -> DownloadSession:
    claimed_at = time.time() if now is None else float(now)
    if not isinstance(download_id, str) or not isinstance(capability, str):
        raise _private_transfer_error()
    candidate_hash = hashlib.sha256(capability.encode()).hexdigest()
    with _connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM download_sessions WHERE id=?",
            (download_id,),
        ).fetchone()
        if (
            row is None
            or row["expires_at"] <= claimed_at
            or not hmac.compare_digest(
                row["capability_hash"], candidate_hash
            )
        ):
            raise _private_transfer_error()
        if row["state"] != "pending":
            raise McpServiceError(
                "UPLOAD_ALREADY_USED", "download capability was already used"
            )
        db.execute(
            """
            UPDATE download_sessions
            SET state='streaming', consumed_at=?
            WHERE id=?
            """,
            (claimed_at, download_id),
        )
        updated = dict(row)
        updated.update({"state": "streaming", "consumed_at": claimed_at})
    return _download_from_row(updated)


def authorize_download(
    db_path: Path,
    download_id: str,
    capability: str,
    now: float | None = None,
) -> DownloadSession:
    checked_at = time.time() if now is None else float(now)
    if not isinstance(download_id, str) or not isinstance(capability, str):
        raise _private_transfer_error()
    candidate_hash = hashlib.sha256(capability.encode()).hexdigest()
    with _connect(db_path) as db:
        row = db.execute(
            "SELECT * FROM download_sessions WHERE id=?",
            (download_id,),
        ).fetchone()
    if (
        row is None
        or row["expires_at"] <= checked_at
        or not hmac.compare_digest(
            row["capability_hash"], candidate_hash
        )
    ):
        raise _private_transfer_error()
    if row["state"] != "pending":
        raise McpServiceError(
            "UPLOAD_ALREADY_USED", "download capability was already used"
        )
    return _download_from_row(row)


def finish_download(db_path: Path, download_id: str) -> None:
    with _connect(db_path) as db:
        db.execute(
            """
            UPDATE download_sessions SET state='complete'
            WHERE id=? AND state='streaming'
            """,
            (download_id,),
        )


def _sanitized_targets(targets: tuple[str, ...]) -> list[str]:
    if not isinstance(targets, tuple):
        raise ValueError("audit targets must be a tuple")
    sanitized = []
    for target in targets:
        if (
            not isinstance(target, str)
            or not target
            or len(target) > 1024
            or "\x00" in target
            or "\\" in target
        ):
            raise ValueError("invalid audit target")
        path = PurePosixPath(target)
        if path.is_absolute() or target == "." or ".." in path.parts:
            raise ValueError("audit targets must be relative identifiers")
        sanitized.append(target)
    return sanitized


def record_audit(db_path: Path, event: AuditEvent) -> None:
    if event.error_code is not None and event.error_code not in ERROR_CODES:
        raise ValueError("invalid audit error code")
    if (
        not math.isfinite(event.started_at)
        or not math.isfinite(event.completed_at)
        or event.completed_at < event.started_at
    ):
        raise ValueError("invalid audit timestamps")
    for value, label in (
        (event.user_id, "user_id"),
        (event.token_id, "token_id"),
        (event.tool_name, "tool_name"),
        (event.outcome, "outcome"),
        (event.correlation_id, "correlation_id"),
    ):
        _required_text(value, label)
    targets_json = json.dumps(
        _sanitized_targets(event.targets), separators=(",", ":")
    )

    with _connect(db_path) as db:
        db.execute(
            """
            INSERT INTO mcp_audit_log (
                id, user_id, token_id, tool_name, project_id, targets,
                started_at, completed_at, outcome, error_code, correlation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                secrets.token_hex(12),
                event.user_id,
                event.token_id,
                event.tool_name,
                event.project_id,
                targets_json,
                event.started_at,
                event.completed_at,
                event.outcome,
                event.error_code,
                event.correlation_id,
            ),
        )


def sweep_expired(db_path: Path, now: float | None = None) -> dict[str, int]:
    swept_at = time.time() if now is None else float(now)
    with _connect(db_path) as db:
        leases = db.execute(
            "DELETE FROM project_leases WHERE expires_at<=?", (swept_at,)
        ).rowcount
        audit = db.execute(
            "DELETE FROM mcp_audit_log WHERE completed_at<?",
            (swept_at - AUDIT_RETENTION_SECONDS,),
        ).rowcount
    return {"leases": leases, "audit": audit}


def sweep_transfer_sessions(
    db_path: Path,
    workspace_base: Path,
    now: float | None = None,
) -> dict[str, int]:
    swept_at = time.time() if now is None else float(now)
    with _connect(db_path) as db:
        uploads = [
            dict(row)
            for row in db.execute(
                """
                SELECT id, username FROM upload_sessions
                WHERE expires_at<=?
                """,
                (swept_at,),
            ).fetchall()
        ]
        download_count = db.execute(
            "DELETE FROM download_sessions WHERE expires_at<=?",
            (swept_at,),
        ).rowcount

    base = Path(workspace_base)
    safe_base = None
    if not base.is_symlink():
        try:
            safe_base = base.resolve(strict=True)
        except OSError:
            safe_base = None
    for upload in uploads:
        username = upload["username"]
        upload_id = upload["id"]
        if (
            safe_base is None
            or not isinstance(username, str)
            or not username
            or len(username) > 128
            or any(
                not (character.isalnum() or character in "-_")
                for character in username
            )
            or not isinstance(upload_id, str)
            or len(upload_id) != 32
            or any(
                character not in "0123456789abcdef"
                for character in upload_id
            )
        ):
            continue
        user_root = safe_base / username
        private_root = user_root / ".tcb"
        upload_root = private_root / "uploads"
        if (
            user_root.is_symlink()
            or private_root.is_symlink()
            or upload_root.is_symlink()
            or not upload_root.is_dir()
        ):
            continue
        for suffix in ("part", "ready"):
            candidate = upload_root / f"{upload_id}.{suffix}"
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass

    with _connect(db_path) as db:
        upload_count = db.execute(
            "DELETE FROM upload_sessions WHERE expires_at<=?",
            (swept_at,),
        ).rowcount
    return {"uploads": upload_count, "downloads": download_count}
