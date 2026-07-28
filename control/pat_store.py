"""Personal Access Token persistence for the Vibe Typst control plane."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


VIEWER_SCOPES = frozenset(
    {
        "projects:read",
        "files:read",
        "slides:read",
        "transcripts:read",
        "comments:read",
    }
)
EDITOR_SCOPES = VIEWER_SCOPES | frozenset(
    {
        "projects:write",
        "files:write",
        "documents:write",
        "transcripts:write",
        "comments:write",
    }
)

_PRESETS = {
    "viewer": VIEWER_SCOPES,
    "editor": EDITOR_SCOPES,
}
_LAST_USED_WRITE_INTERVAL = 60.0


@dataclass(frozen=True)
class PatIdentity:
    token_id: str
    user_id: str
    username: str
    port: int
    scopes: frozenset[str]
    expires_at: float | None


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    return connection


def migrate(db_path: Path) -> None:
    with _connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS api_tokens (
                id           TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL,
                name         TEXT NOT NULL,
                token_hash   TEXT NOT NULL UNIQUE,
                token_prefix TEXT NOT NULL,
                scopes       TEXT NOT NULL,
                created_at   REAL NOT NULL,
                expires_at   REAL,
                last_used_at REAL,
                revoked_at   REAL
            );
            CREATE INDEX IF NOT EXISTS api_tokens_user_created
                ON api_tokens(user_id, created_at DESC);
            """
        )


def _validated_expiry(expires_at: float | None) -> float | None:
    if expires_at is None:
        return None
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise ValueError("expires_at must be a Unix timestamp or null")
    value = float(expires_at)
    if not math.isfinite(value):
        raise ValueError("expires_at must be finite")
    return value


def _preset_for(scopes: frozenset[str]) -> str:
    for name, preset_scopes in _PRESETS.items():
        if scopes == preset_scopes:
            return name
    return "custom"


def _public_token(row: sqlite3.Row) -> dict:
    scopes = frozenset(json.loads(row["scopes"]))
    return {
        "id": row["id"],
        "name": row["name"],
        "token_prefix": row["token_prefix"],
        "preset": _preset_for(scopes),
        "scopes": sorted(scopes),
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "last_used_at": row["last_used_at"],
        "revoked_at": row["revoked_at"],
    }


def issue_token(
    db_path: Path,
    user_id: str,
    name: str,
    preset: str,
    expires_at: float | None,
) -> tuple[dict, str]:
    resolved_name = name.strip() if isinstance(name, str) else ""
    if not resolved_name:
        raise ValueError("token name is required")
    if len(resolved_name) > 128:
        raise ValueError("token name must be at most 128 characters")
    if preset not in _PRESETS:
        raise ValueError("preset must be 'viewer' or 'editor'")
    resolved_expiry = _validated_expiry(expires_at)

    token_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    raw_token = f"vbt_{token_id}_{secret}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    token_prefix = f"vbt_{token_id}_{secret[:6]}"
    created_at = time.time()
    scopes_json = json.dumps(sorted(_PRESETS[preset]), separators=(",", ":"))

    with _connect(db_path) as db:
        if not db.execute(
            "SELECT 1 FROM users WHERE id=?", (user_id,)
        ).fetchone():
            raise ValueError("user does not exist")
        db.execute(
            """
            INSERT INTO api_tokens (
                id, user_id, name, token_hash, token_prefix, scopes,
                created_at, expires_at, last_used_at, revoked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                token_id,
                user_id,
                resolved_name,
                token_hash,
                token_prefix,
                scopes_json,
                created_at,
                resolved_expiry,
            ),
        )
        row = db.execute(
            "SELECT * FROM api_tokens WHERE id=?", (token_id,)
        ).fetchone()
    return _public_token(row), raw_token


def list_tokens(db_path: Path, user_id: str) -> list[dict]:
    with _connect(db_path) as db:
        rows = db.execute(
            """
            SELECT * FROM api_tokens
            WHERE user_id=?
            ORDER BY created_at DESC, id DESC
            """,
            (user_id,),
        ).fetchall()
    return [_public_token(row) for row in rows]


def revoke_token(db_path: Path, user_id: str, token_id: str) -> bool:
    with _connect(db_path) as db:
        result = db.execute(
            """
            UPDATE api_tokens
            SET revoked_at=?
            WHERE id=? AND user_id=? AND revoked_at IS NULL
            """,
            (time.time(), token_id, user_id),
        )
    return result.rowcount == 1


def _token_id(raw_token: str) -> str | None:
    if not isinstance(raw_token, str):
        return None
    parts = raw_token.split("_", 2)
    if len(parts) != 3 or parts[0] != "vbt" or not parts[1] or not parts[2]:
        return None
    return parts[1]


def authenticate(
    db_path: Path, raw_token: str, now: float | None = None
) -> PatIdentity | None:
    token_id = _token_id(raw_token)
    if token_id is None:
        return None
    used_at = time.time() if now is None else float(now)
    candidate_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    with _connect(db_path) as db:
        row = db.execute(
            """
            SELECT
                t.id, t.user_id, t.token_hash, t.scopes, t.expires_at,
                t.revoked_at, t.last_used_at,
                u.username, u.port, u.locked
            FROM api_tokens AS t
            JOIN users AS u ON u.id=t.user_id
            WHERE t.id=?
            """,
            (token_id,),
        ).fetchone()
        if (
            row is None
            or row["locked"]
            or row["revoked_at"] is not None
            or (
                row["expires_at"] is not None
                and row["expires_at"] <= used_at
            )
            or not hmac.compare_digest(row["token_hash"], candidate_hash)
        ):
            return None

        if (
            row["last_used_at"] is None
            or row["last_used_at"] <= used_at - _LAST_USED_WRITE_INTERVAL
        ):
            db.execute(
                "UPDATE api_tokens SET last_used_at=? WHERE id=?",
                (used_at, token_id),
            )

    return PatIdentity(
        token_id=row["id"],
        user_id=row["user_id"],
        username=row["username"],
        port=row["port"],
        scopes=frozenset(json.loads(row["scopes"])),
        expires_at=row["expires_at"],
    )
