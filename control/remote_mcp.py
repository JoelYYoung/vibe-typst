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
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import AnyHttpUrl
from typing_extensions import TypedDict

import mcp_store
import pat_store
from mcp_transfer import stage_inline_upload
from mcp_errors import McpServiceError
from mcp_limits import TokenLimiter
from pat_store import PatIdentity
from workspace_gateway import WorkspaceGateway


REMOTE_MCP_INSTRUCTIONS = """\
Use this server to work on Vibe Typst projects through their live web workspace.

Start with list_projects, then call open_project for the project you will change. Keep using the
returned project_handle until a tool reports PROJECT_CONTEXT_CHANGED or PROJECT_HANDLE_EXPIRED.
Those errors mean a human or another client changed the active project/file context: do not force
an overwrite; call open_project again and re-read the document before continuing.

For a Typst project, the current active shared .typ returned by get_document.file is the
authoritative live document and MUST be main.typ. Keep ALL Typst presentation source in main.typ,
including theme and style definitions, helper functions, components, slide content, and inline
speaker notes. NEVER create, upload, generate, import, include, or depend on auxiliary .typ files.
Local .typ imports/includes are forbidden; package imports such as Touying are allowed. Ordinary
file tools may be used only for non-Typst assets such as images, fonts, and data.

Read main.typ with get_document or find_in_document and modify it only with apply_edits, passing
the latest rev when available. Every apply_edits item is
{"selector": {"by": "anchor"|"lines"|"range", ...}, "text": "<replacement>"} — there is no
old_text/new_text form; see the tool description for the full selector shape. A human may be
editing the same document in the browser at the same time: the workspace merges both writers, so
prefer anchor selectors over line or range numbers, re-read before a large rewrite, and add
`expect` when a span must not have changed under you. EDIT_REJECTED means your edit was wrong and
retrying it unchanged will fail again; only REVISION_CONFLICT means the document moved.
Never use generic file writes to replace main.typ or bypass its shared CRDT state.

Every presentation MUST remain in Touying form: preserve or add the Touying
package import, use its slide/theme model, and keep speaker transcripts inline in main.typ. After
meaningful Typst changes, inspect the rendered result with get_slide_preview; fix compile or
layout problems before declaring the work complete.

For human Typst comments, call get_pending_comments, read the requested change and its live
location, apply the change, verify the preview, and only then call mark_comment_done with a short
note. Use mark_comment_dismissed only when a comment is unclear, obsolete, or already resolved.

PDF projects follow a separate workflow. They have page previews and per-page transcripts but no
comment workflow or editable Typst document. Do not overwrite document.pdf with generic file
tools; use the staged PDF replacement tools, then verify pages and transcripts.
"""


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


def _image_protocol_result(result: RemoteToolResult) -> CallToolResult:
    if not result.get("ok"):
        return _protocol_result(result)
    data = result.get("_image_data")
    if not isinstance(data, bytes):
        return _protocol_result(McpServiceError(
            "BACKEND_ERROR", "preview image was unavailable"
        ).as_dict())
    structured = {
        key: value
        for key, value in result.items()
        if key != "_image_data"
    }
    return CallToolResult(
        content=[ImageContent(
            type="image",
            data=base64.b64encode(data).decode("ascii"),
            mimeType="image/png",
        )],
        structuredContent=structured,
        isError=False,
    )


EDIT_SHAPE = (
    'each edit is {"selector": {...}, "text": "<replacement>", "expect"?: "<current span>"}; '
    'a selector is {"by": "anchor", "text": "<exact snippet>", "occurrence"?: 1, '
    '"side"?: "in"|"before"|"after"}, {"by": "lines", "start": <1-based>, "end"?: <1-based>} '
    'or {"by": "range", "from": <offset>, "to": <offset>}'
)
_MAX_EDIT_CONTEXT_CHARS = 400


def _edit_refusal(body: dict) -> McpServiceError:
    """Translate a refused edit batch into the error the caller can act on.

    The workspace reports every refusal with `conflict: true` and says which KIND it was in
    `reason`. Reporting all of them as REVISION_CONFLICT told agents to re-read a document that
    had not moved, so a malformed edit looked exactly like a race and retried forever; only a
    genuine race gets that code now.
    """
    reason = body.get("reason")
    message = body.get("error")
    if not isinstance(message, str) or not message:
        message = "document edits were rejected"
    details: dict[str, Any] = {}
    for key in ("index", "rev"):
        value = body.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            details[key] = value
    context = body.get("context")
    if isinstance(context, str) and context:
        details["context"] = context[:_MAX_EDIT_CONTEXT_CHARS]
    if reason == "revision_conflict":
        return McpServiceError(
            "REVISION_CONFLICT",
            f"{message}; read the document again and retry",
            details=details or None,
        )
    if reason == "selector_missed":
        return McpServiceError(
            "EDIT_REJECTED",
            f"{message}; re-read the document and re-aim the selector",
            details=details or None,
        )
    if reason == "invalid_edit":
        details["expected_shape"] = EDIT_SHAPE
        return McpServiceError("EDIT_REJECTED", message, details=details)
    if body.get("conflict") is True:
        # A workspace older than the `reason` classification: keep the previous mapping.
        return McpServiceError(
            "REVISION_CONFLICT", "document revision changed; read and retry"
        )
    return McpServiceError("BACKEND_ERROR", "document edits were rejected")


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
    def _public_project(project: dict) -> dict:
        """Return only stable project metadata, never workspace paths."""
        if (
            not isinstance(project, dict)
            or not isinstance(project.get("id"), str)
            or not isinstance(project.get("name"), str)
            or project.get("type", "typst") not in {"typst", "pdf"}
        ):
            raise McpServiceError(
                "BACKEND_ERROR",
                "workspace returned invalid project metadata",
            )
        return {
            key: project.get(key)
            for key in (
                "id",
                "name",
                "created",
                "type",
                "main_file",
                "original_filename",
            )
            if project.get(key) is not None
        }

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

    @classmethod
    def _ordinary_mutation_path(cls, project: dict, path: str) -> str:
        safe_path = cls._relative_path(path)
        if (
            project.get("type", "typst") == "typst"
            and PurePosixPath(safe_path).suffix.lower() == ".typ"
        ):
            raise McpServiceError(
                "PATH_NOT_ALLOWED",
                "all Typst source must remain in main.typ; "
                "edit main.typ with apply_edits",
            )
        return safe_path

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
            return {
                "ok": True,
                "projects": [
                    self._public_project(project)
                    for project in projects
                ],
            }

        return await self._run(
            "list_projects", "projects:read", operation
        )

    async def create_typst_project(self, name: str) -> RemoteToolResult:
        audit_context = {}

        async def operation(identity):
            project = await self.gateway.create_typst_project(identity, name)
            audit_context["project_id"] = project["id"]
            audit_context["targets"] = (f"project:{project['id']}",)
            return {
                "ok": True,
                "project": self._public_project(project),
            }

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
                "project": self._public_project(project),
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
                "project": self._public_project(project),
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
            return {
                "ok": True,
                "project": self._public_project(project),
            }

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
            lease, project, _ = await self._handled_project(
                identity, project_handle
            )
            safe_path = self._ordinary_mutation_path(project, path)
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
            lease, project, _ = await self._handled_project(
                identity, project_handle
            )
            safe_old = self._ordinary_mutation_path(project, old_path)
            safe_new = self._ordinary_mutation_path(project, new_path)
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
            lease, project, _ = await self._handled_project(
                identity, project_handle
            )
            safe_path = self._ordinary_mutation_path(project, path)
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
        lease, project, _ = await self._handled_project(
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
            self._ordinary_mutation_path(project, session.destination)
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
            lease, project, _ = await self._handled_project(
                identity, project_handle
            )
            safe_path = self._ordinary_mutation_path(project, path)
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
            lease, project, _ = await self._handled_project(
                identity, project_handle
            )
            safe_trash_id = self._opaque_id(trash_id, "trash id")
            if project.get("type", "typst") == "typst":
                trash = await self.gateway.request(
                    identity, "GET", "/api/agent/files/trash"
                )
                trash = self._checked_backend_result(lease, trash)
                for item in trash.get("items", []):
                    if item.get("id") != safe_trash_id:
                        continue
                    original_path = item.get("original_path")
                    self._ordinary_mutation_path(project, original_path)
                    break
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

    async def _typed_project(
        self,
        identity: PatIdentity,
        project_handle: str,
        required_type: str,
    ) -> tuple[mcp_store.Lease, dict]:
        lease, project, _ = await self._handled_project(
            identity, project_handle
        )
        if project.get("type", "typst") != required_type:
            raise McpServiceError(
                "CAPABILITY_NOT_AVAILABLE",
                f"tool is unavailable for {project.get('type', 'unknown')} projects",
            )
        if (
            required_type == "typst"
            and project.get("main_file", "main.typ") != "main.typ"
        ):
            raise McpServiceError(
                "CAPABILITY_NOT_AVAILABLE",
                "remote Typst tools require main.typ as the primary document",
            )
        return lease, project

    async def _confirm_project_context(
        self,
        identity: PatIdentity,
        project_handle: str,
        lease: mcp_store.Lease,
    ) -> None:
        current, _, _ = await self._handled_project(
            identity, project_handle
        )
        if (
            current.project_id != lease.project_id
            or current.context_version != lease.context_version
        ):
            raise McpServiceError(
                "PROJECT_CONTEXT_CHANGED",
                "the active project context changed; open the project again",
            )

    async def get_document(
        self,
        project_handle: str,
        offset: int = 1,
        limit: int = 120,
    ) -> RemoteToolResult:
        async def operation(identity):
            lease, _ = await self._typed_project(
                identity, project_handle, "typst"
            )
            body = await self.gateway.request(
                identity, "GET", "/api/document"
            )
            source = body.get("source")
            rev = body.get("rev")
            if not isinstance(source, str) or not isinstance(rev, int):
                raise McpServiceError(
                    "BACKEND_ERROR",
                    "workspace returned an invalid document",
                )
            if (
                isinstance(offset, bool)
                or not isinstance(offset, int)
                or offset < 1
                or isinstance(limit, bool)
                or not isinstance(limit, int)
                or limit < 1
            ):
                raise McpServiceError(
                    "PATH_NOT_ALLOWED",
                    "offset and limit must be positive integers",
                )
            lines = source.split("\n")
            start = min(offset - 1, len(lines))
            count = min(limit, 400)
            end = min(start + count, len(lines))
            await self._confirm_project_context(
                identity, project_handle, lease
            )
            return {
                "ok": True,
                "file": body.get("file"),
                "rev": rev,
                "chars": len(source),
                "total_lines": len(lines),
                "shown": (
                    f"{start + 1}-{end}" if end > start else None
                ),
                "text": "\n".join(
                    f"{number:>5}\t{line}"
                    for number, line in zip(
                        range(start + 1, end + 1), lines[start:end]
                    )
                ),
                "truncated": end < len(lines),
                "next": end + 1 if end < len(lines) else None,
            }

        return await self._run(
            "get_document", "files:read", operation
        )

    async def find_in_document(
        self,
        project_handle: str,
        query: str,
        max_hits: int = 40,
    ) -> RemoteToolResult:
        async def operation(identity):
            lease, _ = await self._typed_project(
                identity, project_handle, "typst"
            )
            if (
                not isinstance(query, str)
                or not query
                or len(query) > 512
            ):
                raise McpServiceError(
                    "PATH_NOT_ALLOWED",
                    "query must contain 1 to 512 characters",
                )
            if (
                isinstance(max_hits, bool)
                or not isinstance(max_hits, int)
                or max_hits < 1
            ):
                raise McpServiceError(
                    "PATH_NOT_ALLOWED", "max_hits must be positive"
                )
            body = await self.gateway.request(
                identity, "GET", "/api/document"
            )
            source = body.get("source")
            if not isinstance(source, str):
                raise McpServiceError(
                    "BACKEND_ERROR",
                    "workspace returned an invalid document",
                )
            lines = source.split("\n")
            hits = [
                {"line": number, "text": line}
                for number, line in enumerate(lines, 1)
                if query in line
            ]
            await self._confirm_project_context(
                identity, project_handle, lease
            )
            return {
                "ok": True,
                "file": body.get("file"),
                "rev": body.get("rev"),
                "total_lines": len(lines),
                "query": query,
                "matches": len(hits),
                "shown": min(len(hits), min(max_hits, 40)),
                "hits": hits[:min(max_hits, 40)],
            }

        return await self._run(
            "find_in_document", "files:read", operation
        )

    async def locate(
        self,
        project_handle: str,
        page: int = 0,
        slide: int = 0,
    ) -> RemoteToolResult:
        async def operation(identity):
            lease, _ = await self._typed_project(
                identity, project_handle, "typst"
            )
            if (
                isinstance(page, bool)
                or not isinstance(page, int)
                or isinstance(slide, bool)
                or not isinstance(slide, int)
                or bool(page) == bool(slide)
                or page < 0
                or slide < 0
            ):
                raise McpServiceError(
                    "PATH_NOT_ALLOWED",
                    "pass exactly one positive page or slide number",
                )
            query = f"page={page}" if page else f"slide={slide}"
            body = await self.gateway.request(
                identity, "GET", f"/api/locate?{query}"
            )
            await self._confirm_project_context(
                identity, project_handle, lease
            )
            if body.get("ok") is not True:
                raise McpServiceError(
                    "FILE_NOT_FOUND",
                    "the requested rendered page or slide was not found",
                )
            return {"ok": True, **{
                key: value for key, value in body.items() if key != "ok"
            }}

        return await self._run("locate", "slides:read", operation)

    async def apply_edits(
        self,
        project_handle: str,
        edits: list,
        base_rev: int | None = None,
    ) -> RemoteToolResult:
        audit_context = {}

        async def operation(identity):
            lease, project = await self._typed_project(
                identity, project_handle, "typst"
            )
            if (
                not isinstance(edits, list)
                or not edits
                or len(edits) > 100
            ):
                raise McpServiceError(
                    "PATH_NOT_ALLOWED",
                    "edits must contain 1 to 100 operations",
                )
            audit_context.update({
                "project_id": lease.project_id,
                "targets": (
                    f"document:{project.get('main_file', 'main.typ')}",
                ),
            })
            body = await self.gateway.request(
                identity,
                "POST",
                "/api/edit",
                json={
                    "op": "apply_edits",
                    "edits": edits,
                    "base_rev": base_rev,
                    "file": project.get("main_file"),
                    "require_single_file_typst": True,
                },
            )
            await self._confirm_project_context(
                identity, project_handle, lease
            )
            if body.get("ok") is not True:
                if body.get("policy_violation") is True:
                    raise McpServiceError(
                        "PATH_NOT_ALLOWED",
                        "local .typ imports/includes are forbidden; "
                        "keep all Typst source in main.typ",
                    )
                raise _edit_refusal(body)
            return {"ok": True, **{
                key: value for key, value in body.items() if key != "ok"
            }}

        return await self._run(
            "apply_edits",
            "documents:write",
            operation,
            mutating=True,
            audit_context=audit_context,
        )

    async def get_transcripts(
        self, project_handle: str
    ) -> RemoteToolResult:
        async def operation(identity):
            lease, project, _ = await self._handled_project(
                identity, project_handle
            )
            project_type = project.get("type", "typst")
            if project_type not in {"typst", "pdf"}:
                raise McpServiceError(
                    "CAPABILITY_NOT_AVAILABLE",
                    "transcripts are unavailable for this project type",
                )
            if project_type == "pdf":
                body = await self.gateway.request(
                    identity, "GET", "/api/pdf/transcripts"
                )
                pages = body.get("pages")
                orphans = body.get("orphans")
                if not isinstance(pages, dict) or not isinstance(
                    orphans, dict
                ):
                    raise McpServiceError(
                        "BACKEND_ERROR",
                        "workspace returned invalid transcripts",
                    )
                await self._confirm_project_context(
                    identity, project_handle, lease
                )
                return {
                    "ok": True,
                    "pages": pages,
                    "orphans": orphans,
                }
            body = await self.gateway.request(
                identity, "GET", "/api/slide-map"
            )
            pages = body.get("pages")
            if not isinstance(pages, list):
                raise McpServiceError(
                    "BACKEND_ERROR",
                    "workspace returned invalid transcripts",
                )
            await self._confirm_project_context(
                identity, project_handle, lease
            )
            return {
                "ok": True,
                "pages": [{
                    key: row.get(key)
                    for key in (
                        "page",
                        "slide_no",
                        "slide_line",
                        "sub_index",
                        "sub_total",
                        "section",
                        "note",
                        "note_raw",
                        "note_line",
                    )
                } for row in pages if isinstance(row, dict)],
                "total": body.get("total", len(pages)),
                "orphans": body.get("orphans") or [],
            }

        return await self._run(
            "get_transcripts", "transcripts:read", operation
        )

    async def get_pdf_info(
        self, project_handle: str
    ) -> RemoteToolResult:
        async def operation(identity):
            lease, project = await self._typed_project(
                identity, project_handle, "pdf"
            )
            body = await self.gateway.request(
                identity, "GET", "/api/state"
            )
            pages = body.get("pages")
            if (
                body.get("project_type") != "pdf"
                or not isinstance(pages, list)
                or not all(isinstance(item, str) for item in pages)
            ):
                raise McpServiceError(
                    "BACKEND_ERROR",
                    "workspace returned invalid PDF metadata",
                )
            await self._confirm_project_context(
                identity, project_handle, lease
            )
            return {
                "ok": True,
                "project_type": "pdf",
                "name": project.get("name"),
                "filename": project.get(
                    "original_filename", project.get("main_file")
                ),
                "main_file": project.get("main_file"),
                "page_count": len(pages),
                "version": body.get("version"),
                "generation": body.get("generation"),
            }

        return await self._run(
            "get_pdf_info", "projects:read", operation
        )

    async def get_pdf_text(
        self, project_handle: str, page: int
    ) -> RemoteToolResult:
        async def operation(identity):
            lease, _ = await self._typed_project(
                identity, project_handle, "pdf"
            )
            safe_page = self._page_number(page)
            body = await self.gateway.request(
                identity,
                "GET",
                f"/api/pdf/text?page={safe_page}",
            )
            if (
                body.get("page") != safe_page
                or not isinstance(body.get("text"), str)
            ):
                raise McpServiceError(
                    "BACKEND_ERROR",
                    "workspace returned invalid PDF text",
                )
            await self._confirm_project_context(
                identity, project_handle, lease
            )
            return {
                "ok": True,
                "page": safe_page,
                "text": body["text"],
                "ocr": body.get("ocr") is True,
            }

        return await self._run(
            "get_pdf_text", "files:read", operation
        )

    @staticmethod
    def _page_number(page: int) -> int:
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or page < 1
        ):
            raise McpServiceError(
                "PATH_NOT_ALLOWED", "page must be positive"
            )
        return page

    async def set_transcript(
        self, project_handle: str, page: int, text: str
    ) -> RemoteToolResult:
        audit_context = {}

        async def operation(identity):
            lease, _ = await self._typed_project(
                identity, project_handle, "pdf"
            )
            safe_page = self._page_number(page)
            if not isinstance(text, str) or len(text) > 256 * 1024:
                raise McpServiceError(
                    "PATH_NOT_ALLOWED",
                    "transcript text must be at most 256 KiB",
                )
            audit_context.update({
                "project_id": lease.project_id,
                "targets": (f"transcript:page-{safe_page}",),
            })
            body = await self.gateway.request(
                identity,
                "PATCH",
                f"/api/pdf/transcripts/{safe_page}",
                json={"text": text},
            )
            await self._confirm_project_context(
                identity, project_handle, lease
            )
            return {
                "ok": True,
                **{
                    key: value
                    for key, value in body.items()
                    if key not in {"project_id", "context_version", "ok"}
                },
            }

        return await self._run(
            "set_transcript",
            "transcripts:write",
            operation,
            mutating=True,
            audit_context=audit_context,
        )

    async def set_transcripts(
        self, project_handle: str, updates: list
    ) -> RemoteToolResult:
        audit_context = {}

        async def operation(identity):
            lease, _ = await self._typed_project(
                identity, project_handle, "pdf"
            )
            if (
                not isinstance(updates, list)
                or not updates
                or len(updates) > 400
            ):
                raise McpServiceError(
                    "PATH_NOT_ALLOWED",
                    "updates must contain 1 to 400 transcripts",
                )
            clean_updates = []
            targets = []
            total_chars = 0
            for update in updates:
                if not isinstance(update, dict) or set(update) != {
                    "page", "text"
                }:
                    raise McpServiceError(
                        "PATH_NOT_ALLOWED",
                        "each update must contain only page and text",
                    )
                safe_page = self._page_number(update.get("page"))
                text = update.get("text")
                if not isinstance(text, str) or len(text) > 256 * 1024:
                    raise McpServiceError(
                        "PATH_NOT_ALLOWED",
                        "transcript text must be at most 256 KiB",
                    )
                total_chars += len(text)
                if total_chars > 1024 * 1024:
                    raise McpServiceError(
                        "PATH_NOT_ALLOWED",
                        "transcript batch must be at most 1 MiB",
                    )
                clean_updates.append({
                    "page": safe_page,
                    "text": text,
                })
                targets.append(f"transcript:page-{safe_page}")
            audit_context.update({
                "project_id": lease.project_id,
                "targets": tuple(targets),
            })
            body = await self.gateway.request(
                identity,
                "POST",
                "/api/pdf/transcripts/batch",
                json={"updates": clean_updates},
            )
            await self._confirm_project_context(
                identity, project_handle, lease
            )
            return {
                "ok": True,
                **{
                    key: value
                    for key, value in body.items()
                    if key not in {"project_id", "context_version", "ok"}
                },
            }

        return await self._run(
            "set_transcripts",
            "transcripts:write",
            operation,
            mutating=True,
            audit_context=audit_context,
        )

    @staticmethod
    def _comment_id(comment_id: str) -> str:
        if (
            not isinstance(comment_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", comment_id) is None
        ):
            raise McpServiceError(
                "PATH_NOT_ALLOWED", "comment id is invalid"
            )
        return comment_id

    async def get_pending_comments(
        self, project_handle: str
    ) -> RemoteToolResult:
        async def operation(identity):
            lease, _ = await self._typed_project(
                identity, project_handle, "typst"
            )
            body = await self.gateway.request(
                identity, "GET", "/api/agent/comments/pending"
            )
            return self._checked_backend_result(lease, body)

        return await self._run(
            "get_pending_comments", "comments:read", operation
        )

    async def get_comment(
        self, project_handle: str, comment_id: str
    ) -> RemoteToolResult:
        async def operation(identity):
            lease, _ = await self._typed_project(
                identity, project_handle, "typst"
            )
            safe_id = self._comment_id(comment_id)
            body = await self.gateway.request(
                identity,
                "GET",
                f"/api/agent/comments/{quote(safe_id, safe='')}",
            )
            return self._checked_backend_result(lease, body)

        return await self._run(
            "get_comment", "comments:read", operation
        )

    async def mark_comment_done(
        self,
        project_handle: str,
        comment_id: str,
        note: str = "",
    ) -> RemoteToolResult:
        return await self._set_comment_status(
            project_handle, comment_id, "done", note
        )

    async def mark_comment_dismissed(
        self,
        project_handle: str,
        comment_id: str,
        reason: str = "",
    ) -> RemoteToolResult:
        return await self._set_comment_status(
            project_handle, comment_id, "dismiss", reason
        )

    async def _set_comment_status(
        self,
        project_handle: str,
        comment_id: str,
        action: str,
        note: str,
    ) -> RemoteToolResult:
        audit_context = {}
        tool_name = (
            "mark_comment_done"
            if action == "done"
            else "mark_comment_dismissed"
        )

        async def operation(identity):
            lease, _ = await self._typed_project(
                identity, project_handle, "typst"
            )
            safe_id = self._comment_id(comment_id)
            if not isinstance(note, str) or len(note) > 4096:
                raise McpServiceError(
                    "PATH_NOT_ALLOWED",
                    "comment status note is invalid",
                )
            audit_context.update({
                "project_id": lease.project_id,
                "targets": (f"comment:{safe_id}",),
            })
            body = await self.gateway.request(
                identity,
                "POST",
                (
                    f"/api/agent/comments/{quote(safe_id, safe='')}"
                    f"/{action}"
                ),
                json={"note": note},
            )
            return self._checked_backend_result(lease, body)

        return await self._run(
            tool_name,
            "comments:write",
            operation,
            mutating=True,
            audit_context=audit_context,
        )

    async def get_slide_preview(
        self, project_handle: str, page: int
    ) -> RemoteToolResult:
        async def operation(identity):
            lease, _ = await self._typed_project(
                identity, project_handle, "typst"
            )
            if (
                isinstance(page, bool)
                or not isinstance(page, int)
                or page < 1
            ):
                raise McpServiceError(
                    "PATH_NOT_ALLOWED", "page must be positive"
                )
            data, headers = await self.gateway.read_bytes(
                identity, f"/api/agent/preview/{page}", 8 * 1024 * 1024
            )
            if (
                headers.get("x-project-id") != lease.project_id
                or headers.get("x-context-version")
                != lease.context_version
            ):
                raise McpServiceError(
                    "PROJECT_CONTEXT_CHANGED",
                    "the active project context changed; open the project again",
                )
            if (
                headers.get("content-type", "").split(";", 1)[0]
                != "image/png"
                or not data.startswith(b"\x89PNG\r\n\x1a\n")
            ):
                raise McpServiceError(
                    "BACKEND_ERROR",
                    "workspace returned an invalid preview",
                )
            try:
                page_count = int(headers.get("x-page-count", ""))
            except ValueError as exc:
                raise McpServiceError(
                    "BACKEND_ERROR",
                    "workspace returned invalid preview metadata",
                ) from exc
            return {
                "ok": True,
                "page": page,
                "page_count": page_count,
                "media_type": "image/png",
                "_image_data": data,
            }

        return await self._run(
            "get_slide_preview", "slides:read", operation
        )

    async def get_page_preview(
        self, project_handle: str, page: int
    ) -> RemoteToolResult:
        async def operation(identity):
            lease, project, _ = await self._handled_project(
                identity, project_handle
            )
            project_type = project.get("type", "typst")
            if project_type not in {"typst", "pdf"}:
                raise McpServiceError(
                    "CAPABILITY_NOT_AVAILABLE",
                    "page previews are unavailable for this project type",
                )
            safe_page = self._page_number(page)
            data, headers = await self.gateway.read_bytes(
                identity,
                f"/api/agent/preview/{safe_page}",
                8 * 1024 * 1024,
            )
            if (
                headers.get("x-project-id") != lease.project_id
                or headers.get("x-context-version")
                != lease.context_version
            ):
                raise McpServiceError(
                    "PROJECT_CONTEXT_CHANGED",
                    "the active project context changed; open the project again",
                )
            if (
                headers.get("content-type", "").split(";", 1)[0]
                != "image/png"
                or not data.startswith(b"\x89PNG\r\n\x1a\n")
            ):
                raise McpServiceError(
                    "BACKEND_ERROR",
                    "workspace returned an invalid preview",
                )
            try:
                page_count = int(headers.get("x-page-count", ""))
            except ValueError as exc:
                raise McpServiceError(
                    "BACKEND_ERROR",
                    "workspace returned invalid preview metadata",
                ) from exc
            return {
                "ok": True,
                "project_type": project_type,
                "page": safe_page,
                "page_count": page_count,
                "media_type": "image/png",
                "_image_data": data,
            }

        return await self._run(
            "get_page_preview", "slides:read", operation
        )

    async def export_pdf(
        self, project_handle: str
    ) -> RemoteToolResult:
        async def operation(identity):
            lease, _ = await self._typed_project(
                identity, project_handle, "typst"
            )
            body = await self.gateway.request(
                identity, "POST", "/api/agent/export-pdf"
            )
            result = self._checked_backend_result(lease, body)
            export_id = result.get("export_id")
            download_path = result.get("download_path")
            filename = result.get("filename")
            size = result.get("size")
            sha256 = result.get("sha256")
            if (
                not isinstance(export_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", export_id) is None
                or not isinstance(download_path, str)
                or not isinstance(filename, str)
                or isinstance(size, bool)
                or not isinstance(size, int)
                or not isinstance(sha256, str)
            ):
                raise McpServiceError(
                    "BACKEND_ERROR",
                    "workspace returned invalid export metadata",
                )
            public, capability = mcp_store.begin_download(
                self.db_path,
                identity,
                lease.project_id,
                download_path,
                filename=filename,
                size=size,
                sha256=sha256,
            )
            return {
                "ok": True,
                "download_id": public["id"],
                "download_url": (
                    f"{self.gateway.public_base_url}"
                    f"/mcp-download/{public['id']}"
                ),
                "authorization": f"Download {capability}",
                "filename": filename,
                "size": size,
                "sha256": sha256,
                "expires_at": public["expires_at"],
            }

        return await self._run("export_pdf", "slides:read", operation)

    @staticmethod
    def _pdf_upload_inputs(name: str, filename: str) -> tuple[str, str]:
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name.strip()) > 200
            or "\x00" in name
        ):
            raise McpServiceError(
                "PATH_NOT_ALLOWED",
                "project name must contain 1 to 200 characters",
            )
        if (
            not isinstance(filename, str)
            or not filename
            or len(filename) > 255
            or PurePosixPath(filename).name != filename
            or not filename.lower().endswith(".pdf")
        ):
            raise McpServiceError(
                "PATH_NOT_ALLOWED",
                "filename must be a plain PDF filename",
            )
        return name.strip(), filename

    @staticmethod
    def _upload_result(
        public_base_url: str, public: dict, capability: str
    ) -> RemoteToolResult:
        return {
            "ok": True,
            "upload_id": public["id"],
            "upload_url": (
                f"{public_base_url}/mcp-upload/{public['id']}"
            ),
            "authorization": f"Upload {capability}",
            "expires_at": public["expires_at"],
            "size": public["size"],
            "sha256": public["sha256"],
        }

    async def begin_pdf_project_upload(
        self,
        name: str,
        filename: str,
        size: int,
        sha256: str,
    ) -> RemoteToolResult:
        async def operation(identity):
            safe_name, safe_filename = self._pdf_upload_inputs(
                name, filename
            )
            public, capability = mcp_store.begin_upload(
                self.db_path,
                identity,
                "pdf_project",
                "",
                safe_name,
                size,
                sha256,
                safe_filename,
            )
            return self._upload_result(
                self.gateway.public_base_url, public, capability
            )

        return await self._run(
            "begin_pdf_project_upload",
            "projects:write",
            operation,
        )

    async def finish_pdf_project_upload(
        self, upload_id: str
    ) -> RemoteToolResult:
        audit_context = {}

        async def operation(identity):
            safe_upload_id = self._opaque_id(upload_id, "upload id")
            session = mcp_store.complete_upload(
                self.db_path,
                safe_upload_id,
                identity,
                expected_kind="pdf_project",
                expected_project_id="",
            )
            try:
                body = await self.gateway.request(
                    identity,
                    "POST",
                    "/api/agent/projects/pdf-from-upload",
                    json={
                        "upload_id": session.id,
                        "name": session.destination,
                        "filename": session.filename,
                        "size": session.size,
                        "sha256": session.sha256,
                    },
                )
                project = body.get("project")
                if (
                    not isinstance(project, dict)
                    or project.get("type") != "pdf"
                    or not isinstance(project.get("id"), str)
                ):
                    raise McpServiceError(
                        "BACKEND_ERROR",
                        "workspace returned invalid project metadata",
                    )
                mcp_store.finish_upload(self.db_path, session.id)
                audit_context.update({
                    "project_id": project["id"],
                    "targets": (f"project:{project['id']}",),
                })
                return {
                    "ok": True,
                    "project": self._public_project(project),
                }
            except Exception:
                mcp_store.fail_upload(
                    self.db_path, session.id, "BACKEND_ERROR"
                )
                raise

        return await self._run(
            "finish_pdf_project_upload",
            "projects:write",
            operation,
            mutating=True,
            audit_context=audit_context,
        )

    async def begin_pdf_replacement(
        self,
        project_handle: str,
        filename: str,
        size: int,
        sha256: str,
    ) -> RemoteToolResult:
        async def operation(identity):
            lease, project = await self._typed_project(
                identity, project_handle, "pdf"
            )
            _, safe_filename = self._pdf_upload_inputs(
                project.get("name") or "PDF project", filename
            )
            public, capability = mcp_store.begin_upload(
                self.db_path,
                identity,
                "pdf_replacement",
                lease.project_id,
                "document.pdf",
                size,
                sha256,
                safe_filename,
            )
            return self._upload_result(
                self.gateway.public_base_url, public, capability
            )

        return await self._run(
            "begin_pdf_replacement",
            "documents:write",
            operation,
        )

    async def finish_pdf_replacement(
        self,
        project_handle: str,
        upload_id: str,
        message: str = "",
    ) -> RemoteToolResult:
        audit_context = {}

        async def operation(identity):
            lease, _ = await self._typed_project(
                identity, project_handle, "pdf"
            )
            safe_upload_id = self._opaque_id(upload_id, "upload id")
            if not isinstance(message, str) or len(message) > 4096:
                raise McpServiceError(
                    "PATH_NOT_ALLOWED",
                    "replacement message is invalid",
                )
            session = mcp_store.complete_upload(
                self.db_path,
                safe_upload_id,
                identity,
                expected_kind="pdf_replacement",
                expected_project_id=lease.project_id,
            )
            try:
                current, _ = await self._typed_project(
                    identity, project_handle, "pdf"
                )
                if (
                    current.project_id != lease.project_id
                    or current.context_version != lease.context_version
                ):
                    raise McpServiceError(
                        "PROJECT_CONTEXT_CHANGED",
                        "the active project context changed; open the project again",
                    )
                audit_context.update({
                    "project_id": lease.project_id,
                    "targets": ("document:document.pdf",),
                })
                body = await self.gateway.request(
                    identity,
                    "POST",
                    "/api/agent/pdf/replace-from-upload",
                    json={
                        "upload_id": session.id,
                        "filename": session.filename,
                        "size": session.size,
                        "sha256": session.sha256,
                        "message": message,
                    },
                    timeout=120,
                )
                result = self._checked_backend_result(lease, body)
                mcp_store.finish_upload(self.db_path, session.id)
                return result
            except Exception:
                mcp_store.fail_upload(
                    self.db_path, session.id, "BACKEND_ERROR"
                )
                raise

        return await self._run(
            "finish_pdf_replacement",
            "documents:write",
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
        instructions=REMOTE_MCP_INSTRUCTIONS,
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
        """Replace an ordinary text file at expected_sha256; Typst projects reject .typ paths because all source belongs in main.typ."""
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
        """Move an ordinary file/directory without overwriting; Typst projects reject .typ paths and protected state cannot move."""
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
        """Upload at most 1 MiB inline to an ordinary asset path; Typst projects reject auxiliary .typ source."""
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
        """Begin a 100 MiB asset upload; Typst projects reject .typ paths. PUT once, then call finish_file_upload."""
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

    @server.tool()
    async def get_document(
        project_handle: str,
        offset: int = 1,
        limit: int = 120,
    ) -> CallToolResult:
        """Read a bounded line-numbered window from the handled live Typst CRDT document."""
        return _protocol_result(
            await service.get_document(
                project_handle, offset, limit
            )
        )

    @server.tool()
    async def find_in_document(
        project_handle: str,
        query: str,
        max_hits: int = 40,
    ) -> CallToolResult:
        """Find up to 40 literal case-sensitive matches in the handled live Typst document."""
        return _protocol_result(
            await service.find_in_document(
                project_handle, query, max_hits
            )
        )

    @server.tool()
    async def locate(
        project_handle: str,
        page: int = 0,
        slide: int = 0,
    ) -> CallToolResult:
        """Map exactly one rendered page or logical slide to current Typst source lines."""
        return _protocol_result(
            await service.locate(project_handle, page, slide)
        )

    @server.tool()
    async def apply_edits(
        project_handle: str,
        edits: list[dict],
        base_rev: int | None = None,
    ) -> CallToolResult:
        """Atomically edit main.typ through the live CRDT; keep all source inline, Touying-based, and free of local .typ imports/includes.

        Each edit is an object `{"selector": <Selector>, "text": "<replacement>",
        "expect"?: "<current span>"}` — `text` is what replaces the selected span ("" deletes)
        and a Selector is exactly one of:
          - {"by": "anchor", "text": "<exact snippet>", "occurrence"?: 1,
             "side"?: "in"|"before"|"after"}   // "in" replaces the snippet; before/after insert
          - {"by": "lines", "start": <1-based>, "end"?: <1-based inclusive>}  // end omitted inserts at that line
          - {"by": "range", "from": <code point>, "to": <code point>}         // escape hatch
        There is no old_text/new_text form. The anchor must be copied verbatim from
        get_document / find_in_document and must match exactly once (extend it or pass
        `occurrence`). Optional `expect` is a compare-and-swap against the selected span, and
        `base_rev` is the `rev` from your last read — pass both when a human may be typing in
        the browser at the same time.

        The whole batch applies or none of it does. EDIT_REJECTED means the edit itself was
        wrong (bad shape, or a selector that no longer matches — see `error.details`);
        REVISION_CONFLICT means the document really moved, so re-read and retry.
        """
        return _protocol_result(
            await service.apply_edits(
                project_handle, edits, base_rev
            )
        )

    @server.tool()
    async def get_transcripts(
        project_handle: str,
    ) -> CallToolResult:
        """Read per-page speaker transcripts for the handled project."""
        return _protocol_result(
            await service.get_transcripts(project_handle)
        )

    @server.tool()
    async def get_pdf_info(
        project_handle: str,
    ) -> CallToolResult:
        """Read bounded metadata and page count for the handled PDF project."""
        return _protocol_result(
            await service.get_pdf_info(project_handle)
        )

    @server.tool()
    async def get_pdf_text(
        project_handle: str, page: int
    ) -> CallToolResult:
        """Extract embedded text from one page of the handled PDF project."""
        return _protocol_result(
            await service.get_pdf_text(project_handle, page)
        )

    @server.tool()
    async def set_transcript(
        project_handle: str, page: int, text: str
    ) -> CallToolResult:
        """Set the speaker transcript for one page of the handled PDF project."""
        return _protocol_result(
            await service.set_transcript(
                project_handle, page, text
            )
        )

    @server.tool()
    async def set_transcripts(
        project_handle: str, updates: list
    ) -> CallToolResult:
        """Atomically set speaker transcripts for multiple handled PDF pages."""
        return _protocol_result(
            await service.set_transcripts(
                project_handle, updates
            )
        )

    @server.tool()
    async def get_pending_comments(
        project_handle: str,
    ) -> CallToolResult:
        """List pending human Typst comments with their current CRDT-resolved locations."""
        return _protocol_result(
            await service.get_pending_comments(project_handle)
        )

    @server.tool()
    async def get_comment(
        project_handle: str, comment_id: str
    ) -> CallToolResult:
        """Read one human Typst comment and its current CRDT-resolved location."""
        return _protocol_result(
            await service.get_comment(project_handle, comment_id)
        )

    @server.tool()
    async def mark_comment_done(
        project_handle: str,
        comment_id: str,
        note: str = "",
    ) -> CallToolResult:
        """Mark one handled Typst comment done after applying its requested change."""
        return _protocol_result(
            await service.mark_comment_done(
                project_handle, comment_id, note
            )
        )

    @server.tool()
    async def mark_comment_dismissed(
        project_handle: str,
        comment_id: str,
        reason: str = "",
    ) -> CallToolResult:
        """Dismiss one handled Typst comment as unclear, obsolete, or already resolved."""
        return _protocol_result(
            await service.mark_comment_dismissed(
                project_handle, comment_id, reason
            )
        )

    @server.tool()
    async def get_slide_preview(
        project_handle: str, page: int
    ) -> CallToolResult:
        """Return one handled Typst rendered page as bounded PNG MCP image content."""
        return _image_protocol_result(
            await service.get_slide_preview(project_handle, page)
        )

    @server.tool()
    async def get_page_preview(
        project_handle: str, page: int
    ) -> CallToolResult:
        """Return one handled Typst or PDF rendered page as bounded PNG MCP image content."""
        return _image_protocol_result(
            await service.get_page_preview(project_handle, page)
        )

    @server.tool()
    async def export_pdf(
        project_handle: str,
    ) -> CallToolResult:
        """Compile the handled live Typst deck and return a five-minute one-time PDF download."""
        return _protocol_result(
            await service.export_pdf(project_handle)
        )

    @server.tool()
    async def begin_pdf_project_upload(
        name: str,
        filename: str,
        size: int,
        sha256: str,
    ) -> CallToolResult:
        """Begin a one-PDF project upload; PUT once with the returned Upload authorization, then finish_pdf_project_upload."""
        return _protocol_result(
            await service.begin_pdf_project_upload(
                name, filename, size, sha256
            )
        )

    @server.tool()
    async def finish_pdf_project_upload(
        upload_id: str,
    ) -> CallToolResult:
        """Validate a completed staged PDF upload and atomically create its immutable-type project."""
        return _protocol_result(
            await service.finish_pdf_project_upload(upload_id)
        )

    @server.tool()
    async def begin_pdf_replacement(
        project_handle: str,
        filename: str,
        size: int,
        sha256: str,
    ) -> CallToolResult:
        """Begin a versioned replacement upload for the handled PDF project."""
        return _protocol_result(
            await service.begin_pdf_replacement(
                project_handle, filename, size, sha256
            )
        )

    @server.tool()
    async def finish_pdf_replacement(
        project_handle: str,
        upload_id: str,
        message: str = "",
    ) -> CallToolResult:
        """Validate and install a staged PDF through the existing locked, transcript-preserving version flow."""
        return _protocol_result(
            await service.finish_pdf_replacement(
                project_handle, upload_id, message
            )
        )

    return server
