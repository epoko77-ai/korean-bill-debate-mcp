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
            "한국 국회의 공식 기록을 조사하는 MCP입니다. 특정 법안의 단계별 논의를 묻는 "
            "요청에는 explore_issue를 한 번만 호출하고, 결과 첫머리의 next_action·answer_scope·"
            "answer_brief.mandatory_coverage를 필수 답변 체크리스트로 사용하세요. 소위·상임위·"
            "본회의를 각각 독립 절로 쓰고 모든 direct_claim_evidence를 발언자별 주장·이유·"
            "답변까지 반영하세요. 실질 발언은 한 줄로 축약하지 말고 통상 두 문장 이상의 "
            "상세 항목으로 쓰되 순수 절차 발언만 간결하게 처리하세요. 반환된 본회의 "
            "찬반토론과 supplemental_excerpts의 후반 "
            "논거를 생략하면 미완성 답변입니다. 본회의 직접 근거가 없으면 확인 상태와 "
            "한계를 밝히고, 최종 출력 전 단계별 근거 ID와 논거 수를 대조하세요. "
            "한국어 요청을 우선 정확히 "
            "해석하되 영어 요청도 지원합니다. 제1대부터 제22대까지 명시한 대수·날짜 범위와 "
            "대표발의자·공동발의자·전체 발의자 이름을 공식 필드로 구분합니다. "
            "상위 N건, '5개 정도', 중요 법안 요약과 일반적인 범위 제한 질문은 반드시 "
            "explore_issue(limit=N)를 사용하세요. start_research는 전건·전수·빠짐없이·역대 "
            "또는 여러 국회 대수를 포괄한다고 명시한 조사에만 사용하세요. "
            "특정 법안이나 별칭 하나의 소위원회·상임위원회·본회의 주요 논의는 "
            "explore_issue 한 번으로 확인하고, 먼저 search_bills/list_meetings/search_speeches를 "
            "각각 호출하거나 같은 검색을 반복하지 마세요. "
            "explore_issue가 answer_brief를 반환하면 이를 필수 답변 목차로 사용하세요. "
            "요청 단계와 direct_claim_evidence를 모두 반영하고, 법안 정체·처리 연혁·쟁점별 "
            "주장과 이유·반론 또는 정부 답변·최종 결과·근거 장부·공식 출처를 충분히 "
            "설명하세요. supplemental_excerpts는 같은 발언의 후반 논거이므로 하나도 "
            "버리지 말고, 첫째·둘째·마지막으로처럼 열거된 이유는 각각 나누어 설명하세요. "
            "발언자 몇 명을 짧게 나열하거나 긴 발언의 첫머리만 요약하고 답변을 끝내면 "
            "안 됩니다. "
            "입장은 명시적 발언 없이 추론하지 말고 의원·정부·전문위원을 분리하세요. "
            "모든 응답의 next_action을 그대로 따르세요. "
            "같은 조사가 running이라고 새 research를 만들지 마세요. complete/partial 뒤에는 "
            "get_research_overview로 핵심과 전체 자료 지도를 먼저 확인하세요. 빠른 결과는 "
            "누락을 뜻하지 않으며 catalog의 next_offset을 끝까지 사용해야 합니다. 필요한 "
            "자료만 선택해 get_evidence_document로 열고, 사용자가 전건 조사를 요구했을 때만 "
            "get_research_page(exhaustive=true)와 scope=all을 끝까지 사용하세요. "
            "coverage.complete와 page.complete가 모두 true이기 전에는 전건 조사가 끝났다고 "
            "말하지 마세요. 제한형 답변은 요청한 N건과 조회 범위를 명시하고, 근거 없는 "
            "완전성 주장을 해서는 안 됩니다. 다만 bounded 표시는 상세한 범위 한정 답변을 "
            "생략하라는 뜻이 아니며 한계는 답변 말미에 분리하세요. 필요한 전체 원문은 "
            "get_evidence_document로 "
            "열고 공식 URL, "
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
        # Ordinary explore_issue calls are blocking bounded live reads again;
        # serialize them with the other request-scoped cache/API operations.
        # Explicit exhaustive calls enqueue quickly but sharing this limiter is
        # preferable to allowing overlapping SQLite/cache mutation.
        limiter = legacy_limiter
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
            # start_research may server-reroute an ordinary summary to the
            # bounded live path, so it must share the SQLite/API mutation
            # limiter with explore_issue. Status and artifact reads remain
            # independently available while that bounded call is running.
            limiter = (
                legacy_limiter
                if research_method.__name__ == "start_research"
                else research_limiter
            )
            annotations = (
                research_start_annotations
                if research_method.__name__ == "start_research"
                else read_annotations
            )
            server.tool(annotations=annotations)(
                _offloaded_tool(research_method, limiter, anyio)
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
