"""ASGI entry point for a private user-keyed live search service."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from kasm.app import create_auto_services

from .middleware import FixedWindowRateLimit
from .server import create_server


def create_asgi_app() -> Any:
    try:
        from mcp.server.transport_security import TransportSecuritySettings
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Mount, Route
    except ImportError as exc:  # pragma: no cover - deploy extra
        raise RuntimeError("install the deploy extra to run the public HTTP service") from exc

    services = create_auto_services()
    allowed_hosts = [
        item.strip()
        for item in os.getenv(
            "KASM_ALLOWED_HOSTS",
            "127.0.0.1,127.0.0.1:*,localhost,localhost:*,[::1],[::1]:*",
        ).split(",")
        if item.strip()
    ]
    allowed_origins = [
        item.strip()
        for item in os.getenv(
            "KASM_ALLOWED_ORIGINS",
            "http://127.0.0.1,http://127.0.0.1:*,http://localhost,"
            "http://localhost:*,http://[::1],http://[::1]:*",
        ).split(",")
        if item.strip()
    ]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    mcp_app = create_server(
        services, host="0.0.0.0", transport_security=security
    ).streamable_http_app()

    async def health(_request: Any) -> JSONResponse:
        search = services.search
        database = getattr(search, "database", None)
        counts = {"meetings": None, "speeches": None, "bills": None}
        if database is not None:
            counts = {
                "meetings": database.connection.execute("SELECT count(*) FROM meetings").fetchone()[
                    0
                ],
                "speeches": database.connection.execute("SELECT count(*) FROM speeches").fetchone()[
                    0
                ],
                "bills": database.connection.execute("SELECT count(*) FROM bills").fetchone()[0],
            }
        hybrid = getattr(search, "hybrid", None)
        return JSONResponse(
            {
                "status": "ok",
                "service": "korean-bill-debate-mcp",
                **counts,
                "semantic_index": hybrid is not None,
            }
        )

    @asynccontextmanager
    async def lifespan(_application: Any) -> AsyncIterator[None]:
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    application = Starlette(
        routes=[Route("/healthz", health), Mount("/", app=mcp_app)],
        lifespan=lifespan,
    )
    limit = int(os.getenv("KASM_RATE_LIMIT_PER_MINUTE", "120"))
    return FixedWindowRateLimit(application, limit)


app = create_asgi_app()
