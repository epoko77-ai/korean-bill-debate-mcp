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
    origin = os.getenv("KBD_SMOKE_ORIGIN", "").strip()
    api_key = os.environ.get("ASSEMBLY_OPEN_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ASSEMBLY_OPEN_API_KEY is required")
    headers = {"Origin": origin} if origin else None
    async with httpx.AsyncClient(
        timeout=150, follow_redirects=True, headers=headers
    ) as client:
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
                bill_result = None
                if os.getenv("KBD_SMOKE_SKIP_BILL") != "1":
                    bill_result = await session.call_tool(
                        "get_bill_status", {"bill_id_or_no": "2219564"}
                    )
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
    bill = bill_result.structuredContent if bill_result is not None else None
    if bill_result is not None and (
        bill_result.isError or not isinstance(bill, dict) or bill.get("bill_no") != "2219564"
    ):
        raise RuntimeError("deployed MCP did not return the exact requested bill")
    return {
        "base_url": base_url,
        "origin": origin or None,
        "tool_count": len(names),
        "tools": names,
        "verified_bill_no": bill["bill_no"] if isinstance(bill, dict) else None,
        "verified_bill_name": bill.get("name") if isinstance(bill, dict) else None,
        "passed": True,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(exercise()), ensure_ascii=False, indent=2))
