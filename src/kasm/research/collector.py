"""Deterministic, provenance-preserving collection of legislative metadata.

The collector deliberately accepts an explicit set of partitions.  Query
planning belongs elsewhere; once a plan reaches this layer, every partition is
read through :meth:`AssemblyOpenApiClient.fetch_all` and no first-page shortcut
is allowed.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from kasm.adapters.korea.client import ApiResult, AssemblyOpenApiClient
from kasm.adapters.korea.pipeline import OpenAssemblyPipeline, distinct_minutes_rows


class MetadataKind(StrEnum):
    """The two metadata families collected before document hydration."""

    BILL = "bill"
    MEETING = "meeting"


@dataclass(frozen=True, slots=True)
class MetadataPartition:
    """One complete official API query in a deterministic collection plan."""

    partition_id: str
    kind: MetadataKind
    dataset: str
    parameters: tuple[tuple[str, str | int], ...] = ()
    page_size: int = 100

    def __post_init__(self) -> None:
        if not self.partition_id.strip():
            raise ValueError("partition_id is required")
        if not self.dataset.isalnum():
            raise ValueError("dataset must be an alphanumeric Open Assembly dataset code")
        if not 1 <= self.page_size <= 1000:
            raise ValueError("page_size must be between 1 and 1000")
        names = [name for name, _value in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("partition parameters must have unique names")
        if any(not name.strip() for name in names):
            raise ValueError("partition parameter names must not be empty")
        reserved = {"key", "type", "pindex", "psize"}
        if any(name.casefold() in reserved for name in names):
            raise ValueError("partition parameters must not override client fields")
        canonical = tuple(sorted(self.parameters, key=lambda item: item[0]))
        object.__setattr__(self, "parameters", canonical)

    @classmethod
    def create(
        cls,
        partition_id: str,
        kind: MetadataKind,
        dataset: str,
        *,
        parameters: Mapping[str, str | int] | None = None,
        page_size: int = 100,
    ) -> MetadataPartition:
        """Create a partition from normal API parameters without retaining a mutable map."""

        return cls(
            partition_id=partition_id,
            kind=kind,
            dataset=dataset,
            parameters=tuple((parameters or {}).items()),
            page_size=page_size,
        )

    def parameters_dict(self) -> dict[str, str | int]:
        return dict(self.parameters)


@dataclass(frozen=True, slots=True)
class PageProvenance:
    """Source identity and row accounting for one fetched API page."""

    page: int
    page_size: int
    total_count: int | None
    row_count: int
    source_url: str
    source_hash: str


@dataclass(frozen=True, slots=True)
class PartitionProvenance:
    """Complete page and hash provenance for one planned partition."""

    partition_id: str
    kind: MetadataKind
    dataset: str
    parameters: tuple[tuple[str, str | int], ...]
    expected_rows: int
    fetched_rows: int
    result_hash: str
    pages: tuple[PageProvenance, ...]

    @property
    def complete(self) -> bool:
        return self.expected_rows == self.fetched_rows

    def to_dict(self) -> dict[str, Any]:
        return {
            "partition_id": self.partition_id,
            "kind": self.kind.value,
            "dataset": self.dataset,
            "parameters": dict(self.parameters),
            "expected_rows": self.expected_rows,
            "fetched_rows": self.fetched_rows,
            "result_hash": self.result_hash,
            "complete": self.complete,
            "pages": [asdict(page) for page in self.pages],
        }


@dataclass(frozen=True, slots=True)
class CollectionCoverage:
    """Observable accounting for source rows and normalized metadata records."""

    partitions_expected: int
    partitions_complete: int
    source_rows_expected: int
    source_rows_fetched: int
    bill_source_rows: int
    bill_unique_records: int
    bill_duplicate_rows: int
    bill_rejected_rows: int
    meeting_source_rows: int
    meeting_unique_pdfs: int
    meeting_rows_merged: int
    meeting_rejected_rows: int

    @property
    def source_complete(self) -> bool:
        return bool(
            self.partitions_complete == self.partitions_expected
            and self.source_rows_fetched == self.source_rows_expected
        )

    @property
    def complete(self) -> bool:
        """Whether every source row was both fetched and usable for its metadata kind."""

        return bool(
            self.source_complete
            and self.bill_rejected_rows == 0
            and self.meeting_rejected_rows == 0
        )

    def to_dict(self) -> dict[str, int | bool]:
        return {**asdict(self), "source_complete": self.source_complete, "complete": self.complete}


@dataclass(frozen=True, slots=True)
class MetadataCollection:
    """A deterministic snapshot of bill and meeting metadata."""

    bills: tuple[dict[str, Any], ...]
    meetings: tuple[dict[str, Any], ...]
    partitions: tuple[PartitionProvenance, ...]
    coverage: CollectionCoverage

    @property
    def source_hash(self) -> str:
        """Return a stable digest of the ordered partition snapshots."""

        digest = hashlib.sha256()
        for partition in self.partitions:
            digest.update(partition.partition_id.encode())
            digest.update(b":")
            digest.update(partition.result_hash.encode())
            digest.update(b"\n")
        return digest.hexdigest()


class MetadataCollector:
    """Collect every planned API partition before normalizing metadata."""

    def __init__(self, client: AssemblyOpenApiClient) -> None:
        self.client = client

    def collect(
        self,
        partitions: Iterable[MetadataPartition],
        *,
        refresh: bool = False,
    ) -> MetadataCollection:
        planned = tuple(sorted(partitions, key=_partition_sort_key))
        identifiers = [partition.partition_id for partition in planned]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("metadata partition ids must be unique")

        bill_rows: list[dict[str, Any]] = []
        meeting_rows: list[dict[str, Any]] = []
        provenance: list[PartitionProvenance] = []
        for partition in planned:
            result = self.client.fetch_all(
                partition.dataset,
                page_size=partition.page_size,
                parameters=partition.parameters_dict(),
                refresh=refresh,
            )
            provenance.append(_partition_provenance(partition, result))
            destination = bill_rows if partition.kind is MetadataKind.BILL else meeting_rows
            destination.extend(copy.deepcopy(result.rows))

        bills, bill_duplicates, bill_rejected = _distinct_bills(bill_rows)
        meetings, meeting_rejected = _distinct_meetings(meeting_rows)
        partition_rows_expected = sum(item.expected_rows for item in provenance)
        partition_rows_fetched = sum(item.fetched_rows for item in provenance)
        complete_partitions = sum(item.complete for item in provenance)
        coverage = CollectionCoverage(
            partitions_expected=len(planned),
            partitions_complete=complete_partitions,
            source_rows_expected=partition_rows_expected,
            source_rows_fetched=partition_rows_fetched,
            bill_source_rows=len(bill_rows),
            bill_unique_records=len(bills),
            bill_duplicate_rows=bill_duplicates,
            bill_rejected_rows=bill_rejected,
            meeting_source_rows=len(meeting_rows),
            meeting_unique_pdfs=len(meetings),
            meeting_rows_merged=max(0, len(meeting_rows) - meeting_rejected - len(meetings)),
            meeting_rejected_rows=meeting_rejected,
        )
        return MetadataCollection(tuple(bills), tuple(meetings), tuple(provenance), coverage)


def _partition_sort_key(
    partition: MetadataPartition,
) -> tuple[str, str, tuple[tuple[str, str], ...], str]:
    canonical_parameters = tuple((key, str(value)) for key, value in partition.parameters)
    return partition.kind.value, partition.dataset, canonical_parameters, partition.partition_id


def _partition_provenance(
    partition: MetadataPartition, result: ApiResult
) -> PartitionProvenance:
    if result.dataset != partition.dataset:
        raise ValueError(
            f"partition {partition.partition_id} requested {partition.dataset} "
            f"but received {result.dataset}"
        )
    pages = tuple(
        PageProvenance(
            page=page.page,
            page_size=page.page_size,
            total_count=page.total_count,
            row_count=len(page.rows),
            source_url=page.source_url,
            source_hash=page.source_hash,
        )
        for page in result.pages
    )
    return PartitionProvenance(
        partition_id=partition.partition_id,
        kind=partition.kind,
        dataset=partition.dataset,
        parameters=partition.parameters,
        expected_rows=result.total_count,
        fetched_rows=len(result.rows),
        result_hash=result.source_hash,
        pages=pages,
    )


def _distinct_bills(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    by_number: dict[str, dict[str, Any]] = {}
    duplicate_count = 0
    rejected_count = 0
    for row in rows:
        raw_number = row.get("BILL_NO")
        bill_no = str(raw_number).strip() if raw_number is not None else ""
        if len(bill_no) != 7 or not bill_no.isdigit():
            rejected_count += 1
            continue
        if bill_no in by_number:
            duplicate_count += 1
            _fill_missing_fields(by_number[bill_no], row)
            continue
        by_number[bill_no] = copy.deepcopy(dict(row))
    return [by_number[number] for number in sorted(by_number)], duplicate_count, rejected_count


def _fill_missing_fields(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    """Fill absent data without allowing a later partition to overwrite conflicts."""

    for field, value in source.items():
        current = target.get(field)
        if field not in target or current is None or current == "":
            target[field] = copy.deepcopy(value)


def _distinct_meetings(
    rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    usable: list[dict[str, Any]] = []
    rejected_count = 0
    for row in rows:
        try:
            OpenAssemblyPipeline.minutes_url(row)
        except ValueError:
            rejected_count += 1
            continue
        usable.append(row)
    distinct = distinct_minutes_rows(tuple(usable)) if usable else []
    distinct.sort(key=OpenAssemblyPipeline.minutes_url)
    return distinct, rejected_count
