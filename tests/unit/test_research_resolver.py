from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

import kasm.research.resolver as resolver_module
from kasm.research.collector import CollectionCoverage, MetadataCollection
from kasm.research.planner import plan_research
from kasm.research.resolver import (
    ExactBillNotFoundError,
    resolve_metadata_candidates,
)

AS_OF = datetime(2026, 7, 13, 12, tzinfo=UTC)


def collection(
    *,
    bills: list[dict[str, Any]] | None = None,
    meetings: list[dict[str, Any]] | None = None,
) -> MetadataCollection:
    bill_rows = bills or []
    meeting_rows = meetings or []
    coverage = CollectionCoverage(
        partitions_expected=0,
        partitions_complete=0,
        source_rows_expected=len(bill_rows) + len(meeting_rows),
        source_rows_fetched=len(bill_rows) + len(meeting_rows),
        bill_source_rows=len(bill_rows),
        bill_unique_records=len(bill_rows),
        bill_duplicate_rows=0,
        bill_rejected_rows=0,
        meeting_source_rows=len(meeting_rows),
        meeting_unique_pdfs=len(meeting_rows),
        meeting_rows_merged=0,
        meeting_rejected_rows=0,
    )
    return MetadataCollection(
        bills=tuple(bill_rows),
        meetings=tuple(meeting_rows),
        partitions=(),
        coverage=coverage,
    )


def test_explicit_missing_bill_number_fails_closed_before_substitution() -> None:
    plan = plan_research("2219564번 의안 원문과 회의록", as_of=AS_OF)
    metadata = collection(
        bills=[
            {
                "BILL_NO": "2219565",
                "BILL_NAME": "형사소송법 일부개정법률안",
            }
        ]
    )

    with pytest.raises(ExactBillNotFoundError) as raised:
        resolve_metadata_candidates(plan, metadata)

    assert raised.value.missing_bill_numbers == ("2219564",)
    assert "2219564" in str(raised.value)


def test_resolves_all_agendas_and_rejects_maritime_false_positive() -> None:
    plan = plan_research("보완수사권 관련 법안과 회의록", as_of=AS_OF)
    metadata = collection(
        bills=[
            {
                "BILL_NO": "2219564",
                "BILL_NAME": "형사소송법 일부개정법률안",
                "summary": "검사의 보완수사권을 정비한다.",
            },
            {
                "BILL_NO": "2217000",
                "BILL_NAME": "해양사고의 조사 및 심판에 관한 법률 일부개정법률안",
                "summary": "해양사고 조사 권한을 보완한다.",
            },
        ],
        meetings=[
            {
                "PDF_LINK_URL": "https://record.assembly.go.kr/related.pdf",
                "SUB_NAME": "해양사고 조사 제도 정비",
                "agenda_items": [
                    {"bill_no": "2217000", "title": "해양사고 조사법 개정안"},
                    {
                        "bill_no": "2219564",
                        "title": "형사소송법상 보완수사요구권 정비",
                    },
                ],
                "agenda_text": (
                    "2217000 해양사고 조사법 개정안\n"
                    "2219564 형사소송법상 보완수사요구권 정비"
                ),
            },
            {
                "PDF_LINK_URL": "https://record.assembly.go.kr/exact.pdf",
                "agenda_items": [
                    {"bill_no": "2219564", "title": "보완수사권 정비 안건"}
                ],
                "agenda_text": "2219564 보완수사권 정비 안건",
            },
        ],
    )

    resolved = resolve_metadata_candidates(plan, metadata)

    assert resolved.bills.total_candidates == 2
    assert resolved.bills.accepted_count == 1
    assert resolved.bills.rejected_count == 1
    assert resolved.bills.rejection_reason_counts == (("below_minimum_score", 1),)
    assert [item.candidate_id for item in resolved.bills.accepted] == ["bill:2219564"]
    maritime = next(
        item for item in resolved.bills.decisions if item.candidate_id == "bill:2217000"
    )
    assert maritime.accepted is False
    assert maritime.score == 0

    assert resolved.meetings.total_candidates == 2
    assert resolved.meetings.accepted_count == 2
    assert [item.candidate_id for item in resolved.meetings.accepted] == [
        "meeting:https://record.assembly.go.kr/exact.pdf",
        "meeting:https://record.assembly.go.kr/related.pdf",
    ]
    related = resolved.meetings.accepted[1]
    assert related.match_reasons == (
        "related_statute:형사소송법@agenda",
        "related_issue:보완수사요구권@agenda",
    )
    assert related.score < resolved.meetings.accepted[0].score
    assert "related_concept:보완수사권→보완수사요구권" in (
        resolved.criteria.expansion_reasons
    )


def test_recent_ai_resolves_every_relevant_candidate_without_top_n_truncation() -> None:
    plan = plan_research("최근 AI 입법", as_of=AS_OF)
    relevant = [
        {
            "BILL_NO": f"{2200000 + number:07d}",
            "BILL_NAME": f"인공지능 산업 진흥 제{number}호 법안",
            "PROPOSE_DT": "2026-06-01",
        }
        for number in range(100)
    ]
    irrelevant = [
        {
            "BILL_NO": f"{2210000 + number:07d}",
            "BILL_NAME": f"해양사고 조사 제{number}호 법안",
            "PROPOSE_DT": "2026-06-01",
        }
        for number in range(3)
    ]

    forward = resolve_metadata_candidates(
        plan, collection(bills=[*irrelevant, *relevant])
    )
    reverse = resolve_metadata_candidates(
        plan, collection(bills=list(reversed([*irrelevant, *relevant])))
    )

    assert forward.bills.total_candidates == 103
    assert forward.bills.accepted_count == 100
    assert forward.bills.rejected_count == 3
    assert len(forward.bills.accepted) == 100
    assert [item.candidate_id for item in forward.bills.accepted] == [
        item.candidate_id for item in reverse.bills.accepted
    ]
    assert forward.bills.to_dict() == reverse.bills.to_dict()
    assert forward.bills.rejection_reason_counts == (("below_minimum_score", 3),)


def test_resolver_evaluates_each_candidate_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = plan_research("최근 AI 입법", as_of=AS_OF)
    metadata = collection(
        bills=[
            {
                "BILL_NO": f"{2210000 + number:07d}",
                "BILL_NAME": (
                    "인공지능 안전법안" if number == 1 else "민법 일부개정법률안"
                ),
            }
            for number in range(1, 4)
        ]
    )
    original = resolver_module.evaluate_candidate
    calls = 0

    def counted(candidate, criteria):
        nonlocal calls
        calls += 1
        return original(candidate, criteria)

    monkeypatch.setattr(resolver_module, "evaluate_candidate", counted)

    resolved = resolve_metadata_candidates(plan, metadata)

    assert resolved.bills.total_candidates == 3
    assert calls == 3


def test_threshold_rejections_preserve_partial_match_reason_and_score() -> None:
    plan = plan_research("보완수사권 관련 법안", as_of=AS_OF)
    metadata = collection(
        bills=[
            {
                "BILL_NO": "2218000",
                "BILL_NAME": "수사제도 일부개정법률안",
                # A related term in body text scores 5, below the default threshold 10.
                "summary": "보완수사요구권을 정비한다.",
            }
        ]
    )

    resolved = resolve_metadata_candidates(plan, metadata)
    decision = resolved.bills.decisions[0]

    assert decision.accepted is False
    assert decision.score == 5
    assert decision.match_reasons == ("related_issue:보완수사요구권@body",)
    assert decision.rejection_reasons == ("below_minimum_score",)
    assert resolved.bills.rejection_reason_counts == (("below_minimum_score", 1),)


def test_resolution_payload_preserves_every_decision_and_accounting() -> None:
    plan = plan_research("최근 AI 입법", as_of=AS_OF)
    metadata = collection(
        bills=[
            {
                "BILL_NO": "2210001",
                "BILL_NAME": "인공지능 안전법안",
                "PROPOSE_DT": "2026-07-01",
            },
            {
                "BILL_NO": "2210002",
                "BILL_NAME": "민법 일부개정법률안",
                "PROPOSE_DT": "2026-07-01",
            },
        ]
    )

    payload = resolve_metadata_candidates(plan, metadata).to_dict()

    assert payload["bills"]["total_candidates"] == 2
    assert payload["bills"]["accepted_count"] == 1
    assert payload["bills"]["rejected_count"] == 1
    assert len(payload["bills"]["decisions"]) == 2
    assert payload["bills"]["accepted_candidate_ids"] == ["bill:2210001"]
    assert payload["criteria"]["minimum_score"] == 10
    assert payload["criteria"]["expansion_reasons"]


def test_recent_meeting_uses_official_conf_date_without_mutating_metadata() -> None:
    plan = plan_research("최근 AI 입법 회의록", as_of=AS_OF)
    meeting = {
        "PDF_LINK_URL": "https://record.assembly.go.kr/ai.pdf",
        "CONF_DATE": "2026-06-02",
        "agenda_items": [{"bill_no": "2219000", "title": "인공지능 안전법안"}],
        "agenda_text": "2219000 인공지능 안전법안",
    }

    resolved = resolve_metadata_candidates(plan, collection(meetings=[meeting]))

    assert resolved.meetings.accepted_count == 1
    assert resolved.meetings.accepted[0].match_reasons == (
        "date_in_range:2026-06-02",
        "issue:인공지능@agenda",
    )
    assert "date" not in resolved.meetings.accepted[0].candidate
