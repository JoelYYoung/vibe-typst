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
from fnmatch import fnmatchcase
from pathlib import Path

# Things that change constantly and would otherwise keep the repo perpetually
# "dirty" (and trigger false discard prompts) — keep them out of version control.
_IGNORE_PATTERNS = [
    "/*.backup",
    "/.tcb/",
    "/.claude/",
    "/.mcp.json",
    "/.vibe-typst.json",       # app-managed project metadata
    "/.slide-comments.db",     # SQLite comment store (+ its WAL/SHM sidecars)
    "/.slide-comments.db-shm",
    "/.slide-comments.db-wal",
    "/.pdf-transcript.lock", # cross-process transcript transaction lock
    "/.pdf-replace.lock",    # legacy cross-process PDF replacement lock
    "/.pdf-project-write.lock",
    "/.pdf-replacement-journal.json",
    "/.pdf-journal-*",
    "/.pdf-txn-*",
    "/.pdf-render-*",
    "/.pdf-restore-*",
    "/.pdf-replacement-*",
    "/.pdf-primary-txn-*",
    "/.pdf-candidate-txn-*",
    "/.transcript-*",
    ".DS_Store",
    "Thumbs.db",
    # App-managed agent tooling — regenerated on EVERY project open (workdir.setup), so
    # versioning it makes the repo perpetually "dirty" and saves fire when the DECK itself is
    # unchanged. A version should capture the user's deck (.typ + assets), not this scaffolding.
    "/AGENTS.md",
    "/CLAUDE.md",
    "/AGENTS.md.backup-*",
    "/CLAUDE.md.backup-*",
    "/.codex/",
    "/.agent-home/",          # agent runtime state (auth/config/cache), if it lands in-project
    # Process crash dumps — a crashed codex/node (`--yolo`) can drop a multi-hundred-MB `core`.
    "/core",
    "/core.*",
    "/*.core",
    "/vgcore.*",
    "/*.heapsnapshot",
]

_GIT_CONFIG = [("user.email", "vibe@local"), ("user.name", "Vibe Typst")]
_US = "\x1f"  # unit separator for --format parsing
_HOUSEKEEPING_SUBJECT = "chore: stop tracking app-managed files"
_HOUSEKEEPING_TRAILER = "Vibe-Typst-Housekeeping: 1"
_HOUSEKEEPING_MESSAGE = f"{_HOUSEKEEPING_SUBJECT}\n\n{_HOUSEKEEPING_TRAILER}"


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


def _is_managed_path(path: str) -> bool:
    """Whether *path* is runtime state owned by Vibe Typst rather than deck content."""
    clean = path.strip("/")
    name = Path(clean).name
    for pattern in _IGNORE_PATTERNS:
        anchored = pattern.startswith("/")
        match_pattern = pattern.lstrip("/") if anchored else pattern
        if match_pattern.endswith("/"):
            prefix = match_pattern.rstrip("/")
            if clean == prefix or clean.startswith(prefix + "/"):
                return True
        elif anchored or "/" in match_pattern:
            if fnmatchcase(clean, match_pattern):
                return True
        elif fnmatchcase(name, match_pattern):
            return True
    return False


def _ensure_excludes(project_dir: Path) -> None:
    """Ignore runtime state locally without editing the user's versioned .gitignore."""
    raw, _, rc = _run(["rev-parse", "--git-path", "info/exclude"], project_dir)
    if rc != 0 or not raw:
        return
    exclude = Path(raw)
    if not exclude.is_absolute():
        exclude = project_dir / exclude
    existing = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    have = {l.strip() for l in existing}
    missing = [p for p in _IGNORE_PATTERNS if p not in have]
    if missing:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        header = ["", "# Vibe Typst — app-managed runtime state"] if existing else [
            "# Vibe Typst — app-managed runtime state"
        ]
        exclude.write_text("\n".join(existing + header + missing) + "\n", encoding="utf-8")


def _untrack_ignored(project_dir: Path) -> None:
    """Stop tracking app-managed files now covered by the repository's local excludes.

    User-authored ignore rules are deliberately out of scope: an ignored file is only removed
    when its path also matches Vibe Typst's explicit runtime-state allowlist.

    This covers app-managed configs committed by an older version, crash dumps, and comment
    databases. Without cleanup those volatile files keep the repo permanently "dirty" and
    trigger false discard prompts or spurious saves.

    The removal is committed through an isolated temporary index, so pre-existing staged deck
    edits remain staged instead of leaking into the housekeeping commit. Existing version tags
    remain immutable; status() recognizes a tag immediately behind housekeeping-only commits as
    the current deck version.
    """
    out, _, _ = _run(["ls-files", "-i", "-c", "--exclude-standard"], project_dir)
    ignored = [x.strip() for x in out.splitlines() if x.strip() and _is_managed_path(x)]
    old_head = _head_commit(project_dir)
    if old_head is None:
        return  # no history yet — save_version() will make the first commit
    with tempfile.TemporaryDirectory(prefix="vibe-typst-index-") as td:
        index_path = str(Path(td) / "index")
        index_env = {"GIT_INDEX_FILE": index_path}
        if _run(["read-tree", old_head], project_dir, env=index_env)[2] != 0:
            return
        for f in ignored:
            if _run(["update-index", "--force-remove", "--", f],
                    project_dir, env=index_env)[2] != 0:
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
        if _run(["update-index", "--force-remove", "--", f], project_dir)[2] != 0:
            return
    _, _, rc = _run(
        ["update-ref", "-m", "vibe-typst housekeeping", "HEAD", new_head, old_head],
        project_dir,
    )
    if rc != 0:
        return


def _is_housekeeping_commit(project_dir: Path, commit: str) -> bool:
    """Recognize only commits created by this module, never a matching user subject alone."""
    body, _, rc = _run(["show", "-s", "--format=%B", commit], project_dir)
    if rc != 0 or _HOUSEKEEPING_TRAILER not in body.splitlines():
        return False
    parent, _, rc = _run(["rev-parse", f"{commit}^"], project_dir)
    if rc != 0:
        return False
    changes, _, rc = _run(
        ["diff-tree", "--no-commit-id", "--name-status", "-r", parent, commit],
        project_dir,
    )
    if rc != 0 or not changes:
        return False
    for line in changes.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2 or parts[0] != "D" or not _is_managed_path(parts[1]):
            return False
    return True


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
        if not _is_housekeeping_commit(project_dir, commit):
            return None
        parent, _, rc = _run(["rev-parse", f"{commit}^"], project_dir)
        commit = parent if rc == 0 else None
    return None


def migrate(project_dir: Path) -> None:
    """Idempotent housekeeping for an EXISTING repo: refresh local excludes and stop tracking any
    now-ignored app-managed/junk files. Safe to call on every project open; a no-op once clean."""
    if not is_repo(project_dir):
        return
    for k, v in _GIT_CONFIG:                     # ensure identity exists before any commit
        _run(["config", k, v], project_dir)
    _ensure_excludes(project_dir)
    _untrack_ignored(project_dir)


def _ensure_init(project_dir: Path) -> None:
    if not is_repo(project_dir):
        _run(["init"], project_dir)
    for k, v in _GIT_CONFIG:                     # always ensure identity (needed before commit)
        _run(["config", k, v], project_dir)
    _ensure_excludes(project_dir)
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
    # A pair of snapshots can be tagged in the same second.  Use the numeric version tag as a
    # deterministic newest-first tiebreaker so a replacement reliably reports v2 before v1.
    out, _, rc = _run(["for-each-ref", "--sort=-creatordate", "--sort=-version:refname", "refs/tags",
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


def version_exists(project_dir: Path, tag: str) -> bool:
    """Return whether an exact, validated version tag resolves to a commit."""
    if not isinstance(tag, str) or re.fullmatch(r"v[1-9][0-9]*", tag) is None:
        return False
    _, _, rc = _run(["rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"], project_dir)
    return rc == 0


def version_tracks_path(
    project_dir: Path,
    tag: str,
    relative_path: str,
) -> tuple[bool, str | None]:
    """Return whether an exact project path exists in a validated version tree."""
    if (
        not isinstance(tag, str)
        or re.fullmatch(r"v[1-9][0-9]*", tag) is None
        or not isinstance(relative_path, str)
    ):
        return False, "invalid version path"
    relative = Path(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        return False, "invalid version path"
    canonical = relative.as_posix()
    out, err, rc = _run(
        [
            "--literal-pathspecs",
            "ls-tree",
            "-r",
            "-z",
            f"{tag}^{{commit}}",
            "--",
            canonical,
        ],
        project_dir,
    )
    if rc != 0:
        return False, err or "could not inspect version path"
    return bool(out), None


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
    out, err, rc = _run(["add", "-A"], project_dir)
    if rc != 0:
        return {"ok": False, "error": err or out or "could not stage project files"}
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
    out, err, rc = _run(["tag", "-a", tag, "-m", msg or tag], project_dir)
    if rc != 0:
        return {"ok": False, "error": err or out or "could not create version tag"}
    return {"ok": True, "tag": tag}


def _restore_commit_without_managed_files(
    project_dir: Path, commit: str
) -> tuple[str | None, str | None]:
    """Return a safe restore commit which cannot overwrite live runtime state.

    Old version tags may contain comment databases and agent configuration from before those
    paths were excluded. The tag remains immutable; an untagged housekeeping child removes only
    those known paths before checkout.
    """
    out, err, rc = _run(["ls-tree", "-r", "--name-only", commit], project_dir)
    if rc != 0:
        return None, err or "could not inspect version"
    managed = [path for path in out.splitlines() if _is_managed_path(path)]
    if not managed:
        return commit, None
    with tempfile.TemporaryDirectory(prefix="vibe-typst-restore-index-") as td:
        index_env = {"GIT_INDEX_FILE": str(Path(td) / "index")}
        if _run(["read-tree", commit], project_dir, env=index_env)[2] != 0:
            return None, "could not prepare version restore"
        for path in managed:
            if _run(["update-index", "--force-remove", "--", path],
                    project_dir, env=index_env)[2] != 0:
                return None, f"could not protect app-managed file: {path}"
        tree, err, rc = _run(["write-tree"], project_dir, env=index_env)
        if rc != 0:
            return None, err or "could not prepare protected version tree"
    safe_commit, err, rc = _run(
        ["commit-tree", tree, "-p", commit, "-m", _HOUSEKEEPING_MESSAGE],
        project_dir,
    )
    if rc != 0:
        return None, err or "could not prepare protected version"
    return safe_commit, None


def _tracked_managed_paths(project_dir: Path) -> tuple[list[str] | None, str | None]:
    out, err, rc = _run(["ls-files"], project_dir)
    if rc != 0:
        return None, err or "could not verify protected app-managed files"
    return [path for path in out.splitlines() if _is_managed_path(path)], None


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
    # Protect the CURRENT working copies first. A legacy HEAD may still track the live comment
    # database or generated agent state; checking out even a sanitized target would then delete
    # those paths. Migration removes them from the current index while leaving their ignored
    # working copies in place.
    migrate(project_dir)
    still_tracked, tracking_error = _tracked_managed_paths(project_dir)
    if tracking_error or still_tracked is None:
        return {"ok": False, "error": tracking_error or "could not verify protected files"}
    if still_tracked:
        return {
            "ok": False,
            "error": "could not protect live app-managed files: " + ", ".join(still_tracked),
        }
    safe_commit, error = _restore_commit_without_managed_files(project_dir, commit.strip())
    if error or not safe_commit:
        return {"ok": False, "error": error or "could not prepare version"}
    # Point master at a tree that never contains live comment/agent state. `-f` implements the
    # caller-confirmed discard for versioned deck files while leaving excluded runtime files.
    _, err, rc = _run(["checkout", "-f", "-B", "master", safe_commit], project_dir)
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
