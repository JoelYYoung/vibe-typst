"""Recording HTTP endpoints; document resolution is owned by the workspace app."""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile
from starlette.requests import Request as StarletteRequest

import presentation_recording as recording
import audio_processing


def router(resolve, prepare=None):
    routes = APIRouter(prefix="/api/recording")

    def target(project_id):
        try:
            return resolve(project_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @routes.get("")
    def state(project_id: Optional[str] = None):
        root, pages = target(project_id)
        try:
            return recording.list_takes(root, pages)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @routes.post("/prepare")
    async def prepare_document(project_id: Optional[str] = None):
        try:
            if prepare:
                await prepare(project_id)
            return state(project_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @routes.put("/pages/{page}")
    async def save(page: int, request: Request, project_id: Optional[str] = None):
        root, pages = target(project_id)
        if not 1 <= page <= len(pages):
            raise HTTPException(400, "invalid slide page")
        received = 0

        async def receive():
            nonlocal received
            message = await request.receive()
            received += len(message.get("body", b""))
            if received > recording.MAX_CLIP_BYTES + recording.MAX_METADATA_BYTES + 65536:
                raise HTTPException(413, "recording exceeds 512 MB; record a shorter take")
            return message

        bounded = StarletteRequest(request.scope, receive=receive)
        staged = None
        try:
            async with bounded.form(max_files=1, max_fields=1, max_part_size=recording.MAX_METADATA_BYTES) as form:
                video = form.get("video")
                raw = form.get("metadata")
                if not isinstance(video, UploadFile) or not isinstance(raw, str) or len(form.multi_items()) != 2:
                    raise ValueError("send one video and its recording metadata")
                if len(raw.encode()) > recording.MAX_METADATA_BYTES:
                    raise HTTPException(413, "pointer metadata is too large")
                metadata = recording.validate_metadata(json.loads(raw), page)
                slide = pages[page - 1]
                if slide.get("binding_required") and not slide.get("recording_id"):
                    raise ValueError("reload recording mode to prepare stable slide bindings")
                if metadata.get("recording_id") != slide.get("recording_id"):
                    raise HTTPException(409, "slide moved or changed; reload before recording")
                if metadata["name"] != slide["name"] or metadata["token"] != slide["token"]:
                    raise HTTPException(409, "slide content changed; re-record this page")
                fd, name = tempfile.mkstemp(prefix=".upload-", dir=root)
                staged = Path(name)
                size = 0
                with os.fdopen(fd, "wb") as stream:
                    while chunk := await video.read(1024 * 1024):
                        size += len(chunk)
                        if size > recording.MAX_CLIP_BYTES:
                            raise HTTPException(413, "recording exceeds 512 MB; record a shorter take")
                        stream.write(chunk)
                if not size:
                    raise ValueError("recording is empty")
                await asyncio.to_thread(recording.validate_media, staged)
                # A different tab may have changed the document while the upload streamed.
                now_root, now_pages = target(project_id)
                if now_root != root or page > len(now_pages) or now_pages[page - 1] != slide:
                    raise HTTPException(409, "slide content changed; re-record this page")
                return recording.save_take(root, page, staged, metadata, slide.get("recording_id"))
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            if staged:
                staged.unlink(missing_ok=True)

    @routes.delete("/pages/{page}")
    async def clear(page: int, request: Request, project_id: Optional[str] = None):
        root, pages = target(project_id)
        if not 1 <= page <= len(pages):
            raise HTTPException(400, "invalid slide page")
        try:
            body = await request.body()
            data = json.loads(body) if body else {}
            if not isinstance(data, dict):
                raise ValueError('invalid clear request')
            expected_take = data.get('take')
            if expected_take is not None and (not isinstance(expected_take, str) or not recording._TAKE.fullmatch(expected_take)):
                raise ValueError('invalid recording take')
            recording.clear_take(root, page, pages[page - 1].get("recording_id"), expected_take)
            return {"ok": True}
        except (ValueError, OSError) as exc:
            raise HTTPException(409 if str(exc).startswith('recording moved') else 400, str(exc)) from exc

    @routes.get("/pages/{page}/video")
    def video(page: int, project_id: Optional[str] = None, take: Optional[str] = None):
        root, pages = target(project_id)
        if not 1 <= page <= len(pages):
            raise HTTPException(404, "slide not found")
        try:
            with recording.locked(root):
                data = recording._read_take(root, page, pages[page - 1].get("recording_id"))
                if not data:
                    raise ValueError("recording not found")
                if take is not None and data['take'] != take:
                    raise ValueError('recording moved or changed; reload the preview')
                path = recording.preview_media_path(root, data)
                # FileResponse supports media ranges. Takes remain immutable until replacement.
                return FileResponse(path, media_type=data["mime"].split(";")[0],
                                    headers={"Cache-Control": "no-store"})
        except (ValueError, OSError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @routes.post("/exports")
    async def export(request: Request, project_id: Optional[str] = None):
        root, pages = target(project_id)
        staged = None
        try:
            if request.headers.get('content-type', '').startswith('multipart/form-data'):
                received = 0
                async def receive():
                    nonlocal received
                    message = await request.receive()
                    received += len(message.get('body', b''))
                    if received > audio_processing.MAX_REFERENCE_BYTES + 65536:
                        raise HTTPException(413, 'Reference audio exceeds 20 MB.')
                    return message
                bounded = StarletteRequest(request.scope, receive=receive)
                async with bounded.form(max_files=1, max_fields=1, max_part_size=16384) as form:
                    upload, raw = form.get('reference'), form.get('options')
                    if not isinstance(upload, UploadFile) or not isinstance(raw, str) or len(form.multi_items()) != 2:
                        raise ValueError('send one reference audio file and export options')
                    options = json.loads(raw)
                    fd, name = tempfile.mkstemp(prefix='.reference-', dir=root)
                    staged = Path(name)
                    size = 0
                    with os.fdopen(fd, 'wb') as stream:
                        while chunk := await upload.read(1024 * 1024):
                            size += len(chunk)
                            if size > audio_processing.MAX_REFERENCE_BYTES:
                                raise HTTPException(413, 'Reference audio exceeds 20 MB.')
                            stream.write(chunk)
                    if not size:
                        raise ValueError('Reference audio is empty.')
            else:
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 16384:
                        raise HTTPException(413, 'Export options are too large.')
                options = json.loads(body) if body else {}
            if not isinstance(options, dict) or set(options) - {'skip_pages', 'audio'}:
                raise ValueError('invalid export options')
            return await asyncio.to_thread(recording.start_export, root, pages,
                                           options.get('skip_pages', []), options.get('audio'), staged)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            if staged:
                staged.unlink(missing_ok=True)

    @routes.get('/audio-models')
    def audio_models():
        return audio_processing.capabilities()

    @routes.get("/exports/{job_id}")
    def status(job_id: str, project_id: Optional[str] = None):
        root, _ = target(project_id)
        try:
            return recording.public_job(recording.get_job(root, job_id))
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @routes.get("/exports/{job_id}/video")
    def download(job_id: str, project_id: Optional[str] = None):
        root, _ = target(project_id)
        try:
            job = recording.get_job(root, job_id)
            if job["status"] != "complete":
                raise ValueError("video export is not ready")
            return FileResponse(job["path"], media_type="video/mp4", filename="presentation.mp4",
                                headers={"Cache-Control": "no-store"})
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    return routes
