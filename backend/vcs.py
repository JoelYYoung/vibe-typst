"""Git-backed version management for the active project directory.

A *version* is an annotated git tag. Saving a version commits the working tree and
tags it; restoring resets the working tree to a tag (git-native — discards
uncommitted changes, so the caller must confirm first); deleting just removes the
tag. The commit graph is storage only — the UI lists tags. Dirty detection is
git-native (`status --porcelain`), so it picks up uploaded/deleted files reliably
instead of any hand-rolled heuristic.
"""
import re
import subprocess
from pathlib import Path

# Things that change constantly and would otherwise keep the repo perpetually
# "dirty" (and trigger false discard prompts) — keep them out of version control.
_IGNORE_PATTERNS = [
    "*.backup",
    ".tcb/",
    ".claude/",
    ".mcp.json",
    ".vibe-typst.json",       # app-managed project metadata
    ".slide-comments.db",     # SQLite comment store (+ its WAL/SHM sidecars)
    ".slide-comments.db-shm",
    ".slide-comments.db-wal",
    ".DS_Store",
    "Thumbs.db",
]

_GIT_CONFIG = [("user.email", "vibe@local"), ("user.name", "Vibe Typst")]
_US = "\x1f"  # unit separator for --format parsing


def _run(args, cwd, input=None):
    # Degrade gracefully if git can't even be spawned (e.g. BlockingIOError when the host is
    # out of process slots) — return a non-zero result instead of raising, so the API endpoints
    # report "no repo / unavailable" rather than a 500.
    try:
        r = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                           text=True, input=input, timeout=30)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except Exception as e:
        return "", str(e), -1


def is_repo(project_dir: Path) -> bool:
    _, _, rc = _run(["rev-parse", "--git-dir"], project_dir)
    return rc == 0


def _ensure_gitignore(project_dir: Path) -> None:
    gi = project_dir / ".gitignore"
    existing = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    have = {l.strip() for l in existing}
    missing = [p for p in _IGNORE_PATTERNS if p not in have]
    if missing:
        header = [] if existing else ["# Vibe Typst — generated"]
        gi.write_text("\n".join(existing + header + missing) + "\n", encoding="utf-8")


def _untrack_ignored(project_dir: Path) -> None:
    """Stop tracking any file that now matches .gitignore (e.g. a comment DB that
    an earlier version committed). Without this, those volatile files keep the repo
    permanently 'dirty' and trigger false discard prompts."""
    out, _, _ = _run(["ls-files", "-i", "-c", "--exclude-standard"], project_dir)
    for f in out.splitlines():
        if f.strip():
            _run(["rm", "--cached", "--", f.strip()], project_dir)


def _ensure_init(project_dir: Path) -> None:
    if not is_repo(project_dir):
        _run(["init"], project_dir)
        for k, v in _GIT_CONFIG:
            _run(["config", k, v], project_dir)
    _ensure_gitignore(project_dir)
    _untrack_ignored(project_dir)


def _is_dirty(project_dir: Path) -> bool:
    out, _, _ = _run(["status", "--porcelain"], project_dir)
    return bool(out.strip())


def _head_commit(project_dir: Path) -> str | None:
    out, _, rc = _run(["rev-parse", "HEAD"], project_dir)
    return out.strip() if rc == 0 else None


def status(project_dir: Path) -> dict:
    if not is_repo(project_dir):
        return {"initialized": False, "dirty": False, "current": None}
    dirty = _is_dirty(project_dir)
    head = _head_commit(project_dir)
    current = None
    if head and not dirty:
        out, _, _ = _run(["tag", "--points-at", "HEAD"], project_dir)
        tags = [t for t in out.splitlines() if t.strip()]
        current = tags[-1] if tags else None
    return {"initialized": True, "dirty": dirty, "current": current}


def list_versions(project_dir: Path) -> list[dict]:
    """All saved versions (annotated tags), newest first."""
    if not is_repo(project_dir):
        return []
    head = _head_commit(project_dir)
    dirty = _is_dirty(project_dir)
    fmt = _US.join(["%(refname:short)", "%(*objectname)", "%(objectname)",
                    "%(contents:subject)", "%(creatordate:relative)"])
    out, _, rc = _run(["for-each-ref", "--sort=-creatordate", "refs/tags",
                       f"--format={fmt}"], project_dir)
    if rc != 0:
        return []
    versions = []
    for line in out.splitlines():
        parts = line.split(_US)
        if len(parts) < 5:
            continue
        tag, deref, obj, subject, date = parts[:5]
        commit = deref or obj  # annotated → dereferenced commit; lightweight → obj
        versions.append({
            "tag": tag,
            "commit": commit,
            "short": commit[:7],
            "message": subject or tag,
            "date": date.strip(),
            "is_current": (commit == head and not dirty),
        })
    return versions


def _next_tag(project_dir: Path) -> str:
    out, _, _ = _run(["tag", "-l"], project_dir)
    mx = 0
    for t in out.splitlines():
        m = re.match(r"^v(\d+)$", t.strip())
        if m:
            mx = max(mx, int(m.group(1)))
    return f"v{mx + 1}"


def save_version(project_dir: Path, message: str = "") -> dict:
    """Commit the current working tree and tag it as a new version."""
    _ensure_init(project_dir)
    msg = message.strip()
    _run(["add", "-A"], project_dir)
    out, err, rc = _run(["commit", "-m", msg or "snapshot"], project_dir)
    nothing = "nothing to commit" in (out + err).lower()
    if rc != 0 and not nothing:
        return {"ok": False, "error": err or out}
    # Nothing changed and HEAD is already a version → don't create a duplicate tag.
    if nothing:
        pto, _, _ = _run(["tag", "--points-at", "HEAD"], project_dir)
        existing = [t for t in pto.splitlines() if t.strip()]
        if existing:
            return {"ok": True, "tag": existing[-1], "skipped": True}
    tag = _next_tag(project_dir)
    _run(["tag", "-a", tag, "-m", msg or tag], project_dir)
    return {"ok": True, "tag": tag}


def restore_version(project_dir: Path, tag: str) -> dict:
    """Reset the working tree (and the master branch) to a tagged version.

    git-native and destructive to uncommitted changes — the caller confirms first
    when `status.dirty`. Other versions survive: they are independently referenced
    by their own tags, so moving master never orphans them.
    """
    if not is_repo(project_dir):
        return {"ok": False, "error": "not a repo"}
    commit, _, rc = _run(["rev-parse", "--verify", f"{tag}^{{commit}}"], project_dir)
    if rc != 0:
        return {"ok": False, "error": "unknown version"}
    # Clear any local changes, then point master at the tag's commit and check it
    # out (this also reattaches HEAD if a legacy detached state was left behind).
    _run(["reset", "--hard"], project_dir)
    _, err, rc = _run(["checkout", "-B", "master", commit.strip()], project_dir)
    if rc != 0:
        return {"ok": False, "error": err}
    return {"ok": True}


def delete_version(project_dir: Path, tag: str) -> dict:
    if not is_repo(project_dir):
        return {"ok": False, "error": "not a repo"}
    _, err, rc = _run(["tag", "-d", tag], project_dir)
    if rc != 0:
        return {"ok": False, "error": err or "delete failed"}
    return {"ok": True}
