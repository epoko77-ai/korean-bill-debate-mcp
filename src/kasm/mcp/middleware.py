"""Dependency-free ASGI operational guards."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

AsgiApp = Callable[[Any, Any, Any], Awaitable[None]]


class FixedWindowRateLimit:
    """Small per-process guard; production ingress remains the authoritative limiter."""

    def __init__(
        self,
        app: AsgiApp,
        requests_per_minute: int = 120,
        *,
        path_prefixes: tuple[str, ...] = ("/mcp",),
        path_limits: dict[str, int] | None = None,
    ) -> None:
        if requests_per_minute < 1:
            raise ValueError("requests_per_minute must be positive")
        if not path_prefixes and not path_limits:
            raise ValueError("path_prefixes must not be empty")
        if path_limits and any(limit < 1 for limit in path_limits.values()):
            raise ValueError("path limits must be positive")
        self.app = app
        self.limit = requests_per_minute
        self.path_limits = path_limits or {
            prefix: requests_per_minute for prefix in path_prefixes
        }
        self.path_prefixes = tuple(self.path_limits)
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = str(scope.get("path", ""))
        matched_prefix = next(
            (prefix for prefix in self.path_prefixes if path.startswith(prefix)), None
        )
        if scope.get("type") != "http" or matched_prefix is None:
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        address = str(client[0]) if client else "unknown"
        now = time.monotonic()
        requests = self._requests[f"{matched_prefix}:{address}"]
        while requests and requests[0] <= now - 60:
            requests.popleft()
        if len(requests) >= self.path_limits[matched_prefix]:
            body = b'{"error":"rate limit exceeded"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"cache-control", b"no-store"),
                        (b"retry-after", b"60"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        requests.append(now)
        await self.app(scope, receive, send)
