"""Verify a live personal MCP connection without printing its token or API key."""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import urllib.parse

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

_URL_FIELD = re.compile(r"<textarea[^>]*>(?P<url>https://[^<]+)</textarea>")


async def exercise() -> dict[str, object]:
    base_url = os.getenv(
        "KBD_REMOTE_BASE_URL", "https://korean-bill-debate-mcp.vercel.app"
    ).rstrip("/")
    api_key = os.environ.get("ASSEMBLY_OPEN_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ASSEMBLY_OPEN_API_KEY is required")
    async with httpx.AsyncClient(timeout=150, follow_redirects=True) as client:
        response = await client.post(f"{base_url}/connect", data={"api_key": api_key})
        response.raise_for_status()
        match = _URL_FIELD.search(response.text)
        if match is None:
            raise RuntimeError("deployment did not issue a personal MCP URL")
        personal_url = html.unescape(match.group("url"))
        parsed = urllib.parse.urlsplit(personal_url)
        if not parsed.path.startswith("/mcp/t/") or parsed.query:
            raise RuntimeError("deployment did not issue a path-authenticated MCP URL")
        async with streamable_http_client(personal_url, http_client=client) as streams:
            read_stream, write_stream, _ = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
    names = [tool.name for tool in tools]
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
    if set(names) != expected:
        raise RuntimeError("deployed MCP tool list is incomplete")
    return {"base_url": base_url, "tool_count": len(names), "tools": names, "passed": True}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(exercise()), ensure_ascii=False, indent=2))
