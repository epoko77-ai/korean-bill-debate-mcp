"""Deterministic core-first overview over a complete research snapshot.

This module is deliberately an inventory and routing layer, not a summarizer.
It never rewrites or drops evidence text.  Every :class:`EvidenceRecord` is
represented by its exact public id in the inventory, while a small diversified
core points callers to the first records worth opening.  Entity grouping uses
only explicit metadata identifiers or the canonical public speech-record id;
titles and text are never used to guess relationships.
"""

from __future__ import annotations

import re
import urllib.parse
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from .collector import MetadataKind
from .contracts import CoverageLedger, EvidenceType
from .engine import DiscoveryStageState
from .resolver import CandidateDecision, CandidateSetResolution, MetadataResolution
from .results import EvidenceRecord, ResearchSnapshot
from .source_availability import (
    OfficialSourceAvailability,
    summarize_source_availability,
)

_BILL_NUMBER = re.compile(r"\d{7}")
_SPEECH_RECORD_PREFIX = "evidence:speech:"
_DEFAULT_CORE_LIMIT = 12
_MAX_CORE_LIMIT = 50

_CORE_ORDER = (
    EvidenceType.BILLS,
    EvidenceType.BILL_STATUS,
    EvidenceType.BILL_TEXT,
    EvidenceType.REVIEW_REPORTS,
    EvidenceType.SUBCOMMITTEE_MINUTES,
    EvidenceType.AGENDAS,
    EvidenceType.SPEECHES,
    EvidenceType.GOVERNMENT_RESPONSES,
    EvidenceType.SPEECH_CONTEXT,
)
_CORE_PRIORITY = {evidence_type: rank for rank, evidence_type in enumerate(_CORE_ORDER)}
_CORE_REASON = {
    EvidenceType.BILLS: "bill_identity",
    EvidenceType.BILL_STATUS: "current_bill_status",
    EvidenceType.BILL_TEXT: "original_bill_text",
    EvidenceType.REVIEW_REPORTS: "committee_review",
    EvidenceType.SUBCOMMITTEE_MINUTES: "subcommittee_deliberation",
    EvidenceType.AGENDAS: "formal_agenda",
    EvidenceType.SPEECHES: "source_statement",
    EvidenceType.GOVERNMENT_RESPONSES: "government_response",
    EvidenceType.SPEECH_CONTEXT: "speech_relationship_context",
}


class OverviewStatus(StrEnum):
    """Whether all requested coverage axes are complete."""

    COMPLETE = "complete"
    PROVISIONAL = "provisional"


class OverviewEntityType(StrEnum):
    """Exact entity identifiers available in finalized evidence metadata."""

    BILL = "bill"
    MEETING = "meeting"
    DOCUMENT = "document"
    SPEECH = "speech"


@dataclass(frozen=True, slots=True)
class EvidenceTypeCount:
    evidence_type: EvidenceType
    count: int

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError("evidence type count must be positive")

    def to_dict(self) -> dict[str, str | int]:
        return {"evidence_type": self.evidence_type.value, "count": self.count}


@dataclass(frozen=True, slots=True)
class OverviewEntityGroup:
    """All evidence ids bound to one exact entity identifier."""

    entity_type: OverviewEntityType
    entity_id: str
    evidence_ids: tuple[str, ...]
    evidence_type_counts: tuple[EvidenceTypeCount, ...]
    date_from: date | None
    date_to: date | None
    undated_evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.entity_id.strip() or not self.evidence_ids:
            raise ValueError("overview entity groups require an id and evidence")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("overview entity group evidence ids must be unique")
        if not set(self.undated_evidence_ids) <= set(self.evidence_ids):
            raise ValueError("undated evidence must belong to its entity group")
        if (self.date_from is None) != (self.date_to is None):
            raise ValueError("entity group date bounds must both be present or absent")
        if (
            self.date_from is not None
            and self.date_to is not None
            and self.date_from > self.date_to
        ):
            raise ValueError("entity group date bounds are inverted")
        if sum(item.count for item in self.evidence_type_counts) != len(self.evidence_ids):
            raise ValueError("entity group type counts must account for every evidence id")

    @property
    def evidence_count(self) -> int:
        return len(self.evidence_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type.value,
            "entity_id": self.entity_id,
            "evidence_count": self.evidence_count,
            "evidence_ids": list(self.evidence_ids),
            "evidence_type_counts": [item.to_dict() for item in self.evidence_type_counts],
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "undated_evidence_ids": list(self.undated_evidence_ids),
        }


@dataclass(frozen=True, slots=True)
class EvidenceInventory:
    """A lossless map accounting for every public evidence record id."""

    evidence_ids: tuple[str, ...]
    evidence_type_counts: tuple[EvidenceTypeCount, ...]
    date_from: date | None
    date_to: date | None
    undated_evidence_ids: tuple[str, ...]
    bill_groups: tuple[OverviewEntityGroup, ...]
    meeting_groups: tuple[OverviewEntityGroup, ...]
    document_groups: tuple[OverviewEntityGroup, ...]
    speech_groups: tuple[OverviewEntityGroup, ...]
    unassigned_evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        evidence = set(self.evidence_ids)
        if len(evidence) != len(self.evidence_ids):
            raise ValueError("overview inventory evidence ids must be unique")
        if sum(item.count for item in self.evidence_type_counts) != len(self.evidence_ids):
            raise ValueError("inventory type counts must account for every evidence id")
        if not set(self.undated_evidence_ids) <= evidence:
            raise ValueError("inventory undated ids must belong to the snapshot")
        if not set(self.unassigned_evidence_ids) <= evidence:
            raise ValueError("unassigned ids must belong to the snapshot")
        if (self.date_from is None) != (self.date_to is None):
            raise ValueError("inventory date bounds must both be present or absent")
        if (
            self.date_from is not None
            and self.date_to is not None
            and self.date_from > self.date_to
        ):
            raise ValueError("inventory date bounds are inverted")
        grouped: set[str] = set()
        for expected_type, groups in (
            (OverviewEntityType.BILL, self.bill_groups),
            (OverviewEntityType.MEETING, self.meeting_groups),
            (OverviewEntityType.DOCUMENT, self.document_groups),
            (OverviewEntityType.SPEECH, self.speech_groups),
        ):
            entity_ids = [group.entity_id for group in groups]
            if len(entity_ids) != len(set(entity_ids)):
                raise ValueError("overview entity group ids must be unique by type")
            if tuple(sorted(entity_ids)) != tuple(entity_ids):
                raise ValueError("overview entity groups must be ordered by id")
            for group in groups:
                if group.entity_type is not expected_type:
                    raise ValueError("overview group is stored under the wrong entity type")
                if not set(group.evidence_ids) <= evidence:
                    raise ValueError("entity group contains evidence outside the snapshot")
                grouped.update(group.evidence_ids)
        if grouped | set(self.unassigned_evidence_ids) != evidence:
            raise ValueError("overview inventory does not account for every evidence id")
        if grouped & set(self.unassigned_evidence_ids):
            raise ValueError("assigned evidence cannot also be unassigned")

    @property
    def evidence_count(self) -> int:
        return len(self.evidence_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_count": self.evidence_count,
            "evidence_ids": list(self.evidence_ids),
            "evidence_type_counts": [item.to_dict() for item in self.evidence_type_counts],
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "undated_evidence_ids": list(self.undated_evidence_ids),
            "groups": {
                "bills": [item.to_dict() for item in self.bill_groups],
                "meetings": [item.to_dict() for item in self.meeting_groups],
                "documents": [item.to_dict() for item in self.document_groups],
                "speeches": [item.to_dict() for item in self.speech_groups],
            },
            "unassigned_evidence_ids": list(self.unassigned_evidence_ids),
        }


@dataclass(frozen=True, slots=True)
class CoreEvidence:
    """One deterministic routing choice for the compact core."""

    rank: int
    evidence_id: str
    evidence_type: EvidenceType
    reasons: tuple[str, ...]
    entity_bindings: tuple[tuple[OverviewEntityType, str], ...] = ()

    def __post_init__(self) -> None:
        if self.rank < 1 or not self.evidence_id.strip() or not self.reasons:
            raise ValueError("core evidence requires rank, id, and explicit reasons")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("core evidence reasons must be unique")
        if len(set(self.entity_bindings)) != len(self.entity_bindings):
            raise ValueError("core evidence entity bindings must be unique")

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "evidence_id": self.evidence_id,
            "evidence_type": self.evidence_type.value,
            "reasons": list(self.reasons),
            "entity_bindings": [
                {"entity_type": entity_type.value, "entity_id": entity_id}
                for entity_type, entity_id in self.entity_bindings
            ],
        }


@dataclass(frozen=True, slots=True)
class ResearchOverview:
    """Small diversified core plus a complete evidence-id inventory."""

    research_id: str
    query_fingerprint: str
    index_revision: str
    build_sha: str
    status: OverviewStatus
    coverage: CoverageLedger
    provisional_reasons: tuple[str, ...]
    core: tuple[CoreEvidence, ...]
    inventory: EvidenceInventory

    def __post_init__(self) -> None:
        if not self.research_id or not self.index_revision or not self.build_sha:
            raise ValueError("research overview identity is required")
        if len(self.query_fingerprint) != 64:
            raise ValueError("research overview query fingerprint is invalid")
        if (self.status is OverviewStatus.COMPLETE) != self.coverage.complete:
            raise ValueError("overview status must match coverage completeness")
        if self.coverage.complete and self.provisional_reasons:
            raise ValueError("complete overview cannot contain provisional reasons")
        if not self.coverage.complete and not self.provisional_reasons:
            raise ValueError("provisional overview requires explicit reasons")
        ranks = tuple(item.rank for item in self.core)
        if ranks != tuple(range(1, len(self.core) + 1)):
            raise ValueError("core evidence ranks must be contiguous")
        core_ids = tuple(item.evidence_id for item in self.core)
        if len(core_ids) != len(set(core_ids)):
            raise ValueError("core evidence ids must be unique")
        if not set(core_ids) <= set(self.inventory.evidence_ids):
            raise ValueError("core evidence must belong to the complete inventory")

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
            "coverage": self.coverage.to_dict(),
            "provisional_reasons": list(self.provisional_reasons),
            "core": [item.to_dict() for item in self.core],
            "inventory": self.inventory.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ProvisionalCandidateEntry:
    """One accepted metadata candidate, without a substantive conclusion."""

    position: int
    kind: MetadataKind
    candidate_id: str
    score: int
    match_reasons: tuple[str, ...]
    exact_identifiers: tuple[tuple[str, str], ...]
    label: str | None = None

    def __post_init__(self) -> None:
        if self.position < 0 or not self.candidate_id.strip():
            raise ValueError("provisional candidate position and id are required")
        if self.score < 0:
            raise ValueError("provisional candidate score must not be negative")
        if not self.match_reasons:
            raise ValueError("accepted provisional candidates require match reasons")
        names = tuple(name for name, _value in self.exact_identifiers)
        if (
            not self.exact_identifiers
            or len(names) != len(set(names))
            or any(not name or not value for name, value in self.exact_identifiers)
        ):
            raise ValueError("provisional candidates require exact unique identifiers")

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "kind": self.kind.value,
            "candidate_id": self.candidate_id,
            "score": self.score,
            "match_reasons": list(self.match_reasons),
            "exact_identifiers": dict(self.exact_identifiers),
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class ProvisionalFamilyAccounting:
    kind: MetadataKind
    total_candidates: int
    accepted_count: int
    rejected_count: int
    rejection_reason_counts: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if min(self.total_candidates, self.accepted_count, self.rejected_count) < 0:
            raise ValueError("provisional family counts must not be negative")
        if self.accepted_count + self.rejected_count != self.total_candidates:
            raise ValueError("provisional family accounting is inconsistent")
        if any(not reason or count < 1 for reason, count in self.rejection_reason_counts):
            raise ValueError("provisional rejection accounting is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "total_candidates": self.total_candidates,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "rejection_reason_counts": dict(self.rejection_reason_counts),
        }


@dataclass(frozen=True, slots=True)
class ProvisionalSourceAccounting:
    """Collection facts available only when a DiscoveryStageState is present."""

    source_complete: bool | None
    source_rows_expected: int | None
    source_rows_fetched: int | None
    bills_collected: int | None
    bills_after_strict_filter: int | None
    meetings_collected: int | None
    meetings_after_strict_filter: int | None
    source_availability: tuple[OfficialSourceAvailability, ...] = ()

    def __post_init__(self) -> None:
        values = (
            self.source_rows_expected,
            self.source_rows_fetched,
            self.bills_collected,
            self.bills_after_strict_filter,
            self.meetings_collected,
            self.meetings_after_strict_filter,
        )
        if any(value is not None and value < 0 for value in values):
            raise ValueError("provisional source accounting must not be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_complete": self.source_complete,
            "source_rows_expected": self.source_rows_expected,
            "source_rows_fetched": self.source_rows_fetched,
            "bills_collected": self.bills_collected,
            "bills_after_strict_filter": self.bills_after_strict_filter,
            "meetings_collected": self.meetings_collected,
            "meetings_after_strict_filter": self.meetings_after_strict_filter,
            "source_availability": [item.to_dict() for item in self.source_availability],
        }


@dataclass(frozen=True, slots=True)
class ProvisionalCandidatePage:
    offset: int
    page_size: int
    total: int
    entries: tuple[ProvisionalCandidateEntry, ...]
    next_offset: int | None

    def __post_init__(self) -> None:
        if self.offset < 0 or not 1 <= self.page_size <= 100 or self.total < 0:
            raise ValueError("provisional page bounds are invalid")
        if self.offset + len(self.entries) > self.total:
            raise ValueError("provisional page exceeds its complete candidate inventory")
        expected_next = self.offset + len(self.entries)
        if self.next_offset is None:
            if expected_next < self.total:
                raise ValueError("provisional page omitted its next offset")
        elif self.next_offset != expected_next or self.next_offset >= self.total:
            raise ValueError("provisional page next offset is invalid")

    @property
    def complete(self) -> bool:
        return self.next_offset is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "offset": self.offset,
            "page_size": self.page_size,
            "total": self.total,
            "returned_count": len(self.entries),
            "next_offset": self.next_offset,
            "complete": self.complete,
            "entries": [entry.to_dict() for entry in self.entries],
        }


@dataclass(frozen=True, slots=True)
class ProvisionalResearchOverview:
    """Metadata-only map that cannot be mistaken for a research conclusion."""

    query: str
    source_hash: str
    entries: tuple[ProvisionalCandidateEntry, ...]
    families: tuple[ProvisionalFamilyAccounting, ...]
    source: ProvisionalSourceAccounting
    provisional: bool = True
    substantive_conclusion_available: bool = False

    def __post_init__(self) -> None:
        if not self.query.strip() or not self.source_hash.strip():
            raise ValueError("provisional overview query and source hash are required")
        if not self.provisional or self.substantive_conclusion_available:
            raise ValueError("metadata overview must remain explicitly provisional")
        positions = tuple(entry.position for entry in self.entries)
        if positions != tuple(range(len(self.entries))):
            raise ValueError("provisional entries must have contiguous positions")
        identifiers = tuple(entry.candidate_id for entry in self.entries)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("provisional candidate ids must be unique")
        by_kind = Counter(entry.kind for entry in self.entries)
        if tuple(item.kind for item in self.families) != tuple(MetadataKind):
            raise ValueError("provisional overview requires both family accountings")
        if any(by_kind[item.kind] != item.accepted_count for item in self.families):
            raise ValueError("provisional entries do not match accepted accounting")

    @property
    def accepted_total(self) -> int:
        return len(self.entries)

    def page(self, *, offset: int = 0, page_size: int = 50) -> ProvisionalCandidatePage:
        if offset < 0 or offset > len(self.entries):
            raise ValueError("provisional page offset is outside the inventory")
        if not 1 <= page_size <= 100:
            raise ValueError("provisional page_size must be between 1 and 100")
        selected = self.entries[offset : offset + page_size]
        returned_through = offset + len(selected)
        return ProvisionalCandidatePage(
            offset=offset,
            page_size=page_size,
            total=len(self.entries),
            entries=selected,
            next_offset=(returned_through if returned_through < len(self.entries) else None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "source_hash": self.source_hash,
            "provisional": self.provisional,
            "substantive_conclusion_available": self.substantive_conclusion_available,
            "accepted_total": self.accepted_total,
            "families": [item.to_dict() for item in self.families],
            "source": self.source.to_dict(),
            "entries": [item.to_dict() for item in self.entries],
        }


def build_research_overview(
    snapshot: ResearchSnapshot,
    *,
    core_limit: int = _DEFAULT_CORE_LIMIT,
) -> ResearchOverview:
    """Build a deterministic overview without reading or rewriting evidence text."""

    if not 1 <= core_limit <= _MAX_CORE_LIMIT:
        raise ValueError(f"core_limit must be between 1 and {_MAX_CORE_LIMIT}")
    records = tuple(snapshot.evidence)
    canonical_bill_numbers = _canonical_bill_numbers(records)
    inventory = _inventory(
        records,
        canonical_bill_numbers=canonical_bill_numbers,
    )
    core = _core(
        records,
        snapshot.coverage,
        core_limit,
        canonical_bill_numbers=canonical_bill_numbers,
    )
    provisional_reasons = _provisional_reasons(snapshot.coverage)
    return ResearchOverview(
        research_id=snapshot.research_id,
        query_fingerprint=snapshot.query_fingerprint,
        index_revision=snapshot.index_revision,
        build_sha=snapshot.build_sha,
        status=(
            OverviewStatus.COMPLETE if snapshot.coverage.complete else OverviewStatus.PROVISIONAL
        ),
        coverage=snapshot.coverage,
        provisional_reasons=provisional_reasons,
        core=core,
        inventory=inventory,
    )


def build_provisional_research_overview(
    source: DiscoveryStageState | MetadataResolution,
) -> ProvisionalResearchOverview:
    """Expose all accepted metadata candidates while deeper work is pending.

    The returned entries are compact immutable routing records suitable for a
    separate storage layer.  They contain scores and resolver reasons but no
    generated claim, issue synthesis, or inferred entity relationship.
    """

    if isinstance(source, DiscoveryStageState):
        resolution = source.resolution
        collection = source.collection
        source_accounting = ProvisionalSourceAccounting(
            source_complete=collection.coverage.source_complete,
            source_rows_expected=collection.coverage.source_rows_expected,
            source_rows_fetched=collection.coverage.source_rows_fetched,
            # Hosted discovery artifacts intentionally omit duplicated row
            # payloads: immutable page artifacts preserve every official row.
            # Coverage and strict-filter accounting remain authoritative for
            # the complete provisional map in both compact and memory stores.
            bills_collected=collection.coverage.bill_unique_records,
            bills_after_strict_filter=source.filter_report.bills.kept_count,
            meetings_collected=collection.coverage.meeting_unique_pdfs,
            meetings_after_strict_filter=source.filter_report.meetings.kept_count,
            source_availability=summarize_source_availability(collection),
        )
    else:
        resolution = source
        source_accounting = ProvisionalSourceAccounting(
            source_complete=None,
            source_rows_expected=None,
            source_rows_fetched=None,
            bills_collected=None,
            bills_after_strict_filter=None,
            meetings_collected=None,
            meetings_after_strict_filter=None,
        )

    accepted = tuple(
        sorted(
            (*resolution.bills.accepted, *resolution.meetings.accepted),
            key=lambda item: (-item.score, item.kind.value, item.candidate_id),
        )
    )
    entries = tuple(
        _provisional_entry(position, decision) for position, decision in enumerate(accepted)
    )
    return ProvisionalResearchOverview(
        query=resolution.query,
        source_hash=resolution.source_hash,
        entries=entries,
        families=(
            _family_accounting(resolution.bills),
            _family_accounting(resolution.meetings),
        ),
        source=source_accounting,
    )


def _inventory(
    records: Sequence[EvidenceRecord],
    *,
    canonical_bill_numbers: frozenset[str],
) -> EvidenceInventory:
    bindings = {
        record.id: _entity_bindings(
            record,
            canonical_bill_numbers=canonical_bill_numbers,
        )
        for record in records
    }
    grouped: dict[OverviewEntityType, dict[str, list[EvidenceRecord]]] = {
        entity_type: defaultdict(list) for entity_type in OverviewEntityType
    }
    for record in records:
        for entity_type, entity_id in bindings[record.id]:
            grouped[entity_type][entity_id].append(record)
    assigned = {record.id for record in records if bindings[record.id]}
    dates = tuple(value for record in records if (value := _event_date(record)) is not None)
    return EvidenceInventory(
        evidence_ids=tuple(record.id for record in records),
        evidence_type_counts=_type_counts(records),
        date_from=min(dates) if dates else None,
        date_to=max(dates) if dates else None,
        undated_evidence_ids=tuple(record.id for record in records if _event_date(record) is None),
        bill_groups=_groups(OverviewEntityType.BILL, grouped[OverviewEntityType.BILL]),
        meeting_groups=_groups(
            OverviewEntityType.MEETING,
            grouped[OverviewEntityType.MEETING],
        ),
        document_groups=_groups(
            OverviewEntityType.DOCUMENT,
            grouped[OverviewEntityType.DOCUMENT],
        ),
        speech_groups=_groups(
            OverviewEntityType.SPEECH,
            grouped[OverviewEntityType.SPEECH],
        ),
        unassigned_evidence_ids=tuple(record.id for record in records if record.id not in assigned),
    )


def _provisional_entry(
    position: int,
    decision: CandidateDecision,
) -> ProvisionalCandidateEntry:
    candidate = decision.candidate
    if decision.kind is MetadataKind.BILL:
        raw_number = candidate.get("BILL_NO", candidate.get("bill_no"))
        bill_number = str(raw_number).strip() if raw_number is not None else ""
        if (
            not _BILL_NUMBER.fullmatch(bill_number)
            or decision.candidate_id != f"bill:{bill_number}"
        ):
            raise ValueError("accepted bill candidate lacks an exact matching bill number")
        identifiers = (("bill_no", bill_number),)
        label = _first_candidate_text(candidate, ("BILL_NAME", "bill_name", "title"))
    else:
        raw_url = candidate.get("PDF_LINK_URL", candidate.get("DOWN_URL"))
        meeting_url = str(raw_url).strip() if raw_url is not None else ""
        parsed = urllib.parse.urlsplit(meeting_url)
        if (
            not meeting_url
            or parsed.scheme != "https"
            or parsed.hostname
            not in {
                "open.assembly.go.kr",
                "record.assembly.go.kr",
            }
            or decision.candidate_id != f"meeting:{meeting_url}"
        ):
            raise ValueError("accepted meeting candidate lacks an exact official URL")
        identifiers = (("meeting_url", meeting_url),)
        label = _first_candidate_text(
            candidate,
            ("TITLE", "CONF_NAME", "MEETING_TITLE", "title"),
        )
    return ProvisionalCandidateEntry(
        position=position,
        kind=decision.kind,
        candidate_id=decision.candidate_id,
        score=decision.score,
        match_reasons=decision.match_reasons,
        exact_identifiers=identifiers,
        label=label,
    )


def _first_candidate_text(
    candidate: Mapping[str, Any],
    fields: Sequence[str],
) -> str | None:
    for field in fields:
        value = candidate.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _family_accounting(
    resolution: CandidateSetResolution,
) -> ProvisionalFamilyAccounting:
    return ProvisionalFamilyAccounting(
        kind=resolution.kind,
        total_candidates=resolution.total_candidates,
        accepted_count=resolution.accepted_count,
        rejected_count=resolution.rejected_count,
        rejection_reason_counts=resolution.rejection_reason_counts,
    )


def _groups(
    entity_type: OverviewEntityType,
    values: Mapping[str, Sequence[EvidenceRecord]],
) -> tuple[OverviewEntityGroup, ...]:
    groups: list[OverviewEntityGroup] = []
    for entity_id in sorted(values):
        records = tuple(sorted(values[entity_id], key=_record_key))
        dates = tuple(value for record in records if (value := _event_date(record)) is not None)
        groups.append(
            OverviewEntityGroup(
                entity_type=entity_type,
                entity_id=entity_id,
                evidence_ids=tuple(record.id for record in records),
                evidence_type_counts=_type_counts(records),
                date_from=min(dates) if dates else None,
                date_to=max(dates) if dates else None,
                undated_evidence_ids=tuple(
                    record.id for record in records if _event_date(record) is None
                ),
            )
        )
    return tuple(groups)


def _core(
    records: Sequence[EvidenceRecord],
    coverage: CoverageLedger,
    limit: int,
    *,
    canonical_bill_numbers: frozenset[str],
) -> tuple[CoreEvidence, ...]:
    by_type: dict[EvidenceType, list[EvidenceRecord]] = defaultdict(list)
    for record in records:
        by_type[record.evidence_type].append(record)
    for values in by_type.values():
        values.sort(key=_record_key)

    selected: list[tuple[EvidenceRecord, tuple[str, ...]]] = []
    selected_ids: set[str] = set()
    complete_axes = {entry.evidence_type: entry.complete for entry in coverage.entries}
    for evidence_type in _CORE_ORDER:
        candidates = [
            record
            for record in by_type.get(evidence_type, [])
            if _eligible_for_core(
                record,
                canonical_bill_numbers=canonical_bill_numbers,
            )
        ]
        if not candidates or len(selected) >= limit:
            continue
        record = candidates[-1] if evidence_type is EvidenceType.BILL_STATUS else candidates[0]
        selection = (
            "latest_in_axis" if evidence_type is EvidenceType.BILL_STATUS else "earliest_in_axis"
        )
        selected.append(
            (
                record,
                (
                    f"axis_representative:{_CORE_REASON[evidence_type]}",
                    f"selection:{selection}",
                    (
                        "coverage:complete"
                        if complete_axes.get(evidence_type, False)
                        else "coverage:provisional"
                    ),
                ),
            )
        )
        selected_ids.add(record.id)

    remaining = sorted(
        (
            record
            for record in records
            if record.id not in selected_ids
            and _eligible_for_core(
                record,
                canonical_bill_numbers=canonical_bill_numbers,
            )
        ),
        key=lambda record: (
            _CORE_PRIORITY.get(record.evidence_type, len(_CORE_PRIORITY)),
            record.sort_key,
            record.id,
        ),
    )
    for record in remaining[: max(0, limit - len(selected))]:
        selected.append(
            (
                record,
                (
                    f"capacity_fill:{_CORE_REASON[record.evidence_type]}",
                    "selection:deterministic_type_date_id_order",
                    (
                        "coverage:complete"
                        if complete_axes.get(record.evidence_type, False)
                        else "coverage:provisional"
                    ),
                ),
            )
        )
    return tuple(
        CoreEvidence(
            rank=rank,
            evidence_id=record.id,
            evidence_type=record.evidence_type,
            reasons=reasons,
            entity_bindings=_entity_bindings(
                record,
                canonical_bill_numbers=canonical_bill_numbers,
            ),
        )
        for rank, (record, reasons) in enumerate(selected, start=1)
    )


def _entity_bindings(
    record: EvidenceRecord,
    *,
    canonical_bill_numbers: frozenset[str],
) -> tuple[tuple[OverviewEntityType, str], ...]:
    metadata = dict(record.metadata)
    values: list[tuple[OverviewEntityType, str]] = []
    values.extend(
        (OverviewEntityType.BILL, bill_number)
        for bill_number in _record_bill_numbers(record)
        if bill_number in canonical_bill_numbers
    )
    meeting_ids = set(_metadata_identifiers(metadata, "related_meeting_ids"))
    if (meeting_id := _metadata_identifier(metadata, "meeting_id")) is not None:
        meeting_ids.add(meeting_id)
    values.extend((OverviewEntityType.MEETING, meeting_id) for meeting_id in sorted(meeting_ids))
    work_id = _metadata_identifier(metadata, "work_id")
    if work_id is not None:
        values.append((OverviewEntityType.DOCUMENT, work_id))

    speech_ids = {
        value
        for field in ("speech_id", "source_speech_id", "target_speech_id")
        if (value := _metadata_identifier(metadata, field)) is not None
    }
    direct_id = (
        record.id[len(_SPEECH_RECORD_PREFIX) :]
        if record.evidence_type is EvidenceType.SPEECHES
        and record.id.startswith(_SPEECH_RECORD_PREFIX)
        else ""
    )
    if direct_id:
        speech_ids.add(direct_id)
    values.extend((OverviewEntityType.SPEECH, speech_id) for speech_id in sorted(speech_ids))
    return tuple(sorted(set(values), key=lambda item: (item[0].value, item[1])))


def _eligible_for_core(
    record: EvidenceRecord,
    *,
    canonical_bill_numbers: frozenset[str],
) -> bool:
    """Keep context-only rejected bill agenda items out of the quick core.

    The complete evidence inventory still retains every record. When accepted
    bill evidence exists, however, a record that explicitly names only other
    bill numbers is context for a mixed meeting rather than a target source.
    """

    if not canonical_bill_numbers:
        return True
    mentioned = set(_record_bill_numbers(record))
    return not mentioned or not mentioned.isdisjoint(canonical_bill_numbers)


def _record_bill_numbers(record: EvidenceRecord) -> tuple[str, ...]:
    metadata = dict(record.metadata)
    values = set(_metadata_identifiers(metadata, "related_bill_numbers"))
    if (bill_number := _metadata_identifier(metadata, "bill_no")) is not None:
        values.add(bill_number)
    return tuple(sorted(value for value in values if _BILL_NUMBER.fullmatch(value)))


def _canonical_bill_numbers(records: Sequence[EvidenceRecord]) -> frozenset[str]:
    """Return only bill identities proven by finalized official bill evidence.

    Minutes and speeches can mention additional agenda numbers from the same
    meeting.  Those exact mentions remain in their immutable metadata and the
    evidence graph, but they must not resurrect a resolver-rejected bill as a
    canonical final-catalog entity.  ``EvidenceType.BILLS`` records are created
    only from accepted official bill metadata, so they are the authority for
    final overview bill grouping.
    """

    numbers: list[str] = []
    for record in records:
        if record.evidence_type is not EvidenceType.BILLS:
            continue
        bill_number = _metadata_identifier(dict(record.metadata), "bill_no")
        if bill_number is None or not _BILL_NUMBER.fullmatch(bill_number):
            raise ValueError("bill evidence lacks an exact seven-digit bill number")
        numbers.append(bill_number)
    if len(numbers) != len(set(numbers)):
        raise ValueError("bill evidence contains duplicate canonical bill numbers")
    return frozenset(numbers)


def _metadata_identifier(
    metadata: Mapping[str, str | int | float | bool | None],
    field: str,
) -> str | None:
    value = metadata.get(field)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _metadata_identifiers(
    metadata: Mapping[str, str | int | float | bool | None],
    field: str,
) -> tuple[str, ...]:
    value = _metadata_identifier(metadata, field)
    if value is None:
        return ()
    parts = tuple(part.strip() for part in value.split(","))
    if any(not part for part in parts):
        return ()
    return tuple(sorted(set(parts)))


def _type_counts(records: Iterable[EvidenceRecord]) -> tuple[EvidenceTypeCount, ...]:
    counts = Counter(record.evidence_type for record in records)
    return tuple(
        EvidenceTypeCount(evidence_type, counts[evidence_type])
        for evidence_type in EvidenceType
        if counts[evidence_type]
    )


def _event_date(record: EvidenceRecord) -> date | None:
    if len(record.sort_key) < 10:
        return None
    if len(record.sort_key) > 10 and record.sort_key[10] not in {"|", ":"}:
        return None
    try:
        return date.fromisoformat(record.sort_key[:10])
    except ValueError:
        return None


def _record_key(record: EvidenceRecord) -> tuple[str, str]:
    return record.sort_key, record.id


def _provisional_reasons(coverage: CoverageLedger) -> tuple[str, ...]:
    if coverage.complete:
        return ()
    reasons: list[str] = []
    for entry in coverage.entries:
        if entry.evidence_type not in coverage.requested or entry.complete:
            continue
        reasons.append(f"axis_incomplete:{entry.evidence_type.value}")
        reasons.extend(
            f"axis_gap:{entry.evidence_type.value}:{reason}" for reason in entry.gap_reasons
        )
        if entry.failed_count:
            reasons.append(f"axis_failed:{entry.evidence_type.value}:{entry.failed_count}")
        if entry.pending_count:
            reasons.append(f"axis_pending:{entry.evidence_type.value}:{entry.pending_count}")
        if entry.candidate_total is None:
            reasons.append(f"axis_total_unknown:{entry.evidence_type.value}")
    return tuple(dict.fromkeys(reasons))


__all__ = [
    "CoreEvidence",
    "EvidenceInventory",
    "EvidenceTypeCount",
    "OverviewEntityGroup",
    "OverviewEntityType",
    "OverviewStatus",
    "ProvisionalCandidateEntry",
    "ProvisionalCandidatePage",
    "ProvisionalFamilyAccounting",
    "ProvisionalResearchOverview",
    "ProvisionalSourceAccounting",
    "ResearchOverview",
    "build_provisional_research_overview",
    "build_research_overview",
]
