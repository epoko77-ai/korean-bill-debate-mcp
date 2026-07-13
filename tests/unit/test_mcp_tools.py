from __future__ import annotations

from dataclasses import dataclass

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
    assert tools.list_committees(assembly_term=22)[0]["assembly_term"] == 22
    assert tools.list_meetings(committee="과방위")[0]["committee"] == "과방위"
    assert tools.list_meetings(committee="National Policy Committee")[0]["committee"] == (
        "정무위원회"
    )
    with pytest.raises(LookupError, match="not found"):
        tools.get_speech("missing")
