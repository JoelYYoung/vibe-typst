"""One-time, bounded HTTP transfer capabilities for remote MCP clients."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

import mcp_store
from mcp_errors import McpServiceError
from pat_store import PatIdentity


_SAFE_USERNAME = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_NO_STORE = {"Cache-Control": "no-store"}


def _authorization(request: Request, scheme: str) -> str | None:
    value = request.headers.get("authorization", "")
    prefix = f"{scheme} "
    if not value.lower().startswith(prefix.lower()):
        return None
    secret = value[len(prefix):].strip()
    return secret or None


def _upload_directory(workspace_base: Path, username: str) -> Path:
    if _SAFE_USERNAME.fullmatch(username) is None:
        raise PermissionError("workspace identity is invalid")
    base = Path(workspace_base)
    if base.is_symlink():
        raise PermissionError("workspace base may not be a symbolic link")
    base = base.resolve()
    user_root = base / username
    private_root = user_root / ".tcb"
    uploads = private_root / "uploads"
    for directory in (user_root, private_root, uploads):
        if directory.is_symlink():
            raise PermissionError(
                "upload directory may not be a symbolic link"
            )
        directory.mkdir(exist_ok=True)
        if not directory.is_dir():
            raise NotADirectoryError(str(directory))
    return uploads


def _error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "retryable": False,
            },
        },
        status_code=status,
        headers=_NO_STORE,
    )


def stage_inline_upload(
    db_path: Path,
    workspace_base: Path,
    session: mcp_store.UploadSession,
    content: bytes,
) -> Path:
    if (
        len(content) != session.size
        or hashlib.sha256(content).hexdigest() != session.sha256
    ):
        raise McpServiceError(
            "CHECKSUM_MISMATCH",
            "inline upload metadata does not match its content",
        )
    upload_dir = _upload_directory(workspace_base, session.username)
    part_path = upload_dir / f"{session.id}.part"
    ready_path = upload_dir / f"{session.id}.ready"
    if ready_path.exists() or ready_path.is_symlink():
        raise McpServiceError(
            "UPLOAD_ALREADY_USED", "upload capability was already used"
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(part_path, flags, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(part_path, ready_path)
        mcp_store.mark_upload_received(
            db_path, session.id, len(content)
        )
        return ready_path
    except Exception:
        part_path.unlink(missing_ok=True)
        ready_path.unlink(missing_ok=True)
        raise


def create_transfer_router(
    db_path: Path,
    workspace_base: Path,
    gateway,
) -> APIRouter:
    router = APIRouter()

    @router.put("/mcp-upload/{upload_id}")
    async def upload(upload_id: str, request: Request):
        capability = _authorization(request, "Upload")
        if capability is None:
            return _error_response(
                401, "AUTH_REQUIRED", "upload authorization is required"
            )
        try:
            session = mcp_store.authorize_upload(
                db_path, upload_id, capability
            )
        except McpServiceError as exc:
            status = 409 if exc.code == "UPLOAD_ALREADY_USED" else 404
            return _error_response(status, exc.code, exc.message)

        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError:
                mcp_store.fail_upload(db_path, upload_id, "CHECKSUM_MISMATCH")
                return _error_response(
                    400, "CHECKSUM_MISMATCH", "invalid content length"
                )
            if declared_size > session.size:
                mcp_store.fail_upload(db_path, upload_id, "FILE_TOO_LARGE")
                return _error_response(
                    413, "FILE_TOO_LARGE", "upload exceeds declared size"
                )

        part_path: Path | None = None
        ready_path: Path | None = None
        received = 0
        digest = hashlib.sha256()
        try:
            upload_dir = _upload_directory(
                workspace_base, session.username
            )
            part_path = upload_dir / f"{session.id}.part"
            ready_path = upload_dir / f"{session.id}.ready"
            if ready_path.exists() or ready_path.is_symlink():
                raise McpServiceError(
                    "UPLOAD_ALREADY_USED",
                    "upload capability was already used",
                )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(part_path, flags, 0o600)
            with os.fdopen(fd, "wb") as output:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > session.size:
                        raise McpServiceError(
                            "FILE_TOO_LARGE",
                            "upload exceeds declared size",
                        )
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if received != session.size:
                raise McpServiceError(
                    "CHECKSUM_MISMATCH",
                    "upload length does not match declared size",
                )
            if digest.hexdigest() != session.sha256:
                raise McpServiceError(
                    "CHECKSUM_MISMATCH",
                    "upload checksum does not match",
                )
            os.replace(part_path, ready_path)
            part_path = None
            mcp_store.mark_upload_received(
                db_path, session.id, received
            )
        except McpServiceError as exc:
            if part_path is not None:
                part_path.unlink(missing_ok=True)
            if ready_path is not None:
                ready_path.unlink(missing_ok=True)
            mcp_store.fail_upload(db_path, upload_id, exc.code)
            status = 413 if exc.code == "FILE_TOO_LARGE" else 400
            if exc.code == "UPLOAD_ALREADY_USED":
                status = 409
            return _error_response(status, exc.code, exc.message)
        except Exception:
            if part_path is not None:
                part_path.unlink(missing_ok=True)
            if ready_path is not None:
                ready_path.unlink(missing_ok=True)
            mcp_store.fail_upload(db_path, upload_id, "BACKEND_ERROR")
            return _error_response(
                500, "BACKEND_ERROR", "upload could not be stored"
            )
        return JSONResponse(
            {
                "ok": True,
                "upload_id": session.id,
                "size": received,
                "sha256": digest.hexdigest(),
            },
            headers=_NO_STORE,
        )

    @router.get("/mcp-download/{download_id}")
    async def download(download_id: str, request: Request):
        capability = _authorization(request, "Download")
        if capability is None:
            return _error_response(
                401, "AUTH_REQUIRED", "download authorization is required"
            )
        if gateway is None:
            return _error_response(
                503, "BACKEND_ERROR", "download service is unavailable"
            )
        try:
            session = mcp_store.authorize_download(
                db_path, download_id, capability
            )
        except McpServiceError as exc:
            status = 409 if exc.code == "UPLOAD_ALREADY_USED" else 404
            return _error_response(status, exc.code, exc.message)

        identity = PatIdentity(
            token_id=session.token_id,
            user_id=session.user_id,
            username=session.username,
            port=session.port,
            scopes=frozenset(),
            expires_at=None,
        )
        manager = gateway.stream_response(
            identity, session.backend_path
        )
        try:
            response = await manager.__aenter__()
        except McpServiceError as exc:
            return _error_response(503, exc.code, exc.message)
        except Exception:
            return _error_response(
                503, "WORKSPACE_UNAVAILABLE", "workspace is unavailable"
            )
        if response.status_code >= 400:
            await manager.__aexit__(None, None, None)
            return _error_response(
                502, "BACKEND_ERROR", "download source was unavailable"
            )
        try:
            mcp_store.claim_download(
                db_path, download_id, capability
            )
        except McpServiceError as exc:
            await manager.__aexit__(None, None, None)
            status = 409 if exc.code == "UPLOAD_ALREADY_USED" else 404
            return _error_response(status, exc.code, exc.message)

        async def stream_body():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                mcp_store.finish_download(db_path, download_id)
                await manager.__aexit__(None, None, None)

        headers = {
            **_NO_STORE,
            "Content-Disposition": (
                "attachment; filename*=UTF-8''"
                + quote(session.filename, safe="")
            ),
        }
        content_length = response.headers.get("content-length")
        if content_length is not None:
            headers["Content-Length"] = content_length
        media_type = response.headers.get(
            "content-type", "application/octet-stream"
        )
        return StreamingResponse(
            stream_body(),
            media_type=media_type,
            headers=headers,
        )

    return router
