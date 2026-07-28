"""Authenticated, loopback-only adapter for a user's workspace backend."""

from __future__ import annotations

import inspect
import json as json_module
from contextlib import asynccontextmanager
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import httpx

from mcp_errors import McpServiceError
from pat_store import PatIdentity


_MAX_JSON_RESPONSE_BYTES = 8 * 1024 * 1024


async def _resolve(value):
    if inspect.isawaitable(value):
        return await value
    return value


class WorkspaceGateway:
    def __init__(
        self,
        ensure_workspace: Callable,
        workspace_up: Callable,
        client: httpx.AsyncClient,
        public_base_url: str,
    ):
        base_url = public_base_url.rstrip("/")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("public_base_url must be an HTTP(S) origin")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("public_base_url must not contain a path")
        self._ensure_workspace = ensure_workspace
        self._workspace_up = workspace_up
        self._client = client
        self.public_base_url = base_url

    @staticmethod
    def _backend_url(identity: PatIdentity, path: str) -> str:
        if (
            not isinstance(path, str)
            or not path.startswith("/api/")
            or path.startswith("//")
            or "://" in path
        ):
            raise ValueError("workspace path must be an /api/ path")
        port = identity.port
        if isinstance(port, bool) or not isinstance(port, int):
            raise ValueError("invalid workspace identity")
        if port < 1 or port > 65535:
            raise ValueError("invalid workspace identity")
        return f"http://127.0.0.1:{port}{path}"

    async def request(
        self,
        identity: PatIdentity,
        method: str,
        path: str,
        json: Any = None,
        timeout: float = 30,
    ) -> dict:
        url = self._backend_url(identity, path)
        was_up = bool(await _resolve(self._workspace_up(identity)))
        if not was_up:
            await _resolve(self._ensure_workspace(identity))

        kwargs = {"timeout": timeout}
        if json is not None:
            kwargs["json"] = json
        try:
            async with self._client.stream(
                method.upper(), url, **kwargs
            ) as response:
                if response.status_code >= 400:
                    error_payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        error_payload.extend(chunk)
                        if len(error_payload) > 64 * 1024:
                            break
                    error_code = None
                    try:
                        error_body = json_module.loads(error_payload)
                        detail = error_body.get("detail")
                        if isinstance(detail, dict):
                            candidate = detail.get("code")
                            if isinstance(candidate, str):
                                error_code = candidate
                    except (TypeError, ValueError):
                        pass
                    self._raise_backend_status(
                        response.status_code, path, error_code
                    )
                declared_size = response.headers.get("content-length")
                if declared_size is not None:
                    try:
                        if int(declared_size) > _MAX_JSON_RESPONSE_BYTES:
                            raise McpServiceError(
                                "BACKEND_ERROR",
                                "workspace response is too large",
                            )
                    except ValueError as exc:
                        raise McpServiceError(
                            "BACKEND_ERROR",
                            "workspace returned an invalid response",
                        ) from exc
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > _MAX_JSON_RESPONSE_BYTES:
                        raise McpServiceError(
                            "BACKEND_ERROR",
                            "workspace response is too large",
                        )
        except httpx.HTTPError as exc:
            if was_up:
                raise McpServiceError(
                    "WORKSPACE_UNAVAILABLE",
                    "workspace is unavailable",
                    retryable=True,
                    retry_after=1.0,
                ) from exc
            raise McpServiceError(
                "WORKSPACE_STARTING",
                "workspace is starting",
                retryable=True,
                retry_after=1.0,
            ) from exc

        try:
            body = json_module.loads(payload)
        except (TypeError, ValueError) as exc:
            raise McpServiceError(
                "BACKEND_ERROR", "workspace returned an invalid response"
            ) from exc
        if not isinstance(body, dict):
            raise McpServiceError(
                "BACKEND_ERROR", "workspace returned an invalid response"
            )
        return body

    @asynccontextmanager
    async def stream_response(
        self,
        identity: PatIdentity,
        path: str,
        timeout: float = 30,
    ):
        """Open one fixed backend byte stream without buffering it in control."""
        url = self._backend_url(identity, path)
        was_up = bool(await _resolve(self._workspace_up(identity)))
        if not was_up:
            await _resolve(self._ensure_workspace(identity))
        manager = self._client.stream(
            "GET", url, timeout=timeout
        )
        try:
            response = await manager.__aenter__()
        except httpx.HTTPError as exc:
            if was_up:
                raise McpServiceError(
                    "WORKSPACE_UNAVAILABLE",
                    "workspace is unavailable",
                    retryable=True,
                    retry_after=1.0,
                ) from exc
            raise McpServiceError(
                "WORKSPACE_STARTING",
                "workspace is starting",
                retryable=True,
                retry_after=1.0,
            ) from exc
        try:
            yield response
        finally:
            await manager.__aexit__(None, None, None)

    @staticmethod
    def _raise_backend_status(
        status_code: int,
        path: str,
        error_code: str | None = None,
    ) -> None:
        if status_code == 404:
            if path.startswith("/api/projects/"):
                raise McpServiceError(
                    "PROJECT_NOT_FOUND", "project not found"
                )
            raise McpServiceError("FILE_NOT_FOUND", "resource not found")
        if status_code == 409:
            if error_code == "REVISION_CONFLICT":
                raise McpServiceError(
                    "REVISION_CONFLICT",
                    "file revision changed; read it again before writing",
                )
            raise McpServiceError(
                "DESTINATION_EXISTS", "destination already exists"
            )
        if status_code == 403:
            raise McpServiceError(
                "PATH_NOT_ALLOWED", "path is not allowed"
            )
        if status_code == 400 and path.startswith("/api/agent/files"):
            raise McpServiceError(
                "PATH_NOT_ALLOWED", "file operation was rejected"
            )
        if status_code == 413:
            raise McpServiceError("FILE_TOO_LARGE", "file is too large")
        if status_code == 503:
            raise McpServiceError(
                "WORKSPACE_STARTING",
                "workspace is starting",
                retryable=True,
                retry_after=1.0,
            )
        if status_code >= 500:
            raise McpServiceError(
                "WORKSPACE_UNAVAILABLE",
                "workspace is unavailable",
                retryable=True,
                retry_after=1.0,
            )
        raise McpServiceError(
            "BACKEND_ERROR", "workspace rejected the request"
        )

    async def list_projects(self, identity: PatIdentity) -> list[dict]:
        body = await self.request(identity, "GET", "/api/projects")
        projects = body.get("projects")
        if not isinstance(projects, list):
            raise McpServiceError(
                "BACKEND_ERROR", "workspace returned an invalid project list"
            )
        return projects

    async def create_typst_project(
        self, identity: PatIdentity, name: str
    ) -> dict:
        body = await self.request(
            identity, "POST", "/api/projects", json={"name": name}
        )
        if not isinstance(body.get("id"), str):
            raise McpServiceError(
                "BACKEND_ERROR", "workspace returned an invalid project"
            )
        return body

    async def open_project(
        self, identity: PatIdentity, project_id: str
    ) -> dict:
        encoded = quote(project_id, safe="")
        body = await self.request(
            identity, "POST", f"/api/projects/{encoded}/open"
        )
        if (
            not isinstance(body.get("project"), dict)
            or body.get("project_id") != project_id
            or not isinstance(body.get("context_version"), str)
        ):
            raise McpServiceError(
                "BACKEND_ERROR", "workspace returned an invalid project context"
            )
        return body

    async def active_context(self, identity: PatIdentity) -> dict:
        body = await self.request(identity, "GET", "/api/app/state")
        if not isinstance(body.get("context_version"), str):
            raise McpServiceError(
                "BACKEND_ERROR", "workspace returned an invalid project context"
            )
        project_id = body.get("project_id")
        if project_id is not None and not isinstance(project_id, str):
            raise McpServiceError(
                "BACKEND_ERROR", "workspace returned an invalid project context"
            )
        return body

    def project_web_url(self, project_id: str) -> str:
        return (
            f"{self.public_base_url}/?openProject="
            f"{quote(project_id, safe='')}"
        )
