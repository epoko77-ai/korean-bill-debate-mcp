from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from kasm import __version__

ROOT = Path(__file__).parents[2]


async def exercise_stdio_server() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "kasm.cli", "mcp"],
        cwd=ROOT,
        env={**os.environ, "KBD_OFFLINE_DEMO": "1"},
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        initialized = await session.initialize()
        assert initialized.serverInfo.name == "Korean Bill & Debate MCP"
        assert initialized.serverInfo.version == __version__
        listed = await session.list_tools()
        assert {tool.name for tool in listed.tools} == {
            "search_speeches",
            "get_speech",
            "get_speech_context",
            "list_committees",
            "list_meetings",
            "search_bills",
            "get_bill_status",
            "explore_issue",
        }
        search_tool = next(tool for tool in listed.tools if tool.name == "search_speeches")
        assert "korean_query" in search_tool.inputSchema["properties"]
        result = await session.call_tool(
            "search_speeches",
            {"query": "domestic foundation models", "limit": 3},
        )
        assert not result.isError
        assert result.structuredContent is not None
        assert result.structuredContent["results"]
        assert result.structuredContent["query_language"] == "en"
        assert result.structuredContent["search_query_ko"] == "소버린 AI"
        assert result.structuredContent["source_language"] == "ko"


def test_real_mcp_stdio_round_trip() -> None:
    asyncio.run(exercise_stdio_server())
