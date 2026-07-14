"""Verify the production OAuth and durable 13-tool path without logging credentials.

The default loopback redirect keeps command-line use simple. Set
``KBD_SMOKE_REDIRECT_URI`` to the exact HTTPS callback registered by Claude.ai or ChatGPT and
``KBD_SMOKE_REQUIRE_WEB_CALLBACK=1`` for a real web-connector approval-path check. The callback
Location is intercepted and validated, so the remote service does not need to receive this smoke's
one-time code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import urllib.parse
from collections.abc import Callable
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

from kasm import __version__

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


class HTTPMetrics:
    """Collect aggregate transport metrics without retaining URLs, headers, or bodies."""

    def __init__(self) -> None:
        self.request_count = 0
        self.status_counts: dict[int, int] = {}
        self.slowest_seconds = 0.0

    async def on_request(self, request: httpx.Request) -> None:
        self.request_count += 1
        request.extensions["kbd_smoke_started"] = time.perf_counter()

    async def on_response(self, response: httpx.Response) -> None:
        self.status_counts[response.status_code] = (
            self.status_counts.get(response.status_code, 0) + 1
        )
        started = response.request.extensions.get("kbd_smoke_started")
        if isinstance(started, float):
            self.slowest_seconds = max(
                self.slowest_seconds,
                time.perf_counter() - started,
            )

    def summary(self) -> dict[str, object]:
        failures = {
            str(status): count
            for status, count in sorted(self.status_counts.items())
            if status >= 400
        }
        critical = sum(
            count
            for status, count in self.status_counts.items()
            if status == 429 or status >= 500
        )
        return {
            "request_count": self.request_count,
            "status_counts": {
                str(status): count
                for status, count in sorted(self.status_counts.items())
            },
            "failure_status_counts": failures,
            "critical_failure_count": critical,
            "slowest_seconds": round(self.slowest_seconds, 3),
        }


_LAST_HTTP_METRICS: HTTPMetrics | None = None
_LAST_RESEARCH_ID: str | None = None
_LAST_STATUS: dict[str, Any] | None = None


def _safe_failure_message(exc: Exception) -> str:
    """Keep smoke failures useful without ever echoing credentials or one-time codes."""

    def messages(error: BaseException) -> list[str]:
        if isinstance(error, BaseExceptionGroup):
            return [message for item in error.exceptions for message in messages(item)]
        detail = str(error).strip() or type(error).__name__
        return [f"{type(error).__name__}: {detail}"]

    value = " | ".join(messages(exc))
    api_key = os.getenv("ASSEMBLY_OPEN_API_KEY", "").strip()
    if api_key and api_key in value:
        return "failure detail contained the Open Assembly credential and was suppressed"
    replacements = (
        (r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}", "Bearer [redacted]"),
        (r"\bsk-(?:ant-)?[A-Za-z0-9_-]{8,}", "[redacted-api-key]"),
        (r"\bgAAAAA[A-Za-z0-9_-]{8,}", "[redacted-token]"),
        (r"/mcp/t/[A-Za-z0-9_-]{8,}", "/mcp/t/[redacted]"),
        (
            r"(?i)((?:code|access_token|refresh_token)=)[^&\s]+",
            r"\1[redacted]",
        ),
        (r"(https?://[^?\s]+)\?[^\s]+", r"\1?[redacted]"),
    )
    for pattern, replacement in replacements:
        value = re.sub(pattern, replacement, value)
    return value[:500]


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


def _redirect_origin(uri: str) -> str:
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.fragment:
        raise RuntimeError("KBD_SMOKE_REDIRECT_URI is not a valid OAuth redirect URI")
    if any(name in {"code", "state", "error"} for name, _ in urllib.parse.parse_qsl(parsed.query)):
        raise RuntimeError(
            "KBD_SMOKE_REDIRECT_URI must not reserve code, state, or error query parameters"
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def _extract_callback_result(
    location: str,
    *,
    redirect_uri: str,
    expected_state: str | None,
) -> tuple[str, str | None]:
    """Validate an intercepted web callback without opening a local listener."""

    expected = urllib.parse.urlsplit(redirect_uri)
    callback = urllib.parse.urlsplit(location)
    if (
        callback.scheme,
        callback.netloc,
        callback.path,
        callback.fragment,
    ) != (
        expected.scheme,
        expected.netloc,
        expected.path,
        expected.fragment,
    ):
        raise RuntimeError("OAuth authorization redirected to an unexpected callback URI")
    returned_pairs = urllib.parse.parse_qsl(callback.query, keep_blank_values=True)
    for pair in urllib.parse.parse_qsl(expected.query, keep_blank_values=True):
        if pair not in returned_pairs:
            raise RuntimeError("OAuth callback did not preserve the registered redirect URI")
    returned: dict[str, list[str]] = {}
    for name, value in returned_pairs:
        returned.setdefault(name, []).append(value)
    if returned.get("error"):
        raise RuntimeError("OAuth authorization returned an error callback")
    codes = returned.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise RuntimeError("OAuth callback did not contain exactly one authorization code")
    states = returned.get("state", [])
    if expected_state is None:
        if states:
            raise RuntimeError("OAuth callback returned an unexpected state")
        state: str | None = None
    else:
        if states != [expected_state]:
            raise RuntimeError("OAuth callback state did not match")
        state = states[0]
    return codes[0], state


async def _verify_final_catalog(
    session: ClientSession,
    research_id: str,
    first: dict[str, Any],
) -> tuple[int, int]:
    """Traverse the immutable final overview catalog without a silent top-N."""

    payload = first
    offset = 0
    identities: list[tuple[str, str]] = []
    page_count = 0
    expected_total: int | None = None
    while True:
        if page_count:
            payload = _structured(
                await session.call_tool(
                    "get_research_overview",
                    {
                        "research_id": research_id,
                        "offset": offset,
                        "page_size": 100,
                    },
                ),
                "get_research_overview",
            )
        if (
            payload.get("phase") != "final"
            or payload.get("substantive_conclusion_available") is not True
        ):
            raise RuntimeError("final catalog traversal left the final research phase")
        catalog = payload.get("catalog")
        if not isinstance(catalog, dict):
            raise RuntimeError("final overview catalog is missing")
        page = catalog.get("page")
        groups = catalog.get("groups")
        if not isinstance(page, dict) or not isinstance(groups, list):
            raise RuntimeError("final overview catalog page is malformed")
        if int(page.get("returned_count") or 0) != len(groups):
            raise RuntimeError("final overview catalog count is inconsistent")
        total = int(page.get("total") or 0)
        expected_total = total if expected_total is None else expected_total
        if total != expected_total:
            raise RuntimeError("final overview catalog total changed between pages")
        for group in groups:
            if not isinstance(group, dict):
                raise RuntimeError("final overview catalog entry is malformed")
            identities.append(
                (str(group.get("entity_type") or ""), str(group.get("entity_id") or ""))
            )
        if int(page.get("returned_through") or 0) != len(identities):
            raise RuntimeError("final overview catalog position is inconsistent")
        page_count += 1
        next_offset = page.get("next_offset")
        if bool(page.get("complete")) is not (next_offset is None):
            raise RuntimeError("final overview catalog completion flag is inconsistent")
        if next_offset is None:
            break
        offset = int(next_offset)
    if expected_total != len(identities) or len(set(identities)) != len(identities):
        raise RuntimeError("final overview catalog is incomplete or contains duplicates")
    return len(identities), page_count


async def _verify_metadata_catalog(
    session: ClientSession,
    research_id: str,
    first: dict[str, Any],
) -> tuple[int, int]:
    """Traverse the complete accepted-candidate map before deeper work finishes."""

    payload = first
    offset = 0
    candidate_ids: list[str] = []
    page_count = 0
    expected_total: int | None = None
    while True:
        if page_count:
            payload = _structured(
                await session.call_tool(
                    "get_research_overview",
                    {
                        "research_id": research_id,
                        "offset": offset,
                        "page_size": 100,
                    },
                ),
                "get_research_overview",
            )
        if (
            payload.get("phase") != "metadata"
            or payload.get("provisional") is not True
            or payload.get("substantive_conclusion_available") is not False
        ):
            raise RuntimeError("metadata catalog was exposed as a substantive conclusion")
        source = payload.get("source")
        catalog = payload.get("catalog")
        families = payload.get("families")
        if (
            not isinstance(source, dict)
            or source.get("source_complete") is not True
            or not isinstance(catalog, dict)
            or not isinstance(families, list)
        ):
            raise RuntimeError("metadata catalog does not prove complete source accounting")
        accepted_total = int(payload.get("accepted_total") or 0)
        expected_total = accepted_total if expected_total is None else expected_total
        if accepted_total != expected_total:
            raise RuntimeError("metadata accepted total changed between pages")
        rejected_total = sum(
            int(item.get("rejected_count") or 0)
            for item in families
            if isinstance(item, dict)
        )
        if int(payload.get("rejected_total") or 0) != rejected_total:
            raise RuntimeError("metadata rejected accounting is inconsistent")
        if int(payload.get("pending_total") or 0) != 0:
            raise RuntimeError("metadata catalog was published with unresolved candidates")
        entries = catalog.get("entries")
        if not isinstance(entries, list):
            raise RuntimeError("metadata catalog entries are malformed")
        if (
            int(catalog.get("offset") or 0) != offset
            or int(catalog.get("returned_count") or 0) != len(entries)
        ):
            raise RuntimeError("metadata catalog page accounting is inconsistent")
        for entry in entries:
            if not isinstance(entry, dict) or not str(entry.get("candidate_id") or ""):
                raise RuntimeError("metadata catalog candidate is malformed")
            candidate_ids.append(str(entry["candidate_id"]))
        page_count += 1
        next_offset = catalog.get("next_offset")
        if bool(catalog.get("complete")) is not (next_offset is None):
            raise RuntimeError("metadata catalog completion flag is inconsistent")
        if next_offset is None:
            break
        offset = int(next_offset)
    if expected_total != len(candidate_ids) or len(set(candidate_ids)) != len(candidate_ids):
        raise RuntimeError("metadata catalog is incomplete or contains duplicates")
    return len(candidate_ids), page_count


async def _verify_evidence_inventory(
    session: ClientSession,
    research_id: str,
) -> tuple[int, int, dict[str, Any] | None]:
    """Traverse every evidence index page and retain one long-text descriptor."""

    cursor: str | None = None
    evidence_ids: list[str] = []
    first_full_text: dict[str, Any] | None = None
    matched_total: int | None = None
    page_count = 0
    while True:
        arguments: dict[str, Any] = {
            "research_id": research_id,
            "page_size": 100,
            "exhaustive": True,
        }
        if cursor is not None:
            arguments["cursor"] = cursor
        payload = _structured(
            await session.call_tool("get_research_page", arguments),
            "get_research_page",
        )
        page = payload.get("page")
        evidence = payload.get("evidence")
        if not isinstance(page, dict) or not isinstance(evidence, list):
            raise RuntimeError("evidence inventory page is malformed")
        current_total = int(page.get("matched_total") or 0)
        matched_total = current_total if matched_total is None else matched_total
        if current_total != matched_total or int(page.get("returned_count") or 0) != len(
            evidence
        ):
            raise RuntimeError("evidence inventory accounting changed between pages")
        for item in evidence:
            if not isinstance(item, dict) or not str(item.get("id") or ""):
                raise RuntimeError("evidence inventory entry is malformed")
            evidence_ids.append(str(item["id"]))
            if first_full_text is None and item.get("text_inline_complete") is False:
                first_full_text = item
        if int(page.get("returned_through") or 0) != len(evidence_ids):
            raise RuntimeError("evidence inventory position is inconsistent")
        page_count += 1
        cursor_value = page.get("next_cursor")
        cursor = str(cursor_value) if cursor_value else None
        if bool(page.get("complete")) is not (cursor is None):
            raise RuntimeError("evidence inventory completion flag is inconsistent")
        if cursor is None:
            break
    if matched_total != len(evidence_ids) or len(set(evidence_ids)) != len(evidence_ids):
        raise RuntimeError("evidence inventory is incomplete or contains duplicates")
    return len(evidence_ids), page_count, first_full_text


async def _verify_long_text(
    session: ClientSession,
    research_id: str,
    item: dict[str, Any],
) -> tuple[int, int]:
    """Reassemble one official long text and verify its published SHA-256."""

    evidence_id = str(item.get("id") or "")
    expected_hash = str(item.get("text_hash") or "")
    expected_characters = int(item.get("text_characters") or 0)
    cursor: str | None = None
    chunks: list[str] = []
    character_end = 0
    call_count = 0
    while True:
        arguments: dict[str, Any] = {
            "research_id": research_id,
            "evidence_id": evidence_id,
            "max_characters": 50_000,
            "scope": "selected",
        }
        if cursor is not None:
            arguments["cursor"] = cursor
        payload = _structured(
            await session.call_tool("get_evidence_document", arguments),
            "get_evidence_document",
        )
        returned = payload.get("returned_range")
        text = payload.get("text")
        if not isinstance(returned, dict) or not isinstance(text, str):
            raise RuntimeError("evidence document range is malformed")
        if (
            int(returned.get("character_start") or 0) != character_end
            or int(returned.get("characters") or 0) != len(text)
        ):
            raise RuntimeError("evidence document ranges are not contiguous")
        expected_end = character_end + len(text)
        character_end = int(returned.get("character_end") or 0)
        if character_end != expected_end:
            raise RuntimeError("evidence document range length is inconsistent")
        if (
            str(payload.get("text_hash") or "") != expected_hash
            or int(payload.get("text_characters") or 0) != expected_characters
        ):
            raise RuntimeError("evidence document identity changed between ranges")
        chunks.append(text)
        call_count += 1
        cursor_value = payload.get("next_cursor")
        cursor = str(cursor_value) if cursor_value else None
        if bool(payload.get("complete")) is not (cursor is None):
            raise RuntimeError("evidence document completion flag is inconsistent")
        if cursor is None:
            break
    complete_text = "".join(chunks)
    if (
        len(complete_text) != expected_characters
        or hashlib.sha256(complete_text.encode()).hexdigest() != expected_hash
    ):
        raise RuntimeError("evidence document did not reconstruct losslessly")
    return len(complete_text), call_count


async def exercise() -> dict[str, object]:
    global _LAST_HTTP_METRICS, _LAST_RESEARCH_ID, _LAST_STATUS

    base_url = os.getenv(
        "KBD_REMOTE_BASE_URL", "https://korean-bill-debate-mcp.vercel.app"
    ).rstrip("/")
    api_key = os.getenv("ASSEMBLY_OPEN_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ASSEMBLY_OPEN_API_KEY is required")
    origin = os.getenv("KBD_SMOKE_ORIGIN", "https://chatgpt.com").strip()
    redirect_uri = os.getenv(
        "KBD_SMOKE_REDIRECT_URI", "http://127.0.0.1:8765/callback"
    ).strip()
    callback_origin = _redirect_origin(redirect_uri)
    require_web_callback = (
        os.getenv("KBD_SMOKE_REQUIRE_WEB_CALLBACK", "").strip() == "1"
    )
    redirect_host = (urllib.parse.urlsplit(redirect_uri).hostname or "").lower()
    web_callback = (
        urllib.parse.urlsplit(redirect_uri).scheme == "https"
        and redirect_host not in {"127.0.0.1", "localhost", "::1"}
    )
    if require_web_callback and not web_callback:
        raise RuntimeError("the production web-connector smoke requires an HTTPS callback URI")
    wait_seconds = max(0.0, float(os.getenv("KBD_SMOKE_WAIT_SECONDS", "0")))
    stop_at_overview = os.getenv("KBD_SMOKE_STOP_AT_OVERVIEW", "").strip() == "1"
    exhaustive = os.getenv("KBD_SMOKE_EXHAUSTIVE", "").strip() == "1"
    connection_only = os.getenv("KBD_SMOKE_CONNECTION_ONLY", "").strip() == "1"
    callback_result: tuple[str, str | None] | None = None
    authorization_seconds = 0.0
    storage = MemoryTokenStorage()
    http_metrics = HTTPMetrics()
    _LAST_HTTP_METRICS = http_metrics
    _LAST_RESEARCH_ID = None
    _LAST_STATUS = None

    event_hooks: dict[str, list[Callable[..., Any]]] = {
        "request": [http_metrics.on_request],
        "response": [http_metrics.on_response],
    }
    async with httpx.AsyncClient(
        timeout=90,
        follow_redirects=False,
        event_hooks=event_hooks,
    ) as browser:
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
            if (
                f"form-action 'self' {callback_origin}"
                not in consent.headers.get("content-security-policy", "")
            ):
                raise RuntimeError("OAuth consent CSP blocks the registered web callback")
            started = time.perf_counter()
            authorized = await browser.post(
                urllib.parse.urlunsplit(parsed._replace(query="")),
                data={**values, "api_key": api_key},
            )
            authorization_seconds = time.perf_counter() - started
            if authorized.status_code != 303:
                raise RuntimeError("OAuth authorization did not redirect to the client")
            callback_result = _extract_callback_result(
                authorized.headers["location"],
                redirect_uri=redirect_uri,
                expected_state=values.get("state"),
            )

        async def callback_handler() -> tuple[str, str | None]:
            if callback_result is None:
                raise RuntimeError("OAuth callback did not arrive")
            return callback_result

        auth = OAuthClientProvider(
            f"{base_url}/mcp",
            OAuthClientMetadata(
                redirect_uris=[AnyUrl(redirect_uri)],
                token_endpoint_auth_method="none",
                scope="mcp:tools offline_access",
                client_name=os.getenv(
                    "KBD_SMOKE_CLIENT_NAME",
                    "Korean Bill & Debate production smoke",
                ),
            ),
            storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
            timeout=90,
        )
        headers = {"Origin": origin} if origin else {}
        async with (
            httpx.AsyncClient(
                timeout=90,
                auth=auth,
                headers=headers,
                event_hooks=event_hooks,
            ) as oauth_client,
            streamable_http_client(
                f"{base_url}/mcp", http_client=oauth_client
            ) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            initialized = await session.initialize()
            expected_version = os.getenv("KBD_EXPECTED_VERSION", __version__).strip()
            if initialized.serverInfo.version != expected_version:
                raise RuntimeError("deployed MCP server version does not match the release")
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
            if connection_only:
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
                        "corpus_revision_configured": health.get(
                            "corpus_revision_configured"
                        ),
                    },
                    "oauth": {
                        "official_mcp_sdk": True,
                        "dynamic_registration": storage.client_info is not None,
                        "pkce": True,
                        "offline_refresh": True,
                        "redirect_uri": redirect_uri,
                        "callback_origin": callback_origin,
                        "web_callback": web_callback,
                        "authorization_seconds": round(authorization_seconds, 3),
                    },
                    "http": http_metrics.summary(),
                    "tool_count": len(EXPECTED_TOOLS),
                    "all_tools_read_only": True,
                    "connection_only": True,
                    "passed": True,
                }
            started = time.perf_counter()
            research_started = started
            existing_research_id = os.getenv(
                "KBD_SMOKE_EXISTING_RESEARCH_ID", ""
            ).strip()
            if existing_research_id:
                if not re.fullmatch(r"research_[0-9a-f]{32}", existing_research_id):
                    raise RuntimeError("KBD_SMOKE_EXISTING_RESEARCH_ID is invalid")
                research_id = existing_research_id
                receipt_seconds = 0.0
            else:
                query = os.getenv("KBD_SMOKE_QUERY", "").strip() or (
                    "2219564번 의안의 처리상태, 회의록, 전문위원 검토보고서를 "
                    "공식 원문 기준으로 조사해줘"
                )
                research_arguments: dict[str, Any] = {"query": query}
                raw_assembly_term = os.getenv(
                    "KBD_SMOKE_ASSEMBLY_TERM", "22"
                ).strip()
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
            _LAST_RESEARCH_ID = research_id

            status: dict[str, Any] = {}
            deadline = time.monotonic() + wait_seconds
            status_poll_count = 0
            slowest_status_seconds = 0.0
            first_overview_seconds: float | None = None
            first_overview_phase: str | None = None
            first_overview_accepted_total = 0
            first_overview_catalog_pages = 0
            first_overview_verified = False
            while wait_seconds > 0:
                status_started = time.perf_counter()
                status = _structured(
                    await session.call_tool(
                        "get_research_status", {"research_id": research_id}
                    ),
                    "get_research_status",
                )
                _LAST_STATUS = status
                status_poll_count += 1
                slowest_status_seconds = max(
                    slowest_status_seconds,
                    time.perf_counter() - status_started,
                )
                if status.get("overview_available") and not first_overview_verified:
                    overview = _structured(
                        await session.call_tool(
                            "get_research_overview",
                            {"research_id": research_id, "page_size": 100},
                        ),
                        "get_research_overview",
                    )
                    phase = str(overview.get("phase") or "")
                    if phase == "metadata":
                        (
                            accepted_total,
                            first_overview_catalog_pages,
                        ) = await _verify_metadata_catalog(
                            session,
                            research_id,
                            overview,
                        )
                    elif phase == "final":
                        accepted_total, first_overview_catalog_pages = (
                            await _verify_final_catalog(
                                session,
                                research_id,
                                overview,
                            )
                        )
                    else:
                        raise RuntimeError("available research overview has an invalid phase")
                    if accepted_total < 1:
                        raise RuntimeError("available research overview contained no candidates")
                    first_overview_seconds = time.perf_counter() - research_started
                    first_overview_phase = phase
                    first_overview_accepted_total = accepted_total
                    first_overview_verified = True
                    if stop_at_overview:
                        break
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
            final_catalog_total = 0
            final_catalog_pages = 0
            evidence_inventory_total = 0
            evidence_inventory_pages = 0
            long_text_characters = 0
            long_text_calls = 0
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
                final_catalog = overview.get("catalog")
                final_catalog_page = (
                    final_catalog.get("page") if isinstance(final_catalog, dict) else None
                )
                if not isinstance(final_catalog_page, dict):
                    raise RuntimeError("terminal research returned no final entity catalog")
                final_catalog_total = int(final_catalog_page.get("total") or 0)
                final_catalog_pages = 1
                if final_catalog_total < 1:
                    raise RuntimeError("terminal research returned an empty final entity catalog")
                final_overview_verified = True
                if exhaustive:
                    final_catalog_total, final_catalog_pages = await _verify_final_catalog(
                        session,
                        research_id,
                        overview,
                    )
                    (
                        evidence_inventory_total,
                        evidence_inventory_pages,
                        long_text_item,
                    ) = await _verify_evidence_inventory(session, research_id)
                    if evidence_inventory_total != evidence_count:
                        raise RuntimeError(
                            "final overview and exhaustive evidence inventory disagree"
                        )
                    if long_text_item is None:
                        raise RuntimeError(
                            "terminal research exposed no long official text to verify"
                        )
                    long_text_characters, long_text_calls = await _verify_long_text(
                        session,
                        research_id,
                        long_text_item,
                    )
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
            research_elapsed_seconds = time.perf_counter() - research_started

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
            "redirect_uri": redirect_uri,
            "callback_origin": callback_origin,
            "web_callback": web_callback,
            "authorization_seconds": round(authorization_seconds, 3),
        },
        "tool_count": len(EXPECTED_TOOLS),
        "server_version": initialized.serverInfo.version,
        "all_tools_read_only": True,
        "connection_only": False,
        "http": http_metrics.summary(),
        "research_id": research_id,
        "research_receipt_seconds": round(receipt_seconds, 3),
        "status_poll_count": status_poll_count,
        "slowest_status_seconds": round(slowest_status_seconds, 3),
        "first_overview_seconds": (
            round(first_overview_seconds, 3)
            if first_overview_seconds is not None
            else None
        ),
        "first_overview_phase": first_overview_phase,
        "first_overview_accepted_total": first_overview_accepted_total,
        "first_overview_catalog_pages": first_overview_catalog_pages,
        "first_overview_verified": first_overview_verified,
        "first_overview_duplicate_count": 0 if first_overview_verified else None,
        "terminal_status": status.get("status") if status else None,
        "final_overview_verified": final_overview_verified,
        "exhaustive_verified": bool(exhaustive and final_overview_verified),
        "final_catalog_total": final_catalog_total,
        "final_catalog_pages": final_catalog_pages,
        "evidence_inventory_total": evidence_inventory_total,
        "evidence_inventory_pages": evidence_inventory_pages,
        "long_text_characters": long_text_characters,
        "long_text_calls": long_text_calls,
        "exact_bill_verified": exact_bill_verified,
        "evidence_count": evidence_count,
        "research_elapsed_seconds": round(research_elapsed_seconds, 3),
        "final_catalog_duplicate_count": (
            0 if exhaustive or first_overview_phase == "final" else None
        ),
        "evidence_duplicate_count": 0 if exhaustive else None,
        "passed": True,
    }


def main() -> int:
    try:
        result = asyncio.run(exercise())
    except Exception as exc:  # noqa: BLE001 - CLI must emit a secret-safe failure object
        result = {
            "passed": False,
            "error_type": type(exc).__name__,
            "error": _safe_failure_message(exc),
            "research_id": _LAST_RESEARCH_ID,
            "last_status": (
                {
                    name: _LAST_STATUS.get(name)
                    for name in (
                        "status",
                        "stage",
                        "progress",
                        "overview_available",
                        "overview_phase",
                        "work",
                    )
                }
                if _LAST_STATUS is not None
                else None
            ),
            "http": (
                _LAST_HTTP_METRICS.summary()
                if _LAST_HTTP_METRICS is not None
                else {
                    "request_count": 0,
                    "status_counts": {},
                    "failure_status_counts": {},
                    "critical_failure_count": 0,
                    "slowest_seconds": 0.0,
                }
            ),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
