from datetime import UTC, date, datetime

import pytest

from kasm.core.exceptions import ValidationError
from kasm.core.models import Meeting, SpeechRelation


def test_meeting_serialization_round_trip() -> None:
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
        datetime(2025, 3, 19, tzinfo=UTC),
    )
    assert Meeting.from_dict(meeting.to_dict()) == meeting


def test_relation_validation() -> None:
    with pytest.raises(ValidationError):
        SpeechRelation("a", "b", "UNKNOWN", 0.5)
