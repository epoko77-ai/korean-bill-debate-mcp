from __future__ import annotations

import asyncio

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from kasm.app import create_services
from kasm.mcp.server import create_server


async def exercise_stateless_http_server() -> None:
    server = create_server(create_services(), stateless_http=True)
    app = server.streamable_http_app()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client,
        streamable_http_client("http://127.0.0.1:8000/mcp", http_client=client) as streams,
    ):
        read_stream, write_stream, _ = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            assert len(listed.tools) == 8
            result = await session.call_tool("list_meetings", {})
            assert not result.isError
            assert result.structuredContent


def test_stateless_streamable_http_round_trip_without_user_key() -> None:
    asyncio.run(exercise_stateless_http_server())
