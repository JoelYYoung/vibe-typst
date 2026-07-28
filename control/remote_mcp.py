"""PAT-authenticated, project-level MCP server for Vibe Typst."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import time
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from pydantic import AnyHttpUrl
from typing_extensions import TypedDict

import mcp_store
import pat_store
from mcp_transfer import stage_inline_upload
from mcp_errors import McpServiceError
from mcp_limits import TokenLimiter
from pat_store import PatIdentity
from workspace_gateway import WorkspaceGateway


class RemoteToolResult(TypedDict, total=False):
    ok: bool
    error: dict[str, Any]
    projects: list[dict[str, Any]]
    project: dict[str, Any]
    project_handle: str
    capabilities: list[str]
    web_url: str
    project_id: str | None
    context_version: str


def _protocol_result(result: RemoteToolResult) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            )
        ],
        structuredContent=dict(result),
        isError=not bool(result.get("ok")),
    )


class PatTokenVerifier(TokenVerifier):
    def __init__(self, db_path: Path):
        self.db_path = db_path

    async def verify_token(self, token: str) -> AccessToken | None:
        identity = pat_store.authenticate(self.db_path, token)
        if identity is None:
            return None
        return AccessToken(
            token=token,
            client_id=identity.token_id,
            subject=identity.user_id,
            scopes=sorted(identity.scopes),
            expires_at=(
                int(identity.expires_at)
                if identity.expires_at is not None
                else None
            ),
            claims={
                "token_id": identity.token_id,
                "user_id": identity.user_id,
                "username": identity.username,
                "port": identity.port,
            },
        )


def capabilities_for(project_type: str) -> list[str]:
    if project_type == "typst":
        return ["files", "slides", "typst_document", "transcripts", "comments"]
    if project_type == "pdf":
        return ["files", "slides", "pdf_document", "transcripts"]
    raise McpServiceError(
        "CAPABILITY_NOT_AVAILABLE", "unsupported project type"
    )


class _RemoteProjectService:
    def __init__(
        self,
        db_path: Path,
        gateway: WorkspaceGateway,
        limiter: TokenLimiter | None = None,
        workspace_base: Path = Path("/workspaces"),
    ):
        self.db_path = db_path
        self.gateway = gateway
        self.limiter = limiter or TokenLimiter()
        self.workspace_base = Path(workspace_base)

    @staticmethod
    def _identity() -> PatIdentity:
        access = get_access_token()
        if access is None:
            raise McpServiceError("AUTH_REQUIRED", "authentication is required")
        claims = access.claims or {}
        token_id = claims.get("token_id")
        user_id = claims.get("user_id")
        username = claims.get("username")
        port = claims.get("port")
        if (
            not isinstance(token_id, str)
            or not isinstance(user_id, str)
            or not isinstance(username, str)
            or isinstance(port, bool)
            or not isinstance(port, int)
        ):
            raise McpServiceError("TOKEN_INVALID", "token identity is invalid")
        return PatIdentity(
            token_id=token_id,
            user_id=user_id,
            username=username,
            port=port,
            scopes=frozenset(access.scopes),
            expires_at=(
                float(access.expires_at)
                if access.expires_at is not None
                else None
            ),
        )

    async def _run(
        self,
        tool_name: str,
        required_scope: str,
        operation: Callable[[PatIdentity], Awaitable[RemoteToolResult]],
        *,
        mutating: bool = False,
        project_id: str | None = None,
        targets: tuple[str, ...] = (),
        audit_context: dict[str, Any] | None = None,
    ) -> RemoteToolResult:
        started_at = time.time()
        correlation_id = secrets.token_hex(12)
        identity = None
        permit = None
        result: RemoteToolResult
        outcome = "error"
        error_code = None
        try:
            identity = self._identity()
            if required_scope not in identity.scopes:
                raise McpServiceError(
                    "SCOPE_DENIED",
                    f"scope '{required_scope}' is required",
                )
            permit = await self.limiter.acquire(identity.token_id)
            result = await operation(identity)
            outcome = "ok"
        except McpServiceError as exc:
            error_code = exc.code
            result = exc.as_dict()
        except Exception:
            error_code = "BACKEND_ERROR"
            result = McpServiceError(
                "BACKEND_ERROR", "project operation failed"
            ).as_dict()
        finally:
            if permit is not None:
                permit.release()

        if mutating and identity is not None:
            try:
                audit_project_id = (
                    audit_context.get("project_id", project_id)
                    if audit_context is not None
                    else project_id
                )
                audit_targets = (
                    audit_context.get("targets", targets)
                    if audit_context is not None
                    else targets
                )
                mcp_store.record_audit(
                    self.db_path,
                    mcp_store.AuditEvent(
                        user_id=identity.user_id,
                        token_id=identity.token_id,
                        tool_name=tool_name,
                        project_id=audit_project_id,
                        targets=audit_targets,
                        started_at=started_at,
                        completed_at=time.time(),
                        outcome=outcome,
                        error_code=error_code,
                        correlation_id=correlation_id,
                    ),
                )
            except Exception:
                if outcome == "ok":
                    result = McpServiceError(
                        "BACKEND_ERROR", "operation audit could not be recorded"
                    ).as_dict()
        return result

    async def _handled_project(
        self, identity: PatIdentity, project_handle: str
    ) -> tuple[mcp_store.Lease, dict, dict]:
        context = await self.gateway.active_context(identity)
        current_project_id = context.get("project_id")
        current_context = context.get("context_version")
        if not isinstance(current_context, str):
            raise McpServiceError(
                "BACKEND_ERROR", "workspace returned an invalid project context"
            )
        lease = mcp_store.validate_lease(
            self.db_path,
            project_handle,
            identity,
            current_project_id if isinstance(current_project_id, str) else "",
            current_context,
        )
        project = context.get("active_project")
        if (
            not isinstance(project, dict)
            or project.get("id") != lease.project_id
        ):
            raise McpServiceError(
                "PROJECT_CONTEXT_CHANGED",
                "the active project context changed; open the project again",
            )
        return lease, project, context

    @staticmethod
    def _relative_path(path: str) -> str:
        if (
            not isinstance(path, str)
            or not path
            or len(path) > 1024
            or "\x00" in path
            or "\\" in path
        ):
            raise McpServiceError(
                "PATH_NOT_ALLOWED", "path is not allowed"
            )
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or path == "." or ".." in parsed.parts:
            raise McpServiceError(
                "PATH_NOT_ALLOWED", "path is not allowed"
            )
        return parsed.as_posix()

    @staticmethod
    def _opaque_id(value: str, label: str) -> str:
        if (
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{32}", value) is None
        ):
            raise McpServiceError(
                "PATH_NOT_ALLOWED", f"{label} is invalid"
            )
        return value

    @staticmethod
    def _checked_backend_result(
        lease: mcp_store.Lease, body: dict
    ) -> RemoteToolResult:
        if (
            body.get("project_id") != lease.project_id
            or body.get("context_version") != lease.context_version
        ):
            raise McpServiceError(
                "PROJECT_CONTEXT_CHANGED",
                "the active project context changed; open the project again",
            )
        return {
            "ok": True,
            **{
                key: value
                for key, value in body.items()
                if key not in {"project_id", "context_version", "ok"}
            },
        }

    async def list_projects(self) -> RemoteToolResult:
        async def operation(identity):
            projects = await self.gateway.list_projects(identity)
            return {"ok": True, "projects": projects}

        return await self._run(
            "list_projects", "projects:read", operation
        )

    async def create_typst_project(self, name: str) -> RemoteToolResult:
        audit_context = {}

        async def operation(identity):
            project = await self.gateway.create_typst_project(identity, name)
            audit_context["project_id"] = project["id"]
            audit_context["targets"] = (f"project:{project['id']}",)
            return {"ok": True, "project": project}

        return await self._run(
            "create_typst_project",
            "projects:write",
            operation,
            mutating=True,
            audit_context=audit_context,
        )

    async def open_project(self, project_id: str) -> RemoteToolResult:
        async def operation(identity):
            opened = await self.gateway.open_project(identity, project_id)
            project = opened["project"]
            _, raw_handle = mcp_store.issue_lease(
                self.db_path,
                identity,
                project["id"],
                opened["context_version"],
            )
            return {
                "ok": True,
                "project": project,
                "project_handle": raw_handle,
                "capabilities": capabilities_for(
                    project.get("type", "typst")
                ),
                "web_url": self.gateway.project_web_url(project["id"]),
            }

        return await self._run(
            "open_project",
            "projects:read",
            operation,
            mutating=True,
            project_id=project_id,
            targets=(f"project:{project_id}",),
        )

    async def get_project(self, project_handle: str) -> RemoteToolResult:
        async def operation(identity):
            _, project, context = await self._handled_project(
                identity, project_handle
            )
            return {
                "ok": True,
                "project": project,
                "project_id": context["project_id"],
                "context_version": context["context_version"],
            }

        return await self._run(
            "get_project", "projects:read", operation
        )

    async def rename_project(
        self, project_handle: str, name: str
    ) -> RemoteToolResult:
        audit_context = {}

        async def operation(identity):
            lease, _, _ = await self._handled_project(
                identity, project_handle
            )
            audit_context["project_id"] = lease.project_id
            audit_context["targets"] = (f"project:{lease.project_id}",)
            project = await self.gateway.request(
                identity,
                "PATCH",
                f"/api/projects/{quote(lease.project_id, safe='')}",
                json={"name": name},
            )
            return {"ok": True, "project": project}

        return await self._run(
            "rename_project",
            "projects:write",
            operation,
            mutating=True,
            audit_context=audit_context,
        )

    async def close_project(
        self, project_handle: str
    ) -> RemoteToolResult:
        audit_context = {}

        async def operation(identity):
            lease, _, _ = await self._handled_project(
                identity, project_handle
            )
            audit_context["project_id"] = lease.project_id
            audit_context["targets"] = (f"project:{lease.project_id}",)
            closed = await self.gateway.request(
                identity, "POST", "/api/projects/close"
            )
            return {
                "ok": True,
                "project_id": closed.get("project_id"),
                "context_version": closed.get("context_version", ""),
            }

        return await self._run(
            "close_project",
            "projects:read",
            operation,
            mutating=True,
            audit_context=audit_context,
        )

    async def list_files(self, project_handle: str) -> RemoteToolResult:
        async def operation(identity):
            lease, _, _ = await self._handled_project(
                identity, project_handle
            )
            body = await self.gateway.request(
                identity, "GET", "/api/agent/files"
            )
            return self._checked_backend_result(lease, body)

        return await self._run("list_files", "files:read", operation)

    async def read_text_file(
        self,
        project_handle: str,
        path: str,
        offset: int = 1,
        limit: int = 120,
    ) -> RemoteToolResult:
        async def operation(identity):
            lease, _, _ = await self._handled_project(
                identity, project_handle
            )
            safe_path = self._relative_path(path)
            body = await self.gateway.request(
                identity,
                "GET",
                (
                    "/api/agent/files/read"
                    f"?path={quote(safe_path, safe='')}"
                    f"&offset={offset}&limit={limit}"
                ),
            )
            result = self._checked_backend_result(lease, body)
            if result.get("download_required") is True:
                public, capability = mcp_store.begin_download(
                    self.db_path,
                    identity,
                    lease.project_id,
                    (
                        "/api/project/files/download"
                        f"?path={quote(safe_path, safe='')}"
                    ),
                    filename=PurePosixPath(safe_path).name,
                    size=result.get("size"),
                    sha256=result.get("sha256"),
                )
                result.update({
                    "download_url": (
                        f"{self.gateway.public_base_url}"
                        f"/mcp-download/{public['id']}"
                    ),
                    "authorization": f"Download {capability}",
                    "expires_at": public["expires_at"],
                })
            return result

        return await self._run(
            "read_text_file", "files:read", operation
        )

    async def write_text_file(
        self,
        project_handle: str,
        path: str,
        content: str,
        expected_sha256: str,
    ) -> RemoteToolResult:
        audit_context = {}

        async def operation(identity):
            lease, _, _ = await self._handled_project(
                identity, project_handle
            )
            safe_path = self._relative_path(path)
            audit_context.update({
                "project_id": lease.project_id,
                "targets": (f"file:{safe_path}",),
            })
            body = await self.gateway.request(
                identity,
                "POST",
                "/api/agent/files/write",
                json={
                    "path": safe_path,
                    "content": content,
                    "expected_sha256": expected_sha256,
                },
            )
            return self._checked_backend_result(lease, body)

        return await self._run(
            "write_text_file",
            "files:write",
            operation,
            mutating=True,
            audit_context=audit_context,
        )

    async def create_directory(
        self, project_handle: str, path: str
    ) -> RemoteToolResult:
        audit_context = {}

        async def operation(identity):
            lease, _, _ = await self._handled_project(
                identity, project_handle
            )
            safe_path = self._relative_path(path)
            audit_context.update({
                "project_id": lease.project_id,
                "targets": (f"directory:{safe_path}",),
            })
            body = await self.gateway.request(
                identity,
                "POST",
                "/api/agent/files/mkdir",
                json={"path": safe_path},
            )
            return self._checked_backend_result(lease, body)

        return await self._run(
            "create_directory",
            "files:write",
            operation,
            mutating=True,
            audit_context=audit_context,
        )

    async def move_file(
        self,
        project_handle: str,
        old_path: str,
        new_path: str,
    ) -> RemoteToolResult:
        audit_context = {}

        async def operation(identity):
            lease, _, _ = await self._handled_project(
                identity, project_handle
            )
            safe_old = self._relative_path(old_path)
            safe_new = self._relative_path(new_path)
            audit_context.update({
                "project_id": lease.project_id,
                "targets": (
                    f"file:{safe_old}",
                    f"file:{safe_new}",
                ),
            })
            body = await self.gateway.request(
                identity,
                "POST",
                "/api/agent/files/move",
                json={"from": safe_old, "to": safe_new},
            )
            return self._checked_backend_result(lease, body)

        return await self._run(
            "move_file",
            "files:write",
            operation,
            mutating=True,
            audit_context=audit_context,
        )

    async def begin_file_upload(
        self,
        project_handle: str,
        path: str,
        filename: str,
        size: int,
        sha256: str,
        overwrite: bool = False,
        expected_sha256: str | None = None,
    ) -> RemoteToolResult:
        async def operation(identity):
            lease, _, _ = await self._handled_project(
                identity, project_handle
            )
            safe_path = self._relative_path(path)
            public, capability = mcp_store.begin_upload(
                self.db_path,
                identity,
                "file",
                lease.project_id,
                safe_path,
                size,
                sha256,
                filename,
                overwrite=overwrite,
                expected_sha256=expected_sha256,
            )
            return {
                "ok": True,
                "upload_id": public["id"],
                "upload_url": (
                    f"{self.gateway.public_base_url}"
                    f"/mcp-upload/{public['id']}"
                ),
                "authorization": f"Upload {capability}",
                "expires_at": public["expires_at"],
                "size": public["size"],
                "sha256": public["sha256"],
            }

        return await self._run(
            "begin_file_upload", "files:write", operation
        )

    async def _finish_file_session(
        self,
        identity: PatIdentity,
        project_handle: str,
        upload_id: str,
    ) -> RemoteToolResult:
        lease, _, _ = await self._handled_project(
            identity, project_handle
        )
        session = mcp_store.complete_upload(
            self.db_path,
            upload_id,
            identity,
            expected_kind="file",
            expected_project_id=lease.project_id,
        )
        try:
            body = await self.gateway.request(
                identity,
                "POST",
                "/api/agent/files/install-upload",
                json={
                    "upload_id": session.id,
                    "path": session.destination,
                    "size": session.size,
                    "sha256": session.sha256,
                    "overwrite": session.overwrite,
                    "expected_sha256": session.expected_sha256,
                },
            )
            result = self._checked_backend_result(lease, body)
            mcp_store.finish_upload(self.db_path, upload_id)
            return result
        except Exception:
            mcp_store.fail_upload(
                self.db_path, upload_id, "BACKEND_ERROR"
            )
            raise

    async def finish_file_upload(
        self, project_handle: str, upload_id: str
    ) -> RemoteToolResult:
        audit_context = {}

        async def operation(identity):
            safe_upload_id = self._opaque_id(upload_id, "upload id")
            lease, _, _ = await self._handled_project(
                identity, project_handle
            )
            audit_context.update({
                "project_id": lease.project_id,
                "targets": (f"upload:{safe_upload_id}",),
            })
            result = await self._finish_file_session(
                identity, project_handle, safe_upload_id
            )
            return result

        return await self._run(
            "finish_file_upload",
            "files:write",
            operation,
            mutating=True,
            audit_context=audit_context,
        )

    async def upload_file(
        self,
        project_handle: str,
        path: str,
        content_base64: str,
        size: int,
        sha256: str,
        overwrite: bool = False,
        expected_sha256: str | None = None,
    ) -> RemoteToolResult:
        audit_context = {}

        async def operation(identity):
            lease, _, _ = await self._handled_project(
                identity, project_handle
            )
            safe_path = self._relative_path(path)
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or size > 1024 * 1024
            ):
                raise McpServiceError(
                    "FILE_TOO_LARGE",
                    "inline uploads are limited to 1 MiB",
                )
            try:
                content = base64.b64decode(
                    content_base64, validate=True
                )
            except (TypeError, ValueError, binascii.Error) as exc:
                raise McpServiceError(
                    "CHECKSUM_MISMATCH",
                    "inline upload is not valid base64",
                ) from exc
            if (
                len(content) != size
                or not isinstance(sha256, str)
                or hashlib.sha256(content).hexdigest()
                != sha256.lower()
            ):
                raise McpServiceError(
                    "CHECKSUM_MISMATCH",
                    "inline upload metadata does not match",
                )
            public, capability = mcp_store.begin_upload(
                self.db_path,
                identity,
                "file",
                lease.project_id,
                safe_path,
                size,
                sha256,
                PurePosixPath(safe_path).name,
                overwrite=overwrite,
                expected_sha256=expected_sha256,
            )
            session = mcp_store.authorize_upload(
                self.db_path, public["id"], capability
            )
            ready_path = None
            try:
                ready_path = stage_inline_upload(
                    self.db_path,
                    self.workspace_base,
                    session,
                    content,
                )
                result = await self._finish_file_session(
                    identity, project_handle, session.id
                )
            except Exception:
                if ready_path is not None:
                    ready_path.unlink(missing_ok=True)
                mcp_store.fail_upload(
                    self.db_path, session.id, "BACKEND_ERROR"
                )
                raise
            audit_context.update({
                "project_id": lease.project_id,
                "targets": (f"file:{safe_path}",),
            })
            return result

        return await self._run(
            "upload_file",
            "files:write",
            operation,
            mutating=True,
            audit_context=audit_context,
        )

    async def delete_file(
        self, project_handle: str, path: str
    ) -> RemoteToolResult:
        audit_context = {}

        async def operation(identity):
            lease, _, _ = await self._handled_project(
                identity, project_handle
            )
            safe_path = self._relative_path(path)
            audit_context.update({
                "project_id": lease.project_id,
                "targets": (f"file:{safe_path}",),
            })
            body = await self.gateway.request(
                identity,
                "POST",
                "/api/agent/files/delete",
                json={
                    "path": safe_path,
                    "actor_token_id": identity.token_id,
                },
            )
            return self._checked_backend_result(lease, body)

        return await self._run(
            "delete_file",
            "files:write",
            operation,
            mutating=True,
            audit_context=audit_context,
        )

    async def list_deleted_files(
        self, project_handle: str
    ) -> RemoteToolResult:
        async def operation(identity):
            lease, _, _ = await self._handled_project(
                identity, project_handle
            )
            body = await self.gateway.request(
                identity, "GET", "/api/agent/files/trash"
            )
            return self._checked_backend_result(lease, body)

        return await self._run(
            "list_deleted_files", "files:read", operation
        )

    async def restore_deleted_file(
        self, project_handle: str, trash_id: str
    ) -> RemoteToolResult:
        audit_context = {}

        async def operation(identity):
            lease, _, _ = await self._handled_project(
                identity, project_handle
            )
            safe_trash_id = self._opaque_id(trash_id, "trash id")
            audit_context.update({
                "project_id": lease.project_id,
                "targets": (f"trash:{safe_trash_id}",),
            })
            body = await self.gateway.request(
                identity,
                "POST",
                "/api/agent/files/restore",
                json={"trash_id": safe_trash_id},
            )
            return self._checked_backend_result(lease, body)

        return await self._run(
            "restore_deleted_file",
            "files:write",
            operation,
            mutating=True,
            audit_context=audit_context,
        )


def create_remote_mcp(
    db_path: Path,
    gateway: WorkspaceGateway,
    public_base_url: str,
    workspace_base: Path = Path("/workspaces"),
) -> FastMCP:
    public_base_url = public_base_url.rstrip("/")
    parsed = urlsplit(public_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("public_base_url must be an HTTP(S) origin")

    server = FastMCP(
        "vibe-typst-projects",
        token_verifier=PatTokenVerifier(db_path),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(public_base_url),
            resource_server_url=AnyHttpUrl(f"{public_base_url}/mcp"),
            required_scopes=[],
        ),
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                parsed.netloc,
                "127.0.0.1:*",
                "localhost:*",
            ],
            allowed_origins=[
                public_base_url,
                "http://127.0.0.1:*",
                "http://localhost:*",
            ],
        ),
    )
    service = _RemoteProjectService(
        db_path, gateway, workspace_base=workspace_base
    )

    @server.tool()
    async def list_projects() -> CallToolResult:
        """List projects owned by the authenticated Vibe Typst user."""
        return _protocol_result(await service.list_projects())

    @server.tool()
    async def create_typst_project(name: str) -> CallToolResult:
        """Create a new Typst project. The project type cannot later change."""
        return _protocol_result(await service.create_typst_project(name))

    @server.tool()
    async def open_project(project_id: str) -> CallToolResult:
        """Open one project and return an opaque handle for scoped operations."""
        return _protocol_result(await service.open_project(project_id))

    @server.tool()
    async def get_project(project_handle: str) -> CallToolResult:
        """Return metadata for the currently handled project."""
        return _protocol_result(await service.get_project(project_handle))

    @server.tool()
    async def rename_project(
        project_handle: str, name: str
    ) -> CallToolResult:
        """Rename the handled project without changing its stable ID."""
        return _protocol_result(
            await service.rename_project(project_handle, name)
        )

    @server.tool()
    async def close_project(project_handle: str) -> CallToolResult:
        """Close the handled project and invalidate its project context."""
        return _protocol_result(await service.close_project(project_handle))

    @server.tool()
    async def list_files(project_handle: str) -> CallToolResult:
        """List safe visible paths in the handled project; protected main files are labelled."""
        return _protocol_result(await service.list_files(project_handle))

    @server.tool()
    async def read_text_file(
        project_handle: str,
        path: str,
        offset: int = 1,
        limit: int = 120,
    ) -> CallToolResult:
        """Read a bounded text window from the handled project; binary/large files return a one-time download."""
        return _protocol_result(
            await service.read_text_file(
                project_handle, path, offset, limit
            )
        )

    @server.tool()
    async def write_text_file(
        project_handle: str,
        path: str,
        content: str,
        expected_sha256: str,
    ) -> CallToolResult:
        """Replace an ordinary text file only at expected_sha256; Typst main and PDF-managed files are protected."""
        return _protocol_result(
            await service.write_text_file(
                project_handle, path, content, expected_sha256
            )
        )

    @server.tool()
    async def create_directory(
        project_handle: str, path: str
    ) -> CallToolResult:
        """Create a visible directory inside the handled project."""
        return _protocol_result(
            await service.create_directory(project_handle, path)
        )

    @server.tool()
    async def move_file(
        project_handle: str,
        old_path: str,
        new_path: str,
    ) -> CallToolResult:
        """Move one ordinary file/directory without overwriting; protected project state cannot move."""
        return _protocol_result(
            await service.move_file(
                project_handle, old_path, new_path
            )
        )

    @server.tool()
    async def upload_file(
        project_handle: str,
        path: str,
        content_base64: str,
        size: int,
        sha256: str,
        overwrite: bool = False,
        expected_sha256: str | None = None,
    ) -> CallToolResult:
        """Upload at most 1 MiB inline to an ordinary path; overwrite is off unless paired with the current SHA-256."""
        return _protocol_result(
            await service.upload_file(
                project_handle,
                path,
                content_base64,
                size,
                sha256,
                overwrite,
                expected_sha256,
            )
        )

    @server.tool()
    async def begin_file_upload(
        project_handle: str,
        path: str,
        filename: str,
        size: int,
        sha256: str,
        overwrite: bool = False,
        expected_sha256: str | None = None,
    ) -> CallToolResult:
        """Begin a 100 MiB maximum staged upload; PUT once with the returned Upload authorization, then finish_file_upload."""
        return _protocol_result(
            await service.begin_file_upload(
                project_handle,
                path,
                filename,
                size,
                sha256,
                overwrite,
                expected_sha256,
            )
        )

    @server.tool()
    async def finish_file_upload(
        project_handle: str, upload_id: str
    ) -> CallToolResult:
        """Atomically install a completed staged upload into its predeclared handled-project path."""
        return _protocol_result(
            await service.finish_file_upload(
                project_handle, upload_id
            )
        )

    @server.tool()
    async def delete_file(
        project_handle: str, path: str
    ) -> CallToolResult:
        """Move an ordinary handled-project file/directory to recoverable 30-day trash."""
        return _protocol_result(
            await service.delete_file(project_handle, path)
        )

    @server.tool()
    async def list_deleted_files(
        project_handle: str,
    ) -> CallToolResult:
        """List recoverable deleted items for the handled project."""
        return _protocol_result(
            await service.list_deleted_files(project_handle)
        )

    @server.tool()
    async def restore_deleted_file(
        project_handle: str, trash_id: str
    ) -> CallToolResult:
        """Restore a trashed item only when its original path is still free."""
        return _protocol_result(
            await service.restore_deleted_file(
                project_handle, trash_id
            )
        )

    return server
