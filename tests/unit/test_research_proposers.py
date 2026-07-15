from __future__ import annotations

import pytest

from kasm.research.proposers import ProposerQueryScope, extract_proposer_query_scope


@pytest.mark.parametrize(
    ("query", "expected"),
    (
        (
            "김남근 대표발의 법안",
            ProposerQueryScope(representative_proposer_names=("김남근",)),
        ),
        (
            "김남근이 대표 발의한 법안",
            ProposerQueryScope(representative_proposer_names=("김남근",)),
        ),
        (
            "김윤 공동발의 법안",
            ProposerQueryScope(co_proposer_names=("김윤",)),
        ),
        (
            "박정이 발의한 법안",
            ProposerQueryScope(proposer_names=("박정",)),
        ),
    ),
)
def test_common_explicit_role_phrases_extract_exact_names(
    query: str,
    expected: ProposerQueryScope,
) -> None:
    assert extract_proposer_query_scope(query) == expected


@pytest.mark.parametrize(
    "query",
    (
        "김남근 의원의 인공지능 관련 발언",
        "정부가 발의한 법안",
        "대통령이 대표 발의한 법안",
        "위원회가 발의한 법안",
    ),
)
def test_non_member_or_role_free_phrases_are_not_member_identity_filters(
    query: str,
) -> None:
    assert extract_proposer_query_scope(query) == ProposerQueryScope()


def test_possessive_particle_is_not_absorbed_into_member_name() -> None:
    assert extract_proposer_query_scope(
        "강명순의 대표발의 법안"
    ) == ProposerQueryScope(representative_proposer_names=("강명순",))


def test_committee_name_is_not_mistaken_for_a_role_agnostic_proposer() -> None:
    assert extract_proposer_query_scope(
        "법사위원회가 발의한 법안"
    ) == ProposerQueryScope()
