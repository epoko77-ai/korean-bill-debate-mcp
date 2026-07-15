from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from kasm.research.contracts import (
    DEFAULT_EVIDENCE_TYPES,
    EvidenceType,
    ResearchIntent,
)
from kasm.research.planner import ResearchContractPlanner, plan_research

AS_OF = datetime(2026, 7, 13, 9, 30, tzinfo=UTC)


def test_plans_explicit_korean_start_through_current_without_inference() -> None:
    query = (
        "2026년 1월 1일부터 현재까지 법제사법위원회의 2219564 법안과 "
        "회의록을 확인해줘. 12345678과 A2219564B는 의안번호가 아니다."
    )

    plan = plan_research(query, as_of=AS_OF)

    assert plan.contract.query == query
    assert plan.contract.date_from == date(2026, 1, 1)
    assert plan.contract.date_to == date(2026, 7, 13)
    assert plan.contract.bill_numbers == ("2219564",)
    assert plan.contract.committees == ("법제사법위원회",)
    assert plan.contract.evidence_types == DEFAULT_EVIDENCE_TYPES
    assert plan.interpreted_scope.date_interpretation == "explicit_start_to_current"


def test_plans_an_explicit_iso_range_in_query_order() -> None:
    plan = plan_research(
        "2026-01-01부터 2026-07-13까지 보완수사권 법안",
        as_of=AS_OF,
    )

    assert (plan.contract.date_from, plan.contract.date_to) == (
        date(2026, 1, 1),
        date(2026, 7, 13),
    )
    assert plan.interpreted_scope.date_interpretation == "explicit_range"


def test_recent_window_is_configurable_and_calendar_safe() -> None:
    leap_day_plan = ResearchContractPlanner(recent_months=6).plan(
        "최근 AI 입법",
        as_of=datetime(2024, 8, 31, tzinfo=UTC),
    )

    assert leap_day_plan.contract.date_from == date(2024, 2, 29)
    assert leap_day_plan.contract.date_to == date(2024, 8, 31)
    assert leap_day_plan.interpreted_scope.date_interpretation == "recent_default"
    assert leap_day_plan.interpreted_scope.recent_months == 6


def test_current_without_a_start_uses_the_configured_recent_window() -> None:
    plan = plan_research("지금까지 AI 입법", as_of=AS_OF, recent_months=3)

    assert plan.contract.date_from == date(2026, 4, 13)
    assert plan.contract.date_to == date(2026, 7, 13)
    assert plan.interpreted_scope.date_interpretation == "current_with_recent_default"


@pytest.mark.parametrize(
    ("query", "expected_from", "expected_to", "interpretation"),
    (
        ("올해 AI 입법", date(2026, 1, 1), date(2026, 7, 13), "current_year_to_date"),
        (
            "지난 3개월 보완수사권 논의",
            date(2026, 4, 13),
            date(2026, 7, 13),
            "relative_3_months_to_current",
        ),
        (
            "2026년 1월부터 현재까지 AI 입법",
            date(2026, 1, 1),
            date(2026, 7, 13),
            "explicit_month_start_to_current",
        ),
        (
            "지난달 법사위 회의록",
            date(2026, 6, 1),
            date(2026, 6, 30),
            "previous_calendar_month",
        ),
    ),
)
def test_understands_common_korean_natural_language_periods(
    query: str,
    expected_from: date,
    expected_to: date,
    interpretation: str,
) -> None:
    plan = plan_research(query, as_of=AS_OF)

    assert plan.contract.date_from == expected_from
    assert plan.contract.date_to == expected_to
    assert plan.interpreted_scope.date_interpretation == interpretation


@pytest.mark.parametrize(
    ("query", "expected_from", "expected_to", "interpretation"),
    (
        (
            "2026년 1월부터 3월까지 플랫폼 노동 입법",
            date(2026, 1, 1),
            date(2026, 3, 31),
            "explicit_calendar_month_range",
        ),
        (
            "2025년 11월부터 2026년 2월까지 기후 적응 법안",
            date(2025, 11, 1),
            date(2026, 2, 28),
            "explicit_calendar_month_range",
        ),
        (
            "2025년부터 2026년까지 장애인 이동권 논의",
            date(2025, 1, 1),
            date(2026, 12, 31),
            "explicit_calendar_year_range",
        ),
        (
            "2026년 1월 15일부터 3월 2일까지 감염병 회의록",
            date(2026, 1, 15),
            date(2026, 3, 2),
            "explicit_korean_day_range",
        ),
    ),
)
def test_understands_korean_ranges_with_inherited_period_context(
    query: str,
    expected_from: date,
    expected_to: date,
    interpretation: str,
) -> None:
    plan = plan_research(query, as_of=AS_OF)

    assert plan.contract.query == query
    assert plan.contract.date_from == expected_from
    assert plan.contract.date_to == expected_to
    assert plan.interpreted_scope.date_interpretation == interpretation


def test_rejects_reversed_or_invalid_shortened_korean_date_ranges() -> None:
    with pytest.raises(ValueError, match="date_from"):
        plan_research("2026년 3월부터 1월까지 교육 법안", as_of=AS_OF)
    with pytest.raises(ValueError, match="invalid explicit date range"):
        plan_research("2026년 1월 1일부터 2월 30일까지 교육 법안", as_of=AS_OF)


def test_understands_explicit_assembly_term_and_committee_alias() -> None:
    plan = plan_research("제21대 국회 법사위 보완수사 논의", as_of=AS_OF)

    assert plan.contract.assembly_term == 21
    assert plan.contract.assembly_terms == (21,)
    assert plan.contract.committees == ("법제사법위원회",)
    assert plan.interpreted_scope.assembly_term == 21
    assert plan.interpreted_scope.assembly_terms == (21,)
    assert plan.interpreted_scope.assembly_term_explicit
    assert plan.interpreted_scope.to_dict()["assembly_term"] == 21


def test_unconstrained_date_range_records_every_intersecting_assembly_term() -> None:
    plan = plan_research(
        "2020년 5월 30일부터 현재까지 인공지능 입법",
        as_of=AS_OF,
    )

    assert plan.contract.assembly_term == 22
    assert plan.contract.assembly_terms == (21, 22)
    assert plan.interpreted_scope.assembly_terms == (21, 22)
    assert not plan.interpreted_scope.assembly_term_explicit
    assert plan.contract.canonical_payload()["assembly_terms"] == [21, 22]


def test_explicit_assembly_term_is_a_hard_constraint_across_wider_dates() -> None:
    plan = plan_research(
        "제21대 국회에서 2020년부터 2026년까지 인공지능 입법",
        as_of=AS_OF,
    )

    assert plan.contract.assembly_term == 21
    assert plan.contract.assembly_terms == (21,)
    assert plan.interpreted_scope.assembly_term_explicit


def test_structured_scope_overrides_are_preserved_without_rewriting_query() -> None:
    plan = plan_research(
        "최근 AI 입법을 찾아줘",
        as_of=AS_OF,
        assembly_term=21,
        committees=("법제사법위원회",),
        date_from=date(2025, 1, 1),
        date_to=date(2025, 6, 30),
    )

    assert plan.contract.query == "최근 AI 입법을 찾아줘"
    assert plan.contract.assembly_term == 21
    assert plan.contract.committees == ("법제사법위원회",)
    assert plan.contract.date_from == date(2025, 1, 1)
    assert plan.contract.date_to == date(2025, 6, 30)
    assert plan.interpreted_scope.date_interpretation == "structured_override"

    open_ended = plan_research(
        "AI 입법",
        as_of=AS_OF,
        date_from=date(2026, 1, 1),
    )
    assert open_ended.contract.date_to == AS_OF.date()
    assert open_ended.interpreted_scope.date_interpretation == (
        "structured_start_to_current"
    )


def test_query_is_preserved_while_prepare_query_exposes_normalized_retrieval_input() -> None:
    query = "최근 AI 입법"

    plan = plan_research(query, as_of=AS_OF)

    assert plan.contract.query == query
    assert plan.prepared_query.original == query
    assert plan.search_query == f"{query} 인공지능"
    assert plan.interpreted_scope.to_dict()["search_query"] == f"{query} 인공지능"


def test_translated_explicit_english_committee_is_structured() -> None:
    plan = plan_research(
        "Bills reviewed by the Legislation and Judiciary Committee",
        as_of=AS_OF,
    )

    assert plan.contract.committees == ("법제사법위원회",)
    assert plan.interpreted_scope.query_language == "en"
    assert plan.interpreted_scope.translation_mode == "built_in_glossary"


def test_no_date_or_committee_stays_unbounded_and_does_not_infer_a_committee() -> None:
    plan = plan_research("보완수사권 쟁점", as_of=AS_OF)

    assert plan.contract.date_from is None
    assert plan.contract.date_to is None
    assert plan.contract.committees == ()
    assert plan.interpreted_scope.date_interpretation == "unbounded"


def test_explicit_evidence_subset_is_validated_and_exposed() -> None:
    plan = plan_research(
        "2219564 검토",
        as_of=AS_OF,
        evidence_types=("bills", EvidenceType.REVIEW_REPORTS),
    )

    assert plan.contract.evidence_types == (
        EvidenceType.BILLS,
        EvidenceType.REVIEW_REPORTS,
    )
    assert plan.interpreted_scope.to_dict()["evidence_types"] == [
        "bills",
        "review_reports",
    ]


def test_planning_is_deterministic_for_the_same_inputs() -> None:
    planner = ResearchContractPlanner(recent_months=4)

    first = planner.plan("최근 2219564 법안", as_of=AS_OF)
    second = planner.plan("최근 2219564 법안", as_of=AS_OF)

    assert first == second
    assert first.to_dict() == second.to_dict()


def test_rejects_invalid_dates_and_recent_configuration() -> None:
    with pytest.raises(ValueError, match="invalid explicit date"):
        plan_research("2026년 2월 30일 법안", as_of=AS_OF)
    with pytest.raises(ValueError, match="recent_months"):
        ResearchContractPlanner(recent_months=0)


@pytest.mark.parametrize(
    ("query", "expected"),
    (
        ("최근 AI 입법 알려줘", (ResearchIntent.DISCOVER,)),
        (
            "왜 막혔고 누가 반대했어",
            (ResearchIntent.EXPLAIN_ISSUES, ResearchIntent.COMPARE_POSITIONS),
        ),
        ("시계열로 정리해줘", (ResearchIntent.TIMELINE,)),
        ("상태 어디까지 왔어", (ResearchIntent.TRACK_STATUS,)),
        ("정부는 어떻게 답했어", (ResearchIntent.QUOTE_EVIDENCE,)),
    ),
)
def test_extracts_multiple_deterministic_research_intents_without_narrowing_evidence(
    query: str,
    expected: tuple[ResearchIntent, ...],
) -> None:
    plan = plan_research(query, as_of=AS_OF)

    assert plan.contract.intents == expected
    assert plan.interpreted_scope.intents == expected
    assert plan.contract.evidence_types == DEFAULT_EVIDENCE_TYPES
    assert plan.interpreted_scope.evidence_types == DEFAULT_EVIDENCE_TYPES
    assert all(item.matched_phrases for item in plan.interpreted_scope.intent_evidence)


def test_ambiguous_request_defaults_to_discovery_and_preserves_original_scope() -> None:
    query = "2219564 좀 봐줘"

    plan = plan_research(query, as_of=AS_OF)

    assert plan.contract.query == query
    assert plan.contract.bill_numbers == ("2219564",)
    assert plan.contract.intents == (ResearchIntent.DISCOVER,)
    assert plan.interpreted_scope.intent_evidence[0].matched_phrases == (
        "default: no explicit analysis mode",
    )


def test_exact_bill_number_selects_its_own_assembly_term() -> None:
    plan = plan_research("2112345 의안 상태와 회의록", as_of=AS_OF)

    assert plan.contract.bill_numbers == ("2112345",)
    assert plan.contract.assembly_term == 21
    assert plan.contract.assembly_terms == (21,)
    assert plan.interpreted_scope.assembly_terms == (21,)
    assert plan.interpreted_scope.assembly_term_explicit


def test_explicit_assembly_term_cannot_conflict_with_exact_bill_number() -> None:
    with pytest.raises(ValueError, match="bill number.*Assembly term"):
        plan_research("제22대 국회 2112345 의안", as_of=AS_OF)


def test_exact_bill_number_cannot_conflict_with_requested_dates() -> None:
    with pytest.raises(ValueError, match="bill number.*date range"):
        plan_research("2010년 2219564 의안", as_of=AS_OF)

    plan = plan_research(
        "2020년 5월 30일부터 현재까지 2219564 의안",
        as_of=AS_OF,
    )
    assert plan.contract.assembly_terms == (21, 22)


def test_interpreted_scope_serializes_intent_and_its_query_evidence() -> None:
    scope = plan_research("왜 막혔고 누가 반대했어", as_of=AS_OF).interpreted_scope.to_dict()

    assert scope["intents"] == ["explain_issues", "compare_positions"]
    assert scope["intent_evidence"] == [
        {"intent": "explain_issues", "matched_phrases": ["왜", "막혔"]},
        {"intent": "compare_positions", "matched_phrases": ["반대"]},
    ]


@pytest.mark.parametrize(
    ("query", "representatives", "co_proposers", "proposers"),
    (
        ("김남근 의원이 대표발의한 법안", ("김남근",), (), ()),
        ("김남근 대표발의 법안", ("김남근",), (), ()),
        ("김남근이 대표 발의한 법안", ("김남근",), (), ()),
        ("공동발의자 김윤 의원의 인공지능 법안", (), ("김윤",), ()),
        ("김윤 공동발의 법안", (), ("김윤",), ()),
        ("박정 의원이 발의한 법안을 찾아줘", (), (), ("박정",)),
        ("박정이 발의한 법안", (), (), ("박정",)),
    ),
)
def test_planner_extracts_exact_proposer_roles_into_immutable_scope(
    query: str,
    representatives: tuple[str, ...],
    co_proposers: tuple[str, ...],
    proposers: tuple[str, ...],
) -> None:
    plan = plan_research(query, as_of=AS_OF)

    assert plan.contract.representative_proposer_names == representatives
    assert plan.contract.co_proposer_names == co_proposers
    assert plan.contract.proposer_names == proposers
    payload = plan.interpreted_scope.to_dict()
    assert payload["representative_proposer_names"] == list(representatives)
    assert payload["co_proposer_names"] == list(co_proposers)
    assert payload["proposer_names"] == list(proposers)


def test_planner_does_not_guess_a_proposer_from_a_bare_member_mention() -> None:
    plan = plan_research("김남근 의원의 인공지능 관련 발언", as_of=AS_OF)

    assert plan.contract.representative_proposer_names == ()
    assert plan.contract.co_proposer_names == ()
    assert plan.contract.proposer_names == ()
