"""PAT-authenticated, project-level MCP server for Vibe Typst."""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
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
    ):
        self.db_path = db_path
        self.gateway = gateway
        self.limiter = limiter or TokenLimiter()

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


def create_remote_mcp(
    db_path: Path,
    gateway: WorkspaceGateway,
    public_base_url: str,
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
    service = _RemoteProjectService(db_path, gateway)

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

    return server
