"""Deterministic scope cues for bounded legislative research requests."""

from __future__ import annotations

import re

_KOREAN_RESULT_COUNT = re.compile(
    r"(?<!\d)(?P<count>[1-9]|[1-4]\d|50)\s*(?:개|건)(?:\s*정도)?"
)
_ENGLISH_RESULT_COUNT = re.compile(
    r"\b(?:top|about|around)\s+(?P<count>[1-9]|[1-4]\d|50)\b",
    re.IGNORECASE,
)
_IMPORTANCE = re.compile(
    r"중요(?:도(?:가)?\s*(?:높은)?)?|주요|핵심|대표적인|\btop\b",
    re.IGNORECASE,
)
_EXHAUSTIVE = re.compile(
    r"전건|전수|빠짐없이|누락(?:하지|없이)|모든\s*(?:법안|자료|회의록)|"
    r"전체\s*(?:목록|법안|자료|회의록)|역대|\b(?:all|every|exhaustive|comprehensive)\b",
    re.IGNORECASE,
)
_COMMITTEE_ONLY = re.compile(r"상임위원회|소위원회|법안심사소위|위원회\s*논의")
_PLENARY = re.compile(r"본회의|전원위원회|\bplenary\b", re.IGNORECASE)


def requested_result_count(query: str) -> int | None:
    """Return an explicit bounded result count, never a year or Assembly term."""

    for pattern in (_KOREAN_RESULT_COUNT, _ENGLISH_RESULT_COUNT):
        if match := pattern.search(query):
            return int(match.group("count"))
    return None


def importance_requested(query: str) -> bool:
    """Whether the user explicitly asks for important or representative items."""

    return bool(_IMPORTANCE.search(query))


def exhaustive_requested(query: str) -> bool:
    """Whether bounded selection would contradict an explicit exhaustive request."""

    return bool(_EXHAUSTIVE.search(query))


def focused_result_request(query: str) -> bool:
    """Whether this is an explicit top-N request rather than corpus-wide research."""

    return requested_result_count(query) is not None and not exhaustive_requested(query)


def committee_only_request(query: str) -> bool:
    """Whether minutes scope explicitly targets committees and omits plenary debate."""

    return bool(_COMMITTEE_ONLY.search(query)) and not bool(_PLENARY.search(query))


__all__ = [
    "committee_only_request",
    "exhaustive_requested",
    "focused_result_request",
    "importance_requested",
    "requested_result_count",
]
