from __future__ import annotations

from kasm.research.request_scope import (
    ResearchExecutionMode,
    committee_only_request,
    decide_research_route,
    exhaustive_requested,
    focused_result_request,
    importance_requested,
    requested_result_count,
    requested_stages,
    sanitize_committee_scope,
)

INCIDENT_QUERY = (
    "최근 본회의를 통과한 닥터나우 금지법과 관련하여, 소위원회, 상임위원회, "
    "본회의에서 의원들의 주요 논의 내용을 정리해줘"
)


def test_exact_ai_question_is_a_focused_five_bill_request() -> None:
    query = (
        "2026년 발의된 인공지능 관련 법안 중 중요도가 높은 법안을 5개 정도 "
        "정리하고, 이에 대한 소위원회, 상임위원회 논의 내용을 정리해줘."
    )

    assert requested_result_count(query) == 5
    assert importance_requested(query)
    assert focused_result_request(query)
    assert committee_only_request(query)


def test_year_and_assembly_term_are_not_mistaken_for_result_counts() -> None:
    assert requested_result_count("2026년 제22대 국회 인공지능 법안") is None


def test_explicit_exhaustive_scope_wins_over_a_display_count() -> None:
    query = "관련 법안 전건을 빠짐없이 조사하고 대표 사례 5개도 보여줘"

    assert requested_result_count(query) == 5
    assert exhaustive_requested(query)
    assert not focused_result_request(query)


def test_plenary_request_is_not_committee_only() -> None:
    assert not committee_only_request("상임위원회와 본회의 논의를 정리해줘")


def test_incident_single_measure_stage_summary_is_bounded_without_a_count() -> None:
    route = decide_research_route(
        INCIDENT_QUERY,
        committees=["소위원회", "상임위원회", "본회의"],
    )

    assert route.mode is ResearchExecutionMode.BOUNDED
    assert route.reason == "bounded_stage_summary"
    assert route.requested_stages == (
        "subcommittee",
        "standing_committee",
        "plenary",
    )
    assert route.committees == ()
    assert requested_stages(INCIDENT_QUERY) == route.requested_stages


def test_only_positive_exhaustive_or_cross_term_evidence_selects_durable() -> None:
    assert decide_research_route(
        INCIDENT_QUERY + " 관련 회의록을 전건 빠짐없이 조사해줘"
    ).mode is ResearchExecutionMode.DURABLE
    assert decide_research_route(
        "제21대와 제22대의 플랫폼 법안을 비교해줘"
    ).mode is ResearchExecutionMode.DURABLE


def test_generic_stages_are_not_structured_committee_filters() -> None:
    assert sanitize_committee_scope(
        ["소위원회", "Standing Committee", "본회의", "보건복지위원회"]
    ) == ("보건복지위원회",)
