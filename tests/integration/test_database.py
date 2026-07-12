from datetime import UTC, date, datetime

from kasm.core.models import Meeting, Speech
from kasm.storage.database import Database
from kasm.storage.repositories import MeetingRepository, SpeechRepository


def test_database_repositories_and_fts() -> None:
    meeting = Meeting(
        "kna:22:committee:2025-03-18:m1",
        22,
        None,
        "과방위",
        None,
        "회의",
        "committee",
        None,
        date(2025, 3, 18),
        "https://example.test",
        "hash",
        datetime.now(UTC),
    )
    speech = Speech(
        f"{meeting.id}:speech-0001",
        meeting.id,
        1,
        None,
        "홍길동",
        "의원",
        None,
        "인공지능 기본법을 논의합니다.",
        None,
        None,
        None,
        "p.1",
        "hash",
        "1.0",
    )
    with Database(":memory:") as database:
        MeetingRepository(database).save(meeting)
        SpeechRepository(database).save(speech)
        assert SpeechRepository(database).require(speech.id) == speech
        count = database.conn.execute(
            "SELECT count(*) FROM speeches_fts WHERE speeches_fts MATCH ?", ("인공지능",)
        ).fetchone()[0]
        assert count == 1
