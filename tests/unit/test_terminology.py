from __future__ import annotations

import pytest

from kasm.search.terminology import (
    LEGAL_TERMINOLOGY,
    TERMINOLOGY_VERSION,
    TermRelation,
)


def test_ai_basic_act_prefers_the_longest_equivalent_and_explains_expansion() -> None:
    result = LEGAL_TERMINOLOGY.expand("최근 AI기본법 심사", include_related=True)

    assert result.registry_version == TERMINOLOGY_VERSION
    assert result.equivalent_terms == ("인공지능 기본법",)
    assert result.related_terms == ("인공지능",)
    assert result.expansions[0].reason == "equivalent_alias:AI기본법→인공지능 기본법"
    assert all(expansion.reason for expansion in result.expansions)


def test_supplementary_authorities_are_related_but_never_equivalent() -> None:
    result = LEGAL_TERMINOLOGY.expand("보완수사권", include_related=True)

    assert result.equivalent_terms == ("보완수사권",)
    assert result.related_terms == ("보완수사요구권", "형사소송법")
    request = next(
        expansion
        for expansion in result.expansions
        if expansion.term == "보완수사요구권"
    )
    assert request.relation is TermRelation.RELATED
    assert request.reason == "related_concept:보완수사권→보완수사요구권"


def test_reviewed_political_and_institutional_terms_keep_their_relationships() -> None:
    separation = LEGAL_TERMINOLOGY.expand("검수완박")
    agency = LEGAL_TERMINOLOGY.expand("중수청")
    platform = LEGAL_TERMINOLOGY.expand("플랫폼 노동")

    assert separation.equivalent_terms == ("검수완박",)
    assert separation.related_terms == ("검찰 수사권 조정",)
    assert agency.equivalent_terms == ("중대범죄수사청",)
    assert platform.equivalent_terms == ("플랫폼 노동",)
    assert platform.related_terms == ("플랫폼 종사자",)


def test_committee_abbreviations_are_equivalent_not_fuzzy_guesses() -> None:
    assert LEGAL_TERMINOLOGY.canonicalize_committee("법사위") == "법제사법위원회"
    assert LEGAL_TERMINOLOGY.canonicalize_committee("과방위") == (
        "과학기술정보방송통신위원회"
    )
    assert LEGAL_TERMINOLOGY.canonicalize_committee("알수없는위") == "알수없는위"


def test_normalization_preserves_related_concept_distinctions() -> None:
    normalized = LEGAL_TERMINOLOGY.normalize_equivalents(
        "supplementary investigation authority와 보완수사요구권"
    )

    assert normalized == "보완수사권와 보완수사요구권"
    assert "보완수사권" in normalized
    assert "보완수사요구권" in normalized
    assert LEGAL_TERMINOLOGY.normalize_equivalents("소버린 AI") == "소버린 AI"


def test_expansion_is_deterministic_length_bounded_and_fail_closed() -> None:
    query = "AI기본법과 검수완박, 중수청, 플랫폼 노동"

    assert LEGAL_TERMINOLOGY.expand(query) == LEGAL_TERMINOLOGY.expand(query)
    with pytest.raises(ValueError, match="must not exceed"):
        LEGAL_TERMINOLOGY.expand("AI " * 400, max_input_chars=500)
    with pytest.raises(ValueError, match="exceeds 1"):
        LEGAL_TERMINOLOGY.expand("보완수사권", max_expansions=1)


def test_unknown_term_is_not_guessed() -> None:
    result = LEGAL_TERMINOLOGY.expand("완전히 새로운 미등록 정책어")

    assert result.expansions == ()
    assert result.equivalent_terms == ()
    assert result.related_terms == ()
