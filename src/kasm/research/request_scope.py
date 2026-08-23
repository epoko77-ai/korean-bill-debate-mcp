"""Deterministic execution and coverage cues for legislative research requests."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

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
_ASSEMBLY_TERM = re.compile(r"제\s*(?P<term>[1-9]|1\d|2[0-2])\s*대")
_COMMITTEE_ONLY = re.compile(r"상임위원회|소위원회|법안심사소위|위원회\s*논의")
_STAGE_PATTERNS = {
    "subcommittee": re.compile(
        r"소위원회|법안심사(?:제?\d*)?소위|법안소위|\bsubcommittee\b",
        re.IGNORECASE,
    ),
    "standing_committee": re.compile(
        r"상임위원회|상임위|소관위원회|전체회의|\bstanding\s+committee\b",
        re.IGNORECASE,
    ),
    "plenary": re.compile(r"본회의|전원위원회|\bplenary\b", re.IGNORECASE),
}
_GENERIC_COMMITTEE_SCOPE = re.compile(
    r"^(?:소위원회|법안심사(?:제?\d*)?소위|법안소위|상임위원회|상임위|"
    r"소관위원회|전체회의|본회의|전원위원회|subcommittee|"
    r"standing\s+committee|plenary)$",
    re.IGNORECASE,
)


class ResearchExecutionMode(StrEnum):
    """Server-enforced execution mode; MCP client tool choice is only advisory."""

    BOUNDED = "bounded_targeted"
    DURABLE = "durable_exhaustive"


@dataclass(frozen=True, slots=True)
class ResearchRoute:
    mode: ResearchExecutionMode
    reason: str
    requested_stages: tuple[str, ...]
    committees: tuple[str, ...]


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


def requested_stages(query: str) -> tuple[str, ...]:
    """Return the legislative stages the user explicitly asked to inspect."""

    return tuple(
        stage for stage, pattern in _STAGE_PATTERNS.items() if pattern.search(query)
    )


def sanitize_committee_scope(values: Iterable[str] | None) -> tuple[str, ...]:
    """Drop generic stage labels while preserving actual named committee filters."""

    if values is None:
        return ()
    return tuple(
        dict.fromkeys(
            value.strip()
            for value in values
            if value.strip() and not _GENERIC_COMMITTEE_SCOPE.fullmatch(value.strip())
        )
    )


def multiple_assembly_terms_requested(query: str) -> bool:
    """Whether the natural-language request explicitly spans multiple Assemblies."""

    return len({int(match.group("term")) for match in _ASSEMBLY_TERM.finditer(query)}) > 1


def decide_research_route(
    query: str,
    *,
    exhaustive: bool = False,
    committees: Iterable[str] | None = None,
) -> ResearchRoute:
    """Choose durable work only from positive exhaustive or cross-term evidence.

    A client may call ``start_research`` for an ordinary summary. That tool choice,
    a missing bill number, or generic stage words never widens the request.
    """

    stages = requested_stages(query)
    named_committees = sanitize_committee_scope(committees)
    if exhaustive or exhaustive_requested(query):
        return ResearchRoute(
            ResearchExecutionMode.DURABLE,
            "explicit_exhaustive_request",
            stages,
            named_committees,
        )
    if multiple_assembly_terms_requested(query):
        return ResearchRoute(
            ResearchExecutionMode.DURABLE,
            "multi_term_scope",
            stages,
            named_committees,
        )
    if requested_result_count(query) is not None:
        reason = "explicit_bounded_result_count"
    elif stages:
        reason = "bounded_stage_summary"
    elif importance_requested(query):
        reason = "bounded_important_summary"
    else:
        reason = "ordinary_bounded_request"
    return ResearchRoute(
        ResearchExecutionMode.BOUNDED,
        reason,
        stages,
        named_committees,
    )


def committee_only_request(query: str) -> bool:
    """Whether minutes scope explicitly targets committees and omits plenary debate."""

    return bool(_COMMITTEE_ONLY.search(query)) and "plenary" not in requested_stages(query)


__all__ = [
    "ResearchExecutionMode",
    "ResearchRoute",
    "committee_only_request",
    "decide_research_route",
    "exhaustive_requested",
    "focused_result_request",
    "importance_requested",
    "multiple_assembly_terms_requested",
    "requested_result_count",
    "requested_stages",
    "sanitize_committee_scope",
]
