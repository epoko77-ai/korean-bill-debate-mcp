from __future__ import annotations

import pytest

from kasm.adapters.korea.bills import BILL_DATASET
from kasm.adapters.korea.sources import DATASET_BY_SOURCE, MeetingSource
from kasm.research.collector import (
    CollectionCoverage,
    MetadataCollection,
    MetadataKind,
    PartitionProvenance,
)
from kasm.research.source_availability import (
    SourceAvailabilityState,
    summarize_source_availability,
)


def _partition(
    partition_id: str,
    kind: MetadataKind,
    dataset: str,
    *,
    parameters: dict[str, str | int],
    expected: int,
    fetched: int | None = None,
) -> PartitionProvenance:
    return PartitionProvenance(
        partition_id=partition_id,
        kind=kind,
        dataset=dataset,
        parameters=tuple(sorted(parameters.items())),
        expected_rows=expected,
        fetched_rows=expected if fetched is None else fetched,
        result_hash="a" * 64,
        pages=(),
    )


def _collection(*partitions: PartitionProvenance) -> MetadataCollection:
    expected = sum(item.expected_rows for item in partitions)
    fetched = sum(item.fetched_rows for item in partitions)
    complete = sum(item.complete for item in partitions)
    return MetadataCollection(
        (),
        (),
        partitions,
        CollectionCoverage(
            partitions_expected=len(partitions),
            partitions_complete=complete,
            source_rows_expected=expected,
            source_rows_fetched=fetched,
            bill_source_rows=0,
            bill_unique_records=0,
            bill_duplicate_rows=0,
            bill_rejected_rows=0,
            meeting_source_rows=0,
            meeting_unique_pdfs=0,
            meeting_rows_merged=0,
            meeting_rejected_rows=0,
        ),
    )


def test_successful_zero_is_visible_per_source_and_assembly_term() -> None:
    collection = _collection(
        _partition(
            "bill-term-1",
            MetadataKind.BILL,
            BILL_DATASET,
            parameters={"AGE": 1},
            expected=0,
        )
    )

    availability = summarize_source_availability(collection)

    assert len(availability) == 1
    result = availability[0]
    assert result.source == "bill_metadata"
    assert result.assembly_term == 1
    assert result.state is SourceAvailabilityState.NO_RECORDS
    assert result.complete is True
    assert result.no_records is True
    assert result.to_dict()["message_ko"] == (
        "제1대 bill_metadata: 해당 열린국회 데이터셋에서 확인된 자료 없음(0건)."
    )
    assert "No records found in this Open Assembly dataset" in str(result.to_dict()["message_en"])


def test_month_partitions_are_bounded_into_one_source_term_summary() -> None:
    dataset = DATASET_BY_SOURCE[MeetingSource.COMMITTEE]
    collection = _collection(
        _partition(
            "committee-18-2010-01",
            MetadataKind.MEETING,
            dataset,
            parameters={"DAE_NUM": 18, "CONF_DATE": "2010-01"},
            expected=0,
        ),
        _partition(
            "committee-18-2010-02",
            MetadataKind.MEETING,
            dataset,
            parameters={"DAE_NUM": 18, "CONF_DATE": "2010-02"},
            expected=3,
        ),
    )

    result = summarize_source_availability(collection)[0]

    assert result.source == "committee_minutes"
    assert result.assembly_term == 18
    assert result.partitions_expected == 2
    assert result.partitions_complete == 2
    assert result.source_rows_expected == 3
    assert result.source_rows_fetched == 3
    assert result.state is SourceAvailabilityState.RECORDS_FOUND


def test_incomplete_partition_can_never_be_reported_as_no_records() -> None:
    dataset = DATASET_BY_SOURCE[MeetingSource.PLENARY]
    collection = _collection(
        _partition(
            "plenary-22-preview",
            MetadataKind.MEETING,
            dataset,
            parameters={"DAE_NUM": 22, "CONF_DATE": "2026-07"},
            expected=10,
            fetched=2,
        )
    )

    result = summarize_source_availability(collection)[0]

    assert result.state is SourceAvailabilityState.INCOMPLETE
    assert result.complete is False
    assert result.no_records is False
    assert "자료 없음으로 판단할 수 없습니다" in str(result.to_dict()["message_ko"])


def test_subcommittee_eraco_parameter_binds_the_term() -> None:
    dataset = DATASET_BY_SOURCE[MeetingSource.SUBCOMMITTEE]
    result = summarize_source_availability(
        _collection(
            _partition(
                "subcommittee-15",
                MetadataKind.MEETING,
                dataset,
                parameters={"ERACO": "제15대"},
                expected=0,
            )
        )
    )[0]

    assert result.source == "subcommittee_minutes"
    assert result.assembly_term == 15
    assert result.state is SourceAvailabilityState.NO_RECORDS
    assert "소위원회 논의 자체가 없었다는 뜻은 아닙니다" in str(result.to_dict()["message_ko"])


def test_conflicting_term_parameters_fail_closed() -> None:
    collection = _collection(
        _partition(
            "conflict",
            MetadataKind.BILL,
            BILL_DATASET,
            parameters={"AGE": 18, "BILL_NO": "2212345"},
            expected=0,
        )
    )

    with pytest.raises(ValueError, match="conflicting Assembly terms"):
        summarize_source_availability(collection)
