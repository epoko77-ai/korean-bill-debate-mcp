"""Versioned legal terminology used by query expansion and relevance scoring.

The registry distinguishes two relationships that must not be conflated:

``equivalent``
    A spelling, abbreviation, or translation that denotes the same concept.
``related``
    A concept worth checking alongside the requested one, but not evidence that
    the requested concept itself matched.

The data is intentionally small and reviewed.  Unknown expressions are never
guessed, every expansion explains why it was produced, and all output ordering
is derived from the immutable registry order.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

TERMINOLOGY_VERSION: Final = "2026.07.1"
MAX_TERMINOLOGY_INPUT_CHARS: Final = 1_000
MAX_TERMINOLOGY_EXPANSIONS: Final = 64


class TermCategory(StrEnum):
    """Kinds of concepts that downstream scoring treats differently."""

    ISSUE = "issue"
    STATUTE = "statute"
    COMMITTEE = "committee"


class TermRelation(StrEnum):
    """Strength of the relationship between input and expanded term."""

    EQUIVALENT = "equivalent"
    RELATED = "related"


@dataclass(frozen=True, slots=True)
class TerminologyConcept:
    """One canonical concept and its reviewed surface forms."""

    concept_id: str
    canonical: str
    category: TermCategory
    equivalents: tuple[str, ...] = ()
    related: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TermExpansion:
    """An auditable term emitted from one detected source concept."""

    source_text: str
    source_concept_id: str
    target_concept_id: str
    term: str
    category: TermCategory
    relation: TermRelation
    reason: str


@dataclass(frozen=True, slots=True)
class TerminologyExpansion:
    """Deterministic registry output for one input string."""

    registry_version: str
    expansions: tuple[TermExpansion, ...]

    @property
    def equivalent_terms(self) -> tuple[str, ...]:
        return _distinct(
            expansion.term
            for expansion in self.expansions
            if expansion.relation is TermRelation.EQUIVALENT
        )

    @property
    def related_terms(self) -> tuple[str, ...]:
        return _distinct(
            expansion.term
            for expansion in self.expansions
            if expansion.relation is TermRelation.RELATED
        )

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(expansion.reason for expansion in self.expansions)


@dataclass(frozen=True, slots=True)
class _DetectedConcept:
    concept: TerminologyConcept
    source_text: str
    start: int
    end: int


class TerminologyRegistry:
    """Validated immutable concept registry."""

    def __init__(self, version: str, concepts: Sequence[TerminologyConcept]) -> None:
        if not version.strip():
            raise ValueError("terminology version must not be empty")
        if not concepts:
            raise ValueError("terminology registry must contain concepts")
        ids = tuple(concept.concept_id for concept in concepts)
        if len(ids) != len(set(ids)) or any(not value.strip() for value in ids):
            raise ValueError("terminology concept ids must be non-empty and unique")
        known_ids = set(ids)
        surfaces: dict[str, str] = {}
        for concept in concepts:
            if not concept.canonical.strip():
                raise ValueError("terminology canonical terms must not be empty")
            if concept.concept_id in concept.related:
                raise ValueError("a terminology concept cannot relate to itself")
            if unknown := set(concept.related) - known_ids:
                raise ValueError(f"unknown related terminology concepts: {sorted(unknown)}")
            for surface in (concept.canonical, *concept.equivalents):
                key = _surface_key(surface)
                if not key:
                    raise ValueError("terminology surfaces must not be empty")
                owner = surfaces.setdefault(key, concept.concept_id)
                if owner != concept.concept_id:
                    raise ValueError(f"ambiguous terminology surface: {surface}")
        self.version = version
        self.concepts = tuple(concepts)
        self._by_id = {concept.concept_id: concept for concept in self.concepts}
        self._surface_owner = surfaces

    def expand(
        self,
        text: str,
        *,
        include_related: bool = True,
        max_input_chars: int = MAX_TERMINOLOGY_INPUT_CHARS,
        max_expansions: int = MAX_TERMINOLOGY_EXPANSIONS,
    ) -> TerminologyExpansion:
        """Resolve reviewed concepts in ``text`` without guessing unknown terms."""

        if max_input_chars < 1 or max_expansions < 1:
            raise ValueError("terminology limits must be positive")
        if len(text) > max_input_chars:
            raise ValueError(
                f"terminology input must not exceed {max_input_chars} characters"
            )
        detected = self._detect(text)
        expansions: list[TermExpansion] = []
        emitted: set[tuple[str, TermRelation]] = set()
        for match in detected:
            concept = match.concept
            equivalent = TermExpansion(
                source_text=match.source_text,
                source_concept_id=concept.concept_id,
                target_concept_id=concept.concept_id,
                term=concept.canonical,
                category=concept.category,
                relation=TermRelation.EQUIVALENT,
                reason=(
                    f"canonical_match:{concept.concept_id}"
                    if _surface_key(match.source_text) == _surface_key(concept.canonical)
                    else f"equivalent_alias:{match.source_text}→{concept.canonical}"
                ),
            )
            key = (equivalent.target_concept_id, equivalent.relation)
            if key not in emitted:
                emitted.add(key)
                expansions.append(equivalent)
            if not include_related:
                continue
            for target_id in concept.related:
                target = self._by_id[target_id]
                related = TermExpansion(
                    source_text=match.source_text,
                    source_concept_id=concept.concept_id,
                    target_concept_id=target.concept_id,
                    term=target.canonical,
                    category=target.category,
                    relation=TermRelation.RELATED,
                    reason=f"related_concept:{concept.canonical}→{target.canonical}",
                )
                key = (related.target_concept_id, related.relation)
                if key not in emitted:
                    emitted.add(key)
                    expansions.append(related)
        # If a concept occurred directly, its equivalent relationship is
        # stronger than a related edge emitted by another matched concept.
        exact_ids = {
            expansion.target_concept_id
            for expansion in expansions
            if expansion.relation is TermRelation.EQUIVALENT
        }
        expansions = [
            expansion
            for expansion in expansions
            if not (
                expansion.relation is TermRelation.RELATED
                and expansion.target_concept_id in exact_ids
            )
        ]
        if len(expansions) > max_expansions:
            raise ValueError(
                f"terminology expansion exceeds {max_expansions} reviewed terms"
            )
        return TerminologyExpansion(self.version, tuple(expansions))

    def canonicalize(self, value: str, *, category: TermCategory | None = None) -> str:
        """Canonicalize a complete equivalent surface; leave unknown text unchanged."""

        concept_id = self._surface_owner.get(_surface_key(value))
        if concept_id is None:
            return value
        concept = self._by_id[concept_id]
        if category is not None and concept.category is not category:
            return value
        return concept.canonical

    def canonicalize_committee(self, value: str | None) -> str | None:
        """Return a full committee name only for a reviewed equivalent alias."""

        if value is None:
            return None
        return self.canonicalize(value, category=TermCategory.COMMITTEE)

    def normalize_equivalents(
        self, text: str, *, max_input_chars: int = 100_000
    ) -> str:
        """Replace equivalent surfaces while preserving all related distinctions."""

        if len(text) > max_input_chars:
            raise ValueError(
                f"terminology normalization input must not exceed {max_input_chars} characters"
            )
        detected = sorted(self._detect(text), key=lambda item: item.start)
        if not detected:
            return " ".join(text.casefold().split())
        parts: list[str] = []
        cursor = 0
        for match in detected:
            parts.append(text[cursor : match.start].casefold())
            parts.append(match.concept.canonical)
            cursor = match.end
        parts.append(text[cursor:].casefold())
        return " ".join("".join(parts).split())

    def _detect(self, text: str) -> tuple[_DetectedConcept, ...]:
        possible: list[_DetectedConcept] = []
        for concept in self.concepts:
            for surface in (concept.canonical, *concept.equivalents):
                for match in _surface_pattern(surface).finditer(text):
                    possible.append(
                        _DetectedConcept(
                            concept=concept,
                            source_text=match.group(),
                            start=match.start(),
                            end=match.end(),
                        )
                    )
        possible.sort(
            key=lambda item: (
                -(item.end - item.start),
                item.start,
                self._concept_index(item.concept),
            )
        )
        selected: list[_DetectedConcept] = []
        occupied: list[tuple[int, int]] = []
        for item in possible:
            if any(item.start < end and item.end > start for start, end in occupied):
                continue
            selected.append(item)
            occupied.append((item.start, item.end))
        selected.sort(key=lambda item: (self._concept_index(item.concept), item.start))
        return tuple(selected)

    def _concept_index(self, concept: TerminologyConcept) -> int:
        return self.concepts.index(concept)


def _surface_pattern(surface: str) -> re.Pattern[str]:
    parts = re.findall(r"[0-9A-Za-z가-힣]+", surface)
    if not parts:
        return re.compile(r"(?!x)x")
    body = r"[\s._·()/\-]*".join(re.escape(part) for part in parts)
    prefix = r"(?<![a-z0-9])" if parts[0][0].isascii() else ""
    suffix = r"(?![a-z0-9])" if parts[-1][-1].isascii() else ""
    return re.compile(prefix + body + suffix, re.IGNORECASE)


def _surface_key(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", value.casefold())


def _distinct(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


_CONCEPTS: Final = (
    TerminologyConcept(
        "committee_legislation_judiciary",
        "법제사법위원회",
        TermCategory.COMMITTEE,
        ("법사위", "Legislation and Judiciary Committee"),
    ),
    TerminologyConcept(
        "committee_science_ict",
        "과학기술정보방송통신위원회",
        TermCategory.COMMITTEE,
        (
            "과방위",
            "Science ICT Broadcasting and Communications Committee",
            "Science, ICT, Broadcasting and Communications Committee",
        ),
    ),
    TerminologyConcept(
        "committee_culture",
        "문화체육관광위원회",
        TermCategory.COMMITTEE,
        ("문체위", "Culture Sports and Tourism Committee"),
    ),
    TerminologyConcept(
        "committee_national_policy",
        "정무위원회",
        TermCategory.COMMITTEE,
        ("정무위", "National Policy Committee"),
    ),
    TerminologyConcept(
        "committee_environment_labor",
        "기후에너지환경노동위원회",
        TermCategory.COMMITTEE,
        (
            "환노위",
            "Climate Energy Environment and Labor Committee",
            "Climate Energy Environment and Labour Committee",
        ),
    ),
    TerminologyConcept(
        "committee_agriculture_oceans",
        "농림축산식품해양수산위원회",
        TermCategory.COMMITTEE,
        (
            "농해수위",
            "Agriculture Food Rural Affairs Oceans and Fisheries Committee",
        ),
    ),
    TerminologyConcept(
        "committee_finance_planning",
        "재정경제기획위원회",
        TermCategory.COMMITTEE,
        ("재경위", "Finance and Economy Planning Committee"),
    ),
    TerminologyConcept(
        "committee_public_administration",
        "행정안전위원회",
        TermCategory.COMMITTEE,
        ("행안위",),
    ),
    TerminologyConcept(
        "committee_health_welfare",
        "보건복지위원회",
        TermCategory.COMMITTEE,
        ("복지위",),
    ),
    TerminologyConcept(
        "committee_industry_smes",
        "산업통상자원중소벤처기업위원회",
        TermCategory.COMMITTEE,
        ("산자위",),
    ),
    TerminologyConcept(
        "committee_land_transport",
        "국토교통위원회",
        TermCategory.COMMITTEE,
        ("국토위",),
    ),
    TerminologyConcept(
        "criminal_procedure_act",
        "형사소송법",
        TermCategory.STATUTE,
        ("Criminal Procedure Act",),
    ),
    TerminologyConcept(
        "housing_lease_protection_act",
        "주택임대차보호법",
        TermCategory.STATUTE,
        ("Housing Lease Protection Act",),
    ),
    TerminologyConcept(
        "ai_basic_act",
        "인공지능 기본법",
        TermCategory.STATUTE,
        (
            "AI기본법",
            "AI 기본법",
            "AI Basic Act",
            "Artificial Intelligence Basic Act",
        ),
        ("artificial_intelligence",),
    ),
    TerminologyConcept(
        "ai_data_center",
        "인공지능 데이터센터",
        TermCategory.ISSUE,
        (
            "AI 데이터센터",
            "AI data center",
            "AI data centre",
            "artificial intelligence data center",
        ),
        ("artificial_intelligence",),
    ),
    TerminologyConcept(
        "sovereign_ai",
        "소버린 AI",
        TermCategory.ISSUE,
        ("sovereign AI", "domestic foundation model", "domestic foundation models"),
        ("artificial_intelligence",),
    ),
    TerminologyConcept(
        "artificial_intelligence",
        "인공지능",
        TermCategory.ISSUE,
        ("AI", "artificial intelligence"),
        ("ai_basic_act",),
    ),
    TerminologyConcept(
        "supplementary_investigation_authority",
        "보완수사권",
        TermCategory.ISSUE,
        (
            "supplementary investigation authority",
            "supplementary investigation power",
            "supplementary investigation right",
            "supplementary investigation rights",
        ),
        (
            "supplementary_investigation_request_authority",
            "criminal_procedure_act",
        ),
    ),
    TerminologyConcept(
        "supplementary_investigation_request_authority",
        "보완수사요구권",
        TermCategory.ISSUE,
        (
            "supplementary investigation request authority",
            "supplementary investigation request power",
        ),
        (
            "supplementary_investigation_authority",
            "criminal_procedure_act",
        ),
    ),
    TerminologyConcept(
        "supplementary_investigation",
        "보완수사",
        TermCategory.ISSUE,
        ("supplementary investigation",),
        (
            "supplementary_investigation_authority",
            "supplementary_investigation_request_authority",
            "criminal_procedure_act",
        ),
    ),
    TerminologyConcept(
        "prosecution_investigation_separation",
        "검수완박",
        TermCategory.ISSUE,
        ("검찰 수사권 완전 박탈",),
        ("prosecution_investigation_adjustment",),
    ),
    TerminologyConcept(
        "prosecution_investigation_adjustment",
        "검찰 수사권 조정",
        TermCategory.ISSUE,
        ("검경 수사권 조정", "prosecutorial investigation reform"),
        ("prosecution_investigation_separation",),
    ),
    TerminologyConcept(
        "serious_crimes_investigation_agency",
        "중대범죄수사청",
        TermCategory.ISSUE,
        ("중수청", "Serious Crimes Investigation Agency"),
    ),
    TerminologyConcept(
        "platform_labor",
        "플랫폼 노동",
        TermCategory.ISSUE,
        ("platform labor", "platform labour"),
        ("platform_worker",),
    ),
    TerminologyConcept(
        "platform_worker",
        "플랫폼 종사자",
        TermCategory.ISSUE,
        ("플랫폼 노동자", "platform worker", "platform workers"),
        ("platform_labor",),
    ),
)

LEGAL_TERMINOLOGY: Final = TerminologyRegistry(TERMINOLOGY_VERSION, _CONCEPTS)

__all__ = [
    "LEGAL_TERMINOLOGY",
    "MAX_TERMINOLOGY_EXPANSIONS",
    "MAX_TERMINOLOGY_INPUT_CHARS",
    "TERMINOLOGY_VERSION",
    "TermCategory",
    "TermExpansion",
    "TermRelation",
    "TerminologyConcept",
    "TerminologyExpansion",
    "TerminologyRegistry",
]
