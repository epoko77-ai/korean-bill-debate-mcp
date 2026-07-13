from __future__ import annotations

from datetime import UTC, date, datetime

from kasm.core.models import Meeting
from kasm.research.documents import (
    OfficialDocumentKind,
    ParsedOfficialDocument,
    TextSegment,
)
from kasm.research.transcript_evidence import extract_transcript_evidence

NOW = datetime(2026, 7, 13, tzinfo=UTC)
URL = "https://record.assembly.go.kr/minutes/research.pdf"
HASH = "a" * 64


def _meeting() -> Meeting:
    return Meeting(
        id="kna:meeting:22:subcommittee:2026-07-01:1",
        assembly_term=22,
        committee_id="legislation",
        committee_name_ko="법제사법위원회",
        committee_name_en=None,
        title="법안심사제1소위원회",
        meeting_type="subcommittee",
        meeting_number="1",
        date=date(2026, 7, 1),
        source_url=URL,
        source_hash="b" * 64,
        retrieved_at=NOW,
    )


def _document(*pages: str) -> ParsedOfficialDocument:
    return ParsedOfficialDocument(
        kind=OfficialDocumentKind.MINUTES,
        official_url=URL,
        source_hash=HASH,
        parser_version="pypdf-layout-v1",
        parsed_at=NOW,
        segments=tuple(
            TextSegment(locator=f"p.{number}", text=text)
            for number, text in enumerate(pages, start=1)
        ),
    )


def test_parses_cross_page_sequence_with_exact_page_locators_and_relations() -> None:
    result = extract_transcript_evidence(
        _meeting(),
        _document(
            "1. 형사소송법 일부개정법률안\n○김철수 위원: 정부 입장은 무엇입니까?",
            "○박영희 장관: 정부는 통제 장치가 필요하다고 봅니다.",
        ),
    )

    assert [speech.sequence for speech in result.speeches] == [1, 2]
    assert result.speeches[0].source_locator.startswith("p.1:")
    assert result.speeches[1].source_locator.startswith("p.2:")
    assert result.speeches[1].agenda == "1. 형사소송법 일부개정법률안"
    assert result.speeches[0].next_speech_id == result.speeches[1].id
    assert result.speeches[1].previous_speech_id == result.speeches[0].id
    assert {relation.relation_type for relation in result.relations} >= {
        "QUESTION_TO",
        "ANSWER_TO",
    }


def test_stitches_unmarked_cross_page_continuation_without_losing_text() -> None:
    middle = "둘째 페이지에서도 발언이 계속됩니다.\n통제 장치도 필요합니다."
    last_prefix = "마지막으로 정부 입장은 무엇입니까?\n"
    result = extract_transcript_evidence(
        _meeting(),
        _document(
            "1. 형사소송법 일부개정법률안\n○김철수 위원: 첫 문장입니다.",
            middle,
            f"{last_prefix}○박영희 장관: 정부는 필요하다고 봅니다.",
        ),
    )

    assert len(result.speeches) == 2
    assert result.speeches[0].text == (
        "첫 문장입니다.\n"
        "둘째 페이지에서도 발언이 계속됩니다.\n"
        "통제 장치도 필요합니다.\n"
        "마지막으로 정부 입장은 무엇입니까?"
    )
    locator_parts = (result.speeches[0].source_locator or "").split("|")
    assert len(locator_parts) == 3
    assert locator_parts[0].startswith("p.1:")
    assert locator_parts[1] == f"p.2:0-{len(middle)}"
    assert locator_parts[2] == f"p.3:0-{len(last_prefix)}"
    assert result.failures == ()
    assert {relation.relation_type for relation in result.relations} >= {
        "QUESTION_TO",
        "ANSWER_TO",
    }


def test_does_not_stitch_an_agenda_only_page_into_previous_speech() -> None:
    result = extract_transcript_evidence(
        _meeting(),
        _document(
            "1. 첫 번째 법률안\n○김철수 위원: 첫 안건 발언입니다.",
            "2. 두 번째 법률안",
            "○박영희 장관: 두 번째 안건에 관한 발언입니다.",
        ),
    )

    assert result.speeches[0].text == "첫 안건 발언입니다."
    assert result.speeches[0].source_locator is not None
    assert "p.2" not in result.speeches[0].source_locator
    assert result.failures[0].source_locator == "p.2:0-11"


def test_preserves_a_very_long_speech_without_excerpting() -> None:
    text = "가" * 120_000
    result = extract_transcript_evidence(_meeting(), _document(f"○김위원: {text}"))

    assert result.speeches[0].text == text
    assert len(result.speeches[0].text) == 120_000
    assert result.speech_characters == 120_000


def test_retains_page_parse_failures_as_explicit_coverage_information() -> None:
    result = extract_transcript_evidence(
        _meeting(),
        _document("발언자 표식이 없는 공식 원문", "○김위원: 확인된 발언입니다."),
    )

    assert len(result.speeches) == 1
    assert result.complete_parse is False
    assert result.failures[0].source_locator == "p.1:0-16"
    assert result.page_count == 2
