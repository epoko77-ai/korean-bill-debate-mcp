"""Verify the deployed Claude-style OAuth flow without printing credentials."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import urllib.parse

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def exercise() -> dict[str, object]:
    base_url = os.getenv(
        "KBD_REMOTE_BASE_URL", "https://korean-bill-debate-mcp.vercel.app"
    ).rstrip("/")
    api_key = os.environ.get("ASSEMBLY_OPEN_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ASSEMBLY_OPEN_API_KEY is required")

    client_name = os.getenv("KBD_OAUTH_CLIENT_NAME", "Claude production smoke test")
    callback = os.getenv(
        "KBD_OAUTH_CALLBACK", "https://claude.ai/api/mcp/auth_callback"
    )
    verifier = "claude-oauth-smoke-verifier-0123456789-abcdefghijklmnopqrstuvwxyz"
    async with httpx.AsyncClient(timeout=90, follow_redirects=False) as client:
        challenge = await client.post(f"{base_url}/mcp")
        if challenge.status_code != 401 or "resource_metadata=" not in challenge.headers.get(
            "www-authenticate", ""
        ):
            raise RuntimeError("MCP endpoint did not advertise OAuth resource metadata")

        resource = await client.get(
            f"{base_url}/.well-known/oauth-protected-resource/mcp"
        )
        resource.raise_for_status()
        if resource.json().get("resource") != f"{base_url}/mcp":
            raise RuntimeError("protected resource metadata contains the wrong MCP URL")

        authorization_server = await client.get(
            f"{base_url}/.well-known/oauth-authorization-server"
        )
        authorization_server.raise_for_status()
        metadata = authorization_server.json()
        registered = await client.post(
            metadata["registration_endpoint"],
            json={
                "client_name": client_name,
                "redirect_uris": [callback],
                "token_endpoint_auth_method": "none",
            },
        )
        registered.raise_for_status()
        client_id = registered.json()["client_id"]
        authorization_values = {
            "client_id": client_id,
            "redirect_uri": callback,
            "response_type": "code",
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
            "scope": "mcp:tools",
            "resource": f"{base_url}/mcp",
            "state": "production-smoke",
        }
        consent = await client.get(
            metadata["authorization_endpoint"], params=authorization_values
        )
        consent.raise_for_status()
        if "본인의 열린국회 API 키" not in consent.text:
            raise RuntimeError("OAuth consent page did not request the user key")
        callback_origin = (
            f"{urllib.parse.urlsplit(callback).scheme}://"
            f"{urllib.parse.urlsplit(callback).netloc}"
        )
        if (
            f"form-action 'self' {callback_origin}"
            not in consent.headers.get("content-security-policy", "")
        ):
            raise RuntimeError("OAuth consent CSP blocks the client callback")

        authorized = await client.post(
            metadata["authorization_endpoint"],
            data={**authorization_values, "api_key": api_key},
        )
        if authorized.status_code != 303:
            raise RuntimeError("OAuth authorization did not return a callback code")
        callback_url = urllib.parse.urlsplit(authorized.headers["location"])
        callback_values = urllib.parse.parse_qs(callback_url.query)
        if callback_values.get("state") != ["production-smoke"]:
            raise RuntimeError("OAuth callback state did not match")

        exchanged = await client.post(
            metadata["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": callback_values["code"][0],
                "client_id": client_id,
                "redirect_uri": callback,
                "code_verifier": verifier,
                "resource": f"{base_url}/mcp",
            },
        )
        exchanged.raise_for_status()
        token_payload = exchanged.json()
        access_token = token_payload["access_token"]

        async with (
            httpx.AsyncClient(
                timeout=90,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Origin": "https://claude.ai",
                },
            ) as oauth_client,
            streamable_http_client(
                f"{base_url}/mcp", http_client=oauth_client
            ) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            tools = (await session.list_tools()).tools

        refreshed = await client.post(
            metadata["token_endpoint"],
            data={
                "grant_type": "refresh_token",
                "refresh_token": token_payload["refresh_token"],
                "client_id": client_id,
                "resource": f"{base_url}/mcp",
            },
        )
        refreshed.raise_for_status()

    names = {tool.name for tool in tools}
    expected = {
        "search_speeches",
        "get_speech",
        "get_speech_context",
        "list_committees",
        "list_meetings",
        "search_bills",
        "get_bill_status",
        "explore_issue",
    }
    if names != expected:
        raise RuntimeError("OAuth-authenticated MCP tool list is incomplete")
    return {
        "base_url": base_url,
        "client_name": client_name,
        "callback_origin": urllib.parse.urlsplit(callback).netloc,
        "oauth_discovery": True,
        "dynamic_registration": True,
        "pkce_authorization": True,
        "browser_callback_allowed": True,
        "refresh_token": True,
        "tool_count": len(names),
        "tools": sorted(names),
        "passed": True,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(exercise()), ensure_ascii=False, indent=2))
