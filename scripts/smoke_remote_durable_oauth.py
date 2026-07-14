"""Verify the production OAuth and durable 13-tool path without logging credentials."""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.parse
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from pydantic import AnyUrl

EXPECTED_TOOLS = {
    "search_speeches",
    "get_speech",
    "get_speech_context",
    "list_committees",
    "list_meetings",
    "search_bills",
    "get_bill_status",
    "explore_issue",
    "start_research",
    "get_research_status",
    "get_research_overview",
    "get_research_page",
    "get_evidence_document",
}


class MemoryTokenStorage:
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


def _structured(result: Any, operation: str) -> dict[str, Any]:
    value = result.structuredContent
    if result.isError or not isinstance(value, dict):
        detail = " | ".join(
            str(getattr(item, "text", item)) for item in (result.content or [])
        )
        raise RuntimeError(
            f"{operation} returned an MCP error"
            + (f": {detail[:2000]}" if detail else "")
        )
    return value


async def exercise() -> dict[str, object]:
    base_url = os.getenv(
        "KBD_REMOTE_BASE_URL", "https://korean-bill-debate-mcp.vercel.app"
    ).rstrip("/")
    api_key = os.getenv("ASSEMBLY_OPEN_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ASSEMBLY_OPEN_API_KEY is required")
    origin = os.getenv("KBD_SMOKE_ORIGIN", "https://chatgpt.com").strip()
    wait_seconds = max(0.0, float(os.getenv("KBD_SMOKE_WAIT_SECONDS", "0")))
    callback_result: tuple[str, str | None] | None = None
    authorization_seconds = 0.0
    storage = MemoryTokenStorage()

    async with httpx.AsyncClient(timeout=90, follow_redirects=False) as browser:
        health_response = await browser.get(f"{base_url}/healthz")
        health_response.raise_for_status()
        health = health_response.json()
        if not health.get("durable_research") or health.get("mcp_tool_count") != 13:
            raise RuntimeError("deployment health does not advertise durable 13-tool research")

        async def redirect_handler(authorization_url: str) -> None:
            nonlocal authorization_seconds, callback_result
            parsed = urllib.parse.urlsplit(authorization_url)
            values = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
            consent = await browser.get(authorization_url)
            consent.raise_for_status()
            if "본인의 열린국회 API 키" not in consent.text:
                raise RuntimeError("OAuth consent did not request the Open Assembly key")
            started = time.perf_counter()
            authorized = await browser.post(
                urllib.parse.urlunsplit(parsed._replace(query="")),
                data={**values, "api_key": api_key},
            )
            authorization_seconds = time.perf_counter() - started
            if authorized.status_code != 303:
                raise RuntimeError("OAuth authorization did not redirect to the client")
            callback = urllib.parse.urlsplit(authorized.headers["location"])
            returned = dict(urllib.parse.parse_qsl(callback.query))
            callback_result = (returned["code"], returned.get("state"))

        async def callback_handler() -> tuple[str, str | None]:
            if callback_result is None:
                raise RuntimeError("OAuth callback did not arrive")
            return callback_result

        auth = OAuthClientProvider(
            f"{base_url}/mcp",
            OAuthClientMetadata(
                redirect_uris=[AnyUrl("http://127.0.0.1:8765/callback")],
                token_endpoint_auth_method="none",
                scope="mcp:tools offline_access",
                client_name="Korean Bill & Debate production smoke",
            ),
            storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
            timeout=90,
        )
        headers = {"Origin": origin} if origin else {}
        async with (
            httpx.AsyncClient(timeout=90, auth=auth, headers=headers) as oauth_client,
            streamable_http_client(
                f"{base_url}/mcp", http_client=oauth_client
            ) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            listed_tools = (await session.list_tools()).tools
            names = {tool.name for tool in listed_tools}
            if names != EXPECTED_TOOLS:
                raise RuntimeError("deployed OAuth MCP tool surface is not exactly 13 tools")
            if any(
                tool.annotations is None
                or tool.annotations.readOnlyHint is not True
                or tool.annotations.destructiveHint is not False
                for tool in listed_tools
            ):
                raise RuntimeError("deployed MCP tools are not declared read-only")
            started = time.perf_counter()
            query = os.getenv("KBD_SMOKE_QUERY", "").strip() or (
                "2219564번 의안의 처리상태, 회의록, 전문위원 검토보고서를 "
                "공식 원문 기준으로 조사해줘"
            )
            research_arguments: dict[str, Any] = {"query": query}
            raw_assembly_term = os.getenv("KBD_SMOKE_ASSEMBLY_TERM", "22").strip()
            if raw_assembly_term:
                research_arguments["assembly_term"] = int(raw_assembly_term)
            date_from = os.getenv("KBD_SMOKE_DATE_FROM", "").strip()
            date_to = os.getenv("KBD_SMOKE_DATE_TO", "").strip()
            if date_from:
                research_arguments["date_from"] = date_from
            if date_to:
                research_arguments["date_to"] = date_to
            receipt = _structured(
                await session.call_tool(
                    "start_research",
                    research_arguments,
                ),
                "start_research",
            )
            receipt_seconds = time.perf_counter() - started
            research_id = str(receipt.get("research_id") or "")
            if not research_id or receipt_seconds > 15:
                raise RuntimeError("durable research receipt was missing or too slow")

            status: dict[str, Any] = {}
            deadline = time.monotonic() + wait_seconds
            status_poll_count = 0
            slowest_status_seconds = 0.0
            while wait_seconds > 0:
                status_started = time.perf_counter()
                status = _structured(
                    await session.call_tool(
                        "get_research_status", {"research_id": research_id}
                    ),
                    "get_research_status",
                )
                status_poll_count += 1
                slowest_status_seconds = max(
                    slowest_status_seconds,
                    time.perf_counter() - status_started,
                )
                if status.get("status") in {"complete", "partial", "failed"}:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("durable research did not finish within the smoke deadline")
                await asyncio.sleep(min(float(status.get("retry_after_seconds") or 2), 5))
            if status.get("status") == "failed":
                raise RuntimeError("durable research reached a failed terminal state")

            final_overview_verified = False
            exact_bill_verified = False
            evidence_count = 0
            if status.get("status") in {"complete", "partial"}:
                overview = _structured(
                    await session.call_tool(
                        "get_research_overview",
                        {"research_id": research_id, "page_size": 100},
                    ),
                    "get_research_overview",
                )
                if (
                    overview.get("phase") != "final"
                    or overview.get("substantive_conclusion_available") is not True
                ):
                    raise RuntimeError("terminal research did not expose its final overview")
                evidence_count = int(overview.get("evidence_count") or 0)
                if evidence_count < 1:
                    raise RuntimeError("terminal research returned an empty evidence overview")
                final_overview_verified = True
                expected_bill = os.getenv(
                    "KBD_SMOKE_EXPECT_BILL_NUMBER", "2219564"
                ).strip()
                if expected_bill:
                    exact_bill_verified = expected_bill in json.dumps(
                        overview, ensure_ascii=False, sort_keys=True
                    )
                    if not exact_bill_verified:
                        raise RuntimeError(
                            "final overview lost the expected exact bill identity"
                        )

    if storage.tokens is None or not storage.tokens.refresh_token:
        raise RuntimeError("OAuth flow did not issue refresh credentials")
    if storage.tokens.scope != "mcp:tools offline_access":
        raise RuntimeError("OAuth flow did not grant persistent MCP scope")
    return {
        "base_url": base_url,
        "origin": origin or None,
        "health": {
            "durable_research": health.get("durable_research"),
            "mcp_tool_count": health.get("mcp_tool_count"),
            "corpus_revision_configured": health.get("corpus_revision_configured"),
        },
        "oauth": {
            "official_mcp_sdk": True,
            "dynamic_registration": storage.client_info is not None,
            "pkce": True,
            "offline_refresh": True,
            "authorization_seconds": round(authorization_seconds, 3),
        },
        "tool_count": len(EXPECTED_TOOLS),
        "all_tools_read_only": True,
        "research_id": research_id,
        "research_receipt_seconds": round(receipt_seconds, 3),
        "status_poll_count": status_poll_count,
        "slowest_status_seconds": round(slowest_status_seconds, 3),
        "terminal_status": status.get("status") if status else None,
        "final_overview_verified": final_overview_verified,
        "exact_bill_verified": exact_bill_verified,
        "evidence_count": evidence_count,
        "passed": True,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(exercise()), ensure_ascii=False, indent=2))
