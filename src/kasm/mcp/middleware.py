"""Dependency-free ASGI operational guards."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

AsgiApp = Callable[[Any, Any, Any], Awaitable[None]]


class FixedWindowRateLimit:
    """Small per-process guard; production ingress remains the authoritative limiter."""

    def __init__(self, app: AsgiApp, requests_per_minute: int = 120) -> None:
        if requests_per_minute < 1:
            raise ValueError("requests_per_minute must be positive")
        self.app = app
        self.limit = requests_per_minute
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        address = str(client[0]) if client else "unknown"
        now = time.monotonic()
        requests = self._requests[address]
        while requests and requests[0] <= now - 60:
            requests.popleft()
        if len(requests) >= self.limit:
            body = b'{"error":"rate limit exceeded"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", b"60"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        requests.append(now)
        await self.app(scope, receive, send)
