"""Official National Assembly term boundaries.

The dates below are transcribed from the National Assembly Minutes service's
``역대국회기간`` table.  They describe periods in which an elected National
Assembly existed, rather than assuming that every adjacent term is separated
by exactly one day.  In particular, the table records institutional hiatuses
after the 5th, 8th, and 10th Assemblies.

Official source:
https://record.assembly.go.kr/assembly/mnts/minutes/search.do
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Final

OFFICIAL_ASSEMBLY_TERM_SOURCE_URL: Final = (
    "https://record.assembly.go.kr/assembly/mnts/minutes/search.do"
)


@dataclass(frozen=True, slots=True)
class AssemblyTermMetadata:
    """One official National Assembly term and its inclusive date bounds."""

    number: int
    label: str
    date_from: date
    date_to: date
    source_url: str = OFFICIAL_ASSEMBLY_TERM_SOURCE_URL

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("Assembly term number must be positive")
        if not self.label.strip():
            raise ValueError("Assembly term label is required")
        if self.date_from > self.date_to:
            raise ValueError("Assembly term date bounds are invalid")
        if not self.source_url.startswith("https://"):
            raise ValueError("Assembly term source URL must use HTTPS")

    def intersects(self, date_from: date, date_to: date) -> bool:
        """Return whether this term intersects an inclusive calendar range."""

        if date_from > date_to:
            raise ValueError("date_from must be on or before date_to")
        return self.date_from <= date_to and self.date_to >= date_from


ASSEMBLY_TERMS: Final = (
    AssemblyTermMetadata(1, "제헌", date(1948, 5, 31), date(1950, 5, 30)),
    AssemblyTermMetadata(2, "제2대", date(1950, 5, 31), date(1954, 5, 30)),
    AssemblyTermMetadata(3, "제3대", date(1954, 5, 31), date(1958, 5, 30)),
    AssemblyTermMetadata(4, "제4대", date(1958, 5, 31), date(1960, 7, 28)),
    AssemblyTermMetadata(5, "제5대", date(1960, 7, 29), date(1961, 5, 16)),
    AssemblyTermMetadata(6, "제6대", date(1963, 12, 17), date(1967, 6, 30)),
    AssemblyTermMetadata(7, "제7대", date(1967, 7, 1), date(1971, 6, 30)),
    AssemblyTermMetadata(8, "제8대", date(1971, 7, 1), date(1972, 10, 17)),
    AssemblyTermMetadata(9, "제9대", date(1973, 3, 12), date(1979, 3, 11)),
    AssemblyTermMetadata(10, "제10대", date(1979, 3, 12), date(1980, 10, 27)),
    AssemblyTermMetadata(11, "제11대", date(1981, 4, 11), date(1985, 4, 10)),
    AssemblyTermMetadata(12, "제12대", date(1985, 4, 11), date(1988, 5, 29)),
    AssemblyTermMetadata(13, "제13대", date(1988, 5, 30), date(1992, 5, 29)),
    AssemblyTermMetadata(14, "제14대", date(1992, 5, 30), date(1996, 5, 29)),
    AssemblyTermMetadata(15, "제15대", date(1996, 5, 30), date(2000, 5, 29)),
    AssemblyTermMetadata(16, "제16대", date(2000, 5, 30), date(2004, 5, 29)),
    AssemblyTermMetadata(17, "제17대", date(2004, 5, 30), date(2008, 5, 29)),
    AssemblyTermMetadata(18, "제18대", date(2008, 5, 30), date(2012, 5, 29)),
    AssemblyTermMetadata(19, "제19대", date(2012, 5, 30), date(2016, 5, 29)),
    AssemblyTermMetadata(20, "제20대", date(2016, 5, 30), date(2020, 5, 29)),
    AssemblyTermMetadata(21, "제21대", date(2020, 5, 30), date(2024, 5, 29)),
    AssemblyTermMetadata(22, "제22대", date(2024, 5, 30), date(2028, 5, 29)),
)

_TERM_BY_NUMBER: Final[Mapping[int, AssemblyTermMetadata]] = MappingProxyType(
    {item.number: item for item in ASSEMBLY_TERMS}
)

# Backwards-compatible name used by scope and partition planners.  A read-only
# mapping prevents one request from mutating the process-wide official bounds.
DEFAULT_ASSEMBLY_TERM_BOUNDS: Final[Mapping[int, tuple[date, date]]] = MappingProxyType(
    {item.number: (item.date_from, item.date_to) for item in ASSEMBLY_TERMS}
)


def assembly_term(term: int) -> AssemblyTermMetadata:
    """Return official metadata for ``term`` or reject an unknown future term."""

    try:
        return _TERM_BY_NUMBER[term]
    except KeyError as exc:
        raise ValueError(f"unsupported Assembly term: {term}") from exc


def assembly_term_for_date(value: date) -> AssemblyTermMetadata | None:
    """Return the elected Assembly active on ``value``, if one existed."""

    return next(
        (item for item in ASSEMBLY_TERMS if item.date_from <= value <= item.date_to),
        None,
    )


def assembly_terms_intersecting(
    date_from: date,
    date_to: date,
) -> tuple[AssemblyTermMetadata, ...]:
    """Return every elected Assembly intersecting an inclusive date range."""

    if date_from > date_to:
        raise ValueError("date_from must be on or before date_to")
    return tuple(item for item in ASSEMBLY_TERMS if item.intersects(date_from, date_to))


def _validate_catalog() -> None:
    expected_numbers = tuple(range(1, 23))
    if tuple(item.number for item in ASSEMBLY_TERMS) != expected_numbers:
        raise RuntimeError("Assembly term catalog must contain terms 1 through 22 in order")
    for previous, current in zip(ASSEMBLY_TERMS, ASSEMBLY_TERMS[1:], strict=False):
        if previous.date_to >= current.date_from:
            raise RuntimeError("Assembly term catalog contains an overlap")


_validate_catalog()


__all__ = [
    "ASSEMBLY_TERMS",
    "DEFAULT_ASSEMBLY_TERM_BOUNDS",
    "OFFICIAL_ASSEMBLY_TERM_SOURCE_URL",
    "AssemblyTermMetadata",
    "assembly_term",
    "assembly_term_for_date",
    "assembly_terms_intersecting",
]
