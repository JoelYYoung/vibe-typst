"""In-memory per-token call-rate and concurrency limits."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from mcp_errors import McpServiceError


class _Permit:
    def __init__(self, limiter: "TokenLimiter", token_id: str):
        self._limiter = limiter
        self._token_id = token_id
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._limiter._release(self._token_id)

    async def __aenter__(self) -> "_Permit":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.release()


class TokenLimiter:
    def __init__(
        self, calls_per_minute: int = 120, max_concurrent: int = 4
    ):
        if calls_per_minute < 1 or max_concurrent < 1:
            raise ValueError("rate and concurrency limits must be positive")
        self.calls_per_minute = calls_per_minute
        self.max_concurrent = max_concurrent
        self._calls: dict[str, deque[float]] = defaultdict(deque)
        self._active: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def acquire(self, token_id: str) -> _Permit:
        now = time.monotonic()
        async with self._lock:
            calls = self._calls[token_id]
            boundary = now - 60.0
            while calls and calls[0] <= boundary:
                calls.popleft()

            if self._active[token_id] >= self.max_concurrent:
                raise McpServiceError(
                    "RATE_LIMITED",
                    "too many concurrent calls",
                    retryable=True,
                    retry_after=0.1,
                )
            if len(calls) >= self.calls_per_minute:
                retry_after = max(0.0, calls[0] + 60.0 - now)
                raise McpServiceError(
                    "RATE_LIMITED",
                    "call rate exceeded",
                    retryable=True,
                    retry_after=retry_after,
                )

            calls.append(now)
            self._active[token_id] += 1
        return _Permit(self, token_id)

    def _release(self, token_id: str) -> None:
        active = self._active.get(token_id, 0)
        if active <= 1:
            self._active.pop(token_id, None)
        else:
            self._active[token_id] = active - 1
