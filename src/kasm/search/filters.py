"""Structured search filters shared by lexical and semantic backends."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


def _get(item: object, key: str) -> Any:
    return item.get(key) if isinstance(item, Mapping) else getattr(item, key, None)


def _date(value: date | str | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)


@dataclass(frozen=True, slots=True)
class SearchFilters:
    assembly_term: int | None = None
    committee: str | None = None
    speaker: str | None = None
    speaker_role: str | None = None
    organization: str | None = None
    meeting_type: str | None = None
    date_from: date | str | None = None
    date_to: date | str | None = None

    def __post_init__(self) -> None:
        start, end = _date(self.date_from), _date(self.date_to)
        if start and end and start > end:
            raise ValueError("date_from must be on or before date_to")

    def matches(self, item: object) -> bool:
        exact = ("assembly_term", "speaker_role", "organization", "meeting_type")
        for key in exact:
            expected = getattr(self, key)
            if expected is not None and _get(item, key) != expected:
                return False
        # Committee and speaker accept canonical IDs, exact names, or a
        # case-insensitive name fragment.
        for key in ("committee", "speaker"):
            expected = getattr(self, key)
            if expected is not None:
                actual = (
                    _get(item, key) or _get(item, f"{key}_name") or _get(item, f"{key}_name_ko")
                )
                if actual is None or str(expected).casefold() not in str(actual).casefold():
                    return False
        actual_date = _get(item, "date") or _get(item, "meeting_date")
        if self.date_from is not None or self.date_to is not None:
            if actual_date is None:
                return False
            actual = _date(
                actual_date.isoformat() if isinstance(actual_date, datetime) else actual_date
            )
            start = _date(self.date_from)
            end = _date(self.date_to)
            if actual is None:
                return False
            if (start is not None and actual < start) or (end is not None and actual > end):
                return False
        return True

    def as_dict(self) -> dict[str, object]:
        return {
            key: value.isoformat() if isinstance(value, date) else value
            for key, value in (
                ("assembly_term", self.assembly_term),
                ("committee", self.committee),
                ("speaker", self.speaker),
                ("speaker_role", self.speaker_role),
                ("organization", self.organization),
                ("meeting_type", self.meeting_type),
                ("date_from", self.date_from),
                ("date_to", self.date_to),
            )
            if value is not None
        }
