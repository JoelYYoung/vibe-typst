"""Stable, structured errors exposed by the remote project-control MCP."""

from __future__ import annotations


ERROR_CODES = frozenset(
    {
        "AUTH_REQUIRED",
        "TOKEN_INVALID",
        "TOKEN_EXPIRED",
        "TOKEN_REVOKED",
        "ACCOUNT_LOCKED",
        "SCOPE_DENIED",
        "RATE_LIMITED",
        "WORKSPACE_STARTING",
        "WORKSPACE_UNAVAILABLE",
        "PROJECT_NOT_FOUND",
        "PROJECT_CONTEXT_CHANGED",
        "PROJECT_HANDLE_EXPIRED",
        "REVISION_CONFLICT",
        "EDIT_REJECTED",
        "PATH_NOT_ALLOWED",
        "FILE_NOT_FOUND",
        "DESTINATION_EXISTS",
        "FILE_TOO_LARGE",
        "CHECKSUM_MISMATCH",
        "UPLOAD_EXPIRED",
        "UPLOAD_ALREADY_USED",
        "CAPABILITY_NOT_AVAILABLE",
        "BACKEND_ERROR",
    }
)


class McpServiceError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        retry_after: float | None = None,
        details: dict | None = None,
    ):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown MCP error code: {code}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = bool(retryable)
        self.retry_after = retry_after
        # Curated, caller-actionable extras only (which edit failed, the live text around it).
        # Raising sites build this explicitly; a backend body is never forwarded wholesale.
        self.details = dict(details) if details else None

    def as_dict(self) -> dict:
        detail = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.retry_after is not None:
            detail["retry_after"] = self.retry_after
        if self.details:
            detail["details"] = dict(self.details)
        return {"ok": False, "error": detail}
