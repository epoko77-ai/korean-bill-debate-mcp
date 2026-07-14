from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from kasm.mcp.tools import KasmTools, ServiceContext


@dataclass
class Result:
    speech_id: str
    text: str


class FakeSearch:
    def __init__(self) -> None:
        self.call = None

    def search(self, query, **filters):
        self.call = (query, filters)
        return [Result("speech-1", "소버린 AI가 필요합니다.")]


class FakeRepository:
    def get_speech(self, speech_id):
        return Result(speech_id, "원문") if speech_id == "speech-1" else None

    def get_speech_context(self, speech_id, *, before, after):
        return {"speech_id": speech_id, "before": before, "after": after, "speeches": []}

    def list_committees(self, **filters):
        return [{"id": "science-ict", **filters}]

    def list_meetings(self, **filters):
        return [{"id": "meeting-1", **filters}]


@pytest.fixture
def configured_tools():
    search = FakeSearch()
    return KasmTools(ServiceContext(search, FakeRepository())), search


def test_search_speeches_is_transport_independent_and_forwards_filters(configured_tools):
    tools, search = configured_tools
    response = tools.search_speeches("소버린 AI", assembly_term=22, limit=3)

    assert response["query"] == "소버린 AI"
    assert response["results"][0]["speech_id"] == "speech-1"
    assert search.call[1]["assembly_term"] == 22
    assert search.call[1]["limit"] == 3
    assert "committee" not in search.call[1]


@pytest.mark.parametrize(
    ("explicit", "expected_complete"),
    ((False, False), (True, True)),
)
def test_legacy_live_pagination_separates_window_from_overall_temporal_scope(
    explicit: bool,
    expected_complete: bool,
) -> None:
    class LiveRefreshSearch(FakeSearch):
        def __init__(self) -> None:
            super().__init__()
            self.last_refresh = {
                "has_more": False,
                "minutes_failures": 0,
                "months_queried": ["2026-06", "2026-07"],
                "temporal_scope": {
                    "mode": "explicit" if explicit else "implicit_recent_two_month_window",
                    "explicit": explicit,
                    "requested_months": ["2026-06", "2026-07"] if explicit else [],
                    "queried_months": ["2026-06", "2026-07"],
                },
            }

    search = LiveRefreshSearch()
    tools = KasmTools(ServiceContext(search, FakeRepository()))

    response = tools.search_speeches("인공지능 입법")

    pagination = response["research_pagination"]
    assert pagination["window_complete"] is True
    assert pagination["overall_complete"] is expected_complete
    assert pagination["complete"] is expected_complete
    assert pagination["partial"] is not expected_complete
    assert pagination["temporal_scope"]["explicit"] is explicit


def test_search_validates_query_and_limit(configured_tools):
    tools, _ = configured_tools
    with pytest.raises(ValueError, match="query"):
        tools.search_speeches("  ")
    with pytest.raises(ValueError, match="limit"):
        tools.search_speeches("AI", limit=101)


def test_english_search_uses_korean_terms_and_reports_language_metadata(configured_tools):
    tools, search = configured_tools

    response = tools.search_speeches(
        "What did lawmakers say about the AI Basic Act?",
        korean_query="인공지능 기본법 의원 발언",
    )

    assert search.call[0] == "인공지능 기본법 의원 발언"
    assert response["query"] == "What did lawmakers say about the AI Basic Act?"
    assert response["query_language"] == "en"
    assert response["search_query_ko"] == "인공지능 기본법 의원 발언"
    assert response["query_translation"] == "client_supplied"
    assert response["source_language"] == "ko"


def test_speech_and_catalog_tools(configured_tools):
    tools, _ = configured_tools
    assert tools.get_speech("speech-1")["text"] == "원문"
    assert tools.get_speech_context("speech-1", 1, 3)["after"] == 3
    assert tools.get_speech_context("speech-1", 125, 250)["after"] == 250
    assert tools.list_committees(assembly_term=22)[0]["assembly_term"] == 22
    assert tools.list_meetings(committee="과방위")[0]["committee"] == "과방위"
    assert tools.list_meetings(committee="National Policy Committee")[0]["committee"] == (
        "정무위원회"
    )
    with pytest.raises(LookupError, match="not found"):
        tools.get_speech("missing")


class FakeResearchBackend:
    def __init__(self) -> None:
        self.start_call: tuple[str, dict[str, Any]] | None = None
        self.status: dict[str, Any] = {
            "research_id": "research_1",
            "status": "running",
            "progress": 0.5,
            "retry_after_seconds": 3,
        }

    def start_research(self, query: str, **options: Any) -> dict[str, Any]:
        self.start_call = (query, options)
        return {"research_id": "research_1", "status": "queued"}

    def get_research_status(self, research_id: str) -> dict[str, Any]:
        assert research_id == "research_1"
        return self.status

    def get_research_overview(
        self,
        research_id: str,
        *,
        offset: int = 0,
        page_size: int = 20,
    ) -> dict[str, Any]:
        assert research_id == "research_1"
        assert offset == 0
        assert page_size == 20
        return {
            "research_id": research_id,
            "phase": "final",
            "complete": True,
            "provisional": False,
            "substantive_conclusion_available": True,
            "core": [
                {"rank": 1, "evidence_id": "e1", "text_inline_complete": False},
                {"rank": 2, "evidence_id": "e2", "text_inline_complete": False},
            ],
            "core_full_text_required_ids": ["e1", "e2"],
            "catalog": {
                "page": {
                    "total": 2,
                    "returned_count": 2,
                    "returned_through": 2,
                    "next_offset": None,
                    "complete": True,
                },
                "groups": [
                    {"entity_type": "bill", "entity_id": "2219564"},
                    {"entity_type": "document", "entity_id": "review-1"},
                ],
            },
        }

    def get_research_page(
        self,
        research_id: str,
        *,
        cursor: str | None = None,
        page_size: int = 20,
    ) -> dict[str, Any]:
        assert research_id == "research_1"
        assert page_size == 20
        if cursor is None:
            return {
                "research_id": research_id,
                "coverage": {"complete": True},
                "page": {
                    "matched_total": 2,
                    "returned_count": 1,
                    "returned_through": 1,
                    "next_cursor": "stable-cursor",
                    "complete": False,
                },
                "evidence": [
                    {
                        "id": "e1",
                        "text_inline_complete": False,
                        "text_characters": 120_000,
                        "text_hash": "1" * 64,
                        "text_delivery": "get_evidence_document",
                    }
                ],
                "full_text_required_ids": ["e1"],
                "full_text_required_count": 1,
                "full_text_required_total": 2,
                "first_full_text_required_id": "e1",
            }
        assert cursor == "stable-cursor"
        return {
            "research_id": research_id,
            "coverage": {"complete": True},
            "page": {
                "matched_total": 2,
                "returned_count": 1,
                "returned_through": 2,
                "next_cursor": None,
                "complete": True,
            },
            "evidence": [
                {
                    "id": "e2",
                    "text_inline_complete": False,
                    "text_characters": 130_000,
                    "text_hash": "2" * 64,
                    "text_delivery": "get_evidence_document",
                }
            ],
            "full_text_required_ids": ["e2"],
            "full_text_required_count": 1,
            "full_text_required_total": 2,
            "first_full_text_required_id": "e1",
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
        assert research_id == "research_1"
        assert evidence_id in {"e1", "e2"}
        text = ("원🙂" if evidence_id == "e1" else "둘🙂") * 100_000
        start = int(cursor.removeprefix("document-")) if cursor else 0
        end = min(len(text), start + max_characters)
        return {
            "research_id": research_id,
            "evidence_id": evidence_id,
            "official_url": "https://record.assembly.go.kr/example.pdf",
            "source_locator": "p.1-p.80",
            "text": text[start:end],
            "text_hash": "a" * 64,
            "total_characters": len(text),
            "returned_range": {
                "character_start": start,
                "character_end": end,
            },
            "next_cursor": f"document-{end}" if end < len(text) else None,
            "complete": end == len(text),
            "scope": scope,
            "next_evidence_id": (
                "e2"
                if scope in {"core", "all"}
                and evidence_id == "e1"
                and end == len(text)
                else None
            ),
            "selected_evidence_complete": end == len(text),
            "core_evidence_complete": (
                scope == "core" and evidence_id == "e2" and end == len(text)
            ),
            "research_evidence_complete": (
                scope == "all" and evidence_id == "e2" and end == len(text)
            ),
            "research_coverage_complete": True,
        }


def research_tools() -> tuple[KasmTools, FakeResearchBackend]:
    backend = FakeResearchBackend()
    search = FakeSearch()
    return (
        KasmTools(
            ServiceContext(
                search=search,
                repository=FakeRepository(),
                research=backend,
            )
        ),
        backend,
    )


def test_start_research_returns_receipt_and_preserves_natural_language_scope() -> None:
    tools, backend = research_tools()

    receipt = tools.start_research(
        "최근 AI 입법은 왜 지연됐나?",
        committees=["National Policy Committee", "정무위원회"],
        date_from="2026-01-01",
        korean_query="인공지능 입법 지연",
    )

    assert backend.start_call == (
        "최근 AI 입법은 왜 지연됐나?",
        {
            "korean_query": "인공지능 입법 지연",
            "assembly_term": None,
            "committees": ("정무위원회",),
            "date_from": "2026-01-01",
            "date_to": None,
        },
    )
    assert receipt["research_id"] == "research_1"
    assert receipt["comprehensive_answer_allowed"] is False
    assert receipt["next_action"]["tool"] == "get_research_status"


def test_explore_issue_uses_durable_workflow_without_legacy_truncation() -> None:
    tools, backend = research_tools()

    receipt = tools.explore_issue("최근 AI 입법", limit=1, minutes_offset=99)

    assert backend.start_call is not None
    assert receipt["next_action"]["tool"] == "get_research_status"
    assert receipt["compatibility"] == {
        "entrypoint": "explore_issue",
        "workflow": "durable_research",
        "limit_does_not_truncate_research": True,
        "minutes_offset_ignored": 99,
    }


def test_omitted_structured_scope_never_overrides_natural_language_scope() -> None:
    tools, backend = research_tools()

    tools.explore_issue("제21대 법사위의 플랫폼 노동 관련 논의를 조사해줘")

    assert backend.start_call is not None
    assert backend.start_call[1]["assembly_term"] is None
    assert backend.start_call[1]["committees"] is None


def test_research_status_drives_poll_then_page_without_claiming_completion() -> None:
    tools, backend = research_tools()

    running = tools.get_research_status("research_1")
    assert running["next_action"]["tool"] == "get_research_status"
    assert running["next_action"]["retry_after_seconds"] == 3
    assert running["comprehensive_answer_allowed"] is False

    backend.status = {
        "research_id": "research_1",
        "status": "partial",
        "coverage": {"complete": False, "evidence": {"review_reports": {}}},
        "overview_available": True,
    }
    partial = tools.get_research_status("research_1")
    assert partial["next_action"]["tool"] == "get_research_overview"
    assert partial["comprehensive_answer_allowed"] is False


def test_research_overview_routes_core_before_optional_full_inventory() -> None:
    tools, _backend = research_tools()

    overview = tools.get_research_overview("research_1")

    assert overview["catalog"]["page"]["complete"] is True
    assert overview["core_full_text_required_ids"] == ["e1", "e2"]
    assert overview["next_action"]["tool"] == "get_evidence_document"
    assert overview["next_action"]["arguments"] == {
        "research_id": "research_1",
        "evidence_id": "e1",
        "scope": "core",
    }
    assert overview["comprehensive_answer_allowed"] is False


def test_metadata_overview_routes_every_accepted_catalog_page_before_polling() -> None:
    class PagedMetadataBackend(FakeResearchBackend):
        def get_research_overview(
            self,
            research_id: str,
            *,
            offset: int = 0,
            page_size: int = 20,
        ) -> dict[str, Any]:
            assert research_id == "research_1"
            assert offset == 0
            assert page_size == 20
            return {
                "research_id": research_id,
                "phase": "metadata",
                "complete": False,
                "provisional": True,
                "substantive_conclusion_available": False,
                "accepted_total": 25,
                "catalog": {
                    "offset": 0,
                    "page_size": 20,
                    "total": 25,
                    "returned_count": 20,
                    "next_offset": 20,
                    "complete": False,
                    "entries": [
                        {"candidate_id": f"bill:{number:07d}"}
                        for number in range(20)
                    ],
                },
            }

    backend = PagedMetadataBackend()
    tools = KasmTools(
        ServiceContext(
            search=FakeSearch(),
            repository=FakeRepository(),
            research=backend,
        )
    )

    overview = tools.get_research_overview("research_1")

    assert overview["next_action"]["tool"] == "get_research_overview"
    assert overview["next_action"]["arguments"] == {
        "research_id": "research_1",
        "offset": 20,
        "page_size": 20,
    }
    assert overview["comprehensive_answer_allowed"] is False


def test_research_pages_index_long_text_and_require_lossless_document_reads() -> None:
    tools, _backend = research_tools()

    first = tools.get_research_page("research_1")
    assert "text" not in first["evidence"][0]
    assert first["full_text_required_ids"] == ["e1"]
    assert first["next_action"]["arguments"] == {
        "research_id": "research_1",
        "cursor": "stable-cursor",
        "page_size": 20,
        "exhaustive": False,
    }
    assert first["comprehensive_answer_allowed"] is False

    last = tools.get_research_page("research_1", cursor="stable-cursor")
    assert "text" not in last["evidence"][0]
    assert last["full_text_required_ids"] == ["e2"]
    assert last["comprehensive_answer_allowed"] is False
    assert last["next_action"]["optional"] is True

    exhaustive = tools.get_research_page(
        "research_1", cursor="stable-cursor", exhaustive=True
    )
    assert exhaustive["next_action"]["tool"] == "get_evidence_document"
    assert exhaustive["next_action"]["arguments"] == {
        "research_id": "research_1",
        "evidence_id": "e1",
        "scope": "all",
    }


def test_incomplete_coverage_never_allows_a_comprehensive_answer() -> None:
    tools, backend = research_tools()
    original = backend.get_research_page

    def incomplete_page(*args: Any, **kwargs: Any) -> dict[str, Any]:
        payload = original(*args, **kwargs)
        payload["coverage"] = {
            "complete": False,
            "evidence": {"review_reports": {"gap_reasons": ["upstream timeout"]}},
        }
        return payload

    backend.get_research_page = incomplete_page  # type: ignore[method-assign]
    result = tools.get_research_page("research_1", cursor="stable-cursor")

    assert result["comprehensive_answer_allowed"] is False
    assert result["next_action"]["optional"] is True
    assert result["coverage"]["complete"] is False


def test_evidence_document_pages_reconstruct_full_text_without_truncation() -> None:
    tools, _backend = research_tools()

    chunks: list[str] = []
    cursor = None
    expected_start = 0
    while True:
        document = tools.get_evidence_document(
            "research_1",
            "e1",
            cursor=cursor,
            max_characters=50_000,
            scope="all",
        )
        assert document["returned_range"]["character_start"] == expected_start
        chunks.append(document["text"])
        expected_start = document["returned_range"]["character_end"]
        cursor = document["next_cursor"]
        if document["complete"]:
            assert document["next_action"]["tool"] == "get_evidence_document"
            assert document["next_action"]["arguments"] == {
                "research_id": "research_1",
                "evidence_id": "e2",
                "max_characters": 50_000,
                "scope": "all",
            }
            break
        assert document["next_action"]["tool"] == "get_evidence_document"
        assert document["next_action"]["arguments"] == {
            "research_id": "research_1",
            "evidence_id": "e1",
            "cursor": cursor,
            "max_characters": 50_000,
            "scope": "all",
        }

    assert "".join(chunks) == "원🙂" * 100_000
    assert document["source_locator"] == "p.1-p.80"
    final = None
    cursor = None
    while final is None or not final["complete"]:
        final = tools.get_evidence_document(
            "research_1",
            "e2",
            cursor=cursor,
            max_characters=50_000,
            scope="all",
        )
        cursor = final["next_cursor"]
    assert final["next_action"]["tool"] is None
    assert final["comprehensive_answer_allowed"] is True


def test_core_document_completion_routes_explicit_exhaustive_requests() -> None:
    tools, _backend = research_tools()

    final = None
    cursor = None
    for evidence_id in ("e1", "e2"):
        final = None
        cursor = None
        while final is None or not final["complete"]:
            final = tools.get_evidence_document(
                "research_1",
                evidence_id,
                cursor=cursor,
                max_characters=50_000,
                scope="core",
            )
            cursor = final["next_cursor"]

    assert final is not None
    assert final["core_evidence_complete"] is True
    assert final["comprehensive_answer_allowed"] is False
    assert final["next_action"] == {
        "tool": "get_research_page",
        "arguments": {
            "research_id": "research_1",
            "page_size": 20,
            "exhaustive": True,
        },
        "instruction_ko": (
            "핵심 공식 원문 확인을 마쳤습니다. 현재 확인 범위를 밝혀 우선 답변하고, "
            "사용자가 전건 조사를 요청했다면 전체 근거 목록을 처음부터 exhaustive=true로 "
            "순회한 뒤 모든 긴 원문을 이어서 읽으세요."
        ),
        "instruction_en": (
            "Core source review is complete. Give a scoped answer now; when the user requested "
            "every record, traverse the entire inventory from the beginning with exhaustive=true "
            "and then read every routed long source."
        ),
        "optional": True,
    }
