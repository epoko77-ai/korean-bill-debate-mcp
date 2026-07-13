"""Complete, resumable inventory of official Assembly full-text documents.

The searchable corpus can only claim completeness after it knows the exact
document universe.  This module enumerates that universe from complete Open
Assembly term snapshots and the exact bill-detail document index.  It never
uses a topic query or a top-N limit.

Bill-detail discovery is checkpointed one bill at a time.  A process can be
interrupted after thousands of requests and resume without repeating completed
index checks.  The cache contains public document URLs and stable reason codes
only; an Open Assembly key is read by the API client and is never serialized.
"""

from __future__ import annotations

import calendar
import contextlib
import hashlib
import os
import re
import urllib.parse
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from kasm.adapters.korea.bills import BILL_DATASET
from kasm.adapters.korea.client import AssemblyOpenApiClient
from kasm.adapters.korea.documents import (
    BillDocumentIdentityError,
    BillDocumentLink,
    BillDocumentsClient,
)
from kasm.adapters.korea.pipeline import OpenAssemblyPipeline
from kasm.adapters.korea.sources import DATASET_BY_SOURCE, MeetingSource
from kasm.research.collector import MetadataCollector, MetadataKind, MetadataPartition
from kasm.research.contracts import EvidenceType
from kasm.research.documents import OfficialDocumentKind
from kasm.research.engine import DocumentWorkItem
from kasm.research.planner import DEFAULT_ASSEMBLY_TERM_BOUNDS

from .models import (
    CORPUS_SCHEMA_VERSION,
    CorpusDocumentIdentity,
    CorpusEvidenceKind,
    CorpusIngestionFailure,
)
from .serialization import canonical_hash, canonical_json, decode_canonical_json

INVENTORY_SCHEMA_VERSION: Final = 1
_BILL_NO: Final = re.compile(r"\d{7}")
_BILL_ID: Final = re.compile(r"[A-Za-z0-9_]+")
_SAFE_IDENTIFIER_PART: Final = re.compile(r"[A-Za-z0-9_.:-]+")
_OFFICIAL_HOSTS: Final = {
    "open.assembly.go.kr",
    "record.assembly.go.kr",
    "likms.assembly.go.kr",
}
_SENSITIVE_QUERY_NAMES: Final = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "key",
    "password",
    "secret",
    "token",
}


@dataclass(frozen=True, slots=True)
class CorpusInventoryItem:
    """One exactly identified official document scheduled for parsing."""

    assembly_term: int
    evidence_kind: CorpusEvidenceKind
    official_identifier: str
    work_item: DocumentWorkItem
    title: str = ""
    document_date: date | None = None
    committee: str = ""

    def __post_init__(self) -> None:
        identity = CorpusDocumentIdentity(
            self.assembly_term,
            self.evidence_kind,
            self.official_identifier,
        )
        expected_kind = {
            CorpusEvidenceKind.BILL_ORIGINAL: OfficialDocumentKind.BILL_TEXT,
            CorpusEvidenceKind.REVIEW_REPORT: OfficialDocumentKind.REVIEW_REPORT,
            CorpusEvidenceKind.MINUTES: OfficialDocumentKind.MINUTES,
        }[self.evidence_kind]
        if self.work_item.kind is not expected_kind:
            raise ValueError("inventory evidence kind does not match document work")
        expected_work_id = DocumentWorkItem.create(
            self.work_item.kind,
            self.work_item.official_url,
            evidence_types=self.work_item.evidence_types,
            related_bill_numbers=self.work_item.related_bill_numbers,
        ).work_id
        if self.work_item.work_id != expected_work_id:
            raise ValueError("inventory document work identity is invalid")
        _validate_public_official_url(self.work_item.official_url)
        if self.evidence_kind is CorpusEvidenceKind.BILL_ORIGINAL:
            bills = self.work_item.related_bill_numbers
            if (
                len(bills) != 1
                or self.official_identifier != f"bill:{bills[0]}:original"
            ):
                raise ValueError("bill original inventory identity is not exact")
        if any(
            int(number[:2]) != self.assembly_term
            for number in self.work_item.related_bill_numbers
        ):
            raise ValueError("inventory related bill belongs to another Assembly term")
        if self.title and (self.title != self.title.strip() or len(self.title) > 2_000):
            raise ValueError("inventory title is invalid")
        if self.committee and (
            self.committee != self.committee.strip() or len(self.committee) > 500
        ):
            raise ValueError("inventory committee is invalid")
        # Force validation and make the exact identity available to callers.
        _ = identity.identity_id

    @property
    def identity(self) -> CorpusDocumentIdentity:
        return CorpusDocumentIdentity(
            self.assembly_term,
            self.evidence_kind,
            self.official_identifier,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "assembly_term": self.assembly_term,
            "evidence_kind": self.evidence_kind.value,
            "official_identifier": self.official_identifier,
            "work_item": {
                "work_id": self.work_item.work_id,
                "kind": self.work_item.kind.value,
                "official_url": self.work_item.official_url,
                "evidence_types": [item.value for item in self.work_item.evidence_types],
                "related_bill_numbers": list(self.work_item.related_bill_numbers),
            },
            "title": self.title,
            "document_date": self.document_date.isoformat() if self.document_date else None,
            "committee": self.committee,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CorpusInventoryItem:
        raw_work = _mapping(payload.get("work_item"), "inventory work_item")
        raw_evidence = raw_work.get("evidence_types")
        raw_bills = raw_work.get("related_bill_numbers")
        if not isinstance(raw_evidence, list) or not isinstance(raw_bills, list):
            raise ValueError("inventory work lists are invalid")
        item = DocumentWorkItem(
            work_id=_text(raw_work, "work_id"),
            kind=OfficialDocumentKind(_text(raw_work, "kind")),
            official_url=_text(raw_work, "official_url"),
            evidence_types=tuple(EvidenceType(str(value)) for value in raw_evidence),
            related_bill_numbers=tuple(str(value) for value in raw_bills),
        )
        raw_date = payload.get("document_date")
        if raw_date is not None and not isinstance(raw_date, str):
            raise ValueError("inventory document_date is invalid")
        return cls(
            assembly_term=_integer(payload, "assembly_term"),
            evidence_kind=CorpusEvidenceKind(_text(payload, "evidence_kind")),
            official_identifier=_text(payload, "official_identifier"),
            work_item=item,
            title=_text(payload, "title", allow_empty=True),
            document_date=date.fromisoformat(raw_date) if raw_date else None,
            committee=_text(payload, "committee", allow_empty=True),
        )


@dataclass(frozen=True, slots=True)
class CorpusInventoryGap:
    """A sanitized source gap that prevents a completeness claim."""

    assembly_term: int
    evidence_kind: CorpusEvidenceKind
    failure: CorpusIngestionFailure
    retryable: bool

    def __post_init__(self) -> None:
        if self.assembly_term < 1:
            raise ValueError("inventory gap Assembly term is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "assembly_term": self.assembly_term,
            "evidence_kind": self.evidence_kind.value,
            "failure": self.failure.to_dict(),
            "retryable": self.retryable,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CorpusInventoryGap:
        return cls(
            assembly_term=_integer(payload, "assembly_term"),
            evidence_kind=CorpusEvidenceKind(_text(payload, "evidence_kind")),
            failure=CorpusIngestionFailure.from_dict(
                dict(_mapping(payload.get("failure"), "inventory failure"))
            ),
            retryable=_boolean(payload, "retryable"),
        )


@dataclass(frozen=True, slots=True)
class CorpusInventoryCoverage:
    """Expected inventory accounting for one term/evidence cell."""

    assembly_term: int
    evidence_kind: CorpusEvidenceKind
    expected_count: int | None
    item_count: int
    gap_count: int

    def __post_init__(self) -> None:
        if self.assembly_term < 1 or self.item_count < 0 or self.gap_count < 0:
            raise ValueError("inventory coverage counts are invalid")
        if self.expected_count is not None and self.expected_count < 0:
            raise ValueError("inventory expected_count is invalid")
        if self.expected_count is not None and (
            self.item_count + self.gap_count > self.expected_count
        ):
            raise ValueError("inventory accounts for more documents than expected")

    @property
    def unaccounted_count(self) -> int | None:
        if self.expected_count is None:
            return None
        return self.expected_count - self.item_count - self.gap_count

    @property
    def complete(self) -> bool:
        return bool(
            self.expected_count is not None
            and self.item_count == self.expected_count
            and self.gap_count == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "assembly_term": self.assembly_term,
            "evidence_kind": self.evidence_kind.value,
            "expected_count": self.expected_count,
            "item_count": self.item_count,
            "gap_count": self.gap_count,
            "unaccounted_count": self.unaccounted_count,
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class CorpusInventoryManifest:
    """Immutable, credential-free official inventory snapshot."""

    inventory_id: str
    inventory_as_of: datetime
    assembly_terms: tuple[int, ...]
    source_snapshot_hash: str
    items: tuple[CorpusInventoryItem, ...]
    gaps: tuple[CorpusInventoryGap, ...]
    coverage: tuple[CorpusInventoryCoverage, ...]

    def __post_init__(self) -> None:
        if self.inventory_as_of.tzinfo is None:
            raise ValueError("inventory_as_of must be timezone-aware")
        if (
            not self.assembly_terms
            or tuple(sorted(set(self.assembly_terms))) != self.assembly_terms
        ):
            raise ValueError("inventory Assembly terms must be unique and sorted")
        _sha256(self.inventory_id, "inventory_id")
        _sha256(self.source_snapshot_hash, "source_snapshot_hash")
        item_keys = tuple(
            (item.identity.identity_id, item.work_item.work_id) for item in self.items
        )
        identity_ids = tuple(key[0] for key in item_keys)
        work_ids = tuple(key[1] for key in item_keys)
        if (
            len(identity_ids) != len(set(identity_ids))
            or len(work_ids) != len(set(work_ids))
            or tuple(sorted(item_keys)) != item_keys
        ):
            raise ValueError("inventory items must be unique and sorted")
        gap_keys = tuple(
            (gap.assembly_term, gap.evidence_kind.value, gap.failure.failure_key)
            for gap in self.gaps
        )
        if len(gap_keys) != len(set(gap_keys)) or tuple(sorted(gap_keys)) != gap_keys:
            raise ValueError("inventory gaps must be unique and sorted")
        scopes = {
            (term, kind)
            for term in self.assembly_terms
            for kind in CorpusEvidenceKind
        }
        if any((item.assembly_term, item.evidence_kind) not in scopes for item in self.items):
            raise ValueError("inventory item is outside its scope")
        if any((gap.assembly_term, gap.evidence_kind) not in scopes for gap in self.gaps):
            raise ValueError("inventory gap is outside its scope")
        coverage_scopes = {(entry.assembly_term, entry.evidence_kind) for entry in self.coverage}
        if coverage_scopes != scopes or len(coverage_scopes) != len(self.coverage):
            raise ValueError("inventory coverage matrix is incomplete")
        expected_coverage = tuple(
            sorted(
                self.coverage,
                key=lambda item: (item.assembly_term, item.evidence_kind.value),
            )
        )
        if self.coverage != expected_coverage:
            raise ValueError("inventory coverage must be sorted")
        for entry in self.coverage:
            actual_items = sum(
                item.assembly_term == entry.assembly_term
                and item.evidence_kind is entry.evidence_kind
                for item in self.items
            )
            actual_gaps = sum(
                gap.assembly_term == entry.assembly_term
                and gap.evidence_kind is entry.evidence_kind
                for gap in self.gaps
            )
            if entry.item_count != actual_items or entry.gap_count != actual_gaps:
                raise ValueError("inventory coverage does not match items and gaps")
        if self.inventory_id != canonical_hash(self._identity_payload()):
            raise ValueError("inventory_id does not match inventory content")

    @property
    def complete(self) -> bool:
        return all(entry.complete for entry in self.coverage) and not self.gaps

    @classmethod
    def create(
        cls,
        *,
        inventory_as_of: datetime,
        assembly_terms: Iterable[int],
        source_snapshot_hash: str,
        items: Iterable[CorpusInventoryItem],
        gaps: Iterable[CorpusInventoryGap],
        expected_counts: Mapping[tuple[int, CorpusEvidenceKind], int | None],
    ) -> CorpusInventoryManifest:
        terms = tuple(sorted(set(assembly_terms)))
        normalized_items = tuple(
            sorted(items, key=lambda item: (item.identity.identity_id, item.work_item.work_id))
        )
        normalized_gaps = tuple(
            sorted(
                gaps,
                key=lambda item: (
                    item.assembly_term,
                    item.evidence_kind.value,
                    item.failure.failure_key,
                ),
            )
        )
        coverage = tuple(
            CorpusInventoryCoverage(
                term,
                kind,
                expected_counts.get((term, kind)),
                sum(
                    item.assembly_term == term and item.evidence_kind is kind
                    for item in normalized_items
                ),
                sum(
                    gap.assembly_term == term and gap.evidence_kind is kind
                    for gap in normalized_gaps
                ),
            )
            for term in terms
            for kind in sorted(CorpusEvidenceKind, key=lambda item: item.value)
        )
        payload = {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "corpus_schema_version": CORPUS_SCHEMA_VERSION,
            "inventory_as_of": inventory_as_of.astimezone(UTC).isoformat(),
            "assembly_terms": list(terms),
            "source_snapshot_hash": source_snapshot_hash,
            "items": [item.to_dict() for item in normalized_items],
            "gaps": [gap.to_dict() for gap in normalized_gaps],
            "coverage": [entry.to_dict() for entry in coverage],
        }
        return cls(
            inventory_id=canonical_hash(payload),
            inventory_as_of=inventory_as_of,
            assembly_terms=terms,
            source_snapshot_hash=source_snapshot_hash,
            items=normalized_items,
            gaps=normalized_gaps,
            coverage=coverage,
        )

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "corpus_schema_version": CORPUS_SCHEMA_VERSION,
            "inventory_as_of": self.inventory_as_of.astimezone(UTC).isoformat(),
            "assembly_terms": list(self.assembly_terms),
            "source_snapshot_hash": self.source_snapshot_hash,
            "items": [item.to_dict() for item in self.items],
            "gaps": [gap.to_dict() for gap in self.gaps],
            "coverage": [entry.to_dict() for entry in self.coverage],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "inventory_id": self.inventory_id,
            **self._identity_payload(),
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CorpusInventoryManifest:
        if payload.get("schema_version") != INVENTORY_SCHEMA_VERSION:
            raise ValueError("unsupported inventory schema")
        if payload.get("corpus_schema_version") != CORPUS_SCHEMA_VERSION:
            raise ValueError("unsupported corpus schema in inventory")
        raw_terms = payload.get("assembly_terms")
        raw_items = payload.get("items")
        raw_gaps = payload.get("gaps")
        raw_coverage = payload.get("coverage")
        raw_lists = (raw_terms, raw_items, raw_gaps, raw_coverage)
        if not all(isinstance(value, list) for value in raw_lists):
            raise ValueError("inventory lists are invalid")
        assert isinstance(raw_terms, list)
        assert isinstance(raw_items, list)
        assert isinstance(raw_gaps, list)
        assert isinstance(raw_coverage, list)
        coverage: list[CorpusInventoryCoverage] = []
        for raw in raw_coverage:
            item = _mapping(raw, "inventory coverage")
            expected = item.get("expected_count")
            if expected is not None and (
                not isinstance(expected, int) or isinstance(expected, bool)
            ):
                raise ValueError("inventory expected_count is invalid")
            restored = CorpusInventoryCoverage(
                _integer(item, "assembly_term"),
                CorpusEvidenceKind(_text(item, "evidence_kind")),
                expected,
                _integer(item, "item_count"),
                _integer(item, "gap_count"),
            )
            if item.get("unaccounted_count") != restored.unaccounted_count:
                raise ValueError("inventory unaccounted_count does not match")
            if item.get("complete") is not restored.complete:
                raise ValueError("inventory coverage complete flag does not match")
            coverage.append(restored)
        result = cls(
            inventory_id=_text(payload, "inventory_id"),
            inventory_as_of=datetime.fromisoformat(_text(payload, "inventory_as_of")),
            assembly_terms=tuple(int(value) for value in raw_terms),
            source_snapshot_hash=_text(payload, "source_snapshot_hash"),
            items=tuple(
                CorpusInventoryItem.from_dict(_mapping(item, "inventory item"))
                for item in raw_items
            ),
            gaps=tuple(
                CorpusInventoryGap.from_dict(_mapping(item, "inventory gap"))
                for item in raw_gaps
            ),
            coverage=tuple(coverage),
        )
        if payload.get("complete") is not result.complete:
            raise ValueError("inventory complete flag does not match")
        return result


@dataclass(frozen=True, slots=True)
class _BillIndexRecord:
    fingerprint: str
    bill_number: str
    links: tuple[BillDocumentLink, ...]
    failure_code: str | None = None
    retryable: bool = False

    @property
    def complete(self) -> bool:
        return self.failure_code is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "fingerprint": self.fingerprint,
            "bill_number": self.bill_number,
            "links": [
                {
                    "document_type": item.document_type,
                    "title": item.title,
                    "file_format": item.file_format,
                    "official_url": item.official_url,
                }
                for item in self.links
            ],
            "failure_code": self.failure_code,
            "retryable": self.retryable,
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> _BillIndexRecord:
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported bill-index cache schema")
        raw_links = payload.get("links")
        if not isinstance(raw_links, list):
            raise ValueError("bill-index cache links are invalid")
        raw_failure = payload.get("failure_code")
        if raw_failure is not None and not isinstance(raw_failure, str):
            raise ValueError("bill-index failure code is invalid")
        result = cls(
            fingerprint=_text(payload, "fingerprint"),
            bill_number=_text(payload, "bill_number"),
            links=tuple(
                BillDocumentLink(
                    _text(_mapping(item, "bill-index link"), "document_type"),
                    _text(_mapping(item, "bill-index link"), "title"),
                    _text(_mapping(item, "bill-index link"), "file_format"),
                    _text(_mapping(item, "bill-index link"), "official_url"),
                )
                for item in raw_links
            ),
            failure_code=raw_failure,
            retryable=_boolean(payload, "retryable"),
        )
        if payload.get("complete") is not result.complete:
            raise ValueError("bill-index complete flag does not match")
        return result


class OpenAssemblyCorpusInventorySource:
    """Enumerate every full-text document for complete Assembly terms."""

    def __init__(
        self,
        api_client: AssemblyOpenApiClient,
        *,
        bill_documents: BillDocumentsClient | None = None,
        page_size: int = 1000,
        term_bounds: Mapping[int, tuple[date, date]] | None = None,
    ) -> None:
        if not 1 <= page_size <= 1000:
            raise ValueError("inventory page_size must be between 1 and 1000")
        self.collector = MetadataCollector(api_client)
        self.bill_documents = bill_documents or BillDocumentsClient()
        self.page_size = page_size
        self.term_bounds = dict(term_bounds or DEFAULT_ASSEMBLY_TERM_BOUNDS)

    def collect(
        self,
        assembly_terms: Iterable[int],
        *,
        inventory_as_of: datetime,
        discovery_cache_dir: str | Path,
        refresh_metadata: bool = False,
        refresh_bill_indexes: bool = False,
    ) -> CorpusInventoryManifest:
        if inventory_as_of.tzinfo is None:
            raise ValueError("inventory_as_of must be timezone-aware")
        terms = tuple(sorted(set(assembly_terms)))
        if not terms or any(term < 1 for term in terms):
            raise ValueError("at least one positive Assembly term is required")
        cache_root = Path(discovery_cache_dir)
        cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(cache_root, 0o700)

        all_items: list[CorpusInventoryItem] = []
        all_gaps: list[CorpusInventoryGap] = []
        expected: dict[tuple[int, CorpusEvidenceKind], int | None] = {}
        snapshot_parts: list[str] = []
        for term in terms:
            start, configured_end = self._term_bounds(term)
            end = min(configured_end, inventory_as_of.astimezone(UTC).date())
            if end < start:
                raise ValueError("inventory date precedes the requested Assembly term")
            collection = self.collector.collect(
                self._metadata_partitions(term, start, end),
                refresh=refresh_metadata,
            )
            snapshot_parts.append(f"{term}:{collection.source_hash}")
            items, gaps, counts = self._term_inventory(
                term,
                collection.bills,
                collection.meetings,
                bill_metadata_complete=(
                    collection.coverage.source_complete
                    and collection.coverage.bill_rejected_rows == 0
                ),
                meeting_metadata_complete=(
                    collection.coverage.source_complete
                    and collection.coverage.meeting_rejected_rows == 0
                ),
                source_complete=collection.coverage.source_complete,
                bill_rejected_rows=collection.coverage.bill_rejected_rows,
                meeting_rejected_rows=collection.coverage.meeting_rejected_rows,
                date_from=start,
                date_to=end,
                inventory_as_of=inventory_as_of,
                cache_dir=cache_root / f"term-{term}" / "bill-index",
                refresh_bill_indexes=refresh_bill_indexes,
            )
            all_items.extend(items)
            all_gaps.extend(gaps)
            expected.update(counts)

        source_snapshot_hash = hashlib.sha256("\n".join(snapshot_parts).encode()).hexdigest()
        return CorpusInventoryManifest.create(
            inventory_as_of=inventory_as_of,
            assembly_terms=terms,
            source_snapshot_hash=source_snapshot_hash,
            items=_merge_inventory_items(all_items, all_gaps),
            gaps=all_gaps,
            expected_counts=expected,
        )

    def _term_bounds(self, term: int) -> tuple[date, date]:
        try:
            return self.term_bounds[term]
        except KeyError as exc:
            raise ValueError(
                f"Assembly term {term} requires configured date bounds"
            ) from exc

    def _metadata_partitions(
        self,
        term: int,
        date_from: date,
        date_to: date,
    ) -> tuple[MetadataPartition, ...]:
        partitions = [
            MetadataPartition.create(
                f"corpus:bill:{term}",
                MetadataKind.BILL,
                BILL_DATASET,
                parameters={"AGE": term},
                page_size=self.page_size,
            )
        ]
        for month in _months(date_from, date_to):
            partitions.extend(
                (
                    MetadataPartition.create(
                        f"corpus:plenary:{term}:{month}",
                        MetadataKind.MEETING,
                        DATASET_BY_SOURCE[MeetingSource.PLENARY],
                        parameters={"DAE_NUM": term, "CONF_DATE": month},
                        page_size=self.page_size,
                    ),
                    MetadataPartition.create(
                        f"corpus:committee:{term}:{month}",
                        MetadataKind.MEETING,
                        DATASET_BY_SOURCE[MeetingSource.COMMITTEE],
                        parameters={"DAE_NUM": term, "CONF_DATE": month},
                        page_size=self.page_size,
                    ),
                )
            )
        partitions.append(
            MetadataPartition.create(
                f"corpus:subcommittee:{term}",
                MetadataKind.MEETING,
                DATASET_BY_SOURCE[MeetingSource.SUBCOMMITTEE],
                parameters={"ERACO": f"제{term}대"},
                page_size=self.page_size,
            )
        )
        return tuple(partitions)

    def _term_inventory(
        self,
        term: int,
        bills: tuple[dict[str, Any], ...],
        meetings: tuple[dict[str, Any], ...],
        *,
        bill_metadata_complete: bool,
        meeting_metadata_complete: bool,
        source_complete: bool,
        bill_rejected_rows: int,
        meeting_rejected_rows: int,
        date_from: date,
        date_to: date,
        inventory_as_of: datetime,
        cache_dir: Path,
        refresh_bill_indexes: bool,
    ) -> tuple[
        tuple[CorpusInventoryItem, ...],
        tuple[CorpusInventoryGap, ...],
        dict[tuple[int, CorpusEvidenceKind], int | None],
    ]:
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        items: list[CorpusInventoryItem] = []
        gaps: list[CorpusInventoryGap] = []
        review_index_complete = bill_metadata_complete
        review_urls: set[str] = set()
        if not source_complete:
            for kind in CorpusEvidenceKind:
                gaps.append(
                    _gap(
                        term,
                        kind,
                        f"inventory:metadata-snapshot:{term}:{kind.value}",
                        "metadata_snapshot_incomplete",
                        None,
                        True,
                    )
                )
        for index in range(bill_rejected_rows):
            gaps.extend(
                _metadata_gap_pair(
                    term,
                    "bill_metadata_row_rejected",
                    f"rejected-{index + 1}",
                )
            )
        for index in range(meeting_rejected_rows):
            gaps.append(
                _gap(
                    term,
                    CorpusEvidenceKind.MINUTES,
                    f"inventory:minutes-metadata-rejected:{term}:{index + 1}",
                    "minutes_metadata_row_rejected",
                    None,
                    False,
                )
            )

        for row in bills:
            number = str(row.get("BILL_NO") or "").strip()
            if not _BILL_NO.fullmatch(number) or int(number[:2]) != term:
                bill_metadata_complete = False
                review_index_complete = False
                gaps.extend(
                    _metadata_gap_pair(
                        term,
                        "bill_metadata_identity_invalid",
                        number or None,
                    )
                )
                continue
            bill_id = _bill_id(row)
            if bill_id is None:
                review_index_complete = False
                gaps.extend(_bill_index_gap_pair(term, number, "official_bill_id_missing", False))
                continue
            record = self._bill_index_record(
                term,
                number,
                bill_id,
                inventory_as_of=inventory_as_of,
                cache_dir=cache_dir,
                refresh=refresh_bill_indexes,
            )
            if not record.complete:
                review_index_complete = False
                assert record.failure_code is not None
                gaps.extend(
                    _bill_index_gap_pair(
                        term,
                        number,
                        record.failure_code,
                        record.retryable,
                    )
                )
                continue
            originals = tuple(
                link
                for link in record.links
                if link.document_type == "bill_text" and link.file_format.casefold() == "pdf"
            )
            if len(originals) != 1:
                gaps.append(
                    _gap(
                        term,
                        CorpusEvidenceKind.BILL_ORIGINAL,
                        f"inventory:bill:{number}:original",
                        "bill_original_index_invalid",
                        f"bill:{number}:original",
                        False,
                    )
                )
            else:
                link = originals[0]
                items.append(
                    _item(
                        term,
                        CorpusEvidenceKind.BILL_ORIGINAL,
                        f"bill:{number}:original",
                        OfficialDocumentKind.BILL_TEXT,
                        link.official_url,
                        (EvidenceType.BILL_TEXT,),
                        (number,),
                        title=str(row.get("BILL_NAME") or row.get("BILL_NM") or link.title).strip(),
                        document_date=_row_date(row, ("PROPOSE_DT",)),
                        committee=_row_text(row, ("COMMITTEE", "COMMITTEE_NM")),
                    )
                )
            for link in record.links:
                if (
                    link.document_type != "committee_review_report"
                    or link.file_format.casefold() != "pdf"
                ):
                    continue
                review_urls.add(link.official_url)
                items.append(
                    _item(
                        term,
                        CorpusEvidenceKind.REVIEW_REPORT,
                        _review_identifier(link.official_url),
                        OfficialDocumentKind.REVIEW_REPORT,
                        link.official_url,
                        (EvidenceType.REVIEW_REPORTS,),
                        (number,),
                        title=link.title,
                        committee=_row_text(row, ("COMMITTEE", "COMMITTEE_NM")),
                    )
                )

        minute_url_count = 0
        for row in meetings:
            meeting_date = _row_date(row, ("CONF_DATE", "CONF_DT", "MEETING_DATE", "MTG_DATE"))
            if meeting_date is None:
                meeting_metadata_complete = False
                gaps.append(
                    _gap(
                        term,
                        CorpusEvidenceKind.MINUTES,
                        f"inventory:minutes-date:{canonical_hash(dict(row))}",
                        "minutes_date_missing",
                        None,
                        False,
                    )
                )
                continue
            if meeting_date < date_from or meeting_date > date_to:
                continue
            try:
                url = OpenAssemblyPipeline.minutes_url(row)
                identifier = _minutes_identifier(url)
                _validate_public_official_url(url)
            except ValueError:
                meeting_metadata_complete = False
                gaps.append(
                    _gap(
                        term,
                        CorpusEvidenceKind.MINUTES,
                        f"inventory:minutes:invalid:{canonical_hash(dict(row))}",
                        "minutes_identity_invalid",
                        None,
                        False,
                    )
                )
                continue
            minute_url_count += 1
            items.append(
                _item(
                    term,
                    CorpusEvidenceKind.MINUTES,
                    identifier,
                    OfficialDocumentKind.MINUTES,
                    url,
                    (EvidenceType.SPEECHES, EvidenceType.SPEECH_CONTEXT),
                    _related_bill_numbers(row, term),
                    title=_row_text(row, ("TITLE", "CONF_NAME", "MEETING_NAME")),
                    document_date=meeting_date,
                    committee=_row_text(
                        row,
                        ("SB_CMIT_NM", "COMM_NAME", "CMIT_NM", "COMMITTEE_NAME"),
                    ),
                )
            )
        merged_items = _merge_inventory_items(items, gaps)
        counts = {
            (term, CorpusEvidenceKind.BILL_ORIGINAL): (
                len(bills) if bill_metadata_complete else None
            ),
            (term, CorpusEvidenceKind.REVIEW_REPORT): (
                len(review_urls) if review_index_complete else None
            ),
            (term, CorpusEvidenceKind.MINUTES): (
                minute_url_count if meeting_metadata_complete else None
            ),
        }
        return merged_items, tuple(gaps), counts

    def _bill_index_record(
        self,
        term: int,
        bill_number: str,
        bill_id: str,
        *,
        inventory_as_of: datetime,
        cache_dir: Path,
        refresh: bool,
    ) -> _BillIndexRecord:
        fingerprint = canonical_hash(
            {
                "assembly_term": term,
                "bill_number": bill_number,
                "bill_id": bill_id,
                "inventory_as_of": inventory_as_of.astimezone(UTC).isoformat(),
            }
        )
        path = cache_dir / f"{bill_number}.json"
        if path.exists() and not refresh:
            cached = _read_bill_record(path)
            if cached.fingerprint == fingerprint and (cached.complete or not cached.retryable):
                return cached
        try:
            links = self.bill_documents.documents(
                bill_id,
                bill_number,
                include_bill_text=True,
                include_review_reports=True,
            )
            for link in links:
                _validate_public_official_url(link.official_url)
            record = _BillIndexRecord(fingerprint, bill_number, tuple(links))
        except BillDocumentIdentityError:
            record = _BillIndexRecord(
                fingerprint,
                bill_number,
                (),
                "official_bill_identity_unverified",
                False,
            )
        except ValueError:
            record = _BillIndexRecord(
                fingerprint,
                bill_number,
                (),
                "bill_document_index_invalid",
                False,
            )
        except (OSError, RuntimeError, TimeoutError):
            record = _BillIndexRecord(
                fingerprint,
                bill_number,
                (),
                "bill_document_index_unavailable",
                True,
            )
        _atomic_private_write(path, canonical_json(record.to_dict()))
        return record


def write_inventory_manifest(path: str | Path, manifest: CorpusInventoryManifest) -> None:
    """Write a credential-free dry-run manifest atomically with owner-only mode."""

    _atomic_private_write(Path(path), canonical_json(manifest.to_dict()))


def read_inventory_manifest(path: str | Path) -> CorpusInventoryManifest:
    raw = Path(path).read_bytes()
    payload = decode_canonical_json(raw)
    if not isinstance(payload, dict):
        raise ValueError("inventory manifest must be an object")
    return CorpusInventoryManifest.from_dict(payload)


def pin_inventory_session(
    cache_dir: str | Path,
    *,
    requested_as_of: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> datetime:
    """Pin one observation time across interrupted inventory CLI invocations."""

    root = Path(cache_dir)
    path = root / "inventory-session.json"
    selected = requested_as_of
    if selected is None and path.exists():
        try:
            payload = decode_canonical_json(path.read_bytes())
            if (
                isinstance(payload, dict)
                and payload.get("schema_version") == 1
                and payload.get("state") == "running"
                and isinstance(payload.get("inventory_as_of"), str)
            ):
                candidate = datetime.fromisoformat(payload["inventory_as_of"])
                if candidate.tzinfo is not None:
                    selected = candidate
        except (OSError, TypeError, ValueError, OverflowError):
            selected = None
    if selected is None:
        selected = (clock or (lambda: datetime.now(UTC)))()
    if selected.tzinfo is None:
        raise ValueError("inventory session time must be timezone-aware")
    _atomic_private_write(
        path,
        canonical_json(
            {
                "schema_version": 1,
                "state": "running",
                "inventory_as_of": selected.astimezone(UTC).isoformat(),
                "inventory_id": None,
            }
        ),
    )
    return selected


def finish_inventory_session(
    cache_dir: str | Path,
    manifest: CorpusInventoryManifest,
) -> None:
    """Finish a snapshot, retaining its session only for retryable index gaps."""

    retryable = any(gap.retryable for gap in manifest.gaps)
    _atomic_private_write(
        Path(cache_dir) / "inventory-session.json",
        canonical_json(
            {
                "schema_version": 1,
                "state": "running" if retryable else "completed",
                "inventory_as_of": manifest.inventory_as_of.astimezone(UTC).isoformat(),
                "inventory_id": manifest.inventory_id,
            }
        ),
    )


def _item(
    term: int,
    evidence_kind: CorpusEvidenceKind,
    identifier: str,
    kind: OfficialDocumentKind,
    url: str,
    evidence_types: tuple[EvidenceType, ...],
    bills: tuple[str, ...],
    *,
    title: str = "",
    document_date: date | None = None,
    committee: str = "",
) -> CorpusInventoryItem:
    _validate_public_official_url(url)
    return CorpusInventoryItem(
        assembly_term=term,
        evidence_kind=evidence_kind,
        official_identifier=identifier,
        work_item=DocumentWorkItem.create(
            kind,
            url,
            evidence_types=evidence_types,
            related_bill_numbers=bills,
        ),
        title=title or kind.value,
        document_date=document_date,
        committee=committee,
    )


def _merge_inventory_items(
    items: Iterable[CorpusInventoryItem],
    gaps: list[CorpusInventoryGap] | tuple[CorpusInventoryGap, ...],
) -> tuple[CorpusInventoryItem, ...]:
    by_identity: dict[str, CorpusInventoryItem] = {}
    conflicted: set[str] = set()
    mutable_gaps = gaps if isinstance(gaps, list) else None
    for item in items:
        identity_id = item.identity.identity_id
        if identity_id in conflicted:
            continue
        existing = by_identity.get(identity_id)
        if existing is None:
            by_identity[identity_id] = item
            continue
        if (
            existing.work_item.kind is not item.work_item.kind
            or existing.work_item.official_url != item.work_item.official_url
        ):
            if mutable_gaps is not None:
                mutable_gaps.append(
                    _gap(
                        item.assembly_term,
                        item.evidence_kind,
                        f"inventory:identity-conflict:{identity_id}",
                        "inventory_identity_conflict",
                        item.official_identifier,
                        False,
                    )
                )
            by_identity.pop(identity_id, None)
            conflicted.add(identity_id)
            continue
        bills = tuple(
            sorted(
                set(existing.work_item.related_bill_numbers)
                | set(item.work_item.related_bill_numbers)
            )
        )
        merged_work = DocumentWorkItem.create(
            item.work_item.kind,
            item.work_item.official_url,
            evidence_types=tuple(
                dict.fromkeys(existing.work_item.evidence_types + item.work_item.evidence_types)
            ),
            related_bill_numbers=bills,
        )
        by_identity[identity_id] = CorpusInventoryItem(
            item.assembly_term,
            item.evidence_kind,
            item.official_identifier,
            merged_work,
            title=existing.title or item.title,
            document_date=existing.document_date or item.document_date,
            committee=existing.committee or item.committee,
        )
    return tuple(
        sorted(
            by_identity.values(),
            key=lambda item: (item.identity.identity_id, item.work_item.work_id),
        )
    )


def _bill_index_gap_pair(
    term: int,
    bill_number: str,
    reason: str,
    retryable: bool,
) -> tuple[CorpusInventoryGap, CorpusInventoryGap]:
    return (
        _gap(
            term,
            CorpusEvidenceKind.BILL_ORIGINAL,
            f"inventory:bill:{bill_number}:original",
            reason,
            f"bill:{bill_number}:original",
            retryable,
        ),
        _gap(
            term,
            CorpusEvidenceKind.REVIEW_REPORT,
            f"inventory:bill:{bill_number}:review-index",
            reason,
            None,
            retryable,
        ),
    )


def _metadata_gap_pair(
    term: int,
    reason: str,
    identifier: str | None,
) -> tuple[CorpusInventoryGap, CorpusInventoryGap]:
    suffix = identifier or "unknown"
    return (
        _gap(
            term,
            CorpusEvidenceKind.BILL_ORIGINAL,
            f"inventory:bill-metadata:{suffix}:original",
            reason,
            None,
            False,
        ),
        _gap(
            term,
            CorpusEvidenceKind.REVIEW_REPORT,
            f"inventory:bill-metadata:{suffix}:review",
            reason,
            None,
            False,
        ),
    )


def _gap(
    term: int,
    kind: CorpusEvidenceKind,
    key: str,
    reason: str,
    identifier: str | None,
    retryable: bool,
) -> CorpusInventoryGap:
    return CorpusInventoryGap(
        term,
        kind,
        CorpusIngestionFailure(key, reason, identifier),
        retryable,
    )


def _bill_id(row: Mapping[str, Any]) -> str | None:
    direct = str(row.get("BILL_ID") or "").strip()
    if direct:
        return direct if _BILL_ID.fullmatch(direct) else None
    for field in ("DETAIL_LINK", "LINK_URL"):
        raw = str(row.get(field) or "").strip()
        if not raw:
            continue
        try:
            parsed = urllib.parse.urlsplit(raw)
        except ValueError:
            continue
        if parsed.scheme != "https" or parsed.hostname != "likms.assembly.go.kr":
            continue
        candidate = urllib.parse.parse_qs(parsed.query).get("billId", [None])[0]
        if candidate and _BILL_ID.fullmatch(candidate):
            return candidate
    return None


def _minutes_identifier(url: str) -> str:
    _validate_public_official_url(url)
    parsed = urllib.parse.urlsplit(url)
    values = urllib.parse.parse_qs(parsed.query).get("id", [])
    if len(values) != 1 or not _SAFE_IDENTIFIER_PART.fullmatch(values[0]):
        raise ValueError("minutes URL lacks one exact official id")
    return f"minutes:{values[0]}"


def _review_identifier(url: str) -> str:
    _validate_public_official_url(url)
    return "review:url-sha256:" + hashlib.sha256(url.encode()).hexdigest()


def _related_bill_numbers(row: Mapping[str, Any], term: int) -> tuple[str, ...]:
    found: set[str] = set()
    agenda_items = row.get("agenda_items")
    if isinstance(agenda_items, (list, tuple)):
        for item in agenda_items:
            if isinstance(item, Mapping):
                for value in item.values():
                    found.update(_BILL_NO.findall(str(value or "")))
    for field in ("BILL_NO", "BILL_NUM", "BILL_NUMBER", "SUB_NAME", "AGENDA_NAME", "TITLE"):
        found.update(_BILL_NO.findall(str(row.get(field) or "")))
    return tuple(sorted(number for number in found if int(number[:2]) == term))


def _row_date(row: Mapping[str, Any], fields: tuple[str, ...]) -> date | None:
    for field in fields:
        raw = str(row.get(field) or "").strip()
        if not raw:
            continue
        compact = re.sub(r"[^0-9]", "", raw)[:8]
        if len(compact) != 8:
            continue
        try:
            return datetime.strptime(compact, "%Y%m%d").date()
        except ValueError:
            continue
    return None


def _row_text(row: Mapping[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = " ".join(str(row.get(field) or "").split())
        if value:
            return value[:500]
    return ""


def _months(date_from: date, date_to: date) -> tuple[str, ...]:
    cursor = date(date_from.year, date_from.month, 1)
    values: list[str] = []
    while cursor <= date_to:
        values.append(cursor.strftime("%Y-%m"))
        last = calendar.monthrange(cursor.year, cursor.month)[1]
        cursor = date(cursor.year, cursor.month, last) + timedelta(days=1)
    return tuple(values)


def _validate_public_official_url(value: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(value)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError as exc:
        raise ValueError("official document URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _OFFICIAL_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or bool(parsed.fragment)
    ):
        raise ValueError("official document URL is not a credential-free Assembly HTTPS URL")
    for raw_name, _value in query:
        name = re.sub(r"[^a-z0-9]+", "_", raw_name.casefold()).strip("_")
        if (
            name in _SENSITIVE_QUERY_NAMES
            or name.endswith("_token")
            or name.endswith("_api_key")
            or name.endswith("_secret")
        ):
            raise ValueError("official document URL contains a credential parameter")


def _read_bill_record(path: Path) -> _BillIndexRecord:
    payload = decode_canonical_json(path.read_bytes())
    if not isinstance(payload, dict):
        raise ValueError("bill-index cache record must be an object")
    return _BillIndexRecord.from_dict(payload)


def _atomic_private_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return value


def _text(
    payload: Mapping[str, Any],
    field: str,
    *,
    allow_empty: bool = False,
) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{field} must be a string")
    return value


def _integer(payload: Mapping[str, Any], field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    return value


def _boolean(payload: Mapping[str, Any], field: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _sha256(value: str, field: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


__all__ = [
    "CorpusInventoryCoverage",
    "CorpusInventoryGap",
    "CorpusInventoryItem",
    "CorpusInventoryManifest",
    "OpenAssemblyCorpusInventorySource",
    "finish_inventory_session",
    "pin_inventory_session",
    "read_inventory_manifest",
    "write_inventory_manifest",
]
