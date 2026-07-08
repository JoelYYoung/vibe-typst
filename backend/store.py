"""Durable SQLite comment store, shared by the FastAPI backend and the MCP server.

Comments are anchored by *content* (anchor_text + page + spans), never by raw line
number, so they survive edits that shift line numbers elsewhere. Every comment is
tagged with the `file` it belongs to (relative to the project root) so switching the
working .typ shows only that file's comments. A `comment_events` table keeps an
append-only history (created / edited / status changes) so nothing is ever lost.

The store path comes from COMMENT_STORE_PATH (a .db file). A legacy
`.slide-comments.json` next to it is imported once on first open.
"""
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path: str | None = None
_override: str | None = None

# columns stored as JSON text
_JSON_FIELDS = {"anchor_spans", "rel_anchors", "region", "selection"}


def set_path(path: str) -> None:
    """Point the store at a new db file (web backend calls this when a file is opened)."""
    global _override
    _override = str(path)


def close() -> None:
    """Close the cached DB connection so the .slide-comments.db file is no longer held open.
    Required before deleting a project on NFS: an open file there gets silly-renamed to
    `.nfsXXXX` instead of removed, leaving the folder non-empty so rmtree/rmdir fails."""
    global _conn, _conn_path
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn = None
    _conn_path = None


def _store_path() -> Path:
    raw = _override or os.environ.get("COMMENT_STORE_PATH", str(Path.home() / ".tcb-comments.db"))
    p = Path(raw).expanduser()
    # Tolerate an old .json path in the env: keep the same stem with .db.
    if p.suffix == ".json":
        p = p.with_suffix(".db")
    return p


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _connect() -> sqlite3.Connection:
    global _conn, _conn_path
    path = str(_store_path())
    if _conn is not None and _conn_path == path:
        return _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS comments (
            id            TEXT PRIMARY KEY,
            seq           INTEGER UNIQUE,
            file          TEXT,
            kind          TEXT DEFAULT 'element',   -- 'element' | 'page'
            page          INTEGER,
            anchor_text   TEXT DEFAULT '',
            anchor_context TEXT DEFAULT '',
            anchor_spans  TEXT,                      -- JSON [[from,to],...]
            rel_anchors   TEXT,                      -- JSON Yjs RelativePositions
            selection     TEXT,                      -- JSON (legacy single span)
            region        TEXT,                      -- JSON normalized bbox(es)
            raw_context   TEXT DEFAULT '',           -- exact blob handed to Claude
            body          TEXT DEFAULT '',
            status        TEXT DEFAULT 'pending',    -- pending | done | dismissed
            created_at    TEXT,
            updated_at    TEXT,
            done_at       TEXT,
            done_note     TEXT
        );
        CREATE TABLE IF NOT EXISTS comment_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            comment_id TEXT,
            ts         TEXT,
            kind       TEXT,        -- created | edited | status | deleted
            detail     TEXT
        );
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        """
    )
    conn.commit()
    _conn, _conn_path = conn, path
    _maybe_import_legacy(conn, path)
    return conn


def _next_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM comments").fetchone()
    return int(row["n"])


def _maybe_import_legacy(conn: sqlite3.Connection, path: str) -> None:
    """One-shot import of an old .slide-comments.json sitting beside the db."""
    done = conn.execute("SELECT v FROM meta WHERE k='legacy_imported'").fetchone()
    if done:
        return
    legacy = Path(path).with_suffix(".json")
    if legacy.exists():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            for c in data.get("comments", []):
                _insert_row(conn, {
                    "id": c.get("id") or uuid.uuid4().hex[:8],
                    "seq": c.get("seq"),
                    "file": c.get("file"),
                    "kind": c.get("kind", "element"),
                    "page": c.get("page"),
                    "anchor_text": c.get("anchor_text", ""),
                    "anchor_context": c.get("anchor_context", ""),
                    "anchor_spans": c.get("anchor_spans"),
                    "rel_anchors": c.get("rel_anchors"),
                    "selection": c.get("selection"),
                    "region": c.get("region"),
                    "raw_context": c.get("raw_context", ""),
                    "body": c.get("body", ""),
                    "status": c.get("status", "pending"),
                    "created_at": c.get("created_at") or _now(),
                    "updated_at": c.get("updated_at") or _now(),
                    "done_at": c.get("done_at"),
                    "done_note": c.get("done_note"),
                })
        except Exception:
            pass
    conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('legacy_imported', '1')")
    conn.commit()


def _encode(payload: dict) -> dict:
    out = dict(payload)
    for f in _JSON_FIELDS:
        if f in out and not isinstance(out[f], (str, type(None))):
            out[f] = json.dumps(out[f], ensure_ascii=False)
    return out


def _insert_row(conn: sqlite3.Connection, row: dict) -> None:
    row = _encode(row)
    cols = list(row.keys())
    conn.execute(
        f"INSERT OR IGNORE INTO comments ({','.join(cols)}) "
        f"VALUES ({','.join('?' for _ in cols)})",
        [row[c] for c in cols],
    )


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for f in _JSON_FIELDS:
        if d.get(f):
            try:
                d[f] = json.loads(d[f])
            except Exception:
                pass
    return d


def _event(conn: sqlite3.Connection, cid: str, kind: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO comment_events (comment_id, ts, kind, detail) VALUES (?,?,?,?)",
        (cid, _now(), kind, detail),
    )


# ------------------------------------------------------------------ public API
def list_comments(status: str | None = None, file: str | None = None) -> list:
    with _lock:
        conn = _connect()
        q = "SELECT * FROM comments"
        clauses, args = [], []
        if status:
            clauses.append("status = ?")
            args.append(status)
        if file:
            clauses.append("file = ?")
            args.append(file)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY seq"
        return [_row_to_dict(r) for r in conn.execute(q, args).fetchall()]


def get_comment(cid: str) -> dict | None:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM comments WHERE id = ? OR seq = ?", (cid, str(cid))
        ).fetchone()
        return _row_to_dict(row) if row else None


def get_events(cid: str) -> list:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT ts, kind, detail FROM comment_events WHERE comment_id = ? ORDER BY id",
            (cid,),
        ).fetchall()
        return [dict(r) for r in rows]


def add_comment(payload: dict) -> dict:
    with _lock:
        conn = _connect()
        now = _now()
        cid = uuid.uuid4().hex[:8]
        seq = _next_seq(conn)
        row = {
            "id": cid,
            "seq": seq,
            "file": payload.get("file"),
            "kind": payload.get("kind", "element"),
            "page": payload.get("page"),
            "anchor_text": (payload.get("anchor_text") or "").strip(),
            "anchor_context": (payload.get("anchor_context") or "").strip(),
            "anchor_spans": payload.get("anchor_spans"),
            "rel_anchors": payload.get("rel_anchors"),
            "selection": payload.get("selection"),
            "region": payload.get("region"),
            "raw_context": payload.get("raw_context") or "",
            "body": (payload.get("body") or "").strip(),
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            "done_at": None,
            "done_note": None,
        }
        _insert_row(conn, row)
        _event(conn, cid, "created", row["body"][:200])
        conn.commit()
        return _row_to_dict(conn.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone())


def update_comment(cid: str, **fields) -> dict | None:
    with _lock:
        conn = _connect()
        cur = get_comment(cid)
        if not cur:
            return None
        real_id = cur["id"]
        sets, args = [], []
        for k, v in fields.items():
            if v is None:
                continue
            if k in _JSON_FIELDS and not isinstance(v, str):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k} = ?")
            args.append(v)
        sets.append("updated_at = ?")
        args.append(_now())
        args.append(real_id)
        conn.execute(f"UPDATE comments SET {','.join(sets)} WHERE id = ?", args)
        _event(conn, real_id, "edited", ",".join(fields.keys()))
        conn.commit()
        return get_comment(real_id)


def set_status(cid: str, status: str, note: str | None = None) -> dict | None:
    with _lock:
        conn = _connect()
        cur = get_comment(cid)
        if not cur:
            return None
        real_id = cur["id"]
        now = _now()
        if status == "done":
            conn.execute(
                "UPDATE comments SET status=?, updated_at=?, done_at=?, done_note=? WHERE id=?",
                (status, now, now, note, real_id),
            )
        else:
            conn.execute(
                "UPDATE comments SET status=?, updated_at=? WHERE id=?",
                (status, now, real_id),
            )
        _event(conn, real_id, "status", f"{status}: {note or ''}".strip())
        conn.commit()
        return get_comment(real_id)


def delete_comment(cid: str) -> bool:
    with _lock:
        conn = _connect()
        cur = get_comment(cid)
        if not cur:
            return False
        conn.execute("DELETE FROM comments WHERE id = ?", (cur["id"],))
        _event(conn, cur["id"], "deleted", "")
        conn.commit()
        return True
