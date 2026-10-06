"""Project-owned, atomic slide takes and bounded background MP4 export."""
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

MAX_CLIP_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 16 * 1024 * 1024
_jobs = {}
_jobs_lock = threading.Lock()
_export_slot = threading.BoundedSemaphore(1)
_TAKE = re.compile(r"[a-f0-9]{32}$")
_BINDING = re.compile(r"[a-f0-9]{32}-[0-9]+$")


def directory(project: Path, document: Path) -> Path:
    project = project.resolve()
    key = hashlib.sha256(str(document.resolve().relative_to(project)).encode()).hexdigest()[:24]
    target = project
    for name in (".tcb", "recordings", key):
        target = target / name
        if target.is_symlink():
            raise ValueError("recording storage may not contain symlinks")
        target.mkdir(exist_ok=True)
    return target


@contextmanager
def locked(root: Path):
    lock = root / ".lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def write_json(path: Path, value):
    fd, name = tempfile.mkstemp(prefix=".json-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _take_file(root: Path, page: int, recording_id=None):
    if recording_id is not None:
        if not isinstance(recording_id, str) or not _BINDING.fullmatch(recording_id):
            raise ValueError("invalid slide recording identity")
        return root / f"slide-{recording_id}.json"
    return root / f"page-{page}.json"


def _read_take(root: Path, page: int, recording_id=None):
    path = _take_file(root, page, recording_id)
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError("invalid recording metadata")
    data = json.loads(path.read_text())
    if not _TAKE.fullmatch(str(data.get("take", ""))):
        raise ValueError("invalid recording take")
    return data


def media_path(root: Path, data) -> Path:
    path = root / data["take"] / "video"
    if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError("recording media is missing")
    return path


def preview_media_path(root: Path, data) -> Path:
    """Called under the take lock; retain originals and cache a seekable container."""
    original = media_path(root, data)
    if not shutil.which('ffmpeg'):
        return original
    container = 'mp4' if data['mime'].startswith('video/mp4') else 'webm'
    preview = original.parent / ('preview-seekable.' + container)
    if preview.is_symlink():
        raise ValueError('invalid recording preview')
    if preview.is_file():
        return preview
    fd, name = tempfile.mkstemp(prefix='.preview-', suffix='.' + container, dir=original.parent)
    os.close(fd)
    try:
        options = ['-movflags', '+faststart'] if container == 'mp4' else ['-cues_to_front', '1']
        # Stream copy fills duration and seek indexes without changing AV pixels/audio.
        _run_ffmpeg(['-i', str(original), '-map', '0:v:0', '-map', '0:a:0',
                     '-c', 'copy', *options, '-f', container, name])
        os.replace(name, preview)
    finally:
        Path(name).unlink(missing_ok=True)
    return preview


def validate_metadata(value, page: int):
    if not isinstance(value, dict):
        raise ValueError("invalid recording metadata")
    duration = value.get("duration")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or not 0.1 <= duration <= 7200):
        raise ValueError("recording must be between 0.1 seconds and 2 hours")
    token = value.get("token")
    name = value.get("name")
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{12}", token):
        raise ValueError("invalid slide content token")
    if not isinstance(name, str) or not re.fullmatch(r"page-[1-9][0-9]*\.(svg|png)", name):
        raise ValueError("invalid slide name")
    pointer = value.get("pointer", [])
    if not isinstance(pointer, list) or len(pointer) > 220000:
        raise ValueError("too many pointer samples")
    previous = -1
    for point in pointer:
        if not isinstance(point, dict):
            raise ValueError("invalid pointer sample")
        t = point.get("t")
        if (isinstance(t, bool) or not isinstance(t, (int, float))
                or not math.isfinite(t) or not previous <= t <= duration + 0.1):
            raise ValueError("invalid pointer timestamp")
        previous = t
        if point.get("x") is None and point.get("y") is None:
            continue
        for axis in ("x", "y"):
            coordinate = point.get(axis)
            if (isinstance(coordinate, bool) or not isinstance(coordinate, (int, float))
                    or not math.isfinite(coordinate) or not 0 <= coordinate <= 1):
                raise ValueError("invalid pointer position")
    mime = value.get("mime", "")
    if not isinstance(mime, str) or not mime.startswith(("video/webm", "video/mp4")):
        raise ValueError("unsupported recording format")
    result = {"page": page, "name": name, "token": token, "duration": duration,
              "mime": mime, "pointer": pointer}
    identity = value.get("recording_id")
    if identity is not None:
        _take_file(Path('.'), page, identity)
        result["recording_id"] = identity
    return result


def save_take(root: Path, page: int, staged: Path, metadata, recording_id=None):
    """Commit only after the entire new take exists; failures leave the old take intact."""
    take = uuid.uuid4().hex
    target = root / take
    with locked(root):
        old = _read_take(root, page, recording_id)
        target.mkdir()
        try:
            os.replace(staged, target / "video")
            data = {**metadata, "take": take, "saved_at": time.time()}
            if recording_id is not None:
                data["recording_id"] = recording_id
            write_json(target / "metadata.json", data)
            write_json(_take_file(root, page, recording_id), data)
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        if old:
            shutil.rmtree(root / old["take"], ignore_errors=True)
    return {key: value for key, value in data.items() if key != "pointer"}


def list_takes(root: Path, pages):
    result = []
    with locked(root):
        for page, slide in enumerate(pages, 1):
            recording_id = slide.get("recording_id")
            data = _read_take(root, page, recording_id)
            if data:
                media_path(root, data)
                result.append({**{key: value for key, value in data.items() if key != "pointer"},
                               "page": page, "name": slide["name"],
                               "stale": (not recording_id and data["name"] != slide["name"]) or data["token"] != slide["token"]})
    with _jobs_lock:
        job = next((dict(job) for job in reversed(list(_jobs.values())) if job["root"] == str(root)), None)
    return {"takes": result, "slides": pages, "export_available": shutil.which("ffmpeg") is not None,
            "export": public_job(job) if job else None}


def clear_take(root: Path, page: int, recording_id=None, expected_take=None):
    """Remove one page; pinned inputs of in-flight exports remain readable."""
    with locked(root):
        old = _read_take(root, page, recording_id)
        if expected_take is not None and (not old or old["take"] != expected_take):
            raise ValueError("recording moved or changed; reload before clearing")
        _take_file(root, page, recording_id).unlink(missing_ok=True)
        if old:
            shutil.rmtree(root / old["take"], ignore_errors=True)


def public_job(job):
    return {key: value for key, value in job.items() if key not in {"root", "path"}}


def get_job(root: Path, job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or job["root"] != str(root):
            raise ValueError("export not found; retry if the server restarted")
        return dict(job)


def _run_ffmpeg(args):
    process = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=7200)
    if process.returncode:
        raise ValueError("Video conversion failed. Check that the page recording has audio and video, then retry.")


def validate_media(path: Path):
    if not shutil.which("ffprobe"):
        return  # Recording still works on installations without the optional exporter.
    process = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
                             capture_output=True, timeout=30)
    if process.returncode:
        raise ValueError("the recording is not a readable video; the previous take is preserved")
    streams = json.loads(process.stdout).get("streams", [])
    if not any(stream.get("codec_type") == "audio" for stream in streams):
        raise ValueError("recording has no microphone audio; the previous take is preserved")
    if not any(stream.get("codec_type") == "video" and 0 < stream.get("width", 0) <= 4096
               and 0 < stream.get("height", 0) <= 4096 for stream in streams):
        raise ValueError("recording has no valid slide video; the previous take is preserved")


def encode_mp4(clips, durations, work: Path, progress=lambda value: None):
    parts = []
    for index, (clip, duration) in enumerate(zip(clips, durations)):
        part = work / f"part-{index}.mp4"
        # Re-encode each independently recorded container: raw WebM concatenation does not
        # repair timestamps or changing codec settings. Force one interoperable AV format.
        _run_ffmpeg(["-i", str(clip), "-map", "0:v:0", "-map", "0:a:0",
                     "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,tpad=stop_mode=clone:stop_duration=1",
                     "-af", "aresample=48000:async=1:first_pts=0,apad", "-t", str(duration),
                     "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-threads", "2", str(part)])
        parts.append(part)
        progress(round((index + 1) / len(clips) * 90))
    listing = work / "concat.txt"
    listing.write_text("".join(f"file 'part-{index}.mp4'\n" for index in range(len(parts))))
    output = work / "presentation.mp4"
    _run_ffmpeg(["-f", "concat", "-safe", "1", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(output)])
    return output


def start_export(root: Path, pages, skip_pages=()):
    if not shutil.which("ffmpeg"):
        raise ValueError("MP4 export requires FFmpeg on the server")
    if not pages:
        raise ValueError("there are no slides to export")
    if not isinstance(skip_pages, (list, tuple)) or any(
        type(page) is not int or not 1 <= page <= len(pages) for page in skip_pages
    ):
        raise ValueError("invalid pages to skip")
    if not _export_slot.acquire(blocking=False):
        raise ValueError("another video is exporting; wait for it to finish")
    work = None
    try:
        work = Path(tempfile.mkdtemp(prefix=".export-", dir=root))
        durations, clips, takes, included, skipped = [], [], [], [], []
        with locked(root):
            for page, slide in enumerate(pages, 1):
                recording_id = slide.get("recording_id")
                data = _read_take(root, page, recording_id)
                if not data and page in skip_pages:
                    skipped.append(page)
                    continue
                if not data or (not recording_id and data["name"] != slide["name"]) or data["token"] != slide["token"]:
                    raise ValueError(f"record or re-record page {page} before exporting")
                clip = work / f"clip-{page}"
                # Pin an immutable snapshot so re-recording cannot change an in-flight export.
                os.link(media_path(root, data), clip)
                clips.append(clip)
                durations.append(data["duration"])
                takes.append(data["take"])
                included.append(page)
        if not clips:
            raise ValueError("record at least one page before exporting")
        job_id = uuid.uuid4().hex
        with _jobs_lock:
            # Keep one completed output per document; an active download holds its inode.
            for old_id, old in list(_jobs.items()):
                if old["root"] == str(root) and old["status"] != "running":
                    if old.get("path"):
                        Path(old["path"]).unlink(missing_ok=True)
                    del _jobs[old_id]
            _jobs[job_id] = {"id": job_id, "root": str(root), "status": "running", "progress": 0,
                             "takes": takes, "pages": included, "skipped_pages": skipped,
                             "slides": [{key: slide.get(key) for key in ("name", "token", "recording_id")} for slide in pages]}

        def update(**values):
            with _jobs_lock:
                _jobs[job_id].update(values)

        def run():
            try:
                output = encode_mp4(clips, durations, work, lambda value: update(progress=value))
                target = root / f"export-{job_id}.mp4"
                os.replace(output, target)
                update(status="complete", progress=100, path=str(target))
            except Exception as exc:
                message = str(exc) if isinstance(exc, ValueError) else "Export failed or timed out; please retry."
                update(status="failed", error=message)
            finally:
                shutil.rmtree(work, ignore_errors=True)
                _export_slot.release()

        threading.Thread(target=run, daemon=True, name="presentation-export").start()
        return public_job(get_job(root, job_id))
    except BaseException:
        if work:
            shutil.rmtree(work, ignore_errors=True)
        _export_slot.release()
        raise
