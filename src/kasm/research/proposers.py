"""Strict Korean legislator-name scopes for proposer searches.

The Open Assembly bill feed separates the representative proposer
(``RST_PROPOSER``) from the other proposers (``PUBL_PROPOSER``).  This module
only interprets an ordinary Korean name when the user also writes an explicit
proposer role.  A bare name or a surname is intentionally not guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

_NAME_TEXT: Final = r"(?P<name>[가-힣]{2,5})"
_INVALID_NAMES: Final = frozenset(
    {
        "관련",
        "공동",
        "국회",
        "국회의원",
        "국민",
        "대표",
        "대통령",
        "모든",
        "법률안",
        "법안",
        "어떤",
        "여러",
        "여야",
        "여당",
        "의원",
        "위원회",
        "정부",
        "정당",
        "전체",
        "해당",
        "현직",
        "야당",
    }
)
_INVALID_NAME_SUFFIXES: Final = (
    "국회",
    "대통령",
    "위원회",
)


def _name_first(role: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![가-힣]){_NAME_TEXT}\s*의원(?:이|가|은|는|의)?\s*{role}\s*발의"
    )


def _role_first(role: str) -> re.Pattern[str]:
    return re.compile(
        rf"{role}\s*발의자(?:인|는|가|의|로)?\s*{_NAME_TEXT}\s*(?:의원)?(?![가-힣])"
    )


def _explicit_role_name_first(role: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![가-힣])(?P<name>[가-힣]{{2,5}}?)(?:이|가|은|는|의)?\s*"
        rf"{role}\s*발의"
    )


_REPRESENTATIVE_PATTERNS: Final = (
    _name_first(r"대표"),
    _explicit_role_name_first(r"대표"),
    _role_first(r"대표"),
)
_CO_PROPOSER_PATTERNS: Final = (
    _name_first(r"공동"),
    _explicit_role_name_first(r"공동"),
    _role_first(r"공동"),
)
_ANY_PROPOSER_PATTERNS: Final = (
    re.compile(
        rf"(?<![가-힣]){_NAME_TEXT}\s*의원(?:이|가|은|는|의)?\s*"
        rf"(?!대표\s*|공동\s*)발의"
    ),
    re.compile(
        r"(?<![가-힣])(?P<name>[가-힣]{2,5}?)(?:이|가|은|는|의)?\s*"
        r"(?!대표\s*|공동\s*)발의"
    ),
    re.compile(
        rf"(?<!대표)(?<!공동)발의자(?:인|는|가|의|로)?\s*"
        rf"{_NAME_TEXT}\s*(?:의원)?(?![가-힣])"
    ),
)


@dataclass(frozen=True, slots=True)
class ProposerQueryScope:
    """Exact names requested for each official proposer role."""

    representative_proposer_names: tuple[str, ...] = ()
    co_proposer_names: tuple[str, ...] = ()
    proposer_names: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self.representative_proposer_names,
                    *self.co_proposer_names,
                    *self.proposer_names,
                )
            )
        )

    @property
    def explicit(self) -> bool:
        return bool(self.names)


def extract_proposer_query_scope(query: str) -> ProposerQueryScope:
    """Extract only names tied to an explicit proposer expression.

    Role-specific expressions win over the generic ``발의자`` form.  Exact
    full-name matching later prevents ``김민`` from matching ``김민석`` and
    avoids treating an ordinary mention of a legislator as a proposer filter.
    """

    representatives = _names_for_patterns(query, _REPRESENTATIVE_PATTERNS)
    co_proposers = _names_for_patterns(query, _CO_PROPOSER_PATTERNS)
    role_specific = {*representatives, *co_proposers}
    proposers = tuple(
        name
        for name in _names_for_patterns(query, _ANY_PROPOSER_PATTERNS)
        if name not in role_specific
    )
    return ProposerQueryScope(representatives, co_proposers, proposers)


def valid_member_name(value: str) -> bool:
    """Return whether *value* is an unambiguous Korean full-name token."""

    return bool(
        re.fullmatch(r"[가-힣]{2,5}", value)
        and value not in _INVALID_NAMES
        and not value.endswith(_INVALID_NAME_SUFFIXES)
    )


def _names_for_patterns(
    query: str,
    patterns: tuple[re.Pattern[str], ...],
) -> tuple[str, ...]:
    located: list[tuple[int, str]] = []
    for pattern in patterns:
        for match in pattern.finditer(query):
            name = match.group("name")
            if valid_member_name(name):
                located.append((match.start("name"), name))
    located.sort()
    return tuple(dict.fromkeys(name for _position, name in located))


__all__ = [
    "ProposerQueryScope",
    "extract_proposer_query_scope",
    "valid_member_name",
]
