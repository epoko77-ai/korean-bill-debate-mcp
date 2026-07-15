"""Bounded, user-facing accounting for official metadata sources.

The collector already distinguishes a successful empty Open Assembly response
from an API failure.  This module turns its immutable partition provenance into
an explicit source/Assembly-term status without looking at relevance-filtered
candidates.  Consequently, ``no_records`` means that every planned partition
for that source scope completed successfully and the official API reported
zero source rows; it can never be manufactured from an error or a local
filtering decision.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from kasm.adapters.korea.bills import BILL_DATASET, BILL_STATUS_DATASET
from kasm.adapters.korea.sources import DATASET_BY_SOURCE, MeetingSource

from .collector import MetadataCollection, MetadataKind, PartitionProvenance


class SourceAvailabilityState(StrEnum):
    """Completion state for one official dataset and Assembly term."""

    RECORDS_FOUND = "records_found"
    NO_RECORDS = "no_records"
    INCOMPLETE = "incomplete"


_SOURCE_BY_DATASET: Final = {
    BILL_DATASET: "bill_metadata",
    BILL_STATUS_DATASET: "bill_status",
    DATASET_BY_SOURCE[MeetingSource.PLENARY]: "plenary_minutes",
    DATASET_BY_SOURCE[MeetingSource.COMMITTEE]: "committee_minutes",
    DATASET_BY_SOURCE[MeetingSource.SUBCOMMITTEE]: "subcommittee_minutes",
}


@dataclass(frozen=True, slots=True)
class OfficialSourceAvailability:
    """Aggregate raw-row accounting for one dataset/Assembly-term scope."""

    source: str
    dataset: str
    kind: MetadataKind
    assembly_term: int | None
    partitions_expected: int
    partitions_complete: int
    source_rows_expected: int
    source_rows_fetched: int
    state: SourceAvailabilityState

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.dataset.strip():
            raise ValueError("official source availability identity is required")
        if self.assembly_term is not None and self.assembly_term < 1:
            raise ValueError("official source Assembly term must be positive")
        counts = (
            self.partitions_expected,
            self.partitions_complete,
            self.source_rows_expected,
            self.source_rows_fetched,
        )
        if any(value < 0 for value in counts):
            raise ValueError("official source availability counts must not be negative")
        if self.partitions_expected < 1:
            raise ValueError("official source availability requires a planned partition")
        if not 0 <= self.partitions_complete <= self.partitions_expected:
            raise ValueError("completed source partitions exceed the planned count")
        complete = self.complete
        if self.state is SourceAvailabilityState.INCOMPLETE:
            if complete:
                raise ValueError("an incomplete source availability cannot be complete")
        elif not complete:
            raise ValueError("a terminal source availability must be complete")
        elif self.state is SourceAvailabilityState.NO_RECORDS:
            if self.source_rows_expected != 0:
                raise ValueError("no_records requires a successful zero-row source result")
        elif self.source_rows_expected == 0:
            raise ValueError("records_found requires at least one official source row")

    @property
    def complete(self) -> bool:
        return bool(
            self.partitions_complete == self.partitions_expected
            and self.source_rows_fetched == self.source_rows_expected
        )

    @property
    def no_records(self) -> bool:
        return self.state is SourceAvailabilityState.NO_RECORDS

    def to_dict(self) -> dict[str, object]:
        scope_ko = f"제{self.assembly_term}대" if self.assembly_term is not None else "지정 범위"
        scope_en = (
            f"Assembly term {self.assembly_term}"
            if self.assembly_term is not None
            else "the requested scope"
        )
        if self.state is SourceAvailabilityState.NO_RECORDS:
            message_ko = (
                f"{scope_ko} {self.source}: 해당 열린국회 데이터셋에서 확인된 자료 없음(0건)."
            )
            message_en = (
                f"{scope_en} {self.source}: No records found in this Open Assembly dataset (0)."
            )
            if self.source == "subcommittee_minutes":
                message_ko += (
                    " 다른 회의록 데이터셋에 소위원회 논의가 포함될 수 있으므로, "
                    "소위원회 논의 자체가 없었다는 뜻은 아닙니다."
                )
                message_en += (
                    " Subcommittee discussion may still appear in another minutes dataset; "
                    "this does not prove that no subcommittee discussion occurred."
                )
        elif self.state is SourceAvailabilityState.RECORDS_FOUND:
            message_ko = (
                f"{scope_ko} {self.source}: 공식 API 조회 완료, "
                f"원자료 {self.source_rows_fetched}건 확인."
            )
            message_en = (
                f"{scope_en} {self.source}: official API query complete; "
                f"{self.source_rows_fetched} source records found."
            )
        else:
            message_ko = (
                f"{scope_ko} {self.source}: 공식 API 조회가 완료되지 않아 "
                "자료 없음으로 판단할 수 없습니다."
            )
            message_en = (
                f"{scope_en} {self.source}: the official API query is incomplete, so absence "
                "of records cannot be concluded."
            )
        return {
            "source": self.source,
            "dataset": self.dataset,
            "kind": self.kind.value,
            "assembly_term": self.assembly_term,
            "partitions_expected": self.partitions_expected,
            "partitions_complete": self.partitions_complete,
            "source_rows_expected": self.source_rows_expected,
            "source_rows_fetched": self.source_rows_fetched,
            "state": self.state.value,
            "complete": self.complete,
            "no_records": self.no_records,
            "message_ko": message_ko,
            "message_en": message_en,
        }


def summarize_source_availability(
    collection: MetadataCollection,
) -> tuple[OfficialSourceAvailability, ...]:
    """Group immutable partition results by official dataset and Assembly term."""

    grouped: dict[
        tuple[str, str, MetadataKind, int | None],
        list[PartitionProvenance],
    ] = defaultdict(list)
    for partition in collection.partitions:
        source = _SOURCE_BY_DATASET.get(partition.dataset, partition.dataset)
        key = (
            source,
            partition.dataset,
            partition.kind,
            _assembly_term(partition),
        )
        grouped[key].append(partition)

    result: list[OfficialSourceAvailability] = []
    for (source, dataset, kind, assembly_term), partitions in grouped.items():
        partitions_expected = len(partitions)
        partitions_complete = sum(item.complete for item in partitions)
        rows_expected = sum(item.expected_rows for item in partitions)
        rows_fetched = sum(item.fetched_rows for item in partitions)
        complete = bool(
            partitions_complete == partitions_expected and rows_fetched == rows_expected
        )
        state = (
            SourceAvailabilityState.INCOMPLETE
            if not complete
            else SourceAvailabilityState.NO_RECORDS
            if rows_expected == 0
            else SourceAvailabilityState.RECORDS_FOUND
        )
        result.append(
            OfficialSourceAvailability(
                source=source,
                dataset=dataset,
                kind=kind,
                assembly_term=assembly_term,
                partitions_expected=partitions_expected,
                partitions_complete=partitions_complete,
                source_rows_expected=rows_expected,
                source_rows_fetched=rows_fetched,
                state=state,
            )
        )
    return tuple(
        sorted(
            result,
            key=lambda item: (
                item.assembly_term is None,
                item.assembly_term or 0,
                item.source,
                item.dataset,
                item.kind.value,
            ),
        )
    )


def _assembly_term(partition: PartitionProvenance) -> int | None:
    parameters = {name.upper(): str(value).strip() for name, value in partition.parameters}
    candidates: list[int] = []
    for name in ("AGE", "DAE_NUM", "ERACO"):
        value = parameters.get(name, "")
        match = re.search(r"\d+", value)
        if match:
            candidates.append(int(match.group()))
    bill_number = parameters.get("BILL_NO", "")
    if re.fullmatch(r"\d{7}", bill_number):
        candidates.append(int(bill_number[:2]))
    unique = tuple(dict.fromkeys(candidates))
    if len(unique) > 1:
        raise ValueError(f"partition {partition.partition_id} contains conflicting Assembly terms")
    return unique[0] if unique else None


__all__ = [
    "OfficialSourceAvailability",
    "SourceAvailabilityState",
    "summarize_source_availability",
]
