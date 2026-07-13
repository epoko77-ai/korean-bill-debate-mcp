"""Minimal Vercel composition root for durable queue deliveries.

The queue worker intentionally does not import or initialize the public MCP,
OAuth, workspace, or database application.  Keeping it as a separate Python
Function removes that cold-start work from every background task while the
shared-secret ASGI boundary remains the only callable surface.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from .runtime import HostedResearchRuntime, create_hosted_research_runtime
from .task_dispatch import ResearchTaskDispatchASGI, ResearchTaskDispatcher

_RuntimeFactory = Callable[..., HostedResearchRuntime]


def create_research_dispatch_app(
    *,
    runtime_factory: _RuntimeFactory = create_hosted_research_runtime,
) -> ResearchTaskDispatchASGI:
    """Build a network-lazy worker, or a fail-closed 503 boundary."""

    secret = _validated_internal_secret()
    if secret is None or not _complete_worker_configuration():
        return ResearchTaskDispatchASGI(None, secret="")
    try:
        runtime = runtime_factory(assembly_api_key_provider=lambda: None)
    except (RuntimeError, ValueError):
        # Configuration errors must never leave a partially wired worker that
        # can acknowledge queue messages.  The bridge retains 5xx deliveries.
        return ResearchTaskDispatchASGI(None, secret=secret)
    return ResearchTaskDispatchASGI(
        ResearchTaskDispatcher(runtime.engine),
        secret=secret,
    )


def _complete_worker_configuration() -> bool:
    values = (
        os.getenv("KBD_RESEARCH_CREDENTIAL_SECRET", "")
        or os.getenv("KBD_REMOTE_TOKEN_SECRET", ""),
        os.getenv("BLOB_READ_WRITE_TOKEN", "")
        or os.getenv("VERCEL_BLOB_READ_WRITE_TOKEN", ""),
        os.getenv("VERCEL_DEPLOYMENT_ID", ""),
        os.getenv("VERCEL_URL", ""),
    )
    return all(value.strip() for value in values)


def _validated_internal_secret() -> str | None:
    value = os.getenv("KBD_INTERNAL_TASK_SECRET", "")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return None
    if not 32 <= len(encoded) <= 512 or any(
        not 33 <= character <= 126 for character in encoded
    ):
        return None
    return value


app: Any = create_research_dispatch_app()


__all__ = ["app", "create_research_dispatch_app"]
