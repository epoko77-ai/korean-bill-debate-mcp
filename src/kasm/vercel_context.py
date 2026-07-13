"""Request-local Vercel workload identity propagation for ASGI handlers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def bind_vercel_oidc_token(token: str | None) -> Iterator[None]:
    """Expose one inbound Vercel OIDC header to the official SDK.

    Vercel Functions receive workload identity in ``x-vercel-oidc-token`` at
    request time rather than as a runtime environment variable. The SDK stores
    request headers in a ContextVar, which is propagated by both AnyIO and
    ``asyncio.to_thread``. Local/stdio installations do not require the SDK.
    """

    try:
        from vercel.headers import get_headers, set_headers
    except ImportError:
        yield
        return
    previous = get_headers()
    headers = {"x-vercel-oidc-token": token} if token else {}
    set_headers(headers)
    try:
        yield
    finally:
        set_headers(previous)


__all__ = ["bind_vercel_oidc_token"]
