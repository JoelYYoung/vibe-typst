"""Git-backed version management for the active project directory.

A *version* is an annotated git tag. Saving a version commits the working tree and
tags it; restoring resets the working tree to a tag (git-native — discards
uncommitted changes, so the caller must confirm first); deleting just removes the
tag. The commit graph is storage only — the UI lists tags. Dirty detection is
git-native (`status --porcelain`), so it picks up uploaded/deleted files reliably
instead of any hand-rolled heuristic.
"""
import os
import re
import subprocess
import tempfile
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
    # App-managed agent tooling — regenerated on EVERY project open (workdir.setup), so
    # versioning it makes the repo perpetually "dirty" and saves fire when the DECK itself is
    # unchanged. A version should capture the user's deck (.typ + assets), not this scaffolding.
    "AGENTS.md",
    "CLAUDE.md",
    "AGENTS.md.backup-*",
    "CLAUDE.md.backup-*",
    ".codex/",
    ".agent-home/",           # agent runtime state (auth/config/cache), if it lands in-project
    # Process crash dumps — a crashed codex/node (`--yolo`) can drop a multi-hundred-MB `core`.
    "core",
    "core.*",
    "*.core",
    "vgcore.*",
    "*.heapsnapshot",
]

_GIT_CONFIG = [("user.email", "vibe@local"), ("user.name", "Vibe Typst")]
_US = "\x1f"  # unit separator for --format parsing
_HOUSEKEEPING_MESSAGE = "chore: stop tracking app-managed files"


def _run(args, cwd, input=None, env=None):
    # Degrade gracefully if git can't even be spawned (e.g. BlockingIOError when the host is
    # out of process slots) — return a non-zero result instead of raising, so the API endpoints
    # report "no repo / unavailable" rather than a 500.
    try:
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)
        r = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                           text=True, input=input, timeout=30, env=proc_env)
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
    """Stop tracking any file that now matches .gitignore (app-managed configs a prior version
    committed, crash dumps, a comment DB). Without this those volatile files keep the repo
    permanently 'dirty' and trigger false discard prompts / spurious saves.

    The removal is committed through an isolated temporary index, so pre-existing staged deck
    edits remain staged instead of leaking into the housekeeping commit. Existing version tags
    remain immutable; status() recognizes a tag immediately behind housekeeping-only commits as
    the current deck version.
    """
    out, _, _ = _run(["ls-files", "-i", "-c", "--exclude-standard"], project_dir)
    ignored = [x.strip() for x in out.splitlines() if x.strip()]
    old_head = _head_commit(project_dir)
    if old_head is None:
        return  # no history yet — save_version() will make the first commit
    with tempfile.TemporaryDirectory(prefix="vibe-typst-index-") as td:
        index_path = str(Path(td) / "index")
        index_env = {"GIT_INDEX_FILE": index_path}
        if _run(["read-tree", old_head], project_dir, env=index_env)[2] != 0:
            return
        for f in ignored:
            if _run(["rm", "--cached", "--", f], project_dir, env=index_env)[2] != 0:
                return
        if _run(["add", "--", ".gitignore"], project_dir, env=index_env)[2] != 0:
            return
        if _run(["diff", "--cached", "--quiet"], project_dir, env=index_env)[2] == 0:
            return
        tree, _, rc = _run(["write-tree"], project_dir, env=index_env)
        if rc != 0:
            return
        new_head, _, rc = _run(
            ["commit-tree", tree, "-p", old_head, "-m", _HOUSEKEEPING_MESSAGE],
            project_dir,
        )
        if rc != 0:
            return

    # Bring only the housekeeping paths in the caller's real index to the new commit state.
    # Any deck changes already staged there are deliberately untouched.
    for f in ignored:
        if _run(["rm", "--cached", "--", f], project_dir)[2] != 0:
            return
    if _run(["add", "--", ".gitignore"], project_dir)[2] != 0:
        return
    _run(["update-ref", "-m", "vibe-typst housekeeping", "HEAD", new_head, old_head], project_dir)


def _current_version_tag(project_dir: Path) -> str | None:
    """Return the immutable version tag represented by a clean working tree.

    Migration commits change only tracking metadata, not the user's deck. Walk through those
    known commits to the tagged deck snapshot instead of force-moving the tag.
    """
    if _is_dirty(project_dir):
        return None
    commit = _head_commit(project_dir)
    while commit:
        out, _, _ = _run(["tag", "--points-at", commit], project_dir)
        tags = [t for t in out.splitlines() if t.strip()]
        if tags:
            return tags[-1]
        subject, _, rc = _run(["show", "-s", "--format=%s", commit], project_dir)
        if rc != 0 or subject != _HOUSEKEEPING_MESSAGE:
            return None
        parent, _, rc = _run(["rev-parse", f"{commit}^"], project_dir)
        commit = parent if rc == 0 else None
    return None


def migrate(project_dir: Path) -> None:
    """Idempotent housekeeping for an EXISTING repo: refresh .gitignore and stop tracking any
    now-ignored app-managed/junk files. Safe to call on every project open; a no-op once clean."""
    if not is_repo(project_dir):
        return
    for k, v in _GIT_CONFIG:                     # ensure identity exists before any commit
        _run(["config", k, v], project_dir)
    _ensure_gitignore(project_dir)
    _untrack_ignored(project_dir)


def _ensure_init(project_dir: Path) -> None:
    if not is_repo(project_dir):
        _run(["init"], project_dir)
    for k, v in _GIT_CONFIG:                     # always ensure identity (needed before commit)
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
        # A real project with no repository has never been snapshotted. Treat that state as
        # unsaved so clients keep the first-version action available; save_version() will
        # initialize Git and create v1 atomically when the user invokes it.
        return {"initialized": False, "dirty": True, "current": None}
    dirty = _is_dirty(project_dir)
    head = _head_commit(project_dir)
    current = _current_version_tag(project_dir) if head and not dirty else None
    return {"initialized": True, "dirty": dirty, "current": current}


def list_versions(project_dir: Path) -> list[dict]:
    """All saved versions (annotated tags), newest first."""
    if not is_repo(project_dir):
        return []
    head = _head_commit(project_dir)
    dirty = _is_dirty(project_dir)
    current_tag = _current_version_tag(project_dir) if head and not dirty else None
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
            "is_current": (tag == current_tag),
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
        existing = _current_version_tag(project_dir)
        if existing:
            return {"ok": True, "tag": existing, "skipped": True}
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
