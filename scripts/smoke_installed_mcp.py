"""Test a cleanly installed ``kbd`` executable through the real stdio MCP protocol."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def exercise() -> dict[str, object]:
    executable = os.environ.get("KBD_INSTALLED_EXECUTABLE", "kbd")
    if not os.getenv("ASSEMBLY_OPEN_API_KEY"):
        raise RuntimeError("ASSEMBLY_OPEN_API_KEY is required")
    with tempfile.TemporaryDirectory(prefix="kbd-installed-smoke-") as data_dir:
        parameters = StdioServerParameters(
            command=executable,
            args=["mcp"],
            env={
                **os.environ,
                "KBD_DATA_DIR": data_dir,
                "KBD_MAX_MINUTES_PER_REQUEST": "1",
            },
        )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            tools = (await session.list_tools()).tools
            bill_result = await session.call_tool(
                "get_bill_status", {"bill_id_or_no": "2219564"}
            )
    bill = bill_result.structuredContent
    installed_version = importlib.metadata.version("korean-bill-debate-mcp")
    if initialized.serverInfo.version != installed_version:
        raise RuntimeError(
            "installed MCP server version does not match package metadata"
        )
    if len(tools) != 8:
        raise RuntimeError("installed MCP tool list is incomplete")
    if bill_result.isError or not isinstance(bill, dict) or bill.get("bill_no") != "2219564":
        detail = "\n".join(
            str(getattr(item, "text", item)) for item in bill_result.content
        )
        raise RuntimeError(
            "installed MCP did not return the exact requested bill: " + detail
        )
    return {
        "server": initialized.serverInfo.name,
        "version": initialized.serverInfo.version,
        "tool_count": len(tools),
        "verified_bill_no": bill["bill_no"],
        "verified_bill_name": bill.get("name"),
        "passed": True,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(exercise()), ensure_ascii=False, indent=2))
