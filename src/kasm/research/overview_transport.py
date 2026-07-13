"""Bounded storage and response contracts for final research overviews.

The overview inventory remains the authority for lossless evidence accounting.
This module turns it into a small manifest plus immutable entity-catalog
shards.  Group descriptors never repeat their member evidence ids, and full
source text is included only when it is short and complete.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from .contracts import EvidenceCoverage, EvidenceType
from .overview import (
    CoreEvidence,
    EvidenceTypeCount,
    OverviewEntityGroup,
    OverviewEntityType,
    OverviewStatus,
    ResearchOverview,
    build_research_overview,
)
from .results import (
    PUBLIC_INLINE_TEXT_CHARACTERS,
    EvidenceCitation,
    EvidenceRecord,
    ResearchSnapshot,
)

MAX_OVERVIEW_GROUPS_PER_SHARD = 100
MAX_OVERVIEW_LABEL_CHARACTERS = 300

_CATALOG_TYPES = (
    OverviewEntityType.BILL,
    OverviewEntityType.MEETING,
    OverviewEntityType.DOCUMENT,
)
_ENTITY_ORDER = {entity_type: rank for rank, entity_type in enumerate(_CATALOG_TYPES)}
_GROUP_RECORD_PRIORITY: dict[OverviewEntityType, tuple[EvidenceType, ...]] = {
    OverviewEntityType.BILL: (
        EvidenceType.BILLS,
        EvidenceType.BILL_STATUS,
        EvidenceType.BILL_TEXT,
        EvidenceType.REVIEW_REPORTS,
        EvidenceType.AGENDAS,
        EvidenceType.SPEECHES,
        EvidenceType.GOVERNMENT_RESPONSES,
        EvidenceType.SPEECH_CONTEXT,
        EvidenceType.SUBCOMMITTEE_MINUTES,
    ),
    OverviewEntityType.MEETING: (
        EvidenceType.AGENDAS,
        EvidenceType.SUBCOMMITTEE_MINUTES,
        EvidenceType.SPEECHES,
        EvidenceType.GOVERNMENT_RESPONSES,
        EvidenceType.SPEECH_CONTEXT,
        EvidenceType.BILLS,
        EvidenceType.BILL_STATUS,
        EvidenceType.BILL_TEXT,
        EvidenceType.REVIEW_REPORTS,
    ),
    OverviewEntityType.DOCUMENT: (
        EvidenceType.BILL_TEXT,
        EvidenceType.REVIEW_REPORTS,
        EvidenceType.SUBCOMMITTEE_MINUTES,
        EvidenceType.SPEECH_CONTEXT,
        EvidenceType.SPEECHES,
        EvidenceType.GOVERNMENT_RESPONSES,
        EvidenceType.AGENDAS,
        EvidenceType.BILLS,
        EvidenceType.BILL_STATUS,
    ),
}


@dataclass(frozen=True, slots=True)
class OverviewCoreRoute:
    """A compact route to one core evidence record."""

    rank: int
    evidence_id: str
    evidence_type: EvidenceType
    reasons: tuple[str, ...]
    title: str
    title_complete: bool
    citation: EvidenceCitation
    text_characters: int
    text_hash: str
    text_inline_complete: bool
    inline_text: str | None = None

    def __post_init__(self) -> None:
        if self.rank < 1 or not self.evidence_id or not self.title or not self.reasons:
            raise ValueError("overview core route identity is required")
        if self.text_characters < 1:
            raise ValueError("overview core text size must be positive")
        if len(self.text_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.text_hash
        ):
            raise ValueError("overview core text hash is invalid")
        if self.text_inline_complete != (self.inline_text is not None):
            raise ValueError("overview core inline-text accounting is inconsistent")
        if self.inline_text is not None:
            if len(self.inline_text) != self.text_characters:
                raise ValueError("overview core inline text size does not match")
            if hashlib.sha256(self.inline_text.encode()).hexdigest() != self.text_hash:
                raise ValueError("overview core inline text hash does not match")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "rank": self.rank,
            "evidence_id": self.evidence_id,
            "evidence_type": self.evidence_type.value,
            "reasons": list(self.reasons),
            "title": self.title,
            "title_complete": self.title_complete,
            "citation": self.citation.to_dict(),
            "text_characters": self.text_characters,
            "text_hash": self.text_hash,
            "text_inline_complete": self.text_inline_complete,
        }
        if self.inline_text is not None:
            payload["text"] = self.inline_text
        else:
            payload["text_delivery"] = "get_evidence_document"
        return payload


@dataclass(frozen=True, slots=True)
class OverviewCoverageAxisSummary:
    """Bounded coverage facts; detailed gap strings stay in the snapshot."""

    evidence_type: EvidenceType
    candidate_total: int | None
    checked_count: int
    matched_count: int
    failed_count: int
    pending_count: int
    complete: bool
    gap_reason_count: int
    gap_reasons_hash: str

    @classmethod
    def from_coverage(cls, coverage: EvidenceCoverage) -> OverviewCoverageAxisSummary:
        return cls(
            evidence_type=coverage.evidence_type,
            candidate_total=coverage.candidate_total,
            checked_count=coverage.checked_count,
            matched_count=coverage.matched_count,
            failed_count=coverage.failed_count,
            pending_count=coverage.pending_count,
            complete=coverage.complete,
            gap_reason_count=len(coverage.gap_reasons),
            gap_reasons_hash=_payload_hash(list(coverage.gap_reasons)),
        )

    def __post_init__(self) -> None:
        if min(
            self.checked_count,
            self.matched_count,
            self.failed_count,
            self.pending_count,
            self.gap_reason_count,
        ) < 0:
            raise ValueError("overview coverage counts must not be negative")
        if len(self.gap_reasons_hash) != 64:
            raise ValueError("overview coverage gap hash is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_type": self.evidence_type.value,
            "candidate_total": self.candidate_total,
            "checked_count": self.checked_count,
            "matched_count": self.matched_count,
            "failed_count": self.failed_count,
            "pending_count": self.pending_count,
            "complete": self.complete,
            "gap_reason_count": self.gap_reason_count,
            "gap_reasons_hash": self.gap_reasons_hash,
            "gap_details_delivery": (
                None if self.gap_reason_count == 0 else "get_research_page"
            ),
        }


@dataclass(frozen=True, slots=True)
class OverviewGroupDescriptor:
    """Compact entity facts with no repeated member evidence-id list."""

    entity_type: OverviewEntityType
    entity_id: str
    display_label: str
    display_label_complete: bool
    primary_official_url: str
    evidence_count: int
    evidence_type_counts: tuple[EvidenceTypeCount, ...]
    date_from: date | None
    date_to: date | None
    undated_count: int

    def __post_init__(self) -> None:
        if self.entity_type not in _CATALOG_TYPES:
            raise ValueError("speech groups do not belong in the top-level catalog")
        if not self.entity_id or not self.display_label or not self.primary_official_url:
            raise ValueError("overview group descriptor identity is required")
        if self.evidence_count < 1 or self.undated_count < 0:
            raise ValueError("overview group descriptor counts are invalid")
        if self.undated_count > self.evidence_count:
            raise ValueError("overview group has too many undated records")
        if sum(item.count for item in self.evidence_type_counts) != self.evidence_count:
            raise ValueError("overview group type counts are inconsistent")
        if (self.date_from is None) != (self.date_to is None):
            raise ValueError("overview group date bounds are inconsistent")
        if (
            self.date_from is not None
            and self.date_to is not None
            and self.date_from > self.date_to
        ):
            raise ValueError("overview group date bounds are inverted")

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type.value,
            "entity_id": self.entity_id,
            "display_label": self.display_label,
            "display_label_complete": self.display_label_complete,
            "primary_official_url": self.primary_official_url,
            "evidence_count": self.evidence_count,
            "evidence_type_counts": [
                item.to_dict() for item in self.evidence_type_counts
            ],
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "undated_count": self.undated_count,
        }


@dataclass(frozen=True, slots=True)
class OverviewGroupShard:
    """One immutable bounded slice of the entity catalog."""

    number: int
    start_position: int
    groups: tuple[OverviewGroupDescriptor, ...]

    def __post_init__(self) -> None:
        if self.number < 0 or self.start_position < 0 or not self.groups:
            raise ValueError("overview group shard identity is invalid")
        if len(self.groups) > MAX_OVERVIEW_GROUPS_PER_SHARD:
            raise ValueError("overview group shard exceeds 100 descriptors")
        if tuple(sorted(self.groups, key=_group_key)) != self.groups:
            raise ValueError("overview group shard descriptors must be ordered")

    @property
    def end_position(self) -> int:
        return self.start_position + len(self.groups)

    @property
    def shard_hash(self) -> str:
        return _payload_hash([group.to_dict() for group in self.groups])

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "start_position": self.start_position,
            "end_position": self.end_position,
            "group_count": len(self.groups),
            "shard_hash": self.shard_hash,
            "groups": [group.to_dict() for group in self.groups],
        }


@dataclass(frozen=True, slots=True)
class OverviewGroupShardDescriptor:
    number: int
    start_position: int
    end_position: int
    first_entity_type: OverviewEntityType
    first_entity_id: str
    last_entity_type: OverviewEntityType
    last_entity_id: str
    shard_hash: str

    @classmethod
    def from_shard(cls, shard: OverviewGroupShard) -> OverviewGroupShardDescriptor:
        return cls(
            number=shard.number,
            start_position=shard.start_position,
            end_position=shard.end_position,
            first_entity_type=shard.groups[0].entity_type,
            first_entity_id=shard.groups[0].entity_id,
            last_entity_type=shard.groups[-1].entity_type,
            last_entity_id=shard.groups[-1].entity_id,
            shard_hash=shard.shard_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "start_position": self.start_position,
            "end_position": self.end_position,
            "group_count": self.end_position - self.start_position,
            "first": {
                "entity_type": self.first_entity_type.value,
                "entity_id": self.first_entity_id,
            },
            "last": {
                "entity_type": self.last_entity_type.value,
                "entity_id": self.last_entity_id,
            },
            "shard_hash": self.shard_hash,
        }


@dataclass(frozen=True, slots=True)
class OverviewEntityTotals:
    bills: int
    meetings: int
    documents: int
    speeches: int

    def __post_init__(self) -> None:
        if min(self.bills, self.meetings, self.documents, self.speeches) < 0:
            raise ValueError("overview entity totals must not be negative")

    @property
    def catalog_total(self) -> int:
        return self.bills + self.meetings + self.documents

    def to_dict(self) -> dict[str, int]:
        return {
            "bills": self.bills,
            "meetings": self.meetings,
            "documents": self.documents,
            "speeches": self.speeches,
            "catalog_total": self.catalog_total,
        }


@dataclass(frozen=True, slots=True)
class OverviewTransportManifest:
    """Small header and routing manifest; it contains no group member ids."""

    research_id: str
    query_fingerprint: str
    index_revision: str
    build_sha: str
    status: OverviewStatus
    coverage_requested: tuple[EvidenceType, ...]
    coverage_axes: tuple[OverviewCoverageAxisSummary, ...]
    provisional_reason_count: int
    provisional_reasons_hash: str
    evidence_count: int
    evidence_type_counts: tuple[EvidenceTypeCount, ...]
    date_from: date | None
    date_to: date | None
    undated_evidence_count: int
    unassigned_evidence_count: int
    entity_totals: OverviewEntityTotals
    core: tuple[OverviewCoreRoute, ...]
    shard_size: int
    shards: tuple[OverviewGroupShardDescriptor, ...]

    def __post_init__(self) -> None:
        if not self.research_id or not self.index_revision or not self.build_sha:
            raise ValueError("overview transport identity is required")
        if len(self.query_fingerprint) != 64:
            raise ValueError("overview transport query fingerprint is invalid")
        if len(set(self.coverage_requested)) != len(self.coverage_requested):
            raise ValueError("overview requested coverage axes must be unique")
        by_type = {axis.evidence_type: axis for axis in self.coverage_axes}
        if len(by_type) != len(self.coverage_axes) or any(
            evidence_type not in by_type for evidence_type in self.coverage_requested
        ):
            raise ValueError("overview coverage summaries are incomplete or duplicated")
        coverage_complete = all(
            by_type[evidence_type].complete
            for evidence_type in self.coverage_requested
        )
        if (self.status is OverviewStatus.COMPLETE) != coverage_complete:
            raise ValueError("overview transport status must match coverage")
        if self.provisional_reason_count < 0 or len(self.provisional_reasons_hash) != 64:
            raise ValueError("overview provisional reason accounting is invalid")
        if coverage_complete != (self.provisional_reason_count == 0):
            raise ValueError("overview provisional reasons must match coverage status")
        if self.evidence_count < 0:
            raise ValueError("overview evidence count must not be negative")
        if sum(item.count for item in self.evidence_type_counts) != self.evidence_count:
            raise ValueError("overview transport type counts are inconsistent")
        if not 1 <= self.shard_size <= MAX_OVERVIEW_GROUPS_PER_SHARD:
            raise ValueError("overview shard size must be between 1 and 100")
        if not 0 <= self.undated_evidence_count <= self.evidence_count:
            raise ValueError("overview undated evidence count is invalid")
        if not 0 <= self.unassigned_evidence_count <= self.evidence_count:
            raise ValueError("overview unassigned evidence count is invalid")
        position = 0
        for number, shard in enumerate(self.shards):
            if (
                shard.number != number
                or shard.start_position != position
                or shard.end_position <= shard.start_position
                or shard.end_position - shard.start_position > self.shard_size
            ):
                raise ValueError("overview shard coverage is not contiguous")
            position = shard.end_position
        if position != self.entity_totals.catalog_total:
            raise ValueError("overview shards do not account for every catalog group")

    @property
    def complete(self) -> bool:
        return self.status is OverviewStatus.COMPLETE

    def to_dict(self) -> dict[str, Any]:
        return {
            "research_id": self.research_id,
            "query_fingerprint": self.query_fingerprint,
            "index_revision": self.index_revision,
            "build_sha": self.build_sha,
            "status": self.status.value,
            "complete": self.complete,
            "coverage": {
                "requested": [item.value for item in self.coverage_requested],
                "complete": self.complete,
                "evidence": {
                    item.evidence_type.value: item.to_dict()
                    for item in self.coverage_axes
                },
            },
            "provisional_reason_count": self.provisional_reason_count,
            "provisional_reasons_hash": self.provisional_reasons_hash,
            "provisional_reason_details_delivery": (
                None
                if self.provisional_reason_count == 0
                else "get_research_page"
            ),
            "evidence_count": self.evidence_count,
            "evidence_type_counts": [
                item.to_dict() for item in self.evidence_type_counts
            ],
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "undated_evidence_count": self.undated_evidence_count,
            "unassigned_evidence_count": self.unassigned_evidence_count,
            "entity_totals": self.entity_totals.to_dict(),
            "speech_catalog_delivery": "evidence_result_pages",
            "core": [route.to_dict() for route in self.core],
            "shard_size": self.shard_size,
            "shards": [shard.to_dict() for shard in self.shards],
        }


@dataclass(frozen=True, slots=True)
class OverviewCatalogPageAccounting:
    total: int
    returned_count: int
    returned_through: int
    next_offset: int | None

    def __post_init__(self) -> None:
        if min(self.total, self.returned_count, self.returned_through) < 0:
            raise ValueError("overview page accounting must not be negative")
        if self.returned_through > self.total or self.returned_count > self.returned_through:
            raise ValueError("overview page accounting exceeds its catalog")
        if self.next_offset is None:
            if self.returned_through < self.total:
                raise ValueError("overview page omitted a required next offset")
        elif self.next_offset != self.returned_through or self.next_offset >= self.total:
            raise ValueError("overview page next offset is invalid")

    @property
    def complete(self) -> bool:
        return self.next_offset is None

    def to_dict(self) -> dict[str, int | bool | None]:
        return {
            "total": self.total,
            "returned_count": self.returned_count,
            "returned_through": self.returned_through,
            "next_offset": self.next_offset,
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class OverviewCatalogPage:
    accounting: OverviewCatalogPageAccounting
    groups: tuple[OverviewGroupDescriptor, ...]

    def __post_init__(self) -> None:
        if len(self.groups) != self.accounting.returned_count:
            raise ValueError("overview page groups do not match its accounting")
        if len(self.groups) > MAX_OVERVIEW_GROUPS_PER_SHARD:
            raise ValueError("overview catalog response exceeds 100 groups")

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.accounting.to_dict(),
            "groups": [group.to_dict() for group in self.groups],
        }


@dataclass(frozen=True, slots=True)
class OverviewTransportBundle:
    manifest: OverviewTransportManifest
    shards: tuple[OverviewGroupShard, ...]

    def __post_init__(self) -> None:
        if len(self.shards) != len(self.manifest.shards):
            raise ValueError("overview bundle shard count does not match its manifest")
        for shard, descriptor in zip(self.shards, self.manifest.shards, strict=True):
            if OverviewGroupShardDescriptor.from_shard(shard) != descriptor:
                raise ValueError("overview bundle contains a changed or misplaced shard")

    def page(self, *, offset: int = 0, page_size: int = 100) -> OverviewCatalogPage:
        return overview_catalog_page(
            self.manifest,
            self.shards,
            offset=offset,
            page_size=page_size,
        )


def build_overview_transport(
    snapshot: ResearchSnapshot,
    overview: ResearchOverview | None = None,
    *,
    inline_text_characters: int = PUBLIC_INLINE_TEXT_CHARACTERS,
    shard_size: int = MAX_OVERVIEW_GROUPS_PER_SHARD,
) -> OverviewTransportBundle:
    """Build a bounded manifest and complete immutable group catalog."""

    if not 0 <= inline_text_characters <= PUBLIC_INLINE_TEXT_CHARACTERS:
        raise ValueError(
            f"inline_text_characters must be between 0 and {PUBLIC_INLINE_TEXT_CHARACTERS}"
        )
    if not 1 <= shard_size <= MAX_OVERVIEW_GROUPS_PER_SHARD:
        raise ValueError("shard_size must be between 1 and 100")
    overview = overview or build_research_overview(snapshot)
    _validate_overview_identity(snapshot, overview)
    record_by_id = {record.id: record for record in snapshot.evidence}
    core = tuple(
        _core_route(item, record_by_id, inline_text_characters)
        for item in overview.core
    )
    descriptors = tuple(
        sorted(
            (
                _group_descriptor(group, record_by_id)
                for group in (
                    *overview.inventory.bill_groups,
                    *overview.inventory.meeting_groups,
                    *overview.inventory.document_groups,
                )
            ),
            key=_group_key,
        )
    )
    shards = tuple(
        OverviewGroupShard(
            number=start // shard_size,
            start_position=start,
            groups=descriptors[start : start + shard_size],
        )
        for start in range(0, len(descriptors), shard_size)
    )
    totals = OverviewEntityTotals(
        bills=len(overview.inventory.bill_groups),
        meetings=len(overview.inventory.meeting_groups),
        documents=len(overview.inventory.document_groups),
        speeches=len(overview.inventory.speech_groups),
    )
    manifest = OverviewTransportManifest(
        research_id=overview.research_id,
        query_fingerprint=overview.query_fingerprint,
        index_revision=overview.index_revision,
        build_sha=overview.build_sha,
        status=overview.status,
        coverage_requested=overview.coverage.requested,
        coverage_axes=tuple(
            OverviewCoverageAxisSummary.from_coverage(entry)
            for entry in overview.coverage.entries
            if entry.evidence_type in overview.coverage.requested
        ),
        provisional_reason_count=len(overview.provisional_reasons),
        provisional_reasons_hash=_payload_hash(list(overview.provisional_reasons)),
        evidence_count=overview.inventory.evidence_count,
        evidence_type_counts=overview.inventory.evidence_type_counts,
        date_from=overview.inventory.date_from,
        date_to=overview.inventory.date_to,
        undated_evidence_count=len(overview.inventory.undated_evidence_ids),
        unassigned_evidence_count=len(overview.inventory.unassigned_evidence_ids),
        entity_totals=totals,
        core=core,
        shard_size=shard_size,
        shards=tuple(OverviewGroupShardDescriptor.from_shard(shard) for shard in shards),
    )
    return OverviewTransportBundle(manifest, shards)


def overview_catalog_page(
    manifest: OverviewTransportManifest,
    shards: Sequence[OverviewGroupShard],
    *,
    offset: int = 0,
    page_size: int = 100,
) -> OverviewCatalogPage:
    """Return one bounded page while loading only its overlapping shards."""

    if not 1 <= page_size <= MAX_OVERVIEW_GROUPS_PER_SHARD:
        raise ValueError("page_size must be between 1 and 100")
    total = manifest.entity_totals.catalog_total
    if offset < 0 or offset > total:
        raise ValueError("overview catalog offset is outside the inventory")
    required = overview_catalog_required_shards(
        manifest,
        offset=offset,
        page_size=page_size,
    )
    by_number: dict[int, OverviewGroupShard] = {}
    for shard in shards:
        if shard.number in by_number:
            raise ValueError("overview catalog supplied a duplicate shard")
        if shard.number >= len(manifest.shards):
            raise ValueError("overview catalog supplied an unknown shard")
        descriptor = manifest.shards[shard.number]
        if OverviewGroupShardDescriptor.from_shard(shard) != descriptor:
            raise ValueError("overview catalog shard identity does not match")
        by_number[shard.number] = shard
    missing = tuple(
        descriptor.number
        for descriptor in required
        if descriptor.number not in by_number
    )
    if missing:
        raise ValueError("overview catalog required shards are incomplete")
    stop = min(total, offset + page_size)
    groups = tuple(
        group
        for descriptor in required
        for shard in (by_number[descriptor.number],)
        for position, group in enumerate(shard.groups, start=shard.start_position)
        if offset <= position < stop
    )
    returned_through = offset + len(groups)
    accounting = OverviewCatalogPageAccounting(
        total=total,
        returned_count=len(groups),
        returned_through=returned_through,
        next_offset=(returned_through if returned_through < total else None),
    )
    return OverviewCatalogPage(accounting, groups)


def overview_catalog_required_shards(
    manifest: OverviewTransportManifest,
    *,
    offset: int = 0,
    page_size: int = 100,
) -> tuple[OverviewGroupShardDescriptor, ...]:
    """Return the immutable shard descriptors needed for one response page."""

    if not 1 <= page_size <= MAX_OVERVIEW_GROUPS_PER_SHARD:
        raise ValueError("page_size must be between 1 and 100")
    total = manifest.entity_totals.catalog_total
    if offset < 0 or offset > total:
        raise ValueError("overview catalog offset is outside the inventory")
    stop = min(total, offset + page_size)
    return tuple(
        descriptor
        for descriptor in manifest.shards
        if descriptor.end_position > offset and descriptor.start_position < stop
    )


def _validate_overview_identity(
    snapshot: ResearchSnapshot,
    overview: ResearchOverview,
) -> None:
    if (
        overview.research_id != snapshot.research_id
        or overview.query_fingerprint != snapshot.query_fingerprint
        or overview.index_revision != snapshot.index_revision
        or overview.build_sha != snapshot.build_sha
        or overview.coverage != snapshot.coverage
        or set(overview.inventory.evidence_ids)
        != {record.id for record in snapshot.evidence}
    ):
        raise ValueError("research overview does not belong to this immutable snapshot")


def _core_route(
    core: CoreEvidence,
    record_by_id: Mapping[str, EvidenceRecord],
    inline_text_characters: int,
) -> OverviewCoreRoute:
    try:
        record = record_by_id[core.evidence_id]
    except KeyError:
        raise ValueError("core evidence is absent from its immutable snapshot") from None
    title, title_complete = _bounded_label(record.title, record.id)
    inline = record.text if len(record.text) <= inline_text_characters else None
    return OverviewCoreRoute(
        rank=core.rank,
        evidence_id=record.id,
        evidence_type=record.evidence_type,
        reasons=core.reasons,
        title=title,
        title_complete=title_complete,
        citation=record.citation,
        text_characters=len(record.text),
        text_hash=record.text_hash,
        text_inline_complete=inline is not None,
        inline_text=inline,
    )


def _group_descriptor(
    group: OverviewEntityGroup,
    record_by_id: Mapping[str, EvidenceRecord],
) -> OverviewGroupDescriptor:
    records: list[EvidenceRecord] = []
    for evidence_id in group.evidence_ids:
        try:
            records.append(record_by_id[evidence_id])
        except KeyError:
            raise ValueError("overview group references evidence outside its snapshot") from None
    priority = {
        evidence_type: rank
        for rank, evidence_type in enumerate(_GROUP_RECORD_PRIORITY[group.entity_type])
    }
    primary = min(
        records,
        key=lambda record: (
            priority.get(record.evidence_type, len(priority)),
            record.sort_key,
            record.id,
        ),
    )
    label, label_complete = _bounded_label(primary.title, group.entity_id)
    return OverviewGroupDescriptor(
        entity_type=group.entity_type,
        entity_id=group.entity_id,
        display_label=label,
        display_label_complete=label_complete,
        primary_official_url=primary.citation.official_url,
        evidence_count=group.evidence_count,
        evidence_type_counts=group.evidence_type_counts,
        date_from=group.date_from,
        date_to=group.date_to,
        undated_count=len(group.undated_evidence_ids),
    )


def _bounded_label(value: str, fallback: str) -> tuple[str, bool]:
    if len(value) <= MAX_OVERVIEW_LABEL_CHARACTERS:
        return value, True
    return fallback, False


def _group_key(group: OverviewGroupDescriptor) -> tuple[int, str]:
    return _ENTITY_ORDER[group.entity_type], group.entity_id


def _payload_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "MAX_OVERVIEW_GROUPS_PER_SHARD",
    "MAX_OVERVIEW_LABEL_CHARACTERS",
    "OverviewCatalogPage",
    "OverviewCatalogPageAccounting",
    "OverviewCoreRoute",
    "OverviewCoverageAxisSummary",
    "OverviewEntityTotals",
    "OverviewGroupDescriptor",
    "OverviewGroupShard",
    "OverviewGroupShardDescriptor",
    "OverviewTransportBundle",
    "OverviewTransportManifest",
    "build_overview_transport",
    "overview_catalog_page",
    "overview_catalog_required_shards",
]
