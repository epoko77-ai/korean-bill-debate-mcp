from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from kasm.adapters.korea.bills import BILL_DATASET, BILL_STATUS_DATASET
from kasm.adapters.korea.sources import DATASET_BY_SOURCE, MeetingSource
from kasm.research.contracts import EvidenceType
from kasm.research.partitioning import (
    OfficialSourceKind,
    SearchTermRole,
    plan_partitions,
)
from kasm.research.planner import plan_research

AS_OF = datetime(2026, 7, 13, 9, 30, tzinfo=UTC)


def _plan(query: str, **kwargs):
    return plan_partitions(plan_research(query, as_of=AS_OF, **kwargs), page_size=100)


def test_exact_bill_number_is_separate_from_terms_and_all_sources_are_covered() -> None:
    plan = _plan(
        "2026년 1월 1일부터 현재까지 2219564 보완수사권 법안·회의록·검토보고서"
    )

    assert plan.exact_bill_numbers == ("2219564",)
    assert {term.value for term in plan.search_terms} >= {
        "보완수사권",
        "보완수사요구권",
        "형사소송법",
    }
    assert all(term.value != "2219564" for term in plan.search_terms)
    assert [month.value for month in plan.months] == [
        "2026-01",
        "2026-02",
        "2026-03",
        "2026-04",
        "2026-05",
        "2026-06",
        "2026-07",
    ]

    kinds = {source.kind for source in plan.official_sources}
    assert kinds == set(OfficialSourceKind)
    bill_partitions = [
        item
        for item in plan.planned_partitions
        if item.partition.kind.value == "bill"
    ]
    assert len(bill_partitions) == 2
    assert {item.partition.dataset for item in bill_partitions} == {
        BILL_DATASET,
        BILL_STATUS_DATASET,
    }
    assert all(
        item.partition.parameters_dict() == {"AGE": 22, "BILL_NO": "2219564"}
        for item in bill_partitions
    )
    assert all(item.search_term is None for item in bill_partitions)

    # Seven plenary months + seven committee months + one full-term
    # subcommittee endpoint, plus the two exact bill partitions.
    assert plan.coverage.metadata_partition_count == 17
    assert plan.coverage.meeting_partition_count == 15
    assert plan.coverage.bill_metadata_partition_count == 1
    assert plan.coverage.bill_status_partition_count == 1
    assert plan.coverage.exact_review_bill_count == 1
    assert plan.coverage.deferred_requirement_count == 2
    assert not plan.coverage.dynamic_status_count
    assert not plan.coverage.dynamic_review_count

    review_index = next(
        item
        for item in plan.deferred_requirements
        if item.source is OfficialSourceKind.REVIEW_REPORT_INDEX
    )
    assert review_index.expected_count == 1
    assert "BILL_ID" in review_index.required_fields[1]
    assert len(plan.metadata_partitions) == len(
        {partition.partition_id for partition in plan.metadata_partitions}
    )


def test_topic_search_collects_full_term_then_defers_status_to_exact_candidates() -> None:
    plan = _plan("최근 AI 입법")

    terms = {(term.value, term.role) for term in plan.search_terms}
    assert ("인공지능", SearchTermRole.OFFICIAL_EQUIVALENT) in terms
    assert ("인공지능 기본법", SearchTermRole.OFFICIAL_RELATED) in terms
    bill_partitions = [
        item
        for item in plan.planned_partitions
        if item.source is OfficialSourceKind.BILL_METADATA
    ]
    assert len(bill_partitions) == 1
    assert bill_partitions[0].partition.parameters_dict() == {"AGE": 22}
    assert bill_partitions[0].search_term is None
    assert not any(
        item.source is OfficialSourceKind.BILL_STATUS
        for item in plan.planned_partitions
    )
    assert not any(
        item.partition.dataset == BILL_STATUS_DATASET
        and "BILL_NAME" in item.partition.parameters_dict()
        for item in plan.planned_partitions
    )
    status = next(
        item
        for item in plan.deferred_requirements
        if item.source is OfficialSourceKind.BILL_STATUS
    )
    assert status.expected_count is None
    assert status.required_fields == ("BILL_NO",)
    assert plan.coverage.dynamic_status_count
    assert plan.coverage.dynamic_review_count
    assert plan.coverage.bill_metadata_partition_count == 1


def test_bill_text_only_scope_still_collects_bill_metadata_for_every_term() -> None:
    plan = _plan(
        "2020년 5월 30일부터 현재까지 인공지능 법안 원문",
        evidence_types=(EvidenceType.BILL_TEXT,),
    )

    bill_partitions = [
        item
        for item in plan.planned_partitions
        if item.source is OfficialSourceKind.BILL_METADATA
    ]
    assert {
        item.partition.parameters_dict()["AGE"] for item in bill_partitions
    } == {21, 22}
    assert plan.coverage.bill_metadata_partition_count == 2
    assert not any(
        item.partition.kind.value == "meeting" for item in plan.planned_partitions
    )


def test_calendar_month_partitions_have_no_boundary_gap_or_overlap() -> None:
    research = plan_research(
        "2024-01-31부터 2024-03-01까지 인공지능 법안과 회의록",
        as_of=datetime(2024, 3, 1, tzinfo=UTC),
        assembly_term=21,
    )
    plan = plan_partitions(research)

    assert [(item.value, item.date_from, item.date_to) for item in plan.months] == [
        ("2024-01", date(2024, 1, 31), date(2024, 1, 31)),
        ("2024-02", date(2024, 2, 1), date(2024, 2, 29)),
        ("2024-03", date(2024, 3, 1), date(2024, 3, 1)),
    ]
    for left, right in zip(plan.months, plan.months[1:], strict=False):
        assert left.date_to + timedelta(days=1) == right.date_from


def test_unbounded_scope_is_the_full_configured_assembly_term_through_as_of() -> None:
    plan = _plan("보완수사권 쟁점")

    assert plan.requested_date_from is None
    assert plan.requested_date_to is None
    assert plan.effective_date_from == date(2024, 5, 30)
    assert plan.effective_date_to == date(2026, 7, 13)
    assert plan.range_policy == "current_assembly_term_to_as_of"
    assert plan.range_adjustments == ()
    assert plan.months[0].date_from == date(2024, 5, 30)
    assert plan.months[-1].date_to == date(2026, 7, 13)
    assert plan.coverage.month_bucket_count == 27
    assert plan.coverage.scope_fully_represented


def test_unbounded_past_term_uses_the_complete_term_not_a_recent_window() -> None:
    research = plan_research("인공지능 입법", as_of=AS_OF, assembly_term=21)

    plan = plan_partitions(research)

    assert plan.effective_date_from == date(2020, 5, 30)
    assert plan.effective_date_to == date(2024, 5, 29)
    assert plan.range_policy == "complete_configured_assembly_term"


def test_unconstrained_range_crosses_terms_and_only_outer_as_of_is_clipped() -> None:
    plan = _plan("2024-01-01부터 2029-01-01까지 인공지능 입법")

    assert plan.assembly_terms == (21, 22)
    assert plan.effective_date_from == date(2024, 1, 1)
    assert plan.effective_date_to == date(2026, 7, 13)
    assert plan.range_adjustments == ("date_to_clipped_to_as_of",)
    assert not plan.coverage.scope_fully_represented


def test_cross_term_scope_creates_complete_term_specific_bill_and_meeting_partitions() -> None:
    plan = _plan(
        "2020년 5월 30일부터 현재까지 인공지능 법안과 회의록",
        evidence_types=(EvidenceType.BILLS, EvidenceType.AGENDAS),
    )

    assert plan.assembly_terms == (21, 22)
    assert [
        (item.assembly_term, item.date_from, item.date_to)
        for item in plan.term_ranges
    ] == [
        (21, date(2020, 5, 30), date(2024, 5, 29)),
        (22, date(2024, 5, 30), date(2026, 7, 13)),
    ]
    bill_parameters = {
        tuple(sorted(item.partition.parameters_dict().items()))
        for item in plan.planned_partitions
        if item.source is OfficialSourceKind.BILL_METADATA
    }
    assert bill_parameters == {(('AGE', 21),), (('AGE', 22),)}
    meeting_terms = {
        int(item.partition.parameters_dict().get("DAE_NUM", 0))
        for item in plan.planned_partitions
        if item.source
        in {OfficialSourceKind.PLENARY_MINUTES, OfficialSourceKind.COMMITTEE_MINUTES}
    }
    assert meeting_terms == {21, 22}
    subcommittee_terms = {
        item.partition.parameters_dict()["ERACO"]
        for item in plan.planned_partitions
        if item.source is OfficialSourceKind.SUBCOMMITTEE_MINUTES
    }
    assert subcommittee_terms == {"제21대", "제22대"}
    assert plan.range_adjustments == ()
    assert plan.coverage.assembly_term_count == 2
    assert plan.coverage.scope_fully_represented


def test_explicit_term_preserves_single_term_and_reports_both_outer_clips() -> None:
    plan = _plan(
        "제21대 국회에서 2020년 1월부터 2026년 12월까지 인공지능 입법"
    )

    assert plan.assembly_terms == (21,)
    assert plan.effective_date_from == date(2020, 5, 30)
    assert plan.effective_date_to == date(2024, 5, 29)
    assert plan.range_adjustments == (
        "date_from_clipped_to_assembly_term_start",
        "date_to_clipped_to_assembly_term_end",
    )


def test_committee_scope_partitions_each_committee_without_duplicating_plenary() -> None:
    plan = _plan(
        "2026-07-01부터 2026-07-13까지 법제사법위원회와 "
        "정무위원회의 인공지능 회의록"
    )

    committee = [
        item
        for item in plan.planned_partitions
        if item.source is OfficialSourceKind.COMMITTEE_MINUTES
    ]
    plenary = [
        item
        for item in plan.planned_partitions
        if item.source is OfficialSourceKind.PLENARY_MINUTES
    ]
    subcommittee = [
        item
        for item in plan.planned_partitions
        if item.source is OfficialSourceKind.SUBCOMMITTEE_MINUTES
    ]
    assert {
        item.partition.parameters_dict()["COMM_NAME"] for item in committee
    } == {"법제사법위원회", "정무위원회"}
    assert len(plenary) == 1
    assert len(subcommittee) == 1
    assert "CMIT_NM" not in subcommittee[0].partition.parameters_dict()
    assert subcommittee[0].local_committees == ("법제사법위원회", "정무위원회")
    assert plan.committees == ("법제사법위원회", "정무위원회")


def test_search_term_and_partition_deduplication_is_deterministic() -> None:
    first = _plan("최근 AI 인공지능 AI 법안")
    second = _plan("최근 AI 인공지능 AI 법안")

    assert first == second
    assert [term.value for term in first.search_terms].count("인공지능") == 1
    signatures = [
        (
            item.source,
            item.partition.dataset,
            item.partition.parameters,
        )
        for item in first.planned_partitions
    ]
    assert len(signatures) == len(set(signatures))
    assert first.to_dict() == second.to_dict()


def test_maximum_length_contract_preserves_more_than_32_terms_without_failure() -> None:
    query = (
        "2026년 1월 1일부터 현재까지 인공지능, 딥페이크, 플랫폼노동, 장애인이동권, "
        "기후적응, 에너지전환, 감염병병상, 중소기업수출금융, 양육비이행, 전세사기, "
        "산업재해, 개인정보보호, 온라인안전, 알고리즘투명성, 반도체공급망, 우주산업, "
        "해양안전, 산불대응, 재난문자, 고령자돌봄, 청년주거, 지역소멸, 농업재해, "
        "수산자원, 문화유산, 디지털교과서, 학교폭력, 아동학대, 스토킹범죄, 가정폭력, "
        "마약수사, 범죄피해자, 소상공인부채, 공정거래, 기업지배구조, 노동시간, 임금체불, "
        "외국인근로자, 저출생대책, 연금개혁, 보건의료, 필수의료, 간호인력, 바이오안전, "
        "원전안전, 전력망확충, 대중교통, 교통약자, 동물복지, 재생에너지와 관련된 "
        "법안의 원문과 처리 상태, 소위원회 회의록, 전문위원 검토보고서, 의원별 발언, "
        "정부 답변을 모두 조사해줘. 표현이 다른 유사 법안도 누락하지 말고 공식 기록을 "
        "시간순으로 연결해줘. 각 자료의 공식 링크와 확인 건수, "
        "미확인 자료 및 사유도 분명하게 알려줘."
    )
    assert len(query) == 500
    research = plan_research(query, as_of=AS_OF)

    first = plan_partitions(research, page_size=100, max_search_terms=7)
    second = plan_partitions(research, page_size=100, max_search_terms=7)

    assert first == second
    assert len(first.search_terms) > 32
    assert all(1 <= len(batch) <= 7 for batch in first.search_term_batches)
    assert tuple(
        term for batch in first.search_term_batches for term in batch
    ) == first.search_terms
    bill_partitions = [
        item
        for item in first.planned_partitions
        if item.source is OfficialSourceKind.BILL_METADATA
    ]
    assert len(bill_partitions) == 1
    assert bill_partitions[0].partition.parameters_dict() == {"AGE": 22}
    assert bill_partitions[0].search_term is None
    assert first.coverage.official_search_term_count == len(first.search_terms)
    assert first.coverage.official_search_term_batch_size == 7
    assert first.coverage.official_search_term_batch_count == len(
        first.search_term_batches
    )
    assert first.coverage.official_search_terms_preserved
    assert first.to_dict()["coverage"]["official_search_terms_preserved"] is True


def test_output_words_and_particle_variants_do_not_create_bill_partitions() -> None:
    plan = _plan("최근 인공지능 법안과 회의록을 보여줘")

    assert {term.value for term in plan.search_terms} == {
        "인공지능",
        "인공지능 기본법",
    }


def test_broad_research_instruction_keeps_only_subject_search_terms() -> None:
    plan = _plan(
        "2026년 7월 인공지능 관련 법안과 위원회 논의를 "
        "공식 원문 기준으로 조사해줘"
    )

    assert {term.value for term in plan.search_terms} == {
        "인공지능",
        "인공지능 기본법",
    }


@pytest.mark.parametrize(
    ("query", "expected_literals"),
    (
        ("딥페이크 관련 법안", {"딥페이크"}),
        ("플랫폼 노동 관련 법안", {"플랫폼", "노동"}),
        ("장애인 이동권 입법", {"장애인", "이동권"}),
        ("기후 적응과 에너지 전환 법안", {"기후", "적응", "에너지", "전환"}),
        ("감염병 병상 관련 법안", {"감염병", "병상"}),
        ("중소기업 수출금융 법안", {"중소기업", "수출금융"}),
    ),
)
def test_any_korean_policy_topic_uses_one_complete_term_bill_universe(
    query: str,
    expected_literals: set[str],
) -> None:
    plan = _plan(query)

    literal_terms = {
        term.value
        for term in plan.search_terms
        if term.role is SearchTermRole.USER_LITERAL
    }
    bill_partitions = [
        item
        for item in plan.planned_partitions
        if item.source is OfficialSourceKind.BILL_METADATA
    ]

    assert expected_literals.issubset(literal_terms)
    assert len(bill_partitions) == 1
    assert bill_partitions[0].partition.parameters_dict() == {"AGE": 22}
    assert plan.coverage.bill_metadata_partition_count == 1


def test_evidence_subset_limits_sources_and_static_expected_count() -> None:
    plan = _plan("최근 인공지능 법안", evidence_types=(EvidenceType.BILLS,))

    assert {source.kind for source in plan.official_sources} == {
        OfficialSourceKind.BILL_METADATA
    }
    assert plan.coverage.metadata_partition_count == 1
    assert plan.coverage.meeting_partition_count == 0
    assert plan.deferred_requirements == ()


def test_unknown_english_topic_fails_closed_instead_of_issuing_empty_queries() -> None:
    research = plan_research("What happened to Project Aster?", as_of=AS_OF)

    with pytest.raises(ValueError, match="Korean official-source search term"):
        plan_partitions(research)


def test_unknown_assembly_term_requires_explicit_bounds() -> None:
    research = plan_research(
        "인공지능 입법",
        as_of=AS_OF,
        assembly_term=23,
    )

    with pytest.raises(ValueError, match="requires explicit date bounds"):
        plan_partitions(research)


def test_subcommittee_only_evidence_uses_both_official_subcommittee_sources() -> None:
    plan = _plan(
        "최근 인공지능 소위원회 회의록",
        evidence_types=(EvidenceType.SUBCOMMITTEE_MINUTES,),
    )

    assert {source.kind for source in plan.official_sources} == {
        OfficialSourceKind.COMMITTEE_MINUTES,
        OfficialSourceKind.SUBCOMMITTEE_MINUTES,
    }
    assert not any(
        item.source is OfficialSourceKind.PLENARY_MINUTES
        for item in plan.planned_partitions
    )
    assert all(
        item.partition.dataset
        in {
            DATASET_BY_SOURCE[MeetingSource.COMMITTEE],
            DATASET_BY_SOURCE[MeetingSource.SUBCOMMITTEE],
        }
        for item in plan.planned_partitions
    )
