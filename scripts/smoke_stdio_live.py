#!/usr/bin/env python3
"""End-to-end live smoke test through the real MCP stdio protocol."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def exercise() -> None:
    if not os.getenv("ASSEMBLY_OPEN_API_KEY"):
        raise RuntimeError("ASSEMBLY_OPEN_API_KEY is required")
    with tempfile.TemporaryDirectory(prefix="kbd-live-mcp-") as data_dir:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "kasm.cli", "mcp"],
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT / "src"),
                "KBD_DATA_DIR": data_dir,
                "KBD_MAX_MINUTES_PER_REQUEST": "1",
            },
        )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "Korean Bill & Debate MCP"
            tools = await session.list_tools()
            assert len(tools.tools) == 8
            started = time.perf_counter()
            response = await session.call_tool(
                "explore_issue",
                {
                    "query": "2026년 7월 검찰 보완수사권 폐지 관련 법안과 의원 의견",
                    "limit": 5,
                },
            )
            if response.isError:
                detail = "\n".join(
                    str(getattr(item, "text", item)) for item in response.content
                )
                raise RuntimeError(f"explore_issue returned an MCP error: {detail}")
            elapsed_seconds = time.perf_counter() - started
            result = response.structuredContent
            assert result is not None
            assert result["data_mode"] == "live_open_assembly_with_local_cache"
            assert result["bills"]
            assert all("형사소송법" in str(bill.get("name") or "") for bill in result["bills"])
            assert result["speeches"]
            assert all(speech["citation"]["official_url"] for speech in result["speeches"])
            assert result["live_refresh"]["months_queried"] == ["2026-07"]
            assert result["live_refresh"]["meeting_api_calls"] == 3
            assert result["scope_inventory"]["bill_candidates"]["total"] >= len(
                result["bills"]
            )
            print(
                {
                    "server": initialized.serverInfo.name,
                    "tools": len(tools.tools),
                    "bills": len(result["bills"]),
                    "bill_matches": [
                        {
                            "bill_no": bill.get("bill_no"),
                            "name": bill.get("name"),
                        }
                        for bill in result["bills"]
                    ],
                    "speeches": len(result["speeches"]),
                    "threads": len(result["discussion_threads"]),
                    "data_mode": result["data_mode"],
                    "elapsed_seconds": round(elapsed_seconds, 3),
                    "live_refresh": result["live_refresh"],
                }
            )


if __name__ == "__main__":
    asyncio.run(exercise())
