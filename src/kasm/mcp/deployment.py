"""ASGI entry point for a private user-keyed live search service."""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

from kasm import __version__
from kasm.adapters.korea.bills import BILL_DATASET
from kasm.adapters.korea.client import AssemblyOpenApiClient
from kasm.app import create_auto_services
from kasm.live import create_live_services
from kasm.research.runtime import (
    HostedResearchRuntime,
    create_hosted_research_runtime,
)
from kasm.research.task_dispatch import (
    INTERNAL_DISPATCH_PATH,
    ResearchTaskDispatchASGI,
    ResearchTaskDispatcher,
    ResearchTaskEngine,
)
from kasm.workspace import WorkspaceError, run_workspace_research
from kasm.workspace.ui import workspace_page, workspace_script

from .middleware import FixedWindowRateLimit
from .remote_auth import RemoteTokenAuth, request_api_key, result_page, setup_page
from .remote_oauth import RemoteOAuth
from .server import create_server

_HOSTED_MCP_CLIENT_ORIGINS = (
    "https://claude.ai",
    "https://chatgpt.com",
    "https://chat.openai.com",
)


def create_asgi_app(*, research_task_engine: ResearchTaskEngine | None = None) -> Any:
    try:
        from mcp.server.transport_security import TransportSecuritySettings
        from starlette.applications import Starlette
        from starlette.concurrency import run_in_threadpool
        from starlette.requests import Request
        from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
        from starlette.routing import Mount, Route
    except ImportError as exc:  # pragma: no cover - deploy extra
        raise RuntimeError("install the deploy extra to run the public HTTP service") from exc

    remote_secret = os.getenv("KBD_REMOTE_TOKEN_SECRET")
    token_codec: RemoteTokenAuth | None = None
    oauth: RemoteOAuth | None = None
    if remote_secret:
        data_dir = Path(os.getenv("KBD_DATA_DIR", "/tmp/kbd-remote"))
        client = AssemblyOpenApiClient(
            "request-scoped-key",
            api_key_provider=request_api_key,
            cache_dir=data_dir / "api-cache",
            timeout=float(os.getenv("KBD_REMOTE_API_TIMEOUT_SECONDS", "12")),
        )
        services = create_live_services(
            client=client,
            data_dir=data_dir,
            max_minutes_per_request=int(os.getenv("KBD_REMOTE_MAX_MINUTES_PER_REQUEST", "2")),
            source_timeout=float(os.getenv("KBD_REMOTE_SOURCE_TIMEOUT_SECONDS", "15")),
        )
        token_codec = RemoteTokenAuth(None, remote_secret)
        oauth = RemoteOAuth(token_codec)
    else:
        services = create_auto_services()
    research_runtime = _hosted_research_runtime(remote_secret)
    if research_runtime is not None:
        services.research = research_runtime.backend
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
    if token_codec is not None:
        # The hosted user-key service is explicitly intended for these web MCP
        # clients.  Preserve operator-supplied origins while preventing an old
        # environment value from making tool discovery fail before OAuth auth.
        # Every MCP request still requires a valid bearer/path credential.
        allowed_origins = list(
            dict.fromkeys([*allowed_origins, *_HOSTED_MCP_CLIENT_ORIGINS])
        )
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    mcp_app = create_server(
        services, host="0.0.0.0", transport_security=security
    ).streamable_http_app()
    task_engine: ResearchTaskEngine | None = (
        research_runtime.engine if research_runtime is not None else research_task_engine
    )
    task_dispatch_app = ResearchTaskDispatchASGI(
        ResearchTaskDispatcher(task_engine) if task_engine is not None else None
    )

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
                "version": __version__,
                **counts,
                "semantic_index": hybrid is not None,
                "remote_user_key": token_codec is not None,
                # Keep deployment capability explicit.  A partially configured
                # hosted runtime intentionally falls back to the eight legacy
                # tools; without these fields that safety fallback is almost
                # impossible to distinguish from a successful 13-tool rollout.
                "durable_research": research_runtime is not None,
                "mcp_tool_count": 13 if research_runtime is not None else 8,
                "corpus_revision_configured": bool(
                    os.getenv("KBD_RESEARCH_CORPUS_REVISION", "").strip()
                ),
            }
        )

    async def home(_request: Request) -> HTMLResponse:
        if token_codec is None:
            return HTMLResponse(setup_page(error="Remote user-key mode is not configured"), 503)
        return HTMLResponse(setup_page(), headers=_private_headers())

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
            return HTMLResponse(setup_page(error=str(exc)), 400, headers=_private_headers())
        base = str(request.base_url).rstrip("/")
        mcp_url = f"{base}/mcp/t/{urllib.parse.quote(token, safe='')}"
        return HTMLResponse(result_page(mcp_url), headers=_private_headers())

    def oauth_base(request: Request) -> str:
        return str(request.base_url).rstrip("/")

    async def protected_resource(request: Request) -> JSONResponse:
        if oauth is None:
            return JSONResponse({"error": "OAuth is not configured"}, 503)
        return JSONResponse(
            oauth.protected_resource_metadata(oauth_base(request)),
            headers=_oauth_headers(),
        )

    async def authorization_metadata(request: Request) -> JSONResponse:
        if oauth is None:
            return JSONResponse({"error": "OAuth is not configured"}, 503)
        return JSONResponse(
            oauth.authorization_server_metadata(oauth_base(request)),
            headers=_oauth_headers(),
        )

    async def oauth_register(request: Request) -> JSONResponse:
        if oauth is None:
            return JSONResponse({"error": "OAuth is not configured"}, 503)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("registration metadata must be an object")
            result = oauth.register(payload)
        except (ValueError, UnicodeError) as exc:
            return JSONResponse(
                {"error": "invalid_client_metadata", "error_description": str(exc)},
                400,
                headers=_oauth_headers(),
            )
        return JSONResponse(result, 201, headers=_oauth_headers())

    async def oauth_authorize(request: Request) -> HTMLResponse | RedirectResponse:
        if oauth is None:
            return HTMLResponse("OAuth is not configured", 503)
        if request.method == "GET":
            values = {name: value for name, value in request.query_params.items()}
            values.setdefault("resource", f"{oauth_base(request)}/mcp")
            try:
                if values["resource"] != f"{oauth_base(request)}/mcp":
                    raise ValueError("resource does not match this MCP server")
                page = oauth.authorization_page(values)
            except ValueError as exc:
                return HTMLResponse(str(exc), 400, headers=_private_headers())
            return HTMLResponse(
                page,
                headers=_private_headers(
                    oauth_redirect_origin=_redirect_origin(values.get("redirect_uri", ""))
                ),
            )
        form = await request.form()
        values = {name: str(value) for name, value in form.multi_items() if name != "api_key"}
        values.setdefault("resource", f"{oauth_base(request)}/mcp")
        api_key = str(form.get("api_key") or "").strip()
        try:
            if values["resource"] != f"{oauth_base(request)}/mcp":
                raise ValueError("resource does not match this MCP server")
            _validate_remote_key_shape(api_key)
            location = oauth.authorize(values, api_key)
        except (RuntimeError, ValueError) as exc:
            try:
                page = oauth.authorization_page(values, error=str(exc))
            except ValueError:
                page = str(exc)
            return HTMLResponse(
                page,
                400,
                headers=_private_headers(
                    oauth_redirect_origin=_redirect_origin(values.get("redirect_uri", ""))
                ),
            )
        return RedirectResponse(location, status_code=303, headers=_oauth_headers())

    async def oauth_token(request: Request) -> JSONResponse:
        if oauth is None:
            return JSONResponse({"error": "OAuth is not configured"}, 503)
        form = await request.form()
        values = {name: str(value) for name, value in form.multi_items()}
        try:
            result = oauth.token(values)
        except ValueError as exc:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": str(exc)},
                400,
                headers=_oauth_headers(),
            )
        return JSONResponse(result, headers=_oauth_headers())

    async def workspace(_request: Request) -> HTMLResponse:
        if token_codec is None:
            return HTMLResponse(
                setup_page(error="Remote user-key mode is not configured"),
                503,
                headers=_private_headers(),
            )
        return HTMLResponse(workspace_page(), headers=_private_headers(workspace=True))

    async def workspace_javascript(_request: Request) -> Response:
        return Response(
            workspace_script(),
            media_type="text/javascript",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def research(request: Request) -> JSONResponse:
        if token_codec is None:
            return JSONResponse(
                {"error": "플랫폼이 아직 설정되지 않았습니다."},
                503,
                headers=_private_headers(),
            )
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            return JSONResponse(
                {"error": "application/json 요청만 허용됩니다."},
                415,
                headers=_private_headers(),
            )
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            content_length = 0
        if content_length > 8192:
            return JSONResponse(
                {"error": "요청 크기가 너무 큽니다."}, 413, headers=_private_headers()
            )
        try:
            payload = await request.json()
        except (ValueError, UnicodeError):
            return JSONResponse(
                {"error": "JSON 요청 형식을 확인해 주세요."},
                400,
                headers=_private_headers(),
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                {"error": "요청 형식을 확인해 주세요."}, 400, headers=_private_headers()
            )
        values = {
            name: str(payload.get(name) or "")
            for name in ("question", "assembly_api_key", "llm_provider", "llm_api_key")
        }
        try:
            execute = partial(
                run_workspace_research,
                question=values["question"],
                assembly_api_key=values["assembly_api_key"],
                llm_provider=values["llm_provider"],
                llm_api_key=values["llm_api_key"],
            )
            result = await run_in_threadpool(execute)
        except WorkspaceError as exc:
            return JSONResponse(
                {"error": str(exc)}, exc.status_code, headers=_private_headers()
            )
        return JSONResponse(result, headers=_private_headers())

    @asynccontextmanager
    async def lifespan(_application: Any) -> AsyncIterator[None]:
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    application = Starlette(
        routes=[
            Route("/", home),
            Route("/connect", connect, methods=["GET", "POST"]),
            Route(
                "/.well-known/oauth-protected-resource",
                protected_resource,
            ),
            Route(
                "/.well-known/oauth-protected-resource/mcp",
                protected_resource,
            ),
            Route(
                "/.well-known/oauth-authorization-server",
                authorization_metadata,
            ),
            Route("/oauth/register", oauth_register, methods=["POST"]),
            Route("/oauth/authorize", oauth_authorize, methods=["GET", "POST"]),
            Route("/oauth/token", oauth_token, methods=["POST"]),
            Route("/workspace", workspace),
            Route("/workspace/app.js", workspace_javascript),
            Route("/workspace/research", research, methods=["POST"]),
            Route(INTERNAL_DISPATCH_PATH, task_dispatch_app, methods=["POST"]),
            Route("/healthz", health),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )
    limit = int(os.getenv("KASM_RATE_LIMIT_PER_MINUTE", "120"))
    workspace_limit = int(os.getenv("KBD_WORKSPACE_RATE_LIMIT_PER_MINUTE", "6"))
    guarded: Any = FixedWindowRateLimit(
        application,
        limit,
        path_limits={
            "/workspace/research": workspace_limit,
            "/oauth/register": 30,
            "/oauth/authorize": 30,
            "/oauth/token": 60,
            "/mcp": limit,
        },
    )
    if token_codec is not None:
        guarded = RemoteTokenAuth(guarded, remote_secret or "")
    return guarded


def _private_headers(
    *, workspace: bool = False, oauth_redirect_origin: str = ""
) -> dict[str, str]:
    script_source = "'self'" if workspace else "'none'"
    form_action = "'self'"
    if oauth_redirect_origin:
        form_action += f" {oauth_redirect_origin}"
    return {
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": (
            "default-src 'none'; "
            f"script-src {script_source}; "
            "style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
            f"base-uri 'none'; form-action {form_action}; frame-ancestors 'none'"
        ),
    }


def _oauth_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }


def _redirect_origin(uri: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(uri)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _validate_remote_key(api_key: str) -> None:
    """Reject invalid keys before issuing a password-equivalent MCP URL."""
    _validate_remote_key_shape(api_key)
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


def _validate_remote_key_shape(api_key: str) -> None:
    """Reject empty or implausibly large credentials without a network round trip."""
    if not api_key or len(api_key) > 256:
        raise ValueError("열린국회 API 키를 확인해 주세요. / Check your Open Assembly API key.")


def _hosted_research_runtime(remote_secret: str | None) -> HostedResearchRuntime | None:
    """Build one request/worker runtime only for a complete hosted configuration.

    Constructors for Blob, Queue, and official-source clients remain lazy, so
    importing the Vercel entry point performs no network I/O. A partial setup
    deliberately exposes neither the durable MCP tools nor an active worker.
    """

    internal_secret = os.getenv("KBD_INTERNAL_TASK_SECRET", "")
    values = (
        remote_secret or "",
        os.getenv("KBD_RESEARCH_CREDENTIAL_SECRET", "") or remote_secret or "",
        os.getenv("BLOB_READ_WRITE_TOKEN", "")
        or os.getenv("VERCEL_BLOB_READ_WRITE_TOKEN", ""),
        os.getenv("VERCEL_DEPLOYMENT_ID", ""),
        os.getenv("VERCEL_URL", ""),
        internal_secret,
    )
    if not all(value.strip() for value in values):
        return None
    try:
        encoded_internal_secret = internal_secret.encode("ascii")
    except UnicodeEncodeError:
        return None
    if not 32 <= len(encoded_internal_secret) <= 512 or any(
        not 33 <= character <= 126 for character in encoded_internal_secret
    ):
        return None
    return create_hosted_research_runtime(assembly_api_key_provider=request_api_key)


app = create_asgi_app()
