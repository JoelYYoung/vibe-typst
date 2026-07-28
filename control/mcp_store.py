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


def invalidate_user_leases(db_path: Path, user_id: str) -> None:
    with _connect(db_path) as db:
        db.execute("DELETE FROM project_leases WHERE user_id=?", (user_id,))


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
