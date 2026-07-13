from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from kasm.app import create_services
from kasm.mcp.server import create_server
from kasm.mcp.tools import ServiceContext


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


class _BlockingServices:
    def __init__(self) -> None:
        self.started = threading.Event()

    def search(self, _query: str, **_filters: Any) -> list[Any]:
        self.started.set()
        time.sleep(0.25)
        return []


async def exercise_blocking_tool_off_event_loop() -> None:
    blocking = _BlockingServices()
    services = ServiceContext(search=blocking, repository=blocking, catalog=blocking)
    server = create_server(services, stateless_http=True)
    app = server.streamable_http_app()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client,
        streamable_http_client(
            "http://127.0.0.1:8000/mcp", http_client=client, terminate_on_close=False
        ) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        tool_call = asyncio.create_task(
            session.call_tool("search_speeches", {"query": "event-loop responsiveness"})
        )
        while not blocking.started.is_set():
            await asyncio.sleep(0.001)
        started = time.perf_counter()
        await asyncio.sleep(0.02)
        heartbeat_seconds = time.perf_counter() - started
        result = await tool_call

    assert not result.isError
    assert heartbeat_seconds < 0.1


def test_blocking_tool_runs_off_asgi_event_loop() -> None:
    asyncio.run(exercise_blocking_tool_off_event_loop())


class _ResearchBackend:
    def start_research(self, query: str, **_options: Any) -> dict[str, Any]:
        return {"research_id": "research_http", "status": "queued", "query": query}

    def get_research_status(self, research_id: str) -> dict[str, Any]:
        return {"research_id": research_id, "status": "running", "progress": 0.25}

    def get_research_overview(
        self,
        research_id: str,
        *,
        offset: int = 0,
        page_size: int = 20,
    ) -> dict[str, Any]:
        del offset, page_size
        return {
            "research_id": research_id,
            "phase": "final",
            "complete": True,
            "provisional": False,
            "substantive_conclusion_available": True,
            "core": [],
            "core_full_text_required_ids": [],
            "catalog": {
                "page": {"total": 0, "next_offset": None, "complete": True},
                "groups": [],
            },
        }

    def get_research_page(
        self,
        research_id: str,
        *,
        cursor: str | None = None,
        page_size: int = 20,
    ) -> dict[str, Any]:
        del cursor, page_size
        return {
            "research_id": research_id,
            "coverage": {"complete": True},
            "page": {"complete": True, "next_cursor": None},
            "evidence": [],
        }

    def get_evidence_document(
        self,
        research_id: str,
        evidence_id: str,
        *,
        cursor: str | None = None,
        max_characters: int = 20_000,
        scope: str = "selected",
    ) -> dict[str, Any]:
        del cursor, max_characters, scope
        return {
            "research_id": research_id,
            "evidence_id": evidence_id,
            "text": "원문",
            "next_cursor": None,
            "complete": True,
        }


async def exercise_durable_research_tool_surface() -> None:
    services = create_services()
    services.research = _ResearchBackend()  # type: ignore[assignment]
    server = create_server(services, stateless_http=True)
    app = server.streamable_http_app()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client,
        streamable_http_client("http://127.0.0.1:8000/mcp", http_client=client) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        tools_by_name = {tool.name: tool for tool in listed.tools}
        assert set(tools_by_name) == {
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
        assert all(
            tool.annotations is not None
            and tool.annotations.readOnlyHint is True
            and tool.annotations.destructiveHint is False
            and tool.annotations.openWorldHint is True
            for tool in tools_by_name.values()
        )
        assert tools_by_name["start_research"].annotations is not None
        assert tools_by_name["start_research"].annotations.idempotentHint is False
        assert tools_by_name["explore_issue"].annotations is not None
        assert tools_by_name["explore_issue"].annotations.idempotentHint is False
        assert tools_by_name["get_research_status"].annotations is not None
        assert tools_by_name["get_research_status"].annotations.idempotentHint is True
        assert "research_id" in tools_by_name["get_research_status"].inputSchema["required"]
        assert "assembly_term" not in tools_by_name["start_research"].inputSchema.get(
            "required", []
        )
        assert "committees" not in tools_by_name["explore_issue"].inputSchema.get("required", [])
        assert "cursor" in tools_by_name["get_research_page"].inputSchema["properties"]
        assert "offset" in tools_by_name["get_research_overview"].inputSchema["properties"]
        assert "cursor" in tools_by_name["get_evidence_document"].inputSchema["properties"]
        assert "scope" in tools_by_name["get_evidence_document"].inputSchema["properties"]
        assert "max_characters" in tools_by_name["get_evidence_document"].inputSchema["properties"]
        assert "전체" in (tools_by_name["get_evidence_document"].description or "")

        receipt = await session.call_tool("start_research", {"query": "최근 AI 입법"})
        assert not receipt.isError
        assert receipt.structuredContent is not None
        assert receipt.structuredContent["research_id"] == "research_http"
        assert receipt.structuredContent["next_action"]["tool"] == "get_research_status"


def test_durable_research_tools_are_advertised_only_when_backend_is_configured() -> None:
    asyncio.run(exercise_durable_research_tool_surface())


async def exercise_status_poll_is_not_blocked_by_legacy_search() -> None:
    blocking = _BlockingServices()
    services = ServiceContext(
        search=blocking,
        repository=blocking,
        catalog=blocking,
        research=_ResearchBackend(),  # type: ignore[arg-type]
    )
    server = create_server(services, stateless_http=True)
    app = server.streamable_http_app()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client,
        streamable_http_client(
            "http://127.0.0.1:8000/mcp", http_client=client, terminate_on_close=False
        ) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        slow_call = asyncio.create_task(
            session.call_tool("search_speeches", {"query": "slow legacy search"})
        )
        while not blocking.started.is_set():
            await asyncio.sleep(0.001)

        started = time.perf_counter()
        status = await session.call_tool("get_research_status", {"research_id": "research_http"})
        status_seconds = time.perf_counter() - started
        slow_result = await slow_call

    assert not status.isError
    assert not slow_result.isError
    assert status_seconds < 0.15


def test_research_status_poll_does_not_wait_behind_legacy_tool_work() -> None:
    asyncio.run(exercise_status_poll_is_not_blocked_by_legacy_search())
