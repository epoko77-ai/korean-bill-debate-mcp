from __future__ import annotations

import asyncio
import urllib.parse
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from pydantic import AnyUrl
from pytest import MonkeyPatch


class _MemoryTokenStorage:
    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


def test_official_mcp_sdk_completes_discovery_dcr_pkce_and_refresh_scope(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Exercise the same standards path used by web MCP hosts, not a hand-built bearer."""

    monkeypatch.setenv("KBD_REMOTE_TOKEN_SECRET", Fernet.generate_key().decode())
    monkeypatch.setenv("KBD_DATA_DIR", str(tmp_path / "oauth-sdk"))

    from kasm.mcp.deployment import create_asgi_app

    async def exercise() -> None:
        application = create_asgi_app()
        starlette = application.app.app
        transport = httpx.ASGITransport(app=application)
        storage = _MemoryTokenStorage()
        callback: tuple[str, str | None] | None = None

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
            follow_redirects=False,
        ) as browser:

            async def redirect_handler(authorization_url: str) -> None:
                nonlocal callback
                parsed = urllib.parse.urlsplit(authorization_url)
                authorization_values = dict(
                    urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
                )
                consent = await browser.get(authorization_url)
                assert consent.status_code == 200
                assert "본인의 열린국회 API 키" in consent.text
                authorized = await browser.post(
                    parsed.path,
                    data={**authorization_values, "api_key": "sdk-personal-key"},
                )
                assert authorized.status_code == 303
                returned = urllib.parse.urlsplit(authorized.headers["location"])
                values = dict(urllib.parse.parse_qsl(returned.query))
                callback = (values["code"], values.get("state"))

            async def callback_handler() -> tuple[str, str | None]:
                assert callback is not None
                return callback

            auth = OAuthClientProvider(
                "http://127.0.0.1/mcp",
                OAuthClientMetadata(
                    redirect_uris=[AnyUrl("http://127.0.0.1:8765/callback")],
                    token_endpoint_auth_method="none",
                    scope="mcp:tools offline_access",
                    client_name="MCP SDK interoperability test",
                ),
                storage,
                redirect_handler=redirect_handler,
                callback_handler=callback_handler,
            )
            async with (
                starlette.router.lifespan_context(starlette),
                httpx.AsyncClient(
                    transport=transport,
                    base_url="http://127.0.0.1",
                    auth=auth,
                ) as oauth_client,
                streamable_http_client(
                    "http://127.0.0.1/mcp", http_client=oauth_client
                ) as streams,
                ClientSession(streams[0], streams[1]) as session,
            ):
                await session.initialize()
                tools = (await session.list_tools()).tools

        assert len(tools) == 8
        assert storage.client_info is not None
        assert storage.tokens is not None
        assert storage.tokens.scope == "mcp:tools offline_access"
        assert storage.tokens.refresh_token

    asyncio.run(exercise())
