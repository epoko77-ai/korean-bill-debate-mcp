"""Official MCP SDK adapter (optional at import time)."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial, wraps
from typing import Any

from kasm import __version__

from .tools import KasmTools, ServiceContext


def create_server(
    services: ServiceContext,
    *,
    name: str = "Korean Bill & Debate MCP",
    host: str = "127.0.0.1",
    port: int = 8000,
    stateless_http: bool = True,
    transport_security: Any | None = None,
) -> Any:
    """Create a FastMCP server and register the public tools."""

    try:
        import anyio
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "The official MCP SDK is not installed. Install the package with its MCP extra."
        ) from exc

    implementation = KasmTools(services)
    # FastMCP executes synchronous tools on the ASGI event-loop thread. Legacy
    # local tools can still perform blocking API/SQLite work, so serialize only
    # those calls off-loop. Durable research entry/status/page calls are short
    # queue or object-store operations and use a separate limiter: a slow local
    # search must never head-of-line block status polling for existing jobs.
    legacy_limiter = anyio.CapacityLimiter(1)
    research_limiter = anyio.CapacityLimiter(8)
    server = FastMCP(
        name,
        instructions=(
            "한국 국회의 공식 기록을 조사하는 MCP입니다. 한국어 요청을 우선 정확히 "
            "해석하되 영어 요청도 지원합니다. 광범위한 질문은 start_research 또는 "
            "explore_issue를 한 번만 호출하고, 모든 응답의 next_action을 그대로 따르세요. "
            "같은 조사가 running이라고 새 research를 만들지 마세요. complete/partial 뒤에는 "
            "get_research_overview로 핵심과 전체 자료 지도를 먼저 확인하세요. 빠른 결과는 "
            "누락을 뜻하지 않으며 catalog의 next_offset을 끝까지 사용해야 합니다. 필요한 "
            "자료만 선택해 get_evidence_document로 열고, 사용자가 전건 조사를 요구했을 때만 "
            "get_research_page(exhaustive=true)와 scope=all을 끝까지 사용하세요. "
            "coverage.complete와 page.complete가 모두 true이기 전에는 종합 조사가 끝났다고 "
            "말하지 마세요. 일부 top-N만 임의 선택하거나 근거 유형을 생략하거나 원문을 "
            "잘라서는 안 됩니다. 필요한 전체 원문은 get_evidence_document로 열고 공식 URL, "
            "해시, 페이지/구간 locator를 인용하세요. 영문 답변에서는 한국어 원문을 충실히 "
            "번역하고 번역 인용임을 밝히세요. unfamiliar proper noun에는 korean_query로 짧은 "
            "한국어 검색 힌트를 줄 수 있지만 원래 질문의 의도를 바꾸면 안 됩니다. Durable "
            "research tools가 없는 로컬 서버에서는 explore_issue의 research_pagination을 "
            "next_minutes_offset가 없어질 때까지 따라가고, 관련 법안의 전문위원 검토보고서는 "
            "get_bill_status로 확인하세요."
        ),
        host=host,
        port=port,
        stateless_http=stateless_http,
        json_response=True,
        transport_security=transport_security,
    )
    # FastMCP does not currently expose the low-level server version in its
    # constructor. Without setting it here, MCP initialize reports the SDK
    # package version instead of this server's release version.
    server._mcp_server.version = __version__
    legacy_methods: tuple[Callable[..., Any], ...] = (
        implementation.search_speeches,
        implementation.get_speech,
        implementation.get_speech_context,
        implementation.list_committees,
        implementation.list_meetings,
        implementation.search_bills,
        implementation.get_bill_status,
        implementation.explore_issue,
    )
    read_annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
    research_start_annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        # Starting a durable retrieval creates a fresh research capability;
        # clients must follow its research_id rather than silently retrying it.
        idempotentHint=False,
        openWorldHint=True,
    )
    for method in legacy_methods:
        limiter = (
            research_limiter
            if services.research is not None and method.__name__ == "explore_issue"
            else legacy_limiter
        )
        annotations = (
            research_start_annotations
            if method.__name__ == "explore_issue"
            else read_annotations
        )
        server.tool(annotations=annotations)(_offloaded_tool(method, limiter, anyio))
    if services.research is not None:
        research_methods: tuple[Callable[..., Any], ...] = (
            implementation.start_research,
            implementation.get_research_status,
            implementation.get_research_overview,
            implementation.get_research_page,
            implementation.get_evidence_document,
        )
        for research_method in research_methods:
            annotations = (
                research_start_annotations
                if research_method.__name__ == "start_research"
                else read_annotations
            )
            server.tool(annotations=annotations)(
                _offloaded_tool(research_method, research_limiter, anyio)
            )
    return server


def _offloaded_tool(
    method: Callable[..., Any], limiter: Any, anyio_module: Any
) -> Callable[..., Any]:
    """Keep a tool's public signature while running its blocking body off-loop."""

    @wraps(method)
    async def invoke(*args: Any, **kwargs: Any) -> Any:
        call = partial(method, *args, **kwargs)
        return await anyio_module.to_thread.run_sync(call, limiter=limiter)

    return invoke


def run(
    services: ServiceContext,
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Run stdio locally or stateless Streamable HTTP for public deployment."""

    if transport not in {"stdio", "streamable-http"}:
        raise ValueError("transport must be stdio or streamable-http")
    create_server(services, host=host, port=port).run(transport=transport)
