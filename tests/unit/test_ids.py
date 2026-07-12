from datetime import date

import pytest

from kasm.core.ids import meeting_id, speech_id


def test_ids_are_deterministic_and_human_readable() -> None:
    meeting = meeting_id(22, "Committee", date(2025, 3, 18), "Meeting 001")
    assert meeting == "kna:22:committee:2025-03-18:meeting-001"
    assert speech_id(meeting, 183).endswith(":speech-0183")


def test_id_rejects_invalid_sequence() -> None:
    with pytest.raises(ValueError):
        speech_id("kna:22:committee:2025-03-18:m1", -1)
