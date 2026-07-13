"""ASGI entry point for a private user-keyed live search service."""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from kasm.adapters.korea.bills import BILL_DATASET
from kasm.adapters.korea.client import AssemblyOpenApiClient
from kasm.app import create_auto_services
from kasm.live import create_live_services

from .middleware import FixedWindowRateLimit
from .remote_auth import RemoteTokenAuth, request_api_key, result_page, setup_page
from .server import create_server


def create_asgi_app() -> Any:
    try:
        from mcp.server.transport_security import TransportSecuritySettings
        from starlette.applications import Starlette
        from starlette.concurrency import run_in_threadpool
        from starlette.requests import Request
        from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
        from starlette.routing import Mount, Route
    except ImportError as exc:  # pragma: no cover - deploy extra
        raise RuntimeError("install the deploy extra to run the public HTTP service") from exc

    remote_secret = os.getenv("KBD_REMOTE_TOKEN_SECRET")
    token_codec: RemoteTokenAuth | None = None
    if remote_secret:
        data_dir = Path(os.getenv("KBD_DATA_DIR", "/tmp/kbd-remote"))
        client = AssemblyOpenApiClient(
            "request-scoped-key",
            api_key_provider=request_api_key,
            cache_dir=data_dir / "api-cache",
        )
        services = create_live_services(
            client=client,
            data_dir=data_dir,
            max_minutes_per_request=int(os.getenv("KBD_REMOTE_MAX_MINUTES_PER_REQUEST", "1")),
        )
        token_codec = RemoteTokenAuth(None, remote_secret)
    else:
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
                "remote_user_key": token_codec is not None,
            }
        )

    async def home(_request: Request) -> HTMLResponse:
        if token_codec is None:
            return HTMLResponse(setup_page(error="Remote user-key mode is not configured"), 503)
        return HTMLResponse(setup_page(), headers={"Cache-Control": "no-store"})

    async def connect(request: Request) -> HTMLResponse | RedirectResponse:
        if token_codec is None:
            return HTMLResponse(setup_page(error="Remote user-key mode is not configured"), 503)
        if request.method == "GET":
            return RedirectResponse("/", status_code=303)
        form = await request.form()
        api_key = str(form.get("api_key") or "").strip()
        try:
            await run_in_threadpool(_validate_remote_key, api_key)
            token = token_codec.issue(api_key)
        except (RuntimeError, ValueError) as exc:
            return HTMLResponse(setup_page(error=str(exc)), 400)
        base = str(request.base_url).rstrip("/")
        mcp_url = f"{base}/mcp?{urllib.parse.urlencode({'token': token})}"
        return HTMLResponse(result_page(mcp_url), headers={"Cache-Control": "no-store"})

    @asynccontextmanager
    async def lifespan(_application: Any) -> AsyncIterator[None]:
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    application = Starlette(
        routes=[
            Route("/", home),
            Route("/connect", connect, methods=["GET", "POST"]),
            Route("/healthz", health),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )
    limit = int(os.getenv("KASM_RATE_LIMIT_PER_MINUTE", "120"))
    guarded: Any = FixedWindowRateLimit(application, limit)
    if token_codec is not None:
        guarded = RemoteTokenAuth(guarded, remote_secret or "")
    return guarded

def _validate_remote_key(api_key: str) -> None:
    """Reject invalid keys before issuing a password-equivalent MCP URL."""
    if not api_key or len(api_key) > 256:
        raise ValueError("열린국회 API 키를 확인해 주세요. / Check your Open Assembly API key.")
    try:
        AssemblyOpenApiClient(api_key, cache_ttl_seconds=0).fetch_page(
            BILL_DATASET,
            page_size=1,
            parameters={"AGE": 22},
            refresh=True,
        )
    except RuntimeError as exc:
        raise RuntimeError(
            "열린국회 API 키가 유효하지 않거나 공식 API에 연결할 수 없습니다. / "
            "The key is invalid or the official API is temporarily unavailable."
        ) from exc


app = create_asgi_app()
