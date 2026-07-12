"""Exercise a configured deployment through the real Streamable HTTP MCP client."""

from __future__ import annotations

import asyncio
import json

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from kasm.mcp.deployment import create_asgi_app


async def exercise() -> dict[str, object]:
    application = create_asgi_app()
    starlette = application.app
    transport = httpx.ASGITransport(app=application)
    async with (
        starlette.router.lifespan_context(starlette),
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client,
    ):
        health = (await client.get("/healthz")).json()
        async with streamable_http_client("http://127.0.0.1/mcp", http_client=client) as streams:
            read_stream, write_stream, _ = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool(
                    "search_speeches",
                    {
                        "query": "support for victims of rental deposit fraud",
                        "limit": 3,
                    },
                )
                if result.isError or not result.structuredContent:
                    raise RuntimeError("deployed MCP search failed")
    return {
        "health": health,
        "tools": [tool.name for tool in tools.tools],
        "search_result_count": len(result.structuredContent["results"]),
        "passed": len(tools.tools) == 8,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(exercise()), ensure_ascii=False, indent=2))
