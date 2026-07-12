from datetime import UTC, date, datetime

from kasm.core.models import Meeting, Speech
from kasm.search.filters import SearchFilters
from kasm.search.lexical import LexicalSearch, query_terms
from kasm.storage.database import Database
from kasm.storage.repositories import MeetingRepository, SpeechRepository


def test_korean_query_terms_include_particle_stripped_forms() -> None:
    assert query_terms("보완수사권을 폐지에 대한 의견") == [
        "보완수사권을",
        "보완수사권",
        "폐지에",
        "폐지",
        "대한",
        "의견",
    ]


def test_lexical_search_applies_all_structured_filters() -> None:
    with Database(":memory:") as database:
        meeting = Meeting(
            "kna:22:committee:2025-01-23:1",
            22,
            "ICT",
            "과학기술정보방송통신위원회",
            None,
            "회의",
            "committee",
            "1",
            date(2025, 1, 23),
            "https://record.assembly.go.kr/1",
            "hash",
            datetime.now(UTC),
        )
        speech = Speech(
            f"{meeting.id}:speech-0001",
            meeting.id,
            1,
            None,
            "김미래",
            "장관",
            "과학기술정보통신부",
            "인공지능 기본법을 논의합니다.",
            None,
            None,
            None,
            "page:1",
            "hash",
            "v1",
        )
        MeetingRepository(database).save(meeting)
        SpeechRepository(database).save(speech)
        filters = SearchFilters(
            assembly_term=22,
            committee="과학기술",
            speaker="김미",
            speaker_role="장관",
            organization="과학기술정보통신부",
            meeting_type="committee",
            date_from="2025-01-01",
            date_to="2025-12-31",
        )
        assert LexicalSearch(database).search("인공지능", filters)[0]["id"] == speech.id
        assert not LexicalSearch(database).search(
            "인공지능", SearchFilters(organization="다른 기관")
        )
