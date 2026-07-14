"""Fast gateway and bounded background orchestration for legislative research.

The gateway performs no official-source I/O.  Metadata is split first by
partition and then by page: one worker delivery fetches exactly one API page.
Only complete, coherently reassembled partitions reach candidate resolution.
Document hydration is likewise one official PDF per delivery.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol, TypeVar, cast

from kasm.adapters.korea.bills import BILL_STATUS_DATASET
from kasm.adapters.korea.client import ApiPage, ApiResult, AssemblyOpenApiClient
from kasm.adapters.korea.ingestion import meeting_from_open_assembly_row
from kasm.adapters.korea.pipeline import OpenAssemblyPipeline
from kasm.adapters.korea.sources import MeetingSource, classify_meeting

from .collector import (
    CollectionCoverage,
    MetadataCollection,
    MetadataCollector,
    MetadataKind,
    MetadataPartition,
    PartitionProvenance,
)
from .contracts import EvidenceType, ResearchContract
from .credentials import ResearchCredential
from .document_worker import DocumentWorkerError, DocumentWorkResult
from .documents import OfficialDocumentKind
from .jobs import JobStatus, ResearchJob, ResearchJobStore
from .page_collection import (
    MetadataPageWork,
    assemble_partition_pages,
    expand_first_page,
    validate_fetched_page,
)
from .partitioning import OfficialSourceKind, ResearchPartitionPlan, ResearchPartitionPlanner
from .planner import ResearchContractPlanner, ResearchPlan
from .queue import ResearchTask, ResearchTaskQueue, ResearchTaskStage
from .relevance import RelevanceCriteria
from .resolver import (
    CandidateDecision,
    MetadataCandidateResolver,
    MetadataResolution,
    accept_exact_corpus_candidates,
)
from .results import (
    EvidenceIndexEntry,
    EvidenceRecord,
    ResearchSnapshot,
    ResearchSnapshotSummary,
)
from .transcript_evidence import TranscriptEvidence, extract_transcript_evidence

if TYPE_CHECKING:
    from .overview import ProvisionalResearchOverview
    from .overview_transport import OverviewGroupShard, OverviewTransportManifest

_WORK_KIND = "work_kind"
_METADATA_PAGE = "metadata_page"
_BILL_DOCUMENTS = "bill_documents"
_DISCOVERY_FANOUT = "discovery_fanout"
_DEFERRED_FANOUT = "deferred_fanout"
_DOCUMENT_FANOUT = "document_fanout"
_PAGE_FANOUT = "page_fanout"
_PHASE_BARRIER = "phase_barrier"
_DOCUMENT_FINALIZE_BARRIER = "document_finalize_barrier"
_PHASE = "phase"
_ATTEMPT = "attempt"
_PARTITION_ID = "partition_id"
_PAGE = "page"
_EXPECTED_TOTAL = "expected_total"
_BILL_NO = "bill_no"
_DOCUMENT_KIND = "document_kind"
_OFFICIAL_URL = "official_url"
_START = "start"
_STOP = "stop"

ROUTING_SHARD_SIZE: Final = 4

_MEETING_EVIDENCE = (
    EvidenceType.AGENDAS,
    EvidenceType.SPEECHES,
    EvidenceType.SPEECH_CONTEXT,
    EvidenceType.GOVERNMENT_RESPONSES,
)
_BILL_DOCUMENT_EVIDENCE = (
    EvidenceType.BILL_TEXT,
    EvidenceType.REVIEW_REPORTS,
)

_Key = TypeVar("_Key")
_Value = TypeVar("_Value")


class MetadataPhase(StrEnum):
    DISCOVERY = "discovery"
    BILL_STATUS = "bill_status"


class DocumentOutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    FAILED = "failed"


class CorpusRecallStatus(StrEnum):
    """Whether a full-text revision may widen exhaustive metadata recall."""

    NOT_REQUIRED = "not_required"
    UNAVAILABLE = "unavailable"
    INCOMPLETE = "incomplete"
    VERIFIED = "verified"


@dataclass(frozen=True, slots=True)
class CorpusRecallState:
    """Persistable, credential-free accounting for one corpus recall attempt.

    Exact identities are useful only when the configured revision is complete,
    every lexical candidate maps through the corpus bridge, and every mapped
    bill or meeting is present in the completely collected metadata universe.
    Anything else remains an explicit gap and is forbidden from widening the
    accepted candidate set.
    """

    status: CorpusRecallStatus
    revision_id: str | None = None
    candidate_count: int = 0
    mapped_count: int = 0
    exact_bill_numbers: tuple[str, ...] = ()
    exact_meeting_urls: tuple[str, ...] = ()
    required_work_ids: tuple[str, ...] = ()
    gap_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.revision_id is not None and not re.fullmatch(r"[0-9a-f]{64}", self.revision_id):
            raise ValueError("corpus recall revision_id must be a SHA-256 digest")
        if min(self.candidate_count, self.mapped_count) < 0:
            raise ValueError("corpus recall counts must not be negative")
        if self.mapped_count > self.candidate_count:
            raise ValueError("corpus recall cannot map more candidates than it found")
        if any(not re.fullmatch(r"\d{7}", item) for item in self.exact_bill_numbers):
            raise ValueError("corpus recall bill numbers must contain seven digits")
        if any(not item.strip() for item in self.exact_meeting_urls):
            raise ValueError("corpus recall meeting URLs must not be blank")
        if any(not item.strip() for item in self.required_work_ids):
            raise ValueError("corpus recall work ids must not be blank")
        if any(not item.strip() for item in self.gap_reasons):
            raise ValueError("corpus recall gap reasons must not be blank")
        object.__setattr__(self, "exact_bill_numbers", tuple(sorted(set(self.exact_bill_numbers))))
        object.__setattr__(self, "exact_meeting_urls", tuple(sorted(set(self.exact_meeting_urls))))
        object.__setattr__(self, "required_work_ids", tuple(sorted(set(self.required_work_ids))))
        object.__setattr__(self, "gap_reasons", tuple(dict.fromkeys(self.gap_reasons)))
        if self.status is CorpusRecallStatus.NOT_REQUIRED:
            if (
                self.revision_id is not None
                or self.candidate_count
                or self.mapped_count
                or self.exact_bill_numbers
                or self.exact_meeting_urls
                or self.required_work_ids
                or self.gap_reasons
            ):
                raise ValueError("unrequired corpus recall must not carry search state")
        elif self.revision_id is None and self.status is not CorpusRecallStatus.UNAVAILABLE:
            raise ValueError("configured corpus recall requires a revision_id")
        if self.status is CorpusRecallStatus.VERIFIED and (
            self.mapped_count != self.candidate_count or self.gap_reasons
        ):
            raise ValueError("verified corpus recall must account for every candidate")
        if self.status is not CorpusRecallStatus.VERIFIED and (
            self.exact_bill_numbers or self.exact_meeting_urls or self.required_work_ids
        ):
            raise ValueError("unverified corpus recall cannot widen candidate scope")

    @property
    def comprehensive(self) -> bool:
        return self.status in {
            CorpusRecallStatus.NOT_REQUIRED,
            CorpusRecallStatus.VERIFIED,
        }

    @property
    def verified(self) -> bool:
        return self.status is CorpusRecallStatus.VERIFIED

    def fail_closed(self, *reasons: str) -> CorpusRecallState:
        """Discard widening identities after a downstream exactness gap."""

        normalized = tuple(reason.strip() for reason in reasons if reason.strip())
        if not normalized:
            return self
        return CorpusRecallState(
            status=CorpusRecallStatus.INCOMPLETE,
            revision_id=self.revision_id,
            candidate_count=self.candidate_count,
            mapped_count=self.mapped_count,
            gap_reasons=(*self.gap_reasons, *normalized),
        )


class CorpusRecallProvider(Protocol):
    """Revision-bound lexical recall injected into the durable engine."""

    @property
    def revision_id(self) -> str: ...

    @property
    def binding_id(self) -> str: ...

    def recall(
        self,
        plan: ResearchPlan,
        criteria: RelevanceCriteria,
    ) -> CorpusRecallState: ...


class BillDocumentDiscoveryError(RuntimeError):
    """Stable retry semantics for one bill's official document-index check."""

    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        if not code.strip():
            raise ValueError("bill document discovery error code is required")
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class GatewayPlanState:
    job: ResearchJob
    research_plan: ResearchPlan
    partition_plan: ResearchPartitionPlan
    discovery_partitions: tuple[MetadataPartition, ...]


@dataclass(frozen=True, slots=True)
class MetadataPageReadiness:
    """Small final-write marker proving one immutable raw API page is durable."""

    query_fingerprint: str
    index_revision: str
    phase: MetadataPhase
    partition_id: str
    dataset: str
    page: int
    page_size: int
    total_count: int
    row_count: int
    source_hash: str

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[0-9a-f]{64}", self.query_fingerprint)
            or not self.index_revision.strip()
            or not self.partition_id.strip()
            or not self.dataset.isalnum()
            or not re.fullmatch(r"[0-9a-f]{64}", self.source_hash)
        ):
            raise ValueError("metadata page readiness binding is invalid")
        if self.page < 1 or not 1 <= self.page_size <= 1_000 or self.total_count < 0:
            raise ValueError("metadata page readiness pagination is invalid")
        total_pages = max(1, (self.total_count + self.page_size - 1) // self.page_size)
        if self.page > total_pages:
            raise ValueError("metadata page readiness lies beyond the official total")
        expected_rows = min(
            self.page_size,
            max(0, self.total_count - ((self.page - 1) * self.page_size)),
        )
        if self.row_count != expected_rows:
            raise ValueError("metadata page readiness row accounting is invalid")

    @classmethod
    def create(
        cls,
        gateway: GatewayPlanState,
        phase: MetadataPhase,
        partition: MetadataPartition,
        page: ApiPage,
    ) -> MetadataPageReadiness:
        if page.total_count is None:
            raise ValueError("metadata page readiness requires an official total")
        return cls(
            gateway.job.query_fingerprint,
            gateway.job.index_revision,
            phase,
            partition.partition_id,
            partition.dataset,
            page.page,
            page.page_size,
            page.total_count,
            len(page.rows),
            page.source_hash,
        )


@dataclass(frozen=True, slots=True)
class FamilyFilterAccounting:
    source_count: int
    kept_count: int
    outside_date_count: int = 0
    committee_mismatch_count: int = 0
    missing_date_count: int = 0
    missing_committee_count: int = 0

    def __post_init__(self) -> None:
        values = (
            self.source_count,
            self.kept_count,
            self.outside_date_count,
            self.committee_mismatch_count,
            self.missing_date_count,
            self.missing_committee_count,
        )
        if any(value < 0 for value in values):
            raise ValueError("filter accounting must be non-negative")
        if sum(values[1:]) != self.source_count:
            raise ValueError("filter accounting must explain every source candidate")


@dataclass(frozen=True, slots=True)
class StrictFilterReport:
    bills: FamilyFilterAccounting
    meetings: FamilyFilterAccounting


@dataclass(frozen=True, slots=True)
class DiscoveryStageState:
    collection: MetadataCollection
    filtered_collection: MetadataCollection
    filter_report: StrictFilterReport
    resolution: MetadataResolution
    status_partitions: tuple[MetadataPartition, ...]
    document_bill_numbers: tuple[str, ...]
    corpus_recall: CorpusRecallState | None = None


@dataclass(frozen=True, slots=True)
class DeferredWorkManifest:
    """Compact discovery boundary used by every deferred worker.

    Immutable page artifacts remain the authoritative official-row archive;
    the compact discovery state preserves provenance, accepted payloads and
    every decision score/reason. This manifest contains the hot routing and
    resolver bindings, so deferred deliveries never decode even that broader
    compact audit state.
    """

    query_fingerprint: str
    index_revision: str
    discovery_source_hash: str
    criteria_hash: str
    terminology_version: str
    source_partitions: tuple[PartitionProvenance, ...]
    source_coverage: CollectionCoverage
    filter_report: StrictFilterReport
    corpus_recall: CorpusRecallState | None
    status_partitions: tuple[MetadataPartition, ...]
    accepted_bills: tuple[CandidateDecision, ...]
    accepted_meetings: tuple[CandidateDecision, ...]
    accepted_bill_numbers: tuple[str, ...]
    document_bill_numbers: tuple[str, ...]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.query_fingerprint):
            raise ValueError("deferred manifest query fingerprint is invalid")
        if not self.index_revision.strip() or not re.fullmatch(
            r"[0-9a-f]{64}", self.discovery_source_hash
        ):
            raise ValueError("deferred manifest discovery binding is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.criteria_hash) or not (
            self.terminology_version.strip()
        ):
            raise ValueError("deferred manifest resolver binding is invalid")
        source_partition_ids = tuple(item.partition_id for item in self.source_partitions)
        if len(source_partition_ids) != len(set(source_partition_ids)):
            raise ValueError("deferred source partitions must be unique")
        if _provenance_source_hash(self.source_partitions) != self.discovery_source_hash:
            raise ValueError("deferred source provenance hash is invalid")
        if (
            self.source_coverage.partitions_expected != len(self.source_partitions)
            or self.filter_report.bills.kept_count > self.source_coverage.bill_unique_records
            or self.filter_report.meetings.kept_count > self.source_coverage.meeting_unique_pdfs
        ):
            raise ValueError("deferred source accounting is invalid")
        partition_ids = tuple(item.partition_id for item in self.status_partitions)
        if len(partition_ids) != len(set(partition_ids)):
            raise ValueError("deferred status partitions must be unique")
        if any(
            not re.fullmatch(r"\d{7}", number)
            for number in (*self.accepted_bill_numbers, *self.document_bill_numbers)
        ):
            raise ValueError("deferred bill numbers must contain seven digits")
        if len(self.accepted_bill_numbers) != len(set(self.accepted_bill_numbers)) or len(
            self.document_bill_numbers
        ) != len(set(self.document_bill_numbers)):
            raise ValueError("deferred bill numbers must be unique")
        if not set(self.document_bill_numbers) <= set(self.accepted_bill_numbers):
            raise ValueError("document bills must belong to accepted bills")
        accepted_numbers = tuple(
            sorted(_candidate_bill_number(item) for item in self.accepted_bills)
        )
        if accepted_numbers != self.accepted_bill_numbers:
            raise ValueError("accepted bill lookup does not match its identities")
        if any(
            not item.accepted or _candidate_meeting_url(item) is None
            for item in self.accepted_meetings
        ):
            raise ValueError("accepted meeting lookup contains an invalid identity")

    @classmethod
    def create(
        cls,
        gateway: GatewayPlanState,
        discovery: DiscoveryStageState,
    ) -> DeferredWorkManifest:
        return cls(
            gateway.job.query_fingerprint,
            gateway.job.index_revision,
            discovery.resolution.source_hash,
            _criteria_hash(discovery.resolution.criteria),
            discovery.resolution.criteria.terminology_version,
            discovery.collection.partitions,
            discovery.collection.coverage,
            discovery.filter_report,
            discovery.corpus_recall,
            discovery.status_partitions,
            discovery.resolution.bills.accepted,
            discovery.resolution.meetings.accepted,
            tuple(
                sorted(_candidate_bill_number(item) for item in discovery.resolution.bills.accepted)
            ),
            discovery.document_bill_numbers,
        )


class DeferredRouteKind(StrEnum):
    BILL_STATUS = "bill_status"
    BILL_DOCUMENT = "bill_document"


@dataclass(frozen=True, slots=True)
class DeferredWorkRoute:
    """One bounded deferred work identity, separate from the audit manifest."""

    position: int
    kind: DeferredRouteKind
    status_partition: MetadataPartition | None = None
    bill_number: str | None = None

    def __post_init__(self) -> None:
        if self.position < 0:
            raise ValueError("deferred route position must not be negative")
        if self.kind is DeferredRouteKind.BILL_STATUS:
            if self.status_partition is None or self.bill_number is not None:
                raise ValueError("bill-status route requires exactly one partition")
            return
        if (
            self.status_partition is not None
            or self.bill_number is None
            or not re.fullmatch(r"\d{7}", self.bill_number)
        ):
            raise ValueError("bill-document route requires exactly one bill number")


@dataclass(frozen=True, slots=True)
class DeferredRouteShard:
    """A fixed-size routing shard used by one deferred fan-out delivery."""

    number: int
    start_position: int
    total: int
    routes: tuple[DeferredWorkRoute, ...]

    def __post_init__(self) -> None:
        if (
            self.number < 0
            or self.start_position != self.number * ROUTING_SHARD_SIZE
            or self.total < 1
            or not self.routes
            or len(self.routes) > ROUTING_SHARD_SIZE
            or self.start_position + len(self.routes) > self.total
        ):
            raise ValueError("deferred route shard bounds are invalid")
        expected = tuple(range(self.start_position, self.start_position + len(self.routes)))
        if tuple(item.position for item in self.routes) != expected:
            raise ValueError("deferred route shard positions are not contiguous")
        if self.start_position + len(self.routes) < self.total and (
            len(self.routes) != ROUTING_SHARD_SIZE
        ):
            raise ValueError("non-final deferred route shard must be full")

    @classmethod
    def build(cls, manifest: DeferredWorkManifest) -> tuple[DeferredRouteShard, ...]:
        routes = tuple(
            DeferredWorkRoute(position, DeferredRouteKind.BILL_STATUS, partition)
            for position, partition in enumerate(manifest.status_partitions)
        ) + tuple(
            DeferredWorkRoute(
                len(manifest.status_partitions) + offset,
                DeferredRouteKind.BILL_DOCUMENT,
                bill_number=bill_number,
            )
            for offset, bill_number in enumerate(manifest.document_bill_numbers)
        )
        return tuple(
            cls(
                number=start // ROUTING_SHARD_SIZE,
                start_position=start,
                total=len(routes),
                routes=routes[start : start + ROUTING_SHARD_SIZE],
            )
            for start in range(0, len(routes), ROUTING_SHARD_SIZE)
        )


@dataclass(frozen=True, slots=True)
class DiscoveryBoundaryReadiness:
    """Final write proving every compact discovery view is durable."""

    query_fingerprint: str
    index_revision: str
    discovery_source_hash: str
    discovery_hash: str
    manifest_hash: str
    overview_hash: str
    deferred_route_count: int = 0
    deferred_route_shard_count: int = 0
    accepted_bill_count: int = 0
    status_partition_count: int = 0
    route_shard_size: int = ROUTING_SHARD_SIZE

    def __post_init__(self) -> None:
        if (
            any(
                not re.fullmatch(r"[0-9a-f]{64}", value)
                for value in (
                    self.query_fingerprint,
                    self.discovery_source_hash,
                    self.discovery_hash,
                    self.manifest_hash,
                    self.overview_hash,
                )
            )
            or not self.index_revision.strip()
            or min(
                self.deferred_route_count,
                self.deferred_route_shard_count,
                self.accepted_bill_count,
                self.status_partition_count,
            )
            < 0
            or self.route_shard_size != ROUTING_SHARD_SIZE
            or self.deferred_route_shard_count
            != (
                (self.deferred_route_count + ROUTING_SHARD_SIZE - 1)
                // ROUTING_SHARD_SIZE
            )
        ):
            raise ValueError("discovery readiness binding is invalid")


@dataclass(frozen=True, slots=True)
class CoverageGap:
    evidence_types: tuple[EvidenceType, ...]
    reason: str

    def __post_init__(self) -> None:
        if not self.evidence_types or not self.reason.strip():
            raise ValueError("coverage gaps require evidence types and a reason")
        object.__setattr__(self, "evidence_types", tuple(dict.fromkeys(self.evidence_types)))


@dataclass(frozen=True, slots=True)
class DocumentWorkItem:
    work_id: str
    kind: OfficialDocumentKind
    official_url: str
    evidence_types: tuple[EvidenceType, ...]
    related_bill_numbers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.work_id.strip() or not self.official_url.strip():
            raise ValueError("document work requires an id and official URL")
        if not self.evidence_types:
            raise ValueError("document work requires at least one evidence type")
        if any(not re.fullmatch(r"\d{7}", item) for item in self.related_bill_numbers):
            raise ValueError("related bill numbers must contain exactly seven digits")
        object.__setattr__(self, "evidence_types", tuple(dict.fromkeys(self.evidence_types)))
        object.__setattr__(
            self, "related_bill_numbers", tuple(dict.fromkeys(self.related_bill_numbers))
        )

    @classmethod
    def create(
        cls,
        kind: OfficialDocumentKind,
        official_url: str,
        *,
        evidence_types: Sequence[EvidenceType],
        related_bill_numbers: Sequence[str] = (),
    ) -> DocumentWorkItem:
        digest = hashlib.sha256(f"{kind.value}\0{official_url}".encode()).hexdigest()
        return cls(
            work_id=f"document_{digest}",
            kind=kind,
            official_url=official_url,
            evidence_types=tuple(evidence_types),
            related_bill_numbers=tuple(related_bill_numbers),
        )


@dataclass(frozen=True, slots=True)
class BillDocumentDiscovery:
    bill_number: str
    items: tuple[DocumentWorkItem, ...] = ()
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"\d{7}", self.bill_number):
            raise ValueError("bill document discovery requires an exact bill number")
        if any(
            item.related_bill_numbers and self.bill_number not in item.related_bill_numbers
            for item in self.items
        ):
            raise ValueError("bill document work belongs to another bill")
        if self.failure_reason is not None and not self.failure_reason.strip():
            raise ValueError("failure_reason must not be blank")
        bill_texts = tuple(
            item for item in self.items if item.kind is OfficialDocumentKind.BILL_TEXT
        )
        if len(bill_texts) > 1:
            raise ValueError("bill document discovery returned multiple original bill texts")
        if any(
            item.related_bill_numbers != (self.bill_number,)
            or EvidenceType.BILL_TEXT not in item.evidence_types
            for item in bill_texts
        ):
            raise ValueError("original bill text must bind to its exact bill number")


@dataclass(frozen=True, slots=True)
class DocumentWorkManifest:
    items: tuple[DocumentWorkItem, ...]
    bill_discoveries: tuple[BillDocumentDiscovery, ...]
    gaps: tuple[CoverageGap, ...] = ()

    def __post_init__(self) -> None:
        work_ids = tuple(item.work_id for item in self.items)
        bills = tuple(item.bill_number for item in self.bill_discoveries)
        if len(work_ids) != len(set(work_ids)):
            raise ValueError("document manifest work ids must be unique")
        if len(bills) != len(set(bills)):
            raise ValueError("document discoveries must be unique by bill number")

    @classmethod
    def create(
        cls,
        items: Sequence[DocumentWorkItem],
        bill_discoveries: Sequence[BillDocumentDiscovery],
        gaps: Sequence[CoverageGap] = (),
    ) -> DocumentWorkManifest:
        merged: dict[str, DocumentWorkItem] = {}
        for item in items:
            previous = merged.get(item.work_id)
            if previous is None:
                merged[item.work_id] = item
                continue
            if previous.kind is not item.kind or previous.official_url != item.official_url:
                raise ValueError("document work id collision")
            merged[item.work_id] = DocumentWorkItem(
                work_id=item.work_id,
                kind=item.kind,
                official_url=item.official_url,
                evidence_types=tuple(
                    dict.fromkeys((*previous.evidence_types, *item.evidence_types))
                ),
                related_bill_numbers=tuple(
                    dict.fromkeys((*previous.related_bill_numbers, *item.related_bill_numbers))
                ),
            )
        ordered = tuple(sorted(merged.values(), key=lambda item: item.work_id))
        discoveries = tuple(sorted(bill_discoveries, key=lambda item: item.bill_number))
        return cls(ordered, discoveries, tuple(gaps))

    @property
    def fingerprint(self) -> str:
        payload = [
            {
                "work_id": item.work_id,
                "kind": item.kind.value,
                "official_url": item.official_url,
                "evidence_types": [value.value for value in item.evidence_types],
                "related_bill_numbers": list(item.related_bill_numbers),
            }
            for item in self.items
        ]
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class DocumentRouteShard:
    """A fixed-size document routing shard, independent of the full manifest."""

    number: int
    start_position: int
    total: int
    items: tuple[DocumentWorkItem, ...]

    def __post_init__(self) -> None:
        if (
            self.number < 0
            or self.start_position != self.number * ROUTING_SHARD_SIZE
            or self.total < 1
            or not self.items
            or len(self.items) > ROUTING_SHARD_SIZE
            or self.start_position + len(self.items) > self.total
        ):
            raise ValueError("document route shard bounds are invalid")
        if len({item.work_id for item in self.items}) != len(self.items):
            raise ValueError("document route shard contains duplicate work ids")
        if self.start_position + len(self.items) < self.total and (
            len(self.items) != ROUTING_SHARD_SIZE
        ):
            raise ValueError("non-final document route shard must be full")

    @classmethod
    def build(cls, manifest: DocumentWorkManifest) -> tuple[DocumentRouteShard, ...]:
        return tuple(
            cls(
                number=start // ROUTING_SHARD_SIZE,
                start_position=start,
                total=len(manifest.items),
                items=manifest.items[start : start + ROUTING_SHARD_SIZE],
            )
            for start in range(0, len(manifest.items), ROUTING_SHARD_SIZE)
        )


@dataclass(frozen=True, slots=True)
class DocumentBoundaryReadiness:
    """Final write proving metadata, items, routes, and audit manifest are durable."""

    query_fingerprint: str
    index_revision: str
    discovery_source_hash: str
    deferred_manifest_hash: str
    metadata_hash: str
    manifest_hash: str
    manifest_fingerprint: str
    item_count: int
    route_shard_count: int
    route_shard_size: int = ROUTING_SHARD_SIZE

    def __post_init__(self) -> None:
        if (
            any(
                not re.fullmatch(r"[0-9a-f]{64}", value)
                for value in (
                    self.query_fingerprint,
                    self.discovery_source_hash,
                    self.deferred_manifest_hash,
                    self.metadata_hash,
                    self.manifest_hash,
                    self.manifest_fingerprint,
                )
            )
            or not self.index_revision.strip()
            or self.item_count < 0
            or self.route_shard_count
            != (self.item_count + ROUTING_SHARD_SIZE - 1) // ROUTING_SHARD_SIZE
            or self.route_shard_size != ROUTING_SHARD_SIZE
        ):
            raise ValueError("document readiness binding is invalid")


@dataclass(frozen=True, slots=True)
class MetadataStageState:
    discovery: DiscoveryStageState
    status_collection: MetadataCollection
    manifest: DocumentWorkManifest
    coverage_gaps: tuple[CoverageGap, ...]


@dataclass(frozen=True, slots=True)
class DocumentOutcome:
    work_id: str
    status: DocumentOutcomeStatus
    result: DocumentWorkResult | None = None
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not self.work_id.strip():
            raise ValueError("document outcome work_id is required")
        if self.status is DocumentOutcomeStatus.SUCCEEDED and self.result is None:
            raise ValueError("successful document outcome requires a result")
        if self.status is not DocumentOutcomeStatus.SUCCEEDED and not self.error_code:
            raise ValueError("failed document outcome requires an error code")

    @property
    def terminal(self) -> bool:
        return self.status in {
            DocumentOutcomeStatus.SUCCEEDED,
            DocumentOutcomeStatus.FAILED,
        }


@dataclass(frozen=True, slots=True)
class FinalizationContext:
    job: ResearchJob
    gateway: GatewayPlanState
    metadata: MetadataStageState
    outcomes: tuple[DocumentOutcome, ...]
    transcripts: tuple[TranscriptEvidence, ...]
    coverage_gaps: tuple[CoverageGap, ...]


@dataclass(frozen=True, slots=True)
class GatewayReceipt:
    research_id: str
    status: JobStatus
    stage: str
    query_fingerprint: str
    index_revision: str
    interpreted_scope: Mapping[str, Any]
    metadata_task_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "research_id": self.research_id,
            "status": self.status.value,
            "stage": self.stage,
            "query_fingerprint": self.query_fingerprint,
            "index_revision": self.index_revision,
            "interpreted_scope": dict(self.interpreted_scope),
            "metadata_task_count": self.metadata_task_count,
        }


@dataclass(frozen=True, slots=True)
class DerivedResearchStatus:
    """Status derived only from immutable plans, pages, manifests, and outcomes."""

    research_id: str
    stage: str
    metadata_partitions_expected: int
    metadata_partitions_complete: int
    metadata_pages_expected: int
    metadata_pages_complete: int
    bill_document_checks_expected: int
    bill_document_checks_complete: int
    documents_expected: int
    documents_complete: int
    documents_failed: int
    overview_available: bool
    snapshot_ready: bool
    complete: bool


@dataclass(frozen=True, slots=True)
class TaskCompletionReceipt:
    """Write-once proof recorded only after all task side effects finish."""

    research_id: str
    query_fingerprint: str
    index_revision: str
    stage: ResearchTaskStage
    work_id: str
    task_identity: str
    payload_hash: str

    def __post_init__(self) -> None:
        if not self.research_id.strip() or not self.work_id.strip():
            raise ValueError("task completion identity is required")
        if (
            any(
                not re.fullmatch(r"[0-9a-f]{64}", value)
                for value in (
                    self.query_fingerprint,
                    self.task_identity,
                    self.payload_hash,
                )
            )
            or not self.index_revision.strip()
        ):
            raise ValueError("task completion binding is invalid")

    @classmethod
    def from_task(cls, task: ResearchTask) -> TaskCompletionReceipt:
        payload = json.dumps(
            dict(task.payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return cls(
            task.research_id,
            task.query_fingerprint,
            task.index_revision,
            task.stage,
            task.work_id,
            task.idempotency_key,
            hashlib.sha256(payload).hexdigest(),
        )


class MetadataPageClient(Protocol):
    def fetch_page(
        self,
        dataset: str,
        *,
        page: int = 1,
        page_size: int = 100,
        parameters: Mapping[str, str | int] | None = None,
        refresh: bool = False,
    ) -> ApiPage: ...


class ResearchCredentialCapabilityCodec(Protocol):
    def issue(
        self,
        *,
        research_id: str,
        query_fingerprint: str,
        assembly_api_key: str,
        ttl_seconds: int = 3600,
    ) -> str: ...

    def reveal(
        self,
        token: str,
        *,
        research_id: str,
        query_fingerprint: str,
    ) -> ResearchCredential: ...


class BillDocumentDiscoverer(Protocol):
    def discover_one(
        self,
        plan: ResearchPlan,
        bill: CandidateDecision,
    ) -> BillDocumentDiscovery: ...


class DocumentProcessor(Protocol):
    def process(
        self,
        kind: OfficialDocumentKind,
        official_url: str,
        *,
        refresh: bool = False,
    ) -> DocumentWorkResult: ...


class ResearchFinalizer(Protocol):
    def build(self, context: FinalizationContext) -> ResearchSnapshot: ...


class ResearchRunStore(Protocol):
    """Durable orchestration artifacts; implementations must use atomic put-if-absent."""

    def put_gateway(self, research_id: str, state: GatewayPlanState) -> GatewayPlanState: ...

    def get_gateway(self, research_id: str) -> GatewayPlanState | None: ...

    def put_page(
        self,
        research_id: str,
        phase: MetadataPhase,
        partition_id: str,
        page: ApiPage,
    ) -> ApiPage: ...

    def get_page(
        self,
        research_id: str,
        phase: MetadataPhase,
        partition_id: str,
        page_number: int,
    ) -> ApiPage | None: ...

    def page_readiness_for(
        self,
        research_id: str,
        phase: MetadataPhase,
        partition_id: str,
        page_numbers: Sequence[int],
    ) -> tuple[MetadataPageReadiness, ...]: ...

    def pages(
        self, research_id: str, phase: MetadataPhase, partition_id: str
    ) -> tuple[ApiPage, ...]: ...

    def put_discovery(
        self, research_id: str, state: DiscoveryStageState
    ) -> DiscoveryStageState: ...

    def get_discovery(self, research_id: str) -> DiscoveryStageState | None: ...

    def get_deferred_manifest(self, research_id: str) -> DeferredWorkManifest | None: ...

    def get_accepted_bill(self, research_id: str, bill_number: str) -> CandidateDecision | None: ...

    def get_document_bill(self, research_id: str, bill_number: str) -> CandidateDecision | None: ...

    def get_status_partition(
        self, research_id: str, partition_id: str
    ) -> MetadataPartition | None: ...

    def deferred_routes_for(
        self,
        research_id: str,
        start: int,
        stop: int,
        *,
        expected_total: int,
    ) -> tuple[DeferredWorkRoute, ...]: ...

    def get_provisional_overview(self, research_id: str) -> ProvisionalResearchOverview | None: ...

    def put_bill_discovery(
        self, research_id: str, outcome: BillDocumentDiscovery
    ) -> BillDocumentDiscovery: ...

    def get_bill_discovery(
        self, research_id: str, bill_number: str
    ) -> BillDocumentDiscovery | None: ...

    def bill_discoveries_for(
        self, research_id: str, bill_numbers: Sequence[str]
    ) -> tuple[BillDocumentDiscovery, ...]: ...

    def bill_discoveries(self, research_id: str) -> tuple[BillDocumentDiscovery, ...]: ...

    def put_metadata(self, research_id: str, state: MetadataStageState) -> MetadataStageState: ...

    def get_metadata(self, research_id: str) -> MetadataStageState | None: ...

    def get_document_manifest(self, research_id: str) -> DocumentWorkManifest | None: ...

    def get_document_item(self, research_id: str, work_id: str) -> DocumentWorkItem | None: ...

    def document_routes_for(
        self,
        research_id: str,
        start: int,
        stop: int,
        *,
        expected_total: int,
    ) -> tuple[DocumentWorkItem, ...]: ...

    def claim_phase_finalization(
        self,
        research_id: str,
        phase: MetadataPhase,
    ) -> bool: ...

    def put_task_completion(self, task: ResearchTask) -> TaskCompletionReceipt: ...

    def get_task_completion(self, task: ResearchTask) -> TaskCompletionReceipt | None: ...

    def put_document_outcome(
        self, research_id: str, outcome: DocumentOutcome
    ) -> DocumentOutcome: ...

    def get_document_outcome(self, research_id: str, work_id: str) -> DocumentOutcome | None: ...

    def document_outcomes_for(
        self, research_id: str, work_ids: Sequence[str]
    ) -> tuple[DocumentOutcome, ...]: ...

    def document_outcomes(self, research_id: str) -> tuple[DocumentOutcome, ...]: ...

    def put_snapshot(self, research_id: str, snapshot: ResearchSnapshot) -> ResearchSnapshot: ...

    def get_snapshot_summary(self, research_id: str) -> ResearchSnapshotSummary | None: ...

    def get_result_page(
        self,
        research_id: str,
        *,
        cursor: str | None = None,
        page_size: int = 20,
    ) -> dict[str, Any] | None: ...

    def get_overview_page(
        self,
        research_id: str,
        *,
        offset: int = 0,
        page_size: int = 20,
    ) -> dict[str, Any] | None: ...

    def get_evidence_index_entry(
        self, research_id: str, evidence_id: str
    ) -> EvidenceIndexEntry | None: ...

    def get_overflow_evidence_record(
        self, research_id: str, evidence_id: str
    ) -> EvidenceRecord | None: ...

    def get_next_full_text_evidence_id(
        self, research_id: str, after_evidence_id: str
    ) -> str | None: ...

    def get_next_core_evidence_id(self, research_id: str, after_evidence_id: str) -> str | None: ...


class InMemoryResearchRunStore:
    """Thread-safe reference store for tests and single-process local execution."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._gateways: dict[str, GatewayPlanState] = {}
        self._pages: dict[tuple[str, MetadataPhase, str, int], ApiPage] = {}
        self._page_readiness: dict[tuple[str, MetadataPhase, str, int], MetadataPageReadiness] = {}
        self._discoveries: dict[str, DiscoveryStageState] = {}
        self._deferred_manifests: dict[str, DeferredWorkManifest] = {}
        self._accepted_bills: dict[tuple[str, str], CandidateDecision] = {}
        self._document_bills: dict[tuple[str, str], CandidateDecision] = {}
        self._status_partitions: dict[tuple[str, str], MetadataPartition] = {}
        self._deferred_route_shards: dict[tuple[str, int], DeferredRouteShard] = {}
        self._provisional_overviews: dict[str, ProvisionalResearchOverview] = {}
        self._bill_discoveries: dict[tuple[str, str], BillDocumentDiscovery] = {}
        self._metadata: dict[str, MetadataStageState] = {}
        self._document_manifests: dict[str, DocumentWorkManifest] = {}
        self._document_items: dict[tuple[str, str], DocumentWorkItem] = {}
        self._document_route_shards: dict[tuple[str, int], DocumentRouteShard] = {}
        self._phase_finalization_claims: set[tuple[str, MetadataPhase]] = set()
        self._task_completions: dict[tuple[str, str], TaskCompletionReceipt] = {}
        self._outcomes: dict[tuple[str, str], DocumentOutcome] = {}
        self._overview_manifests: dict[str, OverviewTransportManifest] = {}
        self._overview_shards: dict[tuple[str, int], OverviewGroupShard] = {}
        self._snapshots: dict[str, ResearchSnapshot] = {}

    def put_gateway(self, research_id: str, state: GatewayPlanState) -> GatewayPlanState:
        return self._put_once(self._gateways, research_id, state)

    def get_gateway(self, research_id: str) -> GatewayPlanState | None:
        with self._lock:
            return self._gateways.get(research_id)

    def put_page(
        self,
        research_id: str,
        phase: MetadataPhase,
        partition_id: str,
        page: ApiPage,
    ) -> ApiPage:
        with self._lock:
            gateway = self._gateways.get(research_id)
            if gateway is None:
                raise LookupError("research gateway is not available")
            if phase is MetadataPhase.DISCOVERY:
                partitions = gateway.discovery_partitions
                partition = next(
                    (item for item in partitions if item.partition_id == partition_id),
                    None,
                )
            else:
                partition = self._status_partitions.get((research_id, partition_id))
            if partition is None:
                raise ValueError("metadata partition is outside the research plan")
            stored = self._put_once(
                self._pages,
                (research_id, phase, partition_id, page.page),
                page,
            )
            readiness = MetadataPageReadiness.create(gateway, phase, partition, stored)
            self._put_once(
                self._page_readiness,
                (research_id, phase, partition_id, page.page),
                readiness,
            )
            return stored

    def get_page(
        self,
        research_id: str,
        phase: MetadataPhase,
        partition_id: str,
        page_number: int,
    ) -> ApiPage | None:
        with self._lock:
            return self._pages.get((research_id, phase, partition_id, page_number))

    def page_readiness_for(
        self,
        research_id: str,
        phase: MetadataPhase,
        partition_id: str,
        page_numbers: Sequence[int],
    ) -> tuple[MetadataPageReadiness, ...]:
        numbers = tuple(page_numbers)
        if any(number < 1 for number in numbers) or len(numbers) != len(set(numbers)):
            raise ValueError("metadata page readiness numbers must be positive and unique")
        with self._lock:
            return tuple(
                readiness
                for number in numbers
                if (
                    readiness := self._page_readiness.get(
                        (research_id, phase, partition_id, number)
                    )
                )
                is not None
            )

    def pages(
        self, research_id: str, phase: MetadataPhase, partition_id: str
    ) -> tuple[ApiPage, ...]:
        with self._lock:
            return tuple(
                page
                for key, page in sorted(self._pages.items(), key=lambda item: item[0][3])
                if key[:3] == (research_id, phase, partition_id)
            )

    def put_discovery(self, research_id: str, state: DiscoveryStageState) -> DiscoveryStageState:
        # Local imports avoid the engine/overview discovery-state import cycle.
        from .overview import build_provisional_research_overview

        with self._lock:
            gateway = self._gateways.get(research_id)
            if gateway is None:
                raise LookupError("research gateway is not available")
            stored = self._put_once(self._discoveries, research_id, state)
            manifest = DeferredWorkManifest.create(gateway, stored)
            self._put_once(self._deferred_manifests, research_id, manifest)
            for decision in stored.resolution.bills.accepted:
                bill_number = _candidate_bill_number(decision)
                self._put_once(
                    self._accepted_bills,
                    (research_id, bill_number),
                    decision,
                )
                if bill_number in manifest.document_bill_numbers:
                    self._put_once(
                        self._document_bills,
                        (research_id, bill_number),
                        decision,
                    )
            for partition in manifest.status_partitions:
                self._put_once(
                    self._status_partitions,
                    (research_id, partition.partition_id),
                    partition,
                )
            for shard in DeferredRouteShard.build(manifest):
                self._put_once(
                    self._deferred_route_shards,
                    (research_id, shard.number),
                    shard,
                )
            overview = build_provisional_research_overview(stored)
            self._put_once(self._provisional_overviews, research_id, overview)
            return stored

    def get_discovery(self, research_id: str) -> DiscoveryStageState | None:
        with self._lock:
            return self._discoveries.get(research_id)

    def get_deferred_manifest(self, research_id: str) -> DeferredWorkManifest | None:
        with self._lock:
            manifest = self._deferred_manifests.get(research_id)
            if manifest is not None:
                return manifest
            discovery = self._discoveries.get(research_id)
            gateway = self._gateways.get(research_id)
            if discovery is None or gateway is None:
                return None
            return DeferredWorkManifest.create(gateway, discovery)

    def get_accepted_bill(self, research_id: str, bill_number: str) -> CandidateDecision | None:
        with self._lock:
            return self._accepted_bills.get((research_id, bill_number))

    def get_document_bill(self, research_id: str, bill_number: str) -> CandidateDecision | None:
        with self._lock:
            return self._document_bills.get((research_id, bill_number))

    def get_status_partition(
        self, research_id: str, partition_id: str
    ) -> MetadataPartition | None:
        with self._lock:
            return self._status_partitions.get((research_id, partition_id))

    def deferred_routes_for(
        self,
        research_id: str,
        start: int,
        stop: int,
        *,
        expected_total: int,
    ) -> tuple[DeferredWorkRoute, ...]:
        if not 0 <= start < stop <= expected_total:
            raise ValueError("deferred route range is outside its immutable plan")
        with self._lock:
            manifest = self._deferred_manifests.get(research_id)
            if manifest is None:
                return ()
            total = len(manifest.status_partitions) + len(manifest.document_bill_numbers)
            if total != expected_total:
                raise ValueError("deferred route total does not match its immutable plan")
            selected: list[DeferredWorkRoute] = []
            first_shard = start // ROUTING_SHARD_SIZE
            last_shard = (stop - 1) // ROUTING_SHARD_SIZE
            for number in range(first_shard, last_shard + 1):
                shard = self._deferred_route_shards.get((research_id, number))
                if shard is None:
                    return ()
                selected.extend(
                    route for route in shard.routes if start <= route.position < stop
                )
            return tuple(selected)

    def get_provisional_overview(self, research_id: str) -> ProvisionalResearchOverview | None:
        from .overview import build_provisional_research_overview

        with self._lock:
            overview = self._provisional_overviews.get(research_id)
            if overview is not None:
                return overview
            discovery = self._discoveries.get(research_id)
            return build_provisional_research_overview(discovery) if discovery is not None else None

    def put_bill_discovery(
        self, research_id: str, outcome: BillDocumentDiscovery
    ) -> BillDocumentDiscovery:
        return self._put_once(self._bill_discoveries, (research_id, outcome.bill_number), outcome)

    def get_bill_discovery(
        self, research_id: str, bill_number: str
    ) -> BillDocumentDiscovery | None:
        with self._lock:
            return self._bill_discoveries.get((research_id, bill_number))

    def bill_discoveries_for(
        self, research_id: str, bill_numbers: Sequence[str]
    ) -> tuple[BillDocumentDiscovery, ...]:
        with self._lock:
            return tuple(
                outcome
                for bill_number in sorted(set(bill_numbers))
                if (outcome := self._bill_discoveries.get((research_id, bill_number))) is not None
            )

    def bill_discoveries(self, research_id: str) -> tuple[BillDocumentDiscovery, ...]:
        with self._lock:
            return tuple(
                outcome
                for (candidate_research_id, _), outcome in sorted(self._bill_discoveries.items())
                if candidate_research_id == research_id
            )

    def put_metadata(self, research_id: str, state: MetadataStageState) -> MetadataStageState:
        with self._lock:
            stored = self._put_once(self._metadata, research_id, state)
            self._put_once(
                self._document_manifests,
                research_id,
                stored.manifest,
            )
            for item in stored.manifest.items:
                self._put_once(
                    self._document_items,
                    (research_id, item.work_id),
                    item,
                )
            for shard in DocumentRouteShard.build(stored.manifest):
                self._put_once(
                    self._document_route_shards,
                    (research_id, shard.number),
                    shard,
                )
            return stored

    def get_metadata(self, research_id: str) -> MetadataStageState | None:
        with self._lock:
            return self._metadata.get(research_id)

    def get_document_manifest(self, research_id: str) -> DocumentWorkManifest | None:
        with self._lock:
            manifest = self._document_manifests.get(research_id)
            if manifest is not None:
                return manifest
            metadata = self._metadata.get(research_id)
            return metadata.manifest if metadata is not None else None

    def get_document_item(self, research_id: str, work_id: str) -> DocumentWorkItem | None:
        with self._lock:
            return self._document_items.get((research_id, work_id))

    def document_routes_for(
        self,
        research_id: str,
        start: int,
        stop: int,
        *,
        expected_total: int,
    ) -> tuple[DocumentWorkItem, ...]:
        if not 0 <= start < stop <= expected_total:
            raise ValueError("document route range is outside its immutable plan")
        with self._lock:
            manifest = self._document_manifests.get(research_id)
            if manifest is None:
                return ()
            if len(manifest.items) != expected_total:
                raise ValueError("document route total does not match its immutable plan")
            selected: list[DocumentWorkItem] = []
            first_shard = start // ROUTING_SHARD_SIZE
            last_shard = (stop - 1) // ROUTING_SHARD_SIZE
            for number in range(first_shard, last_shard + 1):
                shard = self._document_route_shards.get((research_id, number))
                if shard is None:
                    return ()
                shard_start = shard.start_position
                for offset, item in enumerate(shard.items):
                    position = shard_start + offset
                    if start <= position < stop:
                        selected.append(item)
            return tuple(selected)

    def claim_phase_finalization(
        self,
        research_id: str,
        phase: MetadataPhase,
    ) -> bool:
        with self._lock:
            if research_id not in self._gateways:
                raise LookupError("research gateway is not available")
            key = (research_id, phase)
            if key in self._phase_finalization_claims:
                return False
            self._phase_finalization_claims.add(key)
            return True

    def put_task_completion(self, task: ResearchTask) -> TaskCompletionReceipt:
        return self._put_once(
            self._task_completions,
            (task.research_id, task.idempotency_key),
            TaskCompletionReceipt.from_task(task),
        )

    def get_task_completion(self, task: ResearchTask) -> TaskCompletionReceipt | None:
        with self._lock:
            receipt = self._task_completions.get((task.research_id, task.idempotency_key))
            if receipt is not None and receipt != TaskCompletionReceipt.from_task(task):
                raise ValueError("task completion binding is invalid")
            return receipt

    def put_document_outcome(self, research_id: str, outcome: DocumentOutcome) -> DocumentOutcome:
        key = (research_id, outcome.work_id)
        with self._lock:
            current = self._outcomes.get(key)
            if current is not None and current.terminal:
                if current != outcome:
                    raise ValueError("terminal document outcome cannot be replaced")
                return current
            self._outcomes[key] = outcome
            return outcome

    def get_document_outcome(self, research_id: str, work_id: str) -> DocumentOutcome | None:
        with self._lock:
            outcome = self._outcomes.get((research_id, work_id))
            return outcome if outcome is not None and outcome.terminal else None

    def document_outcomes_for(
        self, research_id: str, work_ids: Sequence[str]
    ) -> tuple[DocumentOutcome, ...]:
        identifiers = tuple(work_ids)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("document outcome work ids must be unique")
        with self._lock:
            return tuple(
                outcome
                for work_id in identifiers
                if (outcome := self._outcomes.get((research_id, work_id))) is not None
                and outcome.terminal
            )

    def document_outcomes(self, research_id: str) -> tuple[DocumentOutcome, ...]:
        with self._lock:
            return tuple(
                outcome
                for (candidate_research_id, _), outcome in sorted(self._outcomes.items())
                if candidate_research_id == research_id
            )

    def put_snapshot(self, research_id: str, snapshot: ResearchSnapshot) -> ResearchSnapshot:
        # Local imports avoid the engine/overview discovery-state import cycle.
        from .overview_transport import build_overview_transport

        bundle = build_overview_transport(snapshot)
        with self._lock:
            gateway = self._gateways.get(research_id)
            if gateway is None:
                raise LookupError("research gateway is not available")
            if (
                snapshot.research_id != research_id
                or snapshot.contract != gateway.job.contract
                or snapshot.query_fingerprint != gateway.job.query_fingerprint
                or snapshot.index_revision != gateway.job.index_revision
            ):
                raise ValueError("snapshot belongs to another research job")
            existing = self._snapshots.get(research_id)
            if existing is not None and existing != snapshot:
                raise ValueError("conflicting idempotent research artifact")
            # The overview is published before the snapshot readiness marker.
            # Readers therefore never observe a ready snapshot whose bounded
            # overview manifest or catalog shards are still missing.
            for shard in bundle.shards:
                self._put_once(
                    self._overview_shards,
                    (research_id, shard.number),
                    shard,
                )
            self._put_once(
                self._overview_manifests,
                research_id,
                bundle.manifest,
            )
            return self._put_once(self._snapshots, research_id, snapshot)

    def get_snapshot(self, research_id: str) -> ResearchSnapshot | None:
        with self._lock:
            return self._snapshots.get(research_id)

    def get_snapshot_summary(self, research_id: str) -> ResearchSnapshotSummary | None:
        with self._lock:
            snapshot = self._snapshots.get(research_id)
            return ResearchSnapshotSummary.from_snapshot(snapshot) if snapshot is not None else None

    def get_result_page(
        self,
        research_id: str,
        *,
        cursor: str | None = None,
        page_size: int = 20,
    ) -> dict[str, Any] | None:
        with self._lock:
            snapshot = self._snapshots.get(research_id)
            if snapshot is None:
                return None
            payload = snapshot.page(cursor=cursor, page_size=page_size).to_index_dict()
            required = tuple(
                item.id
                for item in snapshot.evidence
                if EvidenceIndexEntry.from_record(item).inline_text is None
            )
            payload["full_text_required_total"] = len(required)
            payload["first_full_text_required_id"] = required[0] if required else None
            return payload

    def get_overview_page(
        self,
        research_id: str,
        *,
        offset: int = 0,
        page_size: int = 20,
    ) -> dict[str, Any] | None:
        from .overview_transport import (
            overview_catalog_page,
            overview_catalog_required_shards,
        )

        if offset < 0:
            raise ValueError("offset must not be negative")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        with self._lock:
            snapshot = self._snapshots.get(research_id)
            if snapshot is None:
                return None
            gateway = self._gateways.get(research_id)
            manifest = self._overview_manifests.get(research_id)
            if gateway is None or manifest is None:
                raise RuntimeError("ready snapshot overview is missing")
            _validate_overview_snapshot_binding(
                research_id,
                manifest,
                snapshot,
                gateway,
            )
            required = overview_catalog_required_shards(
                manifest,
                offset=offset,
                page_size=page_size,
            )
            shards: list[OverviewGroupShard] = []
            for descriptor in required:
                shard = self._overview_shards.get((research_id, descriptor.number))
                if shard is None:
                    raise RuntimeError("ready snapshot overview shard is missing")
                shards.append(shard)
            catalog = overview_catalog_page(
                manifest,
                shards,
                offset=offset,
                page_size=page_size,
            )
            return _overview_page_payload(manifest, catalog.to_dict())

    def get_evidence_index_entry(
        self, research_id: str, evidence_id: str
    ) -> EvidenceIndexEntry | None:
        with self._lock:
            snapshot = self._snapshots.get(research_id)
            if snapshot is None:
                return None
            for evidence in snapshot.evidence:
                if evidence.id == evidence_id:
                    return EvidenceIndexEntry.from_record(evidence)
        raise LookupError("evidence id is absent from this immutable snapshot")

    def get_overflow_evidence_record(
        self, research_id: str, evidence_id: str
    ) -> EvidenceRecord | None:
        with self._lock:
            snapshot = self._snapshots.get(research_id)
            if snapshot is None:
                return None
            for evidence in snapshot.evidence:
                if evidence.id == evidence_id:
                    return evidence
        raise LookupError("evidence id is absent from this immutable snapshot")

    def get_next_full_text_evidence_id(
        self, research_id: str, after_evidence_id: str
    ) -> str | None:
        with self._lock:
            snapshot = self._snapshots.get(research_id)
            if snapshot is None:
                return None
            found = False
            for evidence in snapshot.evidence:
                if found and EvidenceIndexEntry.from_record(evidence).inline_text is None:
                    return evidence.id
                if evidence.id == after_evidence_id:
                    found = True
            if not found:
                raise LookupError("evidence id is absent from this immutable snapshot")
            return None

    def get_next_core_evidence_id(self, research_id: str, after_evidence_id: str) -> str | None:
        if not after_evidence_id.strip():
            raise ValueError("after_evidence_id is required")
        with self._lock:
            snapshot = self._snapshots.get(research_id)
            if snapshot is None:
                return None
            gateway = self._gateways.get(research_id)
            manifest = self._overview_manifests.get(research_id)
            if gateway is None or manifest is None:
                raise RuntimeError("ready snapshot overview is missing")
            _validate_overview_snapshot_binding(
                research_id,
                manifest,
                snapshot,
                gateway,
            )
            found = False
            for route in manifest.core:
                if found and not route.text_inline_complete:
                    return route.evidence_id
                if route.evidence_id == after_evidence_id:
                    found = True
            if not found:
                raise LookupError("evidence id is absent from the immutable core")
            return None

    def _put_once(
        self,
        target: MutableMapping[_Key, _Value],
        key: _Key,
        value: _Value,
    ) -> _Value:
        with self._lock:
            current = target.get(key)
            if current is not None:
                if current != value:
                    raise ValueError("conflicting idempotent research artifact")
                return current
            target[key] = value
            return value


def _validate_overview_snapshot_binding(
    research_id: str,
    manifest: OverviewTransportManifest,
    snapshot: ResearchSnapshot,
    gateway: GatewayPlanState,
) -> None:
    evidence_ids = {item.id for item in snapshot.evidence}
    if (
        gateway.job.id != research_id
        or snapshot.research_id != research_id
        or snapshot.contract != gateway.job.contract
        or snapshot.query_fingerprint != gateway.job.query_fingerprint
        or snapshot.index_revision != gateway.job.index_revision
        or manifest.research_id != snapshot.research_id
        or manifest.query_fingerprint != snapshot.query_fingerprint
        or manifest.index_revision != snapshot.index_revision
        or manifest.build_sha != snapshot.build_sha
        or manifest.coverage_requested != snapshot.coverage.requested
        or manifest.complete != snapshot.coverage.complete
        or manifest.evidence_count != len(snapshot.evidence)
        or not {route.evidence_id for route in manifest.core} <= evidence_ids
    ):
        raise RuntimeError("overview snapshot binding is invalid")


def _overview_page_payload(
    manifest: OverviewTransportManifest,
    catalog: dict[str, Any],
) -> dict[str, Any]:
    payload = manifest.to_dict()
    payload["catalog"] = catalog
    payload["core_full_text_required_ids"] = [
        route.evidence_id for route in manifest.core if not route.text_inline_complete
    ]
    return payload


class ResearchEngine:
    """Coordinate fast planning with bounded metadata and document work."""

    def __init__(
        self,
        *,
        index_revision: str,
        planner: ResearchContractPlanner,
        partition_planner: ResearchPartitionPlanner,
        jobs: ResearchJobStore,
        queue: ResearchTaskQueue,
        credentials: ResearchCredentialCapabilityCodec,
        page_client_factory: Callable[[str], MetadataPageClient],
        resolver: MetadataCandidateResolver,
        bill_documents: BillDocumentDiscoverer,
        document_worker: DocumentProcessor,
        finalizer: ResearchFinalizer,
        runs: ResearchRunStore,
        status_page_size: int = 100,
        task_retention_seconds: int = 86_400,
        direct_fanout_limit: int = 4,
        fanout_chunk_size: int = 8,
        fanout_delay_seconds: int = 0,
        corpus_recall_provider: CorpusRecallProvider | None = None,
    ) -> None:
        if not index_revision.strip():
            raise ValueError("index_revision is required")
        if not 1 <= status_page_size <= 1000:
            raise ValueError("status_page_size must be between 1 and 1000")
        if not 60 <= task_retention_seconds <= 86_400:
            raise ValueError("engine task retention must be between 60 seconds and 24 hours")
        if direct_fanout_limit < 1 or fanout_chunk_size < 1:
            raise ValueError("research fan-out limits must be positive")
        if not 0 <= fanout_delay_seconds <= task_retention_seconds:
            raise ValueError("research fan-out delay is outside task retention")
        self.corpus_recall_provider = corpus_recall_provider
        self.corpus_revision_id = (
            corpus_recall_provider.revision_id if corpus_recall_provider else None
        )
        self.corpus_binding_id = (
            corpus_recall_provider.binding_id if corpus_recall_provider else None
        )
        self.index_revision = _index_revision_with_corpus(
            index_revision,
            self.corpus_binding_id,
        )
        self.planner = planner
        self.partition_planner = partition_planner
        self.jobs = jobs
        self.queue = queue
        self.credentials = credentials
        self.page_client_factory = page_client_factory
        self.resolver = resolver
        self.bill_documents = bill_documents
        self.document_worker = document_worker
        self.finalizer = finalizer
        self.runs = runs
        self.status_page_size = status_page_size
        self.task_retention_seconds = task_retention_seconds
        self.direct_fanout_limit = direct_fanout_limit
        self.fanout_chunk_size = fanout_chunk_size
        self.fanout_delay_seconds = fanout_delay_seconds

    def gateway(
        self,
        query: str,
        *,
        assembly_api_key: str,
        as_of: datetime | None = None,
        korean_query: str | None = None,
        assembly_term: int | None = None,
        committees: Sequence[str] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        evidence_types: Sequence[EvidenceType | str] | None = None,
        job_ttl: timedelta = timedelta(days=1),
        credential_ttl_seconds: int = 86_400,
    ) -> GatewayReceipt:
        """Plan, persist, and enqueue page-one tasks without official-source I/O."""

        if not (self.task_retention_seconds <= credential_ttl_seconds <= job_ttl.total_seconds()):
            raise ValueError(
                "research TTLs must satisfy task retention <= credential TTL <= job TTL"
            )
        research_plan = self.planner.plan(
            query,
            as_of=as_of,
            korean_query=korean_query,
            assembly_term=assembly_term,
            committees=committees,
            date_from=date_from,
            date_to=date_to,
            evidence_types=evidence_types,
        )
        partition_plan = self.partition_planner.plan(research_plan)
        discovery = tuple(
            item.partition
            for item in partition_plan.planned_partitions
            if item.source is not OfficialSourceKind.BILL_STATUS
        )
        job = self.jobs.create(research_plan.contract, self.index_revision, ttl=job_ttl)
        state = GatewayPlanState(job, research_plan, partition_plan, discovery)
        try:
            self.runs.put_gateway(job.id, state)
            capability = self.credentials.issue(
                research_id=job.id,
                query_fingerprint=job.query_fingerprint,
                assembly_api_key=assembly_api_key,
                ttl_seconds=credential_ttl_seconds,
            )
            if len(discovery) <= self.direct_fanout_limit:
                for partition in discovery:
                    self._publish_page(
                        job,
                        MetadataPhase.DISCOVERY,
                        partition,
                        MetadataPageWork(partition.partition_id, 1),
                        capability,
                    )
            else:
                # Seed one durable coordinator. Each delivery publishes one
                # bounded window and chains the remainder, preventing a broad
                # request from turning into hundreds of simultaneous workers.
                self._publish_fanout(
                    job,
                    _DISCOVERY_FANOUT,
                    0,
                    len(discovery),
                    credential_capability=capability,
                )
            self._publish_phase_barrier(
                job,
                MetadataPhase.DISCOVERY,
                attempt=1,
                credential_capability=capability,
                delay_seconds=self._barrier_delay_seconds(1),
            )
        except Exception:
            with suppress(Exception):
                self.jobs.transition(
                    job.id,
                    JobStatus.FAILED,
                    stage="gateway_failed",
                    progress=0.0,
                    error_code="gateway_enqueue_failed",
                    error_message="research work could not be queued",
                )
            raise
        return GatewayReceipt(
            research_id=job.id,
            status=job.status,
            stage=job.stage,
            query_fingerprint=job.query_fingerprint,
            index_revision=job.index_revision,
            interpreted_scope=research_plan.interpreted_scope.to_dict(),
            metadata_task_count=len(discovery),
        )

    def derive_status(self, research_id: str) -> DerivedResearchStatus:
        """Derive observable progress without trusting process-local mutable state."""

        gateway = self._required_gateway(research_id)
        deferred = self.runs.get_deferred_manifest(research_id)
        document_manifest = self.runs.get_document_manifest(research_id)
        snapshot = self.runs.get_snapshot_summary(research_id)
        phase_partitions = [
            (MetadataPhase.DISCOVERY, partition) for partition in gateway.discovery_partitions
        ]
        if deferred is not None:
            phase_partitions.extend(
                (MetadataPhase.BILL_STATUS, partition) for partition in deferred.status_partitions
            )
        pages_expected = 0
        pages_complete = 0
        partitions_complete = 0
        for phase, partition in phase_partitions:
            pages = self.runs.pages(research_id, phase, partition.partition_id)
            first = next((page for page in pages if page.page == 1), None)
            expected = 1
            if first is not None and first.total_count is not None:
                expected = max(
                    1,
                    (first.total_count + partition.page_size - 1) // partition.page_size,
                )
            pages_expected += expected
            pages_complete += len({page.page for page in pages})
            if {page.page for page in pages} == set(range(1, expected + 1)):
                partitions_complete += 1

        bill_checks_expected = len(deferred.document_bill_numbers) if deferred is not None else 0
        bill_checks_complete = (
            len(
                self.runs.bill_discoveries_for(
                    research_id,
                    deferred.document_bill_numbers,
                )
            )
            if deferred is not None
            else 0
        )
        documents_expected = len(document_manifest.items) if document_manifest is not None else 0
        outcomes = (
            self.runs.document_outcomes_for(
                research_id,
                tuple(item.work_id for item in document_manifest.items),
            )
            if document_manifest is not None
            else ()
        )
        terminal_outcomes = tuple(item for item in outcomes if item.terminal)
        documents_failed = sum(
            item.status is DocumentOutcomeStatus.FAILED for item in terminal_outcomes
        )
        if snapshot is not None:
            stage = "complete" if snapshot.coverage.complete else "partial"
        elif document_manifest is not None:
            stage = "documents"
        elif deferred is not None:
            stage = "deferred_metadata"
        else:
            stage = "metadata_discovery"
        return DerivedResearchStatus(
            research_id=research_id,
            stage=stage,
            metadata_partitions_expected=len(phase_partitions),
            metadata_partitions_complete=partitions_complete,
            metadata_pages_expected=pages_expected,
            metadata_pages_complete=pages_complete,
            bill_document_checks_expected=bill_checks_expected,
            bill_document_checks_complete=bill_checks_complete,
            documents_expected=documents_expected,
            documents_complete=len(terminal_outcomes),
            documents_failed=documents_failed,
            overview_available=snapshot is not None or deferred is not None,
            snapshot_ready=snapshot is not None,
            complete=bool(snapshot is not None and snapshot.coverage.complete),
        )

    def process_metadata_task(self, task: ResearchTask) -> ApiPage | BillDocumentDiscovery | None:
        self._validate_task(task, ResearchTaskStage.COLLECT_METADATA)
        payload = dict(task.payload)
        work_kind = str(payload.get(_WORK_KIND) or "")
        if work_kind == _METADATA_PAGE:
            return self._process_page_task(task, payload)
        if work_kind == _BILL_DOCUMENTS:
            return self._process_bill_document_task(task, payload)
        if work_kind == _DISCOVERY_FANOUT:
            self._process_discovery_fanout(task, payload)
            return None
        if work_kind == _DEFERRED_FANOUT:
            self._process_deferred_fanout(task, payload)
            return None
        if work_kind == _DOCUMENT_FANOUT:
            self._process_document_fanout(task, payload)
            return None
        if work_kind == _PAGE_FANOUT:
            self._process_page_fanout(task, payload)
            return None
        if work_kind == _PHASE_BARRIER:
            self._process_phase_barrier(task, payload)
            return None
        raise ValueError("unknown metadata work kind")

    def _process_discovery_fanout(self, task: ResearchTask, payload: Mapping[str, Any]) -> None:
        gateway = self._required_gateway(task.research_id)
        start, stop = self._validate_fanout_task(
            task,
            payload,
            _DISCOVERY_FANOUT,
            len(gateway.discovery_partitions),
        )
        if task.credential_capability is None:
            raise ValueError("discovery fan-out lacks a credential capability")
        window_stop = self._fanout_window_stop(start, stop)
        for partition in gateway.discovery_partitions[start:window_stop]:
            self._publish_page(
                gateway.job,
                MetadataPhase.DISCOVERY,
                partition,
                MetadataPageWork(partition.partition_id, 1),
                task.credential_capability,
            )
        self._chain_fanout(
            gateway.job,
            _DISCOVERY_FANOUT,
            window_stop,
            stop,
            credential_capability=task.credential_capability,
        )

    def _process_deferred_fanout(self, task: ResearchTask, payload: Mapping[str, Any]) -> None:
        start, stop = self._validate_fanout_identity(task, payload, _DEFERRED_FANOUT)
        job = self._required_job(task.research_id)
        window_stop = self._fanout_window_stop(start, stop)
        routes = self.runs.deferred_routes_for(
            task.research_id,
            start,
            window_stop,
            expected_total=stop,
        )
        if len(routes) != window_stop - start:
            raise LookupError("deferred fan-out routing window is unavailable")
        for route in routes:
            if route.kind is DeferredRouteKind.BILL_STATUS:
                if task.credential_capability is None:
                    raise ValueError("status fan-out lacks a credential capability")
                assert route.status_partition is not None
                self._publish_page(
                    job,
                    MetadataPhase.BILL_STATUS,
                    route.status_partition,
                    MetadataPageWork(route.status_partition.partition_id, 1),
                    task.credential_capability,
                )
                continue
            assert route.bill_number is not None
            self._publish_bill_document(job, route.bill_number)
        self._chain_fanout(
            job,
            _DEFERRED_FANOUT,
            window_stop,
            stop,
            credential_capability=task.credential_capability,
        )

    def _process_document_fanout(self, task: ResearchTask, payload: Mapping[str, Any]) -> None:
        start, stop = self._validate_fanout_identity(task, payload, _DOCUMENT_FANOUT)
        window_stop = self._fanout_window_stop(start, stop)
        items = self.runs.document_routes_for(
            task.research_id,
            start,
            window_stop,
            expected_total=stop,
        )
        if len(items) != window_stop - start:
            raise LookupError("document fan-out routing window is unavailable")
        self._publish_document_items(
            task.research_id,
            items,
        )
        self._chain_fanout(
            self._required_job(task.research_id),
            _DOCUMENT_FANOUT,
            window_stop,
            stop,
        )

    def _process_page_fanout(self, task: ResearchTask, payload: Mapping[str, Any]) -> None:
        phase = MetadataPhase(str(payload.get(_PHASE) or ""))
        partition_id = str(payload.get(_PARTITION_ID) or "")
        partition = self._partition(task.research_id, phase, partition_id)
        first_values = self.runs.page_readiness_for(
            task.research_id,
            phase,
            partition_id,
            (1,),
        )
        if len(first_values) != 1:
            raise ValueError("page fan-out requires a ready stored first page")
        first = first_values[0]
        if (
            first.phase is not phase
            or first.partition_id != partition_id
            or first.dataset != partition.dataset
            or first.page != 1
            or first.page_size != partition.page_size
        ):
            raise ValueError("page fan-out marker does not match its stored partition")
        raw_expected = payload.get(_EXPECTED_TOTAL)
        if raw_expected is None or int(raw_expected) != first.total_count:
            raise ValueError("page fan-out total does not match its stored first page")
        total_pages = max(1, (first.total_count + first.page_size - 1) // first.page_size)
        follow_ups = tuple(
            MetadataPageWork(partition_id, number, first.total_count)
            for number in range(2, total_pages + 1)
        )
        start, stop = self._validate_page_fanout_task(
            task,
            payload,
            phase,
            partition_id,
            len(follow_ups),
        )
        if task.credential_capability is None:
            raise ValueError("page fan-out lacks a credential capability")
        window_stop = self._fanout_window_stop(start, stop)
        job = self._required_job(task.research_id)
        for follow_up in follow_ups[start:window_stop]:
            self._publish_page(
                job,
                phase,
                partition,
                follow_up,
                task.credential_capability,
            )
        if window_stop < stop:
            self._publish_page_fanout(
                job,
                phase,
                partition,
                first.total_count,
                window_stop,
                stop,
                task.credential_capability,
                delay_seconds=self.fanout_delay_seconds,
            )

    def _process_phase_barrier(self, task: ResearchTask, payload: Mapping[str, Any]) -> None:
        phase = MetadataPhase(str(payload.get(_PHASE) or ""))
        attempt = self._validate_barrier_task(
            task,
            payload,
            _PHASE_BARRIER,
            phase.value,
        )
        discovery_collection: MetadataCollection | None = None
        status_collection: MetadataCollection | None = None
        bill_discoveries: tuple[BillDocumentDiscovery, ...] | None = None
        if phase is MetadataPhase.DISCOVERY:
            # A completed discovery boundary is authoritative. Until then, read
            # only small final-write page markers. Raw page blobs are assembled
            # exactly once, after every dynamically expected marker is present.
            complete = self.runs.get_deferred_manifest(task.research_id) is not None
            if not complete and self._phase_complete(task.research_id, phase):
                if not self.runs.claim_phase_finalization(task.research_id, phase):
                    self._publish_phase_barrier(
                        self._required_job(task.research_id),
                        phase,
                        attempt=attempt + 1,
                        credential_capability=task.credential_capability,
                        delay_seconds=self._barrier_delay_seconds(attempt + 1),
                    )
                    return
                discovery_collection = self._try_assemble_collection(
                    task.research_id,
                    phase,
                    self._phase_partitions(task.research_id, phase),
                )
                if discovery_collection is None:
                    raise RuntimeError("ready metadata pages are not durably readable")
                complete = True
        else:
            manifest = self._required_deferred_manifest(task.research_id)
            complete = self.runs.get_document_manifest(task.research_id) is not None
            if not complete and self._phase_complete(task.research_id, phase):
                bill_discoveries = self.runs.bill_discoveries_for(
                    task.research_id,
                    manifest.document_bill_numbers,
                )
                complete = len(bill_discoveries) == len(manifest.document_bill_numbers)
                if complete:
                    if not self.runs.claim_phase_finalization(task.research_id, phase):
                        self._publish_phase_barrier(
                            self._required_job(task.research_id),
                            phase,
                            attempt=attempt + 1,
                            credential_capability=task.credential_capability,
                            delay_seconds=self._barrier_delay_seconds(attempt + 1),
                        )
                        return
                    status_collection = (
                        self._try_assemble_collection(
                            task.research_id,
                            phase,
                            manifest.status_partitions,
                        )
                        if manifest.status_partitions
                        else _empty_collection()
                    )
                    if status_collection is None:
                        raise RuntimeError("ready metadata pages are not durably readable")
        if not complete:
            self._publish_phase_barrier(
                self._required_job(task.research_id),
                phase,
                attempt=attempt + 1,
                credential_capability=task.credential_capability,
                delay_seconds=self._barrier_delay_seconds(attempt + 1),
            )
            return
        if phase is MetadataPhase.DISCOVERY:
            self._complete_discovery(
                task.research_id,
                task.credential_capability,
                preassembled=discovery_collection,
            )
        else:
            self._complete_metadata(
                task.research_id,
                prerequisites_verified=True,
                preassembled_status=status_collection,
                preassembled_bill_discoveries=bill_discoveries,
            )

    def process_document_task(self, task: ResearchTask) -> DocumentOutcome:
        self._validate_task(task, ResearchTaskStage.HYDRATE_DOCUMENT)
        item = self.runs.get_document_item(task.research_id, task.work_id)
        if item is None:
            raise ValueError("document task is absent from the stored manifest")
        payload = dict(task.payload)
        if (
            payload.get(_DOCUMENT_KIND) != item.kind.value
            or payload.get(_OFFICIAL_URL) != item.official_url
        ):
            raise ValueError("document task payload does not match the stored manifest")

        existing = self.runs.get_document_outcome(task.research_id, item.work_id)
        if existing is not None:
            return existing
        retry_error: DocumentWorkerError | None = None
        try:
            result = self.document_worker.process(item.kind, item.official_url)
            outcome = DocumentOutcome(
                item.work_id,
                DocumentOutcomeStatus.SUCCEEDED,
                result=result,
            )
        except DocumentWorkerError as exc:
            outcome = DocumentOutcome(
                item.work_id,
                (
                    DocumentOutcomeStatus.RETRYABLE_FAILURE
                    if exc.retryable
                    else DocumentOutcomeStatus.FAILED
                ),
                error_code=exc.code,
                error_message=str(exc),
            )
            if exc.retryable:
                retry_error = exc
        stored = self.runs.put_document_outcome(task.research_id, outcome)
        if retry_error is not None:
            # Queue consumers must not acknowledge this delivery.  The immutable
            # retryable outcome remains observable, and redelivery may replace it
            # with a terminal success or permanent failure.
            raise retry_error
        return stored

    def process_finalize_task(self, task: ResearchTask) -> ResearchSnapshot | None:
        self._validate_task(task, ResearchTaskStage.FINALIZE)
        payload = dict(task.payload)
        attempt = self._validate_barrier_task(
            task,
            payload,
            _DOCUMENT_FINALIZE_BARRIER,
            "documents",
        )
        manifest = self._required_document_manifest(task.research_id)
        required_ids = tuple(item.work_id for item in manifest.items)
        outcomes = self.runs.document_outcomes_for(task.research_id, required_ids)
        if len(outcomes) != len(required_ids):
            self._publish_document_finalize_barrier(
                self._required_job(task.research_id),
                attempt=attempt + 1,
                delay_seconds=self._barrier_delay_seconds(attempt + 1),
            )
            return None
        return self.try_finalize(task.research_id, outcomes=outcomes)

    def task_completed(self, task: ResearchTask) -> bool:
        """Return whether the dispatcher already finished every side effect."""

        job = self._required_job(task.research_id)
        if (
            task.query_fingerprint != job.query_fingerprint
            or task.index_revision != job.index_revision
        ):
            raise ValueError("research task does not match its persisted job")
        return self.runs.get_task_completion(task) is not None

    def complete_task(self, task: ResearchTask) -> TaskCompletionReceipt:
        """Record dispatcher completion after work and all chained publishes."""

        job = self._required_job(task.research_id)
        if (
            task.query_fingerprint != job.query_fingerprint
            or task.index_revision != job.index_revision
        ):
            raise ValueError("research task does not match its persisted job")
        return self.runs.put_task_completion(task)

    def fail_task(self, task: ResearchTask, *, error_code: str) -> ResearchJob | None:
        """End a poisoned durable task after the queue retry budget is spent."""

        if not error_code.strip():
            raise ValueError("task failure requires an error code")
        job = self.jobs.get(task.research_id)
        if job is None:
            # A marker-only delivery can outlive the job's artifact retention.
            # There is no durable run left to fail, so absence is an idempotent
            # terminal success rather than a poison message to retry forever.
            return None
        if (
            task.query_fingerprint != job.query_fingerprint
            or task.index_revision != job.index_revision
        ):
            raise ValueError("research task does not match its persisted job")
        if self.runs.get_task_completion(task) is not None:
            # The prior delivery finished and only its HTTP response/ACK was
            # lost. A later retry-budget marker must not overwrite success.
            return job
        if job.terminal:
            return job
        return self.jobs.transition(
            task.research_id,
            JobStatus.FAILED,
            stage=f"{task.stage.value}_failed",
            progress=job.progress,
            error_code=error_code,
            error_message="research task failed after the retry budget was exhausted",
        )

    def try_finalize(
        self,
        research_id: str,
        *,
        outcomes: Sequence[DocumentOutcome] | None = None,
    ) -> ResearchSnapshot | None:
        if self.runs.get_snapshot_summary(research_id) is not None:
            return None
        gateway = self._required_gateway(research_id)
        metadata = self._required_metadata(research_id)
        required_ids = tuple(item.work_id for item in metadata.manifest.items)
        current_outcomes = (
            tuple(outcomes)
            if outcomes is not None
            else self.runs.document_outcomes_for(research_id, required_ids)
        )
        outcome_by_id = {item.work_id: item for item in current_outcomes}
        if any(
            work_id not in outcome_by_id or not outcome_by_id[work_id].terminal
            for work_id in required_ids
        ):
            return None
        outcomes = tuple(outcome_by_id[work_id] for work_id in required_ids)
        document_gaps = tuple(
            CoverageGap(
                next(
                    item.evidence_types
                    for item in metadata.manifest.items
                    if item.work_id == outcome.work_id
                ),
                f"document_failed:{outcome.work_id}:{outcome.error_code}",
            )
            for outcome in outcomes
            if outcome.status is DocumentOutcomeStatus.FAILED
        )
        transcripts, transcript_gaps = _page_aware_transcripts(metadata, outcomes)
        gaps = (*metadata.coverage_gaps, *document_gaps, *transcript_gaps)
        job = self._required_job(research_id)
        context = FinalizationContext(
            job,
            gateway,
            metadata,
            outcomes,
            transcripts,
            gaps,
        )
        snapshot = self.finalizer.build(context)
        self._validate_snapshot(snapshot, context)
        stored = self.runs.put_snapshot(research_id, snapshot)
        current = self.jobs.get(research_id)
        if current is not None and not current.terminal:
            status = JobStatus.COMPLETE if stored.coverage.complete else JobStatus.PARTIAL
            with suppress(ValueError):
                self.jobs.transition(
                    research_id,
                    status,
                    stage=status.value,
                    progress=1.0,
                    coverage=stored.coverage,
                )
        return stored

    def _process_page_task(self, task: ResearchTask, payload: Mapping[str, Any]) -> ApiPage:
        phase = MetadataPhase(str(payload.get(_PHASE) or ""))
        partition_id = str(payload.get(_PARTITION_ID) or "")
        page_number = int(payload.get(_PAGE) or 0)
        raw_expected = payload.get(_EXPECTED_TOTAL)
        expected_total = int(raw_expected) if raw_expected is not None else None
        work = MetadataPageWork(partition_id, page_number, expected_total)
        if task.work_id != work.work_id:
            raise ValueError("metadata task work_id does not match its page")
        partition = self._partition(task.research_id, phase, partition_id)

        # Page identities are fixed and write-once. A direct lookup avoids
        # re-reading every prior page for each delivery (P² Blob reads on a
        # broad partition) while preserving the exact same idempotency check.
        existing = self.runs.get_page(
            task.research_id,
            phase,
            partition_id,
            page_number,
        )
        if existing is None:
            credential = self._reveal_task_credential(task)
            client = self.page_client_factory(credential.assembly_api_key)
            page = client.fetch_page(
                partition.dataset,
                page=work.page,
                page_size=partition.page_size,
                parameters=partition.parameters_dict(),
            )
            validate_fetched_page(partition, work, page)
            existing = page
        else:
            validate_fetched_page(partition, work, existing)
        # Idempotently re-put even an existing raw page so a delivery that died
        # between raw PUT and its final readiness marker heals on redelivery.
        existing = self.runs.put_page(task.research_id, phase, partition_id, existing)

        if existing.page == 1:
            expansion = expand_first_page(partition, existing)
            if task.credential_capability is None:
                raise ValueError("metadata page task lacks a credential capability")
            job = self._required_job(task.research_id)
            if len(expansion.pages) > self.direct_fanout_limit:
                assert existing.total_count is not None
                self._publish_page_fanout(
                    job,
                    phase,
                    partition,
                    existing.total_count,
                    0,
                    len(expansion.pages),
                    task.credential_capability,
                )
            else:
                for follow_up in expansion.pages:
                    self._publish_page(
                        job,
                        phase,
                        partition,
                        follow_up,
                        task.credential_capability,
                    )
        return existing

    def _complete_discovery(
        self,
        research_id: str,
        credential_capability: str | None,
        *,
        preassembled: MetadataCollection | None = None,
    ) -> None:
        gateway = self._required_gateway(research_id)
        deferred = self.runs.get_deferred_manifest(research_id)
        if deferred is not None:
            # A prior invocation may have written the compact readiness marker
            # and then failed while the hosted status subclass wrote its small
            # checkpoint. Re-put the compact audit state idempotently so the
            # checkpoint heals before any child fan-out is repeated.
            persisted = self.runs.get_discovery(research_id)
            if persisted is None:
                raise LookupError("ready discovery audit state is missing")
            self.runs.put_discovery(research_id, persisted)
            deferred = self._required_deferred_manifest(research_id)
        else:
            collection = preassembled or self._assemble_collection(
                research_id,
                MetadataPhase.DISCOVERY,
                gateway.discovery_partitions,
            )
            filtered, report = _strict_filter(
                collection,
                date_from=gateway.partition_plan.effective_date_from,
                date_to=gateway.partition_plan.effective_date_to,
                committees=gateway.research_plan.contract.committees,
            )
            # Contract dates scope legislative events (meetings, speeches, status
            # changes), not the lifetime of the bill entity.  An older proposal
            # can be actively discussed in the requested period and must proceed
            # through relevance, exact status, and document discovery.
            entity_plan = replace(
                gateway.research_plan,
                contract=replace(
                    gateway.research_plan.contract,
                    date_from=None,
                    date_to=None,
                ),
            )
            resolution = self.resolver.resolve(entity_plan, filtered)
            corpus_recall = self._recall_from_corpus(
                gateway.research_plan,
                resolution.criteria,
                expected_index_revision=gateway.job.index_revision,
            )
            if corpus_recall.status is CorpusRecallStatus.VERIFIED:
                bill_candidate_ids = tuple(
                    f"bill:{number}" for number in corpus_recall.exact_bill_numbers
                )
                meeting_id_by_url: dict[str, str] = {}
                duplicate_meeting_ids: set[str] = set()
                for decision in resolution.meetings.decisions:
                    try:
                        url = OpenAssemblyPipeline.minutes_url(dict(decision.candidate))
                    except ValueError:
                        continue
                    previous = meeting_id_by_url.setdefault(url, decision.candidate_id)
                    if previous != decision.candidate_id:
                        duplicate_meeting_ids.add(decision.candidate_id)
                missing_meeting_urls = tuple(
                    url for url in corpus_recall.exact_meeting_urls if url not in meeting_id_by_url
                )
                available_bill_ids = {
                    decision.candidate_id for decision in resolution.bills.decisions
                }
                missing_bill_ids = tuple(
                    candidate_id
                    for candidate_id in bill_candidate_ids
                    if candidate_id not in available_bill_ids
                )
                if missing_bill_ids or missing_meeting_urls or duplicate_meeting_ids:
                    reasons: list[str] = []
                    if missing_bill_ids:
                        reasons.append(
                            f"corpus_exact_bill_absent_from_metadata:{len(missing_bill_ids)}"
                        )
                    if missing_meeting_urls:
                        reasons.append(
                            f"corpus_exact_meeting_absent_from_metadata:{len(missing_meeting_urls)}"
                        )
                    if duplicate_meeting_ids:
                        reasons.append(
                            f"corpus_exact_meeting_metadata_ambiguous:{len(duplicate_meeting_ids)}"
                        )
                    corpus_recall = corpus_recall.fail_closed(*reasons)
                else:
                    resolution = accept_exact_corpus_candidates(
                        resolution,
                        bill_candidate_ids=bill_candidate_ids,
                        meeting_candidate_ids=tuple(
                            meeting_id_by_url[url] for url in corpus_recall.exact_meeting_urls
                        ),
                    )
            bill_numbers = _accepted_bill_numbers(resolution)
            status_partitions = (
                _status_partitions(
                    _accepted_bill_scopes(
                        resolution,
                        fallback_term=gateway.research_plan.contract.assembly_term,
                    ),
                    page_size=self.status_page_size,
                )
                if EvidenceType.BILL_STATUS in gateway.research_plan.contract.evidence_types
                else ()
            )
            document_bills = (
                bill_numbers
                if any(
                    item in gateway.research_plan.contract.evidence_types
                    for item in _BILL_DOCUMENT_EVIDENCE
                )
                else ()
            )
            self.runs.put_discovery(
                research_id,
                DiscoveryStageState(
                    collection,
                    filtered,
                    report,
                    resolution,
                    status_partitions,
                    document_bills,
                    corpus_recall,
                ),
            )
            deferred = self._required_deferred_manifest(research_id)

        job = self._required_job(research_id)
        deferred_count = len(deferred.status_partitions) + len(deferred.document_bill_numbers)
        if deferred_count > self.direct_fanout_limit:
            if deferred.status_partitions and credential_capability is None:
                raise ValueError("status fan-out requires a credential capability")
            self._publish_fanout(
                job,
                _DEFERRED_FANOUT,
                0,
                deferred_count,
                credential_capability=credential_capability,
            )
        else:
            if deferred.status_partitions:
                if credential_capability is None:
                    raise ValueError("status fan-out requires a credential capability")
                for partition in deferred.status_partitions:
                    self._publish_page(
                        job,
                        MetadataPhase.BILL_STATUS,
                        partition,
                        MetadataPageWork(partition.partition_id, 1),
                        credential_capability,
                    )
            for bill_number in deferred.document_bill_numbers:
                self._publish_bill_document(job, bill_number)
        self._transition_job_if_present(
            research_id,
            JobStatus.RUNNING,
            stage="deferred_metadata",
            progress=0.25,
        )
        if deferred_count:
            self._publish_phase_barrier(
                job,
                MetadataPhase.BILL_STATUS,
                attempt=1,
                delay_seconds=self._barrier_delay_seconds(1),
            )
        else:
            self._complete_metadata(research_id, prerequisites_verified=True)

    def _recall_from_corpus(
        self,
        plan: ResearchPlan,
        criteria: RelevanceCriteria,
        *,
        expected_index_revision: str,
    ) -> CorpusRecallState:
        if plan.contract.completeness != "comprehensive" or plan.contract.bill_numbers:
            return CorpusRecallState(CorpusRecallStatus.NOT_REQUIRED)
        if not (
            criteria.statute_terms
            or criteria.issue_terms
            or criteria.related_statute_terms
            or criteria.related_issue_terms
        ):
            return CorpusRecallState(CorpusRecallStatus.NOT_REQUIRED)
        provider = self.corpus_recall_provider
        if provider is None:
            return CorpusRecallState(
                CorpusRecallStatus.UNAVAILABLE,
                gap_reasons=("corpus_recall_provider_unconfigured",),
            )
        revision_id = self.corpus_revision_id
        if revision_id is None:
            raise RuntimeError("configured corpus recall lacks a captured revision")
        if self.index_revision != expected_index_revision:
            return CorpusRecallState(
                CorpusRecallStatus.INCOMPLETE,
                revision_id=revision_id,
                gap_reasons=("corpus_runtime_index_revision_mismatch",),
            )
        if provider.revision_id != revision_id or provider.binding_id != self.corpus_binding_id:
            return CorpusRecallState(
                CorpusRecallStatus.INCOMPLETE,
                revision_id=revision_id,
                gap_reasons=("corpus_runtime_provider_drift",),
            )
        try:
            recalled = provider.recall(plan, criteria)
        except (LookupError, RuntimeError, TypeError, ValueError):
            return CorpusRecallState(
                CorpusRecallStatus.INCOMPLETE,
                revision_id=revision_id,
                gap_reasons=("corpus_recall_provider_failed",),
            )
        if recalled.revision_id != revision_id:
            return CorpusRecallState(
                CorpusRecallStatus.INCOMPLETE,
                revision_id=revision_id,
                gap_reasons=("corpus_recall_revision_mismatch",),
            )
        return recalled

    def _process_bill_document_task(
        self, task: ResearchTask, payload: Mapping[str, Any]
    ) -> BillDocumentDiscovery:
        bill_number = str(payload.get(_BILL_NO) or "")
        if task.work_id != f"bill-documents:{bill_number}":
            raise ValueError("bill document task work_id does not match")
        bill = self.runs.get_document_bill(task.research_id, bill_number)
        if bill is None:
            raise ValueError("bill document task is outside the resolved candidate set")
        # Every bill discovery is stored under a fixed write-once identity.
        # Reading that identity directly keeps N concurrent bill checks O(N);
        # scanning all prior discoveries here made the same workload O(N²) in
        # hosted Blob reads and JSON decoding.
        existing = self.runs.get_bill_discovery(task.research_id, bill_number)
        if existing is None:
            try:
                discovered = self.bill_documents.discover_one(
                    self._required_gateway(task.research_id).research_plan,
                    bill,
                )
            except BillDocumentDiscoveryError as exc:
                if exc.retryable:
                    raise
                discovered = BillDocumentDiscovery(
                    bill_number,
                    failure_reason=exc.code,
                )
            if discovered.bill_number != bill_number:
                raise ValueError("bill document discovery returned another bill")
            existing = self.runs.put_bill_discovery(task.research_id, discovered)
        return existing

    def _deferred_metadata_complete(self, research_id: str) -> bool:
        manifest = self._required_deferred_manifest(research_id)
        if manifest.status_partitions and not self._phase_complete(
            research_id, MetadataPhase.BILL_STATUS
        ):
            return False
        return len(
            self.runs.bill_discoveries_for(
                research_id,
                manifest.document_bill_numbers,
            )
        ) == len(manifest.document_bill_numbers)

    def _complete_metadata(
        self,
        research_id: str,
        *,
        prerequisites_verified: bool = False,
        preassembled_status: MetadataCollection | None = None,
        preassembled_bill_discoveries: Sequence[BillDocumentDiscovery] | None = None,
    ) -> MetadataStageState | None:
        existing_manifest = self.runs.get_document_manifest(research_id)
        if existing_manifest is not None:
            persisted = self.runs.get_metadata(research_id)
            if persisted is None:
                raise LookupError("ready metadata audit state is missing")
            # Heal a status-checkpoint failure after the document manifest's
            # successful last write. All writes remain immutable/idempotent.
            self.runs.put_metadata(research_id, persisted)
            self._publish_document_manifest(research_id, existing_manifest)
            return None
        discovery = self.runs.get_discovery(research_id)
        if discovery is None:
            return None
        deferred = self._required_deferred_manifest(research_id)
        if not prerequisites_verified and not self._deferred_metadata_complete(research_id):
            return None
        bill_discoveries = (
            tuple(preassembled_bill_discoveries)
            if preassembled_bill_discoveries is not None
            else self.runs.bill_discoveries_for(
                research_id,
                deferred.document_bill_numbers,
            )
        )
        if len(bill_discoveries) != len(deferred.document_bill_numbers):
            return None

        status_collection = preassembled_status
        if status_collection is None:
            status_collection = (
                self._assemble_collection(
                    research_id,
                    MetadataPhase.BILL_STATUS,
                    deferred.status_partitions,
                )
                if deferred.status_partitions
                else _empty_collection()
            )
        gateway = self._required_gateway(research_id)
        manifest = _document_manifest(
            gateway.research_plan.contract,
            discovery.resolution,
            bill_discoveries,
        )
        gaps = _metadata_coverage_gaps(
            gateway.research_plan.contract,
            gateway.partition_plan,
            discovery,
            status_collection,
            bill_discoveries,
            manifest,
        )
        existing = self.runs.put_metadata(
            research_id,
            MetadataStageState(discovery, status_collection, manifest, gaps),
        )
        self._publish_document_manifest(research_id, existing.manifest)
        self._transition_job_if_present(
            research_id,
            JobStatus.RUNNING,
            stage="documents",
            progress=0.4,
        )
        return existing

    def _publish_document_manifest(self, research_id: str, manifest: DocumentWorkManifest) -> None:
        job = self._required_job(research_id)
        if len(manifest.items) > self.direct_fanout_limit:
            self._publish_fanout(job, _DOCUMENT_FANOUT, 0, len(manifest.items))
        else:
            self._publish_document_items(research_id, manifest.items)
        self._publish_document_finalize_barrier(
            job,
            attempt=1,
            delay_seconds=self._barrier_delay_seconds(1),
        )

    def _publish_document_items(self, research_id: str, items: Sequence[DocumentWorkItem]) -> None:
        job = self._required_job(research_id)
        for item in items:
            task = ResearchTask(
                research_id=research_id,
                stage=ResearchTaskStage.HYDRATE_DOCUMENT,
                work_id=item.work_id,
                query_fingerprint=job.query_fingerprint,
                index_revision=job.index_revision,
                payload=(
                    (_WORK_KIND, "document"),
                    (_DOCUMENT_KIND, item.kind.value),
                    (_OFFICIAL_URL, item.official_url),
                ),
            )
            self.queue.publish(task, retention_seconds=self.task_retention_seconds)

    def _publish_bill_document(self, job: ResearchJob, bill_number: str) -> None:
        if not re.fullmatch(r"\d{7}", bill_number):
            raise ValueError("bill document task requires an exact bill number")
        task = ResearchTask(
            research_id=job.id,
            stage=ResearchTaskStage.COLLECT_METADATA,
            work_id=f"bill-documents:{bill_number}",
            query_fingerprint=job.query_fingerprint,
            index_revision=job.index_revision,
            payload=((_WORK_KIND, _BILL_DOCUMENTS), (_BILL_NO, bill_number)),
        )
        self.queue.publish(task, retention_seconds=self.task_retention_seconds)

    def _publish_phase_barrier(
        self,
        job: ResearchJob,
        phase: MetadataPhase,
        *,
        attempt: int,
        credential_capability: str | None = None,
        delay_seconds: int = 0,
    ) -> None:
        task = ResearchTask(
            research_id=job.id,
            stage=ResearchTaskStage.COLLECT_METADATA,
            work_id=f"{_PHASE_BARRIER}:{phase.value}:{attempt}",
            query_fingerprint=job.query_fingerprint,
            index_revision=job.index_revision,
            payload=(
                (_WORK_KIND, _PHASE_BARRIER),
                (_PHASE, phase.value),
                (_ATTEMPT, attempt),
            ),
            credential_capability=credential_capability,
        )
        self.queue.publish(
            task,
            retention_seconds=self.task_retention_seconds,
            delay_seconds=delay_seconds,
        )

    def _publish_document_finalize_barrier(
        self,
        job: ResearchJob,
        *,
        attempt: int,
        delay_seconds: int = 0,
    ) -> None:
        task = ResearchTask(
            research_id=job.id,
            stage=ResearchTaskStage.FINALIZE,
            work_id=f"{_DOCUMENT_FINALIZE_BARRIER}:documents:{attempt}",
            query_fingerprint=job.query_fingerprint,
            index_revision=job.index_revision,
            payload=(
                (_WORK_KIND, _DOCUMENT_FINALIZE_BARRIER),
                (_ATTEMPT, attempt),
            ),
        )
        self.queue.publish(
            task,
            retention_seconds=self.task_retention_seconds,
            delay_seconds=delay_seconds,
        )

    def _barrier_delay_seconds(self, attempt: int) -> int:
        if attempt < 1:
            raise ValueError("research barrier attempt must be positive")
        base = max(1, self.fanout_delay_seconds)
        return min(60, base * min(attempt, 60))

    def _publish_fanout(
        self,
        job: ResearchJob,
        work_kind: str,
        start: int,
        stop: int,
        *,
        credential_capability: str | None = None,
        delay_seconds: int = 0,
    ) -> None:
        task = ResearchTask(
            research_id=job.id,
            stage=ResearchTaskStage.COLLECT_METADATA,
            work_id=f"{work_kind}:{start}:{stop}",
            query_fingerprint=job.query_fingerprint,
            index_revision=job.index_revision,
            payload=((_WORK_KIND, work_kind), (_START, start), (_STOP, stop)),
            credential_capability=credential_capability,
        )
        self.queue.publish(
            task,
            retention_seconds=self.task_retention_seconds,
            delay_seconds=delay_seconds,
        )

    def _chain_fanout(
        self,
        job: ResearchJob,
        work_kind: str,
        start: int,
        stop: int,
        *,
        credential_capability: str | None = None,
    ) -> None:
        if start >= stop:
            return
        self._publish_fanout(
            job,
            work_kind,
            start,
            stop,
            credential_capability=credential_capability,
            delay_seconds=self.fanout_delay_seconds,
        )

    def _fanout_window_stop(self, start: int, stop: int) -> int:
        if not 0 <= start < stop:
            raise ValueError("research fan-out window is invalid")
        return min(stop, start + self.fanout_chunk_size)

    def _publish_page_fanout(
        self,
        job: ResearchJob,
        phase: MetadataPhase,
        partition: MetadataPartition,
        expected_total: int,
        start: int,
        stop: int,
        capability: str,
        *,
        delay_seconds: int = 0,
    ) -> None:
        work_id = f"{_PAGE_FANOUT}:{phase.value}:{partition.partition_id}:{start}:{stop}"
        task = ResearchTask(
            research_id=job.id,
            stage=ResearchTaskStage.COLLECT_METADATA,
            work_id=work_id,
            query_fingerprint=job.query_fingerprint,
            index_revision=job.index_revision,
            payload=(
                (_WORK_KIND, _PAGE_FANOUT),
                (_PHASE, phase.value),
                (_PARTITION_ID, partition.partition_id),
                (_EXPECTED_TOTAL, expected_total),
                (_START, start),
                (_STOP, stop),
            ),
            credential_capability=capability,
        )
        self.queue.publish(
            task,
            retention_seconds=self.task_retention_seconds,
            delay_seconds=delay_seconds,
        )

    @staticmethod
    def _validate_fanout_task(
        task: ResearchTask,
        payload: Mapping[str, Any],
        work_kind: str,
        total: int,
    ) -> tuple[int, int]:
        start, stop = ResearchEngine._validate_fanout_identity(task, payload, work_kind)
        if stop > total:
            raise ValueError("research fan-out range is outside its immutable plan")
        return start, stop

    @staticmethod
    def _validate_fanout_identity(
        task: ResearchTask,
        payload: Mapping[str, Any],
        work_kind: str,
    ) -> tuple[int, int]:
        start = int(payload.get(_START, -1))
        stop = int(payload.get(_STOP, -1))
        if not 0 <= start < stop:
            raise ValueError("research fan-out range is invalid")
        if task.work_id != f"{work_kind}:{start}:{stop}":
            raise ValueError("research fan-out task identity does not match")
        return start, stop

    @staticmethod
    def _validate_barrier_task(
        task: ResearchTask,
        payload: Mapping[str, Any],
        work_kind: str,
        scope: str,
    ) -> int:
        attempt = int(payload.get(_ATTEMPT, 0))
        if not 1 <= attempt <= 1_000_000:
            raise ValueError("research barrier attempt is invalid")
        if payload.get(_WORK_KIND) != work_kind:
            raise ValueError("research barrier work kind does not match")
        if task.work_id != f"{work_kind}:{scope}:{attempt}":
            raise ValueError("research barrier task identity does not match")
        return attempt

    @staticmethod
    def _validate_page_fanout_task(
        task: ResearchTask,
        payload: Mapping[str, Any],
        phase: MetadataPhase,
        partition_id: str,
        total: int,
    ) -> tuple[int, int]:
        start = int(payload.get(_START, -1))
        stop = int(payload.get(_STOP, -1))
        if not 0 <= start < stop <= total:
            raise ValueError("page fan-out range is outside its immutable plan")
        expected = f"{_PAGE_FANOUT}:{phase.value}:{partition_id}:{start}:{stop}"
        if task.work_id != expected:
            raise ValueError("page fan-out task identity does not match")
        return start, stop

    def _phase_complete(self, research_id: str, phase: MetadataPhase) -> bool:
        """Check only small page-ready markers; never decode raw page blobs."""

        partitions = self._phase_partitions(research_id, phase)
        for partition in partitions:
            first_values = self.runs.page_readiness_for(
                research_id,
                phase,
                partition.partition_id,
                (1,),
            )
            if len(first_values) != 1:
                return False
            first = first_values[0]
            if (
                first.phase is not phase
                or first.partition_id != partition.partition_id
                or first.dataset != partition.dataset
                or first.page != 1
                or first.page_size != partition.page_size
            ):
                raise RuntimeError("metadata page readiness does not match its partition")
            expected_pages = max(1, (first.total_count + first.page_size - 1) // first.page_size)
            following_numbers = tuple(range(2, expected_pages + 1))
            following = self.runs.page_readiness_for(
                research_id,
                phase,
                partition.partition_id,
                following_numbers,
            )
            if len(following) != len(following_numbers):
                return False
            readiness = (first, *following)
            if tuple(item.page for item in readiness) != tuple(range(1, expected_pages + 1)):
                raise RuntimeError("metadata page readiness sequence is invalid")
            if any(
                item.total_count != first.total_count
                or item.page_size != first.page_size
                or item.dataset != first.dataset
                for item in readiness
            ):
                raise RuntimeError("metadata page readiness totals are inconsistent")
        return True

    def _assemble_collection(
        self,
        research_id: str,
        phase: MetadataPhase,
        partitions: Sequence[MetadataPartition],
    ) -> MetadataCollection:
        result = self._try_assemble_collection(research_id, phase, partitions)
        if result is None:
            raise ValueError("metadata collection is not complete")
        return result

    def _try_assemble_collection(
        self,
        research_id: str,
        phase: MetadataPhase,
        partitions: Sequence[MetadataPartition],
    ) -> MetadataCollection | None:
        """Load and validate each immutable page exactly once for this attempt."""

        page_sets: dict[str, tuple[ApiPage, ...]] = {}
        for partition in partitions:
            pages = self.runs.pages(research_id, phase, partition.partition_id)
            first = next((page for page in pages if page.page == 1), None)
            if first is None or first.total_count is None:
                return None
            expected_pages = max(
                1,
                (first.total_count + partition.page_size - 1) // partition.page_size,
            )
            if {page.page for page in pages} != set(range(1, expected_pages + 1)):
                return None
            page_sets[partition.partition_id] = pages
        results = {
            partition.partition_id: assemble_partition_pages(
                partition,
                page_sets[partition.partition_id],
            )
            for partition in partitions
        }
        client = _ArtifactResultClient(partitions, results)
        return MetadataCollector(cast(AssemblyOpenApiClient, client)).collect(partitions)

    def _publish_page(
        self,
        job: ResearchJob,
        phase: MetadataPhase,
        partition: MetadataPartition,
        work: MetadataPageWork,
        capability: str,
    ) -> None:
        task = ResearchTask(
            research_id=job.id,
            stage=ResearchTaskStage.COLLECT_METADATA,
            work_id=work.work_id,
            query_fingerprint=job.query_fingerprint,
            index_revision=job.index_revision,
            payload=(
                (_WORK_KIND, _METADATA_PAGE),
                (_PHASE, phase.value),
                (_PARTITION_ID, partition.partition_id),
                (_PAGE, work.page),
                (_EXPECTED_TOTAL, work.expected_total),
            ),
            credential_capability=capability,
        )
        self.queue.publish(task, retention_seconds=self.task_retention_seconds)

    def _partition(
        self, research_id: str, phase: MetadataPhase, partition_id: str
    ) -> MetadataPartition:
        if phase is MetadataPhase.BILL_STATUS:
            partition = self.runs.get_status_partition(research_id, partition_id)
            if partition is None:
                raise ValueError("metadata partition is absent from the stored plan")
            return partition
        for partition in self._required_gateway(research_id).discovery_partitions:
            if partition.partition_id == partition_id:
                return partition
        raise ValueError("metadata partition is absent from the stored plan")

    def _phase_partitions(
        self, research_id: str, phase: MetadataPhase
    ) -> tuple[MetadataPartition, ...]:
        if phase is MetadataPhase.DISCOVERY:
            return self._required_gateway(research_id).discovery_partitions
        return self._required_deferred_manifest(research_id).status_partitions

    def _reveal_task_credential(self, task: ResearchTask) -> ResearchCredential:
        if task.credential_capability is None:
            raise ValueError("metadata page task lacks a credential capability")
        return self.credentials.reveal(
            task.credential_capability,
            research_id=task.research_id,
            query_fingerprint=task.query_fingerprint,
        )

    def _validate_task(self, task: ResearchTask, stage: ResearchTaskStage) -> None:
        if task.stage is not stage:
            raise ValueError(f"expected {stage.value} research task")
        job = self._required_job(task.research_id)
        if (
            task.query_fingerprint != job.query_fingerprint
            or task.index_revision != job.index_revision
        ):
            raise ValueError("research task does not match its persisted job")
        if (
            datetime.now(UTC) >= job.expires_at
            and self.runs.get_snapshot_summary(task.research_id) is None
        ):
            raise ValueError("research task belongs to an expired job")

    def _validate_snapshot(self, snapshot: ResearchSnapshot, context: FinalizationContext) -> None:
        if (
            snapshot.research_id != context.job.id
            or snapshot.contract != context.job.contract
            or snapshot.index_revision != context.job.index_revision
        ):
            raise ValueError("finalizer returned a snapshot for another research job")
        coverage_by_type = {entry.evidence_type: entry for entry in snapshot.coverage.entries}
        for gap in context.coverage_gaps:
            for evidence_type in gap.evidence_types:
                if evidence_type not in snapshot.contract.evidence_types:
                    continue
                entry = coverage_by_type[evidence_type]
                if gap.reason not in entry.gap_reasons:
                    raise ValueError("finalizer omitted an explicit engine coverage gap")

    def _required_gateway(self, research_id: str) -> GatewayPlanState:
        result = self.runs.get_gateway(research_id)
        if result is None:
            raise LookupError(f"research gateway state not found: {research_id}")
        return result

    def _required_discovery(self, research_id: str) -> DiscoveryStageState:
        result = self.runs.get_discovery(research_id)
        if result is None:
            raise LookupError(f"research discovery state not found: {research_id}")
        return result

    def _required_deferred_manifest(self, research_id: str) -> DeferredWorkManifest:
        result = self.runs.get_deferred_manifest(research_id)
        if result is None:
            raise LookupError(f"research deferred manifest not found: {research_id}")
        return result

    def _required_metadata(self, research_id: str) -> MetadataStageState:
        result = self.runs.get_metadata(research_id)
        if result is None:
            raise LookupError(f"research metadata state not found: {research_id}")
        return result

    def _required_document_manifest(self, research_id: str) -> DocumentWorkManifest:
        result = self.runs.get_document_manifest(research_id)
        if result is None:
            raise LookupError(f"research document manifest not found: {research_id}")
        return result

    def _required_job(self, research_id: str) -> ResearchJob:
        gateway = self.runs.get_gateway(research_id)
        if gateway is not None:
            return gateway.job
        result = self.jobs.get(research_id)
        if result is None:
            raise LookupError(f"research job not found: {research_id}")
        return result

    def _required_current_job(self, research_id: str) -> ResearchJob:
        result = self.jobs.get(research_id)
        if result is None:
            raise LookupError(f"current research job not found: {research_id}")
        return result

    def _transition_job_if_present(
        self,
        research_id: str,
        status: JobStatus,
        *,
        stage: str,
        progress: float,
    ) -> None:
        if self.jobs.get(research_id) is None:
            return
        self.jobs.transition(
            research_id,
            status,
            stage=stage,
            progress=progress,
        )


class _ArtifactResultClient:
    """Feed coherent stored results through MetadataCollector without network I/O."""

    def __init__(
        self,
        partitions: Sequence[MetadataPartition],
        results: Mapping[str, ApiResult],
    ) -> None:
        self._by_query = {
            (partition.dataset, partition.page_size, partition.parameters): results[
                partition.partition_id
            ]
            for partition in partitions
        }

    def fetch_all(
        self,
        dataset: str,
        *,
        page_size: int = 100,
        parameters: Mapping[str, str | int] | None = None,
        refresh: bool = False,
    ) -> ApiResult:
        del refresh
        key = (dataset, page_size, tuple(sorted((parameters or {}).items())))
        try:
            return self._by_query[key]
        except KeyError as exc:
            raise ValueError("assembled metadata result is absent") from exc


def _chunk_ranges(total: int, chunk_size: int) -> tuple[tuple[int, int], ...]:
    if total < 0 or chunk_size < 1:
        raise ValueError("fan-out range inputs are invalid")
    return tuple((start, min(total, start + chunk_size)) for start in range(0, total, chunk_size))


def _status_partitions(
    bill_scopes: Sequence[tuple[str, int]], *, page_size: int
) -> tuple[MetadataPartition, ...]:
    return tuple(
        MetadataPartition.create(
            f"bill-status:{bill_number}",
            MetadataKind.BILL,
            BILL_STATUS_DATASET,
            parameters={"AGE": assembly_term, "BILL_NO": bill_number},
            page_size=page_size,
        )
        for bill_number, assembly_term in sorted(set(bill_scopes))
    )


def _accepted_bill_numbers(resolution: MetadataResolution) -> tuple[str, ...]:
    return tuple(sorted({_candidate_bill_number(item) for item in resolution.bills.accepted}))


def _candidate_bill_number(decision: CandidateDecision) -> str:
    raw = decision.candidate.get("BILL_NO", decision.candidate.get("bill_no"))
    bill_number = str(raw).strip() if raw is not None else ""
    if (
        decision.kind is not MetadataKind.BILL
        or not decision.accepted
        or decision.candidate_id != f"bill:{bill_number}"
        or not re.fullmatch(r"\d{7}", bill_number)
    ):
        raise ValueError("accepted bill candidate lacks an exact bill identity")
    return bill_number


def _candidate_meeting_url(decision: CandidateDecision) -> str | None:
    if decision.kind is not MetadataKind.MEETING or not decision.accepted:
        return None
    prefix = "meeting:"
    if not decision.candidate_id.startswith(prefix):
        return None
    candidate_url = str(
        decision.candidate.get("PDF_LINK_URL", decision.candidate.get("DOWN_URL", ""))
    ).strip()
    identity_url = decision.candidate_id.removeprefix(prefix).strip()
    return identity_url if candidate_url == identity_url and identity_url else None


def _criteria_hash(criteria: RelevanceCriteria) -> str:
    payload = {
        "query": criteria.query,
        "bill_numbers": list(criteria.bill_numbers),
        "statute_terms": list(criteria.statute_terms),
        "issue_terms": list(criteria.issue_terms),
        "related_statute_terms": list(criteria.related_statute_terms),
        "related_issue_terms": list(criteria.related_issue_terms),
        "committees": list(criteria.committees),
        "date_from": criteria.date_from.isoformat() if criteria.date_from else None,
        "date_to": criteria.date_to.isoformat() if criteria.date_to else None,
        "minimum_score": criteria.minimum_score,
        "terminology_version": criteria.terminology_version,
        "terminology_expansions": [
            {
                "source_text": item.source_text,
                "source_concept_id": item.source_concept_id,
                "target_concept_id": item.target_concept_id,
                "term": item.term,
                "category": item.category.value,
                "relation": item.relation.value,
                "reason": item.reason,
            }
            for item in criteria.terminology_expansions
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _provenance_source_hash(
    partitions: Sequence[PartitionProvenance],
) -> str:
    digest = hashlib.sha256()
    for partition in partitions:
        digest.update(partition.partition_id.encode())
        digest.update(b":")
        digest.update(partition.result_hash.encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _accepted_bill_scopes(
    resolution: MetadataResolution, *, fallback_term: int
) -> tuple[tuple[str, int], ...]:
    """Bind every exact status request to the bill's own Assembly term."""

    result: dict[str, int] = {}
    for item in resolution.bills.accepted:
        bill_number = _candidate_bill_number(item)
        raw_term = _first_text(item.candidate, ("AGE", "AGE_NM", "assembly_term"))
        digits = re.search(r"\d+", raw_term or "")
        assembly_term = int(digits.group()) if digits else int(bill_number[:2])
        if assembly_term < 1:
            assembly_term = fallback_term
        previous = result.setdefault(bill_number, assembly_term)
        if previous != assembly_term:
            raise ValueError("accepted bill number maps to conflicting Assembly terms")
    return tuple(sorted(result.items()))


def _accepted_bill_by_number(resolution: MetadataResolution, bill_number: str) -> CandidateDecision:
    for item in resolution.bills.accepted:
        if _candidate_bill_number(item) == bill_number:
            return item
    raise ValueError("accepted bill is absent from stored resolution")


def _strict_filter(
    collection: MetadataCollection,
    *,
    date_from: date,
    date_to: date,
    committees: tuple[str, ...],
) -> tuple[MetadataCollection, StrictFilterReport]:
    bills, bill_report = _filter_family(
        collection.bills,
        date_fields=("PROPOSE_DT", "PROPOSE_DATE", "date"),
        committee_fields=("COMMITTEE", "COMMITTEE_NM", "committee"),
        date_from=date_from,
        date_to=date_to,
        committees=committees,
        enforce_date=False,
    )
    meetings, meeting_report = _filter_family(
        collection.meetings,
        date_fields=("CONF_DATE", "MEETING_DATE", "MTG_DATE", "date"),
        committee_fields=("COMM_NAME", "COMMITTEE", "COMMITTEE_NAME", "committee"),
        date_from=date_from,
        date_to=date_to,
        committees=committees,
        enforce_date=True,
    )
    return (
        MetadataCollection(
            bills=bills,
            meetings=meetings,
            partitions=collection.partitions,
            coverage=collection.coverage,
        ),
        StrictFilterReport(bill_report, meeting_report),
    )


def _filter_family(
    rows: Sequence[Mapping[str, Any]],
    *,
    date_fields: Sequence[str],
    committee_fields: Sequence[str],
    date_from: date,
    date_to: date,
    committees: tuple[str, ...],
    enforce_date: bool,
) -> tuple[tuple[dict[str, Any], ...], FamilyFilterAccounting]:
    kept: list[dict[str, Any]] = []
    outside_date = committee_mismatch = missing_date = missing_committee = 0
    for row in rows:
        if enforce_date:
            candidate_date = _first_date(row, date_fields)
            if candidate_date is None:
                missing_date += 1
                continue
            if candidate_date < date_from or candidate_date > date_to:
                outside_date += 1
                continue
        if committees:
            committee = _first_text(row, committee_fields)
            if committee is None:
                missing_committee += 1
                continue
            if not any(value in committee for value in committees):
                committee_mismatch += 1
                continue
        kept.append(dict(row))
    return (
        tuple(kept),
        FamilyFilterAccounting(
            source_count=len(rows),
            kept_count=len(kept),
            outside_date_count=outside_date,
            committee_mismatch_count=committee_mismatch,
            missing_date_count=missing_date,
            missing_committee_count=missing_committee,
        ),
    )


def _first_date(row: Mapping[str, Any], fields: Sequence[str]) -> date | None:
    value = _first_text(row, fields)
    if value is None:
        return None
    compact = re.sub(r"[^0-9]", "", value)[:8]
    if len(compact) != 8:
        return None
    try:
        return datetime.strptime(compact, "%Y%m%d").date()
    except ValueError:
        return None


def _first_text(row: Mapping[str, Any], fields: Sequence[str]) -> str | None:
    for field in fields:
        value = row.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _document_manifest(
    contract: ResearchContract,
    resolution: MetadataResolution,
    bill_discoveries: Sequence[BillDocumentDiscovery],
) -> DocumentWorkManifest:
    requested = set(contract.evidence_types)
    items: list[DocumentWorkItem] = []
    for decision in resolution.meetings.accepted:
        url = OpenAssemblyPipeline.minutes_url(dict(decision.candidate))
        evidence_types = [item for item in _MEETING_EVIDENCE if item in requested]
        if (
            EvidenceType.SUBCOMMITTEE_MINUTES in requested
            and classify_meeting(decision.candidate) is MeetingSource.SUBCOMMITTEE
        ):
            evidence_types.append(EvidenceType.SUBCOMMITTEE_MINUTES)
        if evidence_types:
            items.append(
                DocumentWorkItem.create(
                    OfficialDocumentKind.MINUTES,
                    url,
                    evidence_types=evidence_types,
                )
            )
    gaps: list[CoverageGap] = []
    requested_bill_documents = tuple(item for item in _BILL_DOCUMENT_EVIDENCE if item in requested)
    for discovery in bill_discoveries:
        items.extend(discovery.items)
        if discovery.failure_reason and requested_bill_documents:
            gaps.append(
                CoverageGap(
                    requested_bill_documents,
                    f"bill_document_discovery_failed:{discovery.bill_number}:"
                    f"{discovery.failure_reason}",
                )
            )
    return DocumentWorkManifest.create(items, bill_discoveries, gaps)


def _metadata_coverage_gaps(
    contract: ResearchContract,
    partition_plan: ResearchPartitionPlan,
    discovery: DiscoveryStageState,
    status: MetadataCollection,
    bill_discoveries: Sequence[BillDocumentDiscovery],
    manifest: DocumentWorkManifest,
) -> tuple[CoverageGap, ...]:
    requested = tuple(contract.evidence_types)
    gaps = [
        CoverageGap(
            requested,
            f"requested_date_scope_not_fully_represented:{adjustment}",
        )
        for adjustment in partition_plan.range_adjustments
    ]
    corpus_recall = discovery.corpus_recall
    if corpus_recall is not None and not corpus_recall.comprehensive:
        reasons = corpus_recall.gap_reasons or ("corpus_recall_incomplete",)
        gaps.extend(CoverageGap(requested, f"full_text_corpus:{reason}") for reason in reasons)
    if corpus_recall is not None and corpus_recall.status is CorpusRecallStatus.VERIFIED:
        manifest_ids = {item.work_id for item in manifest.items}
        missing_work_ids = tuple(
            work_id for work_id in corpus_recall.required_work_ids if work_id not in manifest_ids
        )
        if missing_work_ids:
            gaps.append(
                CoverageGap(
                    requested,
                    f"full_text_corpus:corpus_candidate_work_missing:{len(missing_work_ids)}",
                )
            )
    coverage = discovery.collection.coverage
    if not coverage.source_complete:
        gaps.append(CoverageGap(requested, "metadata_source_incomplete"))
    if coverage.bill_rejected_rows:
        gaps.append(
            CoverageGap(
                (EvidenceType.BILLS,),
                f"bill_metadata_rejected_rows:{coverage.bill_rejected_rows}",
            )
        )
    if coverage.meeting_rejected_rows:
        types = tuple(item for item in _MEETING_EVIDENCE if item in requested)
        if EvidenceType.SUBCOMMITTEE_MINUTES in requested:
            types = (*types, EvidenceType.SUBCOMMITTEE_MINUTES)
        if types:
            gaps.append(
                CoverageGap(
                    types,
                    f"meeting_metadata_rejected_rows:{coverage.meeting_rejected_rows}",
                )
            )
    for family, report, evidence_types in (
        ("bill", discovery.filter_report.bills, (EvidenceType.BILLS,)),
        (
            "meeting",
            discovery.filter_report.meetings,
            tuple(
                item
                for item in (*_MEETING_EVIDENCE, EvidenceType.SUBCOMMITTEE_MINUTES)
                if item in requested
            ),
        ),
    ):
        if report.missing_date_count and evidence_types:
            gaps.append(
                CoverageGap(
                    evidence_types,
                    f"{family}_metadata_missing_date:{report.missing_date_count}",
                )
            )
        if report.missing_committee_count and evidence_types:
            gaps.append(
                CoverageGap(
                    evidence_types,
                    f"{family}_metadata_missing_committee:{report.missing_committee_count}",
                )
            )
    if discovery.status_partitions:
        if not status.coverage.source_complete:
            gaps.append(CoverageGap((EvidenceType.BILL_STATUS,), "bill_status_source_incomplete"))
        found = {str(row.get("BILL_NO", row.get("bill_no"))).strip() for row in status.bills}
        for bill_number in _accepted_bill_numbers(discovery.resolution):
            if bill_number not in found:
                gaps.append(
                    CoverageGap(
                        (EvidenceType.BILL_STATUS,),
                        f"bill_status_missing:{bill_number}",
                    )
                )
    for item in bill_discoveries:
        requested_bill_documents = tuple(
            evidence_type for evidence_type in _BILL_DOCUMENT_EVIDENCE if evidence_type in requested
        )
        if item.failure_reason and requested_bill_documents:
            gaps.append(
                CoverageGap(
                    requested_bill_documents,
                    f"bill_document_discovery_failed:{item.bill_number}:{item.failure_reason}",
                )
            )
        if (
            EvidenceType.BILL_TEXT in requested
            and not item.failure_reason
            and not any(work.kind is OfficialDocumentKind.BILL_TEXT for work in item.items)
        ):
            gaps.append(
                CoverageGap(
                    (EvidenceType.BILL_TEXT,),
                    f"bill_text_missing:{item.bill_number}",
                )
            )
    # Stable de-duplication retains the first source-stage ordering.
    return tuple(dict.fromkeys(gaps))


def _page_aware_transcripts(
    metadata: MetadataStageState,
    outcomes: Sequence[DocumentOutcome],
) -> tuple[tuple[TranscriptEvidence, ...], tuple[CoverageGap, ...]]:
    """Adapt successful minutes page segments into locator-safe speech evidence."""

    meeting_by_url = {
        OpenAssemblyPipeline.minutes_url(dict(decision.candidate)): decision
        for decision in metadata.discovery.resolution.meetings.accepted
    }
    transcripts: list[TranscriptEvidence] = []
    gaps: list[CoverageGap] = []
    speech_types = (
        EvidenceType.SPEECHES,
        EvidenceType.SPEECH_CONTEXT,
        EvidenceType.GOVERNMENT_RESPONSES,
    )
    for outcome in outcomes:
        if outcome.status is not DocumentOutcomeStatus.SUCCEEDED or outcome.result is None:
            continue
        document = outcome.result.document
        if document.kind is not OfficialDocumentKind.MINUTES:
            continue
        decision = meeting_by_url.get(document.official_url)
        if decision is None:
            gaps.append(
                CoverageGap(
                    speech_types,
                    f"minutes_metadata_link_missing:{outcome.work_id}",
                )
            )
            continue
        try:
            meeting = meeting_from_open_assembly_row(
                decision.candidate,
                source_hash=document.source_hash,
                source_url=document.official_url,
                retrieved_at=document.parsed_at,
            )
            transcript = extract_transcript_evidence(meeting, document)
        except Exception as exc:
            gaps.append(
                CoverageGap(
                    speech_types,
                    f"page_aware_transcript_failed:{outcome.work_id}:{type(exc).__name__}",
                )
            )
            continue
        transcripts.append(transcript)
        if transcript.failures:
            gaps.append(
                CoverageGap(
                    speech_types,
                    f"transcript_parse_gaps:{outcome.work_id}:{len(transcript.failures)}",
                )
            )
        if not transcript.speeches:
            gaps.append(
                CoverageGap(
                    speech_types,
                    f"transcript_no_speeches:{outcome.work_id}",
                )
            )
    return tuple(transcripts), tuple(gaps)


def _index_revision_with_corpus(
    index_revision: str,
    corpus_binding_id: str | None,
) -> str:
    """Bind jobs and retries to one corpus/schema/recall-algorithm tuple."""

    base = index_revision.strip()
    if corpus_binding_id is None:
        return base
    if not re.fullmatch(r"[0-9a-f]{64}", corpus_binding_id):
        raise ValueError("corpus recall binding_id must be a SHA-256 digest")
    return f"{base}+corpus-{corpus_binding_id}"


def _empty_collection() -> MetadataCollection:
    return MetadataCollection(
        (),
        (),
        (),
        CollectionCoverage(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    )


__all__ = [
    "BillDocumentDiscoverer",
    "BillDocumentDiscovery",
    "BillDocumentDiscoveryError",
    "CorpusRecallProvider",
    "CorpusRecallState",
    "CorpusRecallStatus",
    "CoverageGap",
    "DeferredRouteKind",
    "DeferredRouteShard",
    "DeferredWorkManifest",
    "DeferredWorkRoute",
    "DiscoveryBoundaryReadiness",
    "DiscoveryStageState",
    "DerivedResearchStatus",
    "DocumentOutcome",
    "DocumentOutcomeStatus",
    "DocumentBoundaryReadiness",
    "DocumentProcessor",
    "DocumentRouteShard",
    "DocumentWorkItem",
    "DocumentWorkManifest",
    "FamilyFilterAccounting",
    "FinalizationContext",
    "GatewayPlanState",
    "GatewayReceipt",
    "InMemoryResearchRunStore",
    "MetadataPageReadiness",
    "MetadataPageClient",
    "MetadataPhase",
    "MetadataStageState",
    "ResearchEngine",
    "ResearchCredentialCapabilityCodec",
    "ResearchFinalizer",
    "ResearchRunStore",
    "ROUTING_SHARD_SIZE",
    "StrictFilterReport",
]
