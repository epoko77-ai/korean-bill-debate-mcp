from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from kasm.research.partitioning import OfficialSourceKind, plan_partitions
from kasm.research.planner import plan_research
from kasm.research.relevance import RelevanceCriteria, evaluate_candidate

AS_OF = datetime(2026, 7, 15, 9, 30, tzinfo=UTC)


@pytest.mark.parametrize("query", ("제헌국회 경제 법안", "제1대 국회 경제 법안"))
def test_constituent_assembly_aliases_select_the_first_term(query: str) -> None:
    plan = plan_research(query, as_of=AS_OF)

    assert plan.contract.assembly_term == 1
    assert plan.contract.assembly_terms == (1,)
    assert plan.interpreted_scope.assembly_term_explicit


@pytest.mark.parametrize(
    "query",
    (
        "제18대부터 제22대까지 인공지능 입법",
        "제18~22대 인공지능 입법",
    ),
)
def test_explicit_assembly_term_ranges_expand_losslessly(query: str) -> None:
    plan = plan_research(query, as_of=AS_OF)

    assert plan.contract.assembly_terms == (18, 19, 20, 21, 22)
    assert plan.contract.assembly_term == 22


def test_explicit_noncontiguous_term_comparison_does_not_invent_scope() -> None:
    plan = plan_research("제18대와 제22대 인공지능 입법 비교", as_of=AS_OF)
    partitions = plan_partitions(plan)

    assert plan.contract.assembly_terms == (18, 22)
    assert tuple(item.assembly_term for item in partitions.term_ranges) == (18, 22)
    assert {
        int(item.partition.parameters_dict()["AGE"])
        for item in partitions.planned_partitions
        if item.source is OfficialSourceKind.BILL_METADATA
    } == {18, 22}


def test_historical_calendar_range_selects_every_intersecting_official_term() -> None:
    plan = plan_research(
        "1961년 5월부터 1964년 1월까지 경제 법안과 회의록",
        as_of=AS_OF,
    )
    partitions = plan_partitions(plan)

    assert plan.contract.date_from == date(1961, 5, 1)
    assert plan.contract.date_to == date(1964, 1, 31)
    assert plan.contract.assembly_terms == (5, 6)
    assert tuple(
        (item.assembly_term, item.date_from, item.date_to)
        for item in partitions.term_ranges
    ) == (
        (5, date(1961, 5, 1), date(1961, 5, 16)),
        (6, date(1963, 12, 17), date(1964, 1, 31)),
    )
    assert {month.value for month in partitions.months} == {
        "1961-05",
        "1963-12",
        "1964-01",
    }


def test_historical_relevance_date_is_not_rejected_as_missing() -> None:
    criteria = RelevanceCriteria.from_query(
        "경제 법안",
        date_from=date(1950, 1, 1),
        date_to=date(1950, 12, 31),
    )

    result = evaluate_candidate(
        {
            "id": "historical",
            "name": "경제안정법안",
            "date": "1950-06-01",
        },
        criteria,
    )

    assert result.relevant
    assert "date_in_range:1950-06-01" in result.match_reasons


def test_reversed_assembly_term_range_fails_closed() -> None:
    with pytest.raises(ValueError, match="chronological order"):
        plan_research("제22대부터 제18대까지 인공지능 입법", as_of=AS_OF)
