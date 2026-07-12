"""Official MCP SDK adapter (optional at import time)."""

from __future__ import annotations

from typing import Any

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
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "The official MCP SDK is not installed. Install the package with its MCP extra."
        ) from exc

    implementation = KasmTools(services)
    server = FastMCP(
        name,
        host=host,
        port=port,
        stateless_http=stateless_http,
        json_response=True,
        transport_security=transport_security,
    )
    server.tool()(implementation.search_speeches)
    server.tool()(implementation.get_speech)
    server.tool()(implementation.get_speech_context)
    server.tool()(implementation.list_committees)
    server.tool()(implementation.list_meetings)
    server.tool()(implementation.search_bills)
    server.tool()(implementation.get_bill_status)
    server.tool()(implementation.explore_issue)
    return server


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
