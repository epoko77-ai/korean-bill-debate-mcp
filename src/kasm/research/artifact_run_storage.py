"""Durable, append-only orchestration state backed by research artifacts.

The engine's in-memory run store is useful for one process, but hosted queue
deliveries can arrive in different processes and can be delivered more than
once.  This adapter makes every orchestration value an immutable, typed JSON
artifact.  Fixed identities use write-once logical keys; retry observations
use content addressing so a later terminal document result never overwrites
their history.

No request credential is accepted or serialized by this module.  The
underlying :mod:`kasm.research.artifacts` store performs a second recursive
secret check before any bytes are persisted.
"""

from __future__ import annotations

import hashlib
import importlib
import math
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import MISSING, fields, is_dataclass, replace
from datetime import UTC, date, datetime
from enum import Enum
from functools import lru_cache
from typing import Any, Final, TypeVar, cast

from kasm.adapters.korea.client import ApiPage

from .artifacts import (
    ArtifactBackendError,
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactKind,
    ArtifactRef,
    ResearchArtifactStore,
    canonical_hash,
)
from .collector import MetadataCollection, MetadataKind, MetadataPartition
from .contracts import EvidencePage, StableCursor
from .engine import (
    ROUTING_SHARD_SIZE,
    BillDocumentDiscovery,
    DeferredRouteShard,
    DeferredWorkManifest,
    DeferredWorkRoute,
    DiscoveryBoundaryReadiness,
    DiscoveryStageState,
    DocumentBoundaryReadiness,
    DocumentOutcome,
    DocumentRouteShard,
    DocumentWorkItem,
    DocumentWorkManifest,
    GatewayPlanState,
    MetadataPageReadiness,
    MetadataPhase,
    MetadataStageState,
    TaskCompletionReceipt,
)
from .overview import ProvisionalResearchOverview, build_provisional_research_overview
from .overview_transport import (
    OverviewCoverageAxisSummary,
    OverviewGroupShard,
    OverviewGroupShardDescriptor,
    OverviewTransportManifest,
    build_overview_transport,
    overview_catalog_page,
    overview_catalog_required_shards,
)
from .queue import ResearchTask
from .resolver import CandidateDecision, CandidateSetResolution, MetadataResolution
from .results import (
    SNAPSHOT_INDEX_SHARD_SIZE,
    EvidenceIndexEntry,
    EvidenceRecord,
    EvidenceTextShard,
    ResearchResultIndexPage,
    ResearchSnapshot,
    ResearchSnapshotIndex,
    ResearchSnapshotSummary,
    SnapshotIndexLookupBucket,
    SnapshotIndexShard,
    SnapshotIndexShardDescriptor,
    build_snapshot_index,
    snapshot_index_lookup_bucket,
)

_SCHEMA_VERSION: Final = 1
_NAMESPACE: Final = "kasm.research.run"
_WRITE_RETRIES: Final = 25
_WRITE_RETRY_SECONDS: Final = 0.002
_TYPE_MARKER: Final = "__kasm_run_value__"
_ROUTING_IO_CONCURRENCY: Final = 8
_DISCOVERY_STATE_KEY: Final = "run/discovery-v2"
_METADATA_STATE_KEY: Final = "run/metadata-v2"
_LEGACY_DISCOVERY_STATE_KEY: Final = "run/discovery"
_LEGACY_METADATA_STATE_KEY: Final = "run/metadata"
_DISCOVERY_ROUTING_READY_KEY: Final = "run/discovery-routing-ready"
_DOCUMENT_ROUTING_READY_KEY: Final = "run/document-routing-ready"
_FIRST_PAGE_PREVIEW_KEY: Final = "run/first-page-preview"
_FIRST_PAGE_PREVIEW_READY_KEY: Final = "run/first-page-preview-ready-v1"
# Keep a narrow grace period beyond Vercel's 300-second hard limit.  A timed-out
# finalizer cannot still be running when the next generation becomes eligible,
# while a crashed finalizer is recovered promptly instead of stalling for ten
# minutes.
_FINALIZATION_CLAIM_LEASE_SECONDS: Final = 330
_FINALIZATION_CLAIM_GENERATIONS: Final = 64

_ALLOWED_TYPE_MODULES: Final = (
    "kasm.adapters.korea.client",
    "kasm.research.collector",
    "kasm.research.contracts",
    "kasm.research.document_worker",
    "kasm.research.documents",
    "kasm.research.engine",
    "kasm.research.jobs",
    "kasm.research.overview",
    "kasm.research.overview_transport",
    "kasm.research.partitioning",
    "kasm.research.planner",
    "kasm.research.queue",
    "kasm.research.relevance",
    "kasm.research.resolver",
    "kasm.research.results",
    "kasm.research.source_availability",
    "kasm.research.transcript_evidence",
    "kasm.search.bilingual",
    "kasm.search.terminology",
)

_Value = TypeVar("_Value")
_Input = TypeVar("_Input")
_Output = TypeVar("_Output")


class ResearchRunStorageError(RuntimeError):
    """Sanitized base error for durable run-state failures."""


class ResearchRunConflictError(ResearchRunStorageError, ValueError):
    """An immutable run identity was reused for different state."""


class ResearchRunExpiredError(ResearchRunStorageError, LookupError):
    """A worker attempted to mutate a research run after its job TTL."""


class ArtifactResearchRunStore:
    """Restart-safe implementation of the engine ``ResearchRunStore`` protocol.

    ``now`` is injectable so TTL behavior can be tested without sleeps.  TTL is
    enforced at the adapter boundary; immutable backend objects intentionally
    remain available for backend lifecycle policies and forensic accounting.
    """

    def __init__(
        self,
        artifacts: ResearchArtifactStore,
        *,
        now: Callable[[], datetime] | None = None,
        page_read_concurrency: int = 8,
    ) -> None:
        if not 1 <= page_read_concurrency <= 32:
            raise ValueError("page_read_concurrency must be between 1 and 32")
        self.artifacts = artifacts
        self._now = now or (lambda: datetime.now(UTC))
        self._page_read_concurrency = page_read_concurrency
        self._lock = threading.RLock()

    def put_gateway(self, research_id: str, state: GatewayPlanState) -> GatewayPlanState:
        if state.job.id != research_id:
            raise ValueError("gateway job belongs to another research id")
        self._validate_clock()
        if self._now() >= state.job.expires_at:
            raise ResearchRunExpiredError("research run has expired")
        return self._put_fixed(
            research_id,
            ArtifactKind.PLAN,
            "run/gateway",
            "gateway",
            {"research_id": research_id},
            state,
            expires_at=state.job.expires_at,
        )

    def get_gateway(self, research_id: str) -> GatewayPlanState | None:
        state = self._get_gateway_any(research_id)
        if state is None or self._expired(state):
            return None
        return state

    def put_page(
        self,
        research_id: str,
        phase: MetadataPhase,
        partition_id: str,
        page: ApiPage,
    ) -> ApiPage:
        gateway = self._require_active(research_id)
        partition = self._partition(research_id, gateway, phase, partition_id)
        if page.page < 1:
            raise ValueError("metadata page number must be positive")
        if page.dataset != partition.dataset or page.page_size != partition.page_size:
            raise ValueError("metadata page does not match its planned partition")
        entity = {
            "phase": phase.value,
            "partition_id": partition_id,
            "page": page.page,
        }
        stored = self._put_fixed(
            research_id,
            ArtifactKind.PARTITION,
            f"run/page/{phase.value}/{partition_id}/{page.page}",
            "page",
            entity,
            page,
            expires_at=gateway.job.expires_at,
        )
        readiness = MetadataPageReadiness.create(gateway, phase, partition, stored)
        # Final write for this page boundary. A barrier reads only these small
        # markers until every dynamic page is present, then loads raw pages once.
        self._put_fixed(
            research_id,
            ArtifactKind.MANIFEST,
            f"run/page-ready/{phase.value}/{partition_id}/{page.page}",
            "page_readiness",
            {
                "phase": phase.value,
                "partition_id": partition_id,
                "page": page.page,
            },
            readiness,
            expires_at=gateway.job.expires_at,
        )
        return stored

    def get_page(
        self,
        research_id: str,
        phase: MetadataPhase,
        partition_id: str,
        page_number: int,
    ) -> ApiPage | None:
        """Read one immutable API page directly, without a partition scan."""

        if not partition_id.strip() or page_number < 1:
            raise ValueError("metadata page identity is invalid")
        return self._get_page(
            research_id,
            phase,
            partition_id,
            page_number,
        )

    def page_readiness_for(
        self,
        research_id: str,
        phase: MetadataPhase,
        partition_id: str,
        page_numbers: Sequence[int],
    ) -> tuple[MetadataPageReadiness, ...]:
        """Read small fixed page markers without decoding any raw page payload."""

        numbers = tuple(page_numbers)
        if any(number < 1 for number in numbers) or len(numbers) != len(set(numbers)):
            raise ValueError("metadata page readiness numbers must be positive and unique")
        if not numbers:
            return ()
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return ()
        partition = self._partition(research_id, gateway, phase, partition_id)

        def read_page(number: int) -> MetadataPageReadiness | None:
            return self._get_page_readiness(
                research_id,
                gateway,
                phase,
                partition,
                number,
            )

        if len(numbers) == 1 or self._page_read_concurrency == 1:
            values = tuple(read_page(number) for number in numbers)
        else:
            with ThreadPoolExecutor(
                max_workers=min(self._page_read_concurrency, len(numbers)),
                thread_name_prefix="kbd-page-ready-read",
            ) as executor:
                values = tuple(executor.map(read_page, numbers))
        return tuple(value for value in values if value is not None)

    def pages(
        self, research_id: str, phase: MetadataPhase, partition_id: str
    ) -> tuple[ApiPage, ...]:
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return ()
        partition = self._partition(research_id, gateway, phase, partition_id)
        first = self._get_page(research_id, phase, partition_id, 1)
        if first is None:
            return ()
        if first.total_count is None:
            raise ResearchRunStorageError("metadata first page lacks a total count")
        expected_pages = max(
            1,
            (first.total_count + partition.page_size - 1) // partition.page_size,
        )
        page_numbers = tuple(range(2, expected_pages + 1))

        def read_page(page_number: int) -> ApiPage | None:
            return self._get_page(
                research_id,
                phase,
                partition_id,
                page_number,
            )

        if len(page_numbers) <= 1 or self._page_read_concurrency == 1:
            following = tuple(read_page(number) for number in page_numbers)
        else:
            # One broad partition can contain dozens of immutable API pages.
            # Serial private-Blob GETs made a correctness barrier exceed the
            # serverless 300 second budget even though every page already
            # existed. Bound concurrency so latency scales by batches without
            # increasing official API pressure or dropping any source page.
            with ThreadPoolExecutor(
                max_workers=min(self._page_read_concurrency, len(page_numbers)),
                thread_name_prefix="kbd-page-read",
            ) as executor:
                following = tuple(executor.map(read_page, page_numbers))
        values = [first, *(value for value in following if value is not None)]
        return tuple(values)

    def put_first_page_preview(
        self,
        research_id: str,
        preview: ProvisionalResearchOverview,
    ) -> ProvisionalResearchOverview:
        """Publish an observed-only map after every discovery page one is ready.

        The preview is deliberately separate from the complete discovery
        boundary below.  It may orient a caller while follow-up pages are still
        running, but it cannot route deferred work or contribute to final
        coverage.  A small final marker binds the deterministic preview to all
        planned page-one source hashes.
        """

        gateway = self._require_active(research_id)
        first_pages = self._required_discovery_first_pages(research_id, gateway)
        expected_rows = sum(item.total_count for item in first_pages)
        fetched_rows = sum(item.row_count for item in first_pages)
        expected_source_hash = _first_page_collection_source_hash(
            gateway.discovery_partitions,
            first_pages,
        )
        if (
            preview.query != gateway.job.contract.query
            or not preview.provisional
            or preview.substantive_conclusion_available
            or preview.source.source_complete is not False
            or preview.source.source_rows_expected != expected_rows
            or preview.source.source_rows_fetched != fetched_rows
            or fetched_rows >= expected_rows
            or preview.source_hash != expected_source_hash
        ):
            raise ValueError("first-page preview binding is invalid")

        first_page_bindings = tuple(
            {
                "partition_id": item.partition_id,
                "source_hash": item.source_hash,
                "total_count": item.total_count,
                "row_count": item.row_count,
            }
            for item in first_pages
        )
        first_pages_hash = canonical_hash(first_page_bindings)
        pages_expected = sum(
            max(1, (item.total_count + item.page_size - 1) // item.page_size)
            for item in first_pages
        )
        readiness: dict[str, Any] = {
            "query_fingerprint": gateway.job.query_fingerprint,
            "index_revision": gateway.job.index_revision,
            "source_hash": preview.source_hash,
            "preview_hash": canonical_hash(_encode(preview)),
            "first_pages_hash": first_pages_hash,
            "partition_count": len(first_pages),
            "partitions_complete": sum(item.total_count <= item.page_size for item in first_pages),
            "pages_expected": pages_expected,
            "pages_complete": len(first_pages),
            "source_rows_expected": expected_rows,
            "source_rows_fetched": fetched_rows,
        }
        existing_readiness = self._get_first_page_preview_readiness(
            research_id,
            gateway,
        )
        if existing_readiness is not None:
            if dict(existing_readiness) != readiness:
                raise ResearchRunConflictError(
                    "immutable first-page preview readiness contains different state"
                )
            stored = self._get_bound_first_page_preview(
                research_id,
                gateway,
                existing_readiness,
            )
            if stored is None:
                raise ResearchRunStorageError("ready first-page preview is missing")
            return stored

        stored = self._put_fixed(
            research_id,
            ArtifactKind.RESOLUTION,
            _FIRST_PAGE_PREVIEW_KEY,
            "first_page_preview",
            {
                "query_fingerprint": gateway.job.query_fingerprint,
                "index_revision": gateway.job.index_revision,
                "source_hash": preview.source_hash,
                "first_pages_hash": first_pages_hash,
            },
            preview,
            expires_at=gateway.job.expires_at,
        )
        # Last write: status and overview readers never observe a payload that
        # is not bound to every planned discovery page-one marker.
        self._put_fixed(
            research_id,
            ArtifactKind.MANIFEST,
            _FIRST_PAGE_PREVIEW_READY_KEY,
            "first_page_preview_readiness",
            {"research_id": research_id},
            readiness,
            expires_at=gateway.job.expires_at,
        )
        return stored

    def get_first_page_preview(
        self,
        research_id: str,
    ) -> ProvisionalResearchOverview | None:
        """Return the immutable observed-only preview in constant reads."""

        gateway = self.get_gateway(research_id)
        if gateway is None:
            return None
        readiness = self._get_first_page_preview_readiness(research_id, gateway)
        if readiness is None:
            return None
        preview = self._get_bound_first_page_preview(research_id, gateway, readiness)
        if preview is None:
            raise ResearchRunStorageError("ready first-page preview is missing")
        return preview

    def put_discovery(self, research_id: str, state: DiscoveryStageState) -> DiscoveryStageState:
        gateway = self._require_active(research_id)
        if state.resolution.query != gateway.job.contract.query:
            raise ValueError("discovery belongs to another research contract")
        state = _compact_discovery_state(state)
        manifest = DeferredWorkManifest.create(gateway, state)
        overview = build_provisional_research_overview(state)
        deferred_routes = DeferredRouteShard.build(manifest)
        route_count = len(manifest.status_partitions) + len(manifest.document_bill_numbers)
        readiness = DiscoveryBoundaryReadiness(
            query_fingerprint=gateway.job.query_fingerprint,
            index_revision=gateway.job.index_revision,
            discovery_source_hash=manifest.discovery_source_hash,
            discovery_hash=canonical_hash(_encode(state)),
            manifest_hash=canonical_hash(_encode(manifest)),
            overview_hash=canonical_hash(_encode(overview)),
            deferred_route_count=route_count,
            deferred_route_shard_count=len(deferred_routes),
            accepted_bill_count=len(manifest.accepted_bills),
            status_partition_count=len(manifest.status_partitions),
        )
        existing_readiness = self._get_discovery_readiness(research_id, gateway)
        if existing_readiness is not None:
            if existing_readiness != readiness:
                raise ResearchRunConflictError(
                    "immutable discovery readiness already contains different state"
                )
            # The marker is the last write and binds every hot routing view.
            # A hosted status-checkpoint retry must not re-read/re-put O(N)
            # fixed items merely to invoke the status-store subclass again.
            return state
        stored = self._put_fixed(
            research_id,
            ArtifactKind.RESOLUTION,
            _DISCOVERY_STATE_KEY,
            "discovery",
            {"research_id": research_id},
            state,
            expires_at=gateway.job.expires_at,
        )
        routing_value = self._get_fixed(
            research_id,
            ArtifactKind.MANIFEST,
            _DISCOVERY_ROUTING_READY_KEY,
            "discovery_routing_readiness",
        )
        if routing_value is None:
            # Immutable page artifacts preserve every complete official row. The
            # compact discovery above keeps accepted payloads plus each rejected
            # exact identity/score/reason. Publish fixed routing views before a
            # small intermediate marker so a final-marker retry stays O(1).
            writes: list[tuple[str, str, Mapping[str, Any], Any]] = []
            document_bills = set(manifest.document_bill_numbers)
            for decision in manifest.accepted_bills:
                bill_number = _candidate_bill_number(decision)
                logical_key = f"run/accepted-bill/{bill_number}"
                writes.append(
                    (
                        logical_key,
                        "accepted_bill",
                        _hot_entity(
                            gateway,
                            readiness.manifest_hash,
                            logical_key,
                            decision,
                            bill_number=bill_number,
                            document_required=bill_number in document_bills,
                        ),
                        decision,
                    )
                )
            for partition in manifest.status_partitions:
                logical_key = _status_partition_key(partition.partition_id)
                writes.append(
                    (
                        logical_key,
                        "status_partition",
                        _hot_entity(
                            gateway,
                            readiness.manifest_hash,
                            logical_key,
                            partition,
                            partition_id=partition.partition_id,
                        ),
                        partition,
                    )
                )
            for shard in deferred_routes:
                logical_key = f"run/deferred-route-shard/{shard.number}"
                writes.append(
                    (
                        logical_key,
                        "deferred_route_shard",
                        _hot_entity(
                            gateway,
                            readiness.manifest_hash,
                            logical_key,
                            shard,
                            shard=shard.number,
                        ),
                        shard,
                    )
                )
            self._put_manifest_artifacts_parallel(research_id, gateway, writes)
            self._put_fixed(
                research_id,
                ArtifactKind.MANIFEST,
                _DISCOVERY_ROUTING_READY_KEY,
                "discovery_routing_readiness",
                {"research_id": research_id},
                readiness,
                expires_at=gateway.job.expires_at,
            )
        elif (
            self._expect(
                routing_value,
                DiscoveryBoundaryReadiness,
                "discovery routing readiness",
            )
            != readiness
        ):
            raise ResearchRunConflictError(
                "immutable discovery routing readiness contains different state"
            )
        self._put_fixed(
            research_id,
            ArtifactKind.MANIFEST,
            "run/deferred-work-manifest",
            "deferred_work_manifest",
            {
                "query_fingerprint": gateway.job.query_fingerprint,
                "index_revision": gateway.job.index_revision,
                "discovery_source_hash": manifest.discovery_source_hash,
            },
            manifest,
            expires_at=gateway.job.expires_at,
        )
        self._put_fixed(
            research_id,
            ArtifactKind.RESOLUTION,
            "run/provisional-overview",
            "provisional_overview",
            {
                "query_fingerprint": gateway.job.query_fingerprint,
                "source_hash": manifest.discovery_source_hash,
            },
            overview,
            expires_at=gateway.job.expires_at,
        )
        # Last write: workers cannot observe a partially published compact
        # boundary if a serverless invocation stops between independent PUTs.
        self._put_fixed(
            research_id,
            ArtifactKind.MANIFEST,
            "run/discovery-ready",
            "discovery_readiness",
            {"research_id": research_id},
            readiness,
            expires_at=gateway.job.expires_at,
        )
        return stored

    def get_discovery(self, research_id: str) -> DiscoveryStageState | None:
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return None
        readiness = self._discovery_readiness_or_adopt(research_id, gateway)
        if readiness is None:
            return None
        value = self._get_fixed(
            research_id,
            ArtifactKind.RESOLUTION,
            _DISCOVERY_STATE_KEY,
            "discovery",
        )
        if value is None:
            return None
        state = self._expect(value, DiscoveryStageState, "discovery")
        if (
            state.resolution.query != gateway.job.contract.query
            or state.resolution.source_hash != readiness.discovery_source_hash
            or canonical_hash(_encode(state)) != readiness.discovery_hash
        ):
            raise ResearchRunStorageError("discovery contract binding is invalid")
        return state

    def get_deferred_manifest(self, research_id: str) -> DeferredWorkManifest | None:
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return None
        readiness = self._discovery_readiness_or_adopt(research_id, gateway)
        if readiness is None:
            return None
        record = self._get_fixed_record(
            research_id,
            ArtifactKind.MANIFEST,
            "run/deferred-work-manifest",
            "deferred_work_manifest",
        )
        if record is None:
            raise ResearchRunStorageError("ready deferred manifest is missing")
        entity, value = record
        manifest = self._expect(value, DeferredWorkManifest, "deferred work manifest")
        if (
            entity.get("query_fingerprint") != gateway.job.query_fingerprint
            or entity.get("index_revision") != gateway.job.index_revision
            or entity.get("discovery_source_hash") != manifest.discovery_source_hash
            or manifest.query_fingerprint != gateway.job.query_fingerprint
            or manifest.index_revision != gateway.job.index_revision
        ):
            raise ResearchRunStorageError("deferred manifest job binding is invalid")
        if (
            readiness.discovery_source_hash != manifest.discovery_source_hash
            or readiness.manifest_hash != canonical_hash(_encode(manifest))
        ):
            raise ResearchRunStorageError("deferred manifest readiness binding is invalid")
        return manifest

    def get_accepted_bill(self, research_id: str, bill_number: str) -> CandidateDecision | None:
        return self._get_accepted_bill(
            research_id,
            bill_number,
            require_document=False,
        )

    def get_document_bill(self, research_id: str, bill_number: str) -> CandidateDecision | None:
        return self._get_accepted_bill(
            research_id,
            bill_number,
            require_document=True,
        )

    def _get_accepted_bill(
        self,
        research_id: str,
        bill_number: str,
        *,
        require_document: bool,
    ) -> CandidateDecision | None:
        if not re.fullmatch(r"\d{7}", bill_number):
            raise ValueError("accepted bill lookup requires seven digits")
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return None
        readiness = self._discovery_readiness_or_adopt(research_id, gateway)
        if readiness is None:
            return None
        logical_key = f"run/accepted-bill/{bill_number}"
        record = self._get_bound_hot_record(
            research_id,
            gateway,
            readiness.manifest_hash,
            logical_key,
            "accepted_bill",
        )
        if record is None:
            return None
        entity, value = record
        decision = self._expect(value, CandidateDecision, "accepted bill")
        if (
            entity.get("bill_number") != bill_number
            or not isinstance(entity.get("document_required"), bool)
            or not decision.accepted
            or _candidate_bill_number(decision) != bill_number
        ):
            raise ResearchRunStorageError("accepted bill fixed identity is invalid")
        if require_document and entity.get("document_required") is not True:
            return None
        return decision

    def get_status_partition(self, research_id: str, partition_id: str) -> MetadataPartition | None:
        if not partition_id.strip():
            raise ValueError("status partition id is required")
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return None
        readiness = self._discovery_readiness_or_adopt(research_id, gateway)
        if readiness is None:
            return None
        logical_key = _status_partition_key(partition_id)
        record = self._get_bound_hot_record(
            research_id,
            gateway,
            readiness.manifest_hash,
            logical_key,
            "status_partition",
        )
        if record is None:
            return None
        entity, value = record
        partition = self._expect(value, MetadataPartition, "status partition")
        if entity.get("partition_id") != partition_id or partition.partition_id != partition_id:
            raise ResearchRunStorageError("status partition fixed identity is invalid")
        return partition

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
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return ()
        readiness = self._discovery_readiness_or_adopt(research_id, gateway)
        if readiness is None:
            return ()
        if readiness.deferred_route_count != expected_total:
            raise ResearchRunStorageError("deferred route total binding is invalid")
        shard_numbers = tuple(
            range(start // ROUTING_SHARD_SIZE, ((stop - 1) // ROUTING_SHARD_SIZE) + 1)
        )

        def read_shard(number: int) -> DeferredRouteShard:
            logical_key = f"run/deferred-route-shard/{number}"
            record = self._get_bound_hot_record(
                research_id,
                gateway,
                readiness.manifest_hash,
                logical_key,
                "deferred_route_shard",
            )
            if record is None:
                raise ResearchRunStorageError("ready deferred route shard is missing")
            entity, value = record
            shard = self._expect(value, DeferredRouteShard, "deferred route shard")
            if (
                entity.get("shard") != number
                or shard.number != number
                or shard.total != expected_total
            ):
                raise ResearchRunStorageError("deferred route shard binding is invalid")
            return shard

        shards = self._bounded_map(read_shard, shard_numbers, "kbd-deferred-route-read")
        routes = tuple(
            route for shard in shards for route in shard.routes if start <= route.position < stop
        )
        if tuple(item.position for item in routes) != tuple(range(start, stop)):
            raise ResearchRunStorageError("deferred route range is incomplete")
        return routes

    def get_provisional_overview(self, research_id: str) -> ProvisionalResearchOverview | None:
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return None
        readiness = self._discovery_readiness_or_adopt(research_id, gateway)
        if readiness is None:
            preview_readiness = self._get_first_page_preview_readiness(
                research_id,
                gateway,
            )
            if preview_readiness is None:
                return None
            preview = self._get_bound_first_page_preview(
                research_id,
                gateway,
                preview_readiness,
            )
            if preview is None:
                raise ResearchRunStorageError("ready first-page preview is missing")
            return preview
        record = self._get_fixed_record(
            research_id,
            ArtifactKind.RESOLUTION,
            "run/provisional-overview",
            "provisional_overview",
        )
        if record is None:
            raise ResearchRunStorageError("ready provisional overview is missing")
        entity, value = record
        overview = self._expect(value, ProvisionalResearchOverview, "provisional overview")
        if (
            entity.get("query_fingerprint") != gateway.job.query_fingerprint
            or entity.get("source_hash") != overview.source_hash
            or overview.query != gateway.job.contract.query
        ):
            raise ResearchRunStorageError("provisional overview binding is invalid")
        if (
            readiness.discovery_source_hash != overview.source_hash
            or readiness.overview_hash != canonical_hash(_encode(overview))
        ):
            raise ResearchRunStorageError("provisional overview readiness binding is invalid")
        return overview

    def put_bill_discovery(
        self, research_id: str, outcome: BillDocumentDiscovery
    ) -> BillDocumentDiscovery:
        gateway = self._require_active(research_id)
        if self.get_document_bill(research_id, outcome.bill_number) is None:
            raise ValueError("bill discovery is outside the resolved research scope")
        return self._put_fixed(
            research_id,
            ArtifactKind.RESOLUTION,
            f"run/bill-discovery/{outcome.bill_number}",
            "bill_discovery",
            {"bill_number": outcome.bill_number},
            outcome,
            expires_at=gateway.job.expires_at,
        )

    def get_bill_discovery(
        self, research_id: str, bill_number: str
    ) -> BillDocumentDiscovery | None:
        """Read one bill-document index result without listing its siblings."""

        if not re.fullmatch(r"\d{7}", bill_number):
            raise ValueError("bill discovery requires an exact bill number")
        value = self._get_fixed(
            research_id,
            ArtifactKind.RESOLUTION,
            f"run/bill-discovery/{bill_number}",
            "bill_discovery",
        )
        if value is None:
            return None
        restored = self._expect(value, BillDocumentDiscovery, "bill discovery")
        if restored.bill_number != bill_number:
            raise ResearchRunStorageError("bill discovery identity is invalid")
        return restored

    def bill_discoveries_for(
        self, research_id: str, bill_numbers: Sequence[str]
    ) -> tuple[BillDocumentDiscovery, ...]:
        """Read a planned bill set in bounded parallel without listing artifacts."""

        numbers = tuple(sorted(bill_numbers))
        if len(numbers) != len(set(numbers)):
            raise ValueError("bill discovery numbers must be unique")
        if not numbers:
            return ()
        if len(numbers) == 1 or self._page_read_concurrency == 1:
            values = tuple(self.get_bill_discovery(research_id, number) for number in numbers)
        else:
            with ThreadPoolExecutor(
                max_workers=min(self._page_read_concurrency, len(numbers)),
                thread_name_prefix="kbd-bill-read",
            ) as executor:
                values = tuple(
                    executor.map(
                        lambda number: self.get_bill_discovery(research_id, number),
                        numbers,
                    )
                )
        return tuple(value for value in values if value is not None)

    def bill_discoveries(self, research_id: str) -> tuple[BillDocumentDiscovery, ...]:
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return ()
        manifest = self.get_deferred_manifest(research_id)
        allowed = set(manifest.document_bill_numbers) if manifest is not None else set()
        values: dict[str, BillDocumentDiscovery] = {}
        for _ref, entity, value in self._records(
            research_id, ArtifactKind.RESOLUTION, "bill_discovery"
        ):
            restored = self._expect(value, BillDocumentDiscovery, "bill discovery")
            if entity.get("bill_number") != restored.bill_number:
                raise ResearchRunStorageError("bill discovery identity is invalid")
            if restored.bill_number not in allowed:
                raise ResearchRunStorageError("bill discovery scope binding is invalid")
            previous = values.setdefault(restored.bill_number, restored)
            if previous != restored:
                raise ResearchRunConflictError("conflicting bill discovery artifacts")
        return tuple(values[number] for number in sorted(values))

    def put_metadata(self, research_id: str, state: MetadataStageState) -> MetadataStageState:
        gateway = self._require_active(research_id)
        state = replace(state, discovery=_compact_discovery_state(state.discovery))
        existing_readiness = self._get_document_readiness(research_id, gateway)
        if existing_readiness is not None:
            if (
                existing_readiness.discovery_source_hash != state.discovery.resolution.source_hash
                or existing_readiness.metadata_hash != canonical_hash(_encode(state))
                or existing_readiness.manifest_hash != canonical_hash(_encode(state.manifest))
                or existing_readiness.manifest_fingerprint != state.manifest.fingerprint
                or existing_readiness.item_count != len(state.manifest.items)
            ):
                raise ResearchRunConflictError(
                    "immutable document readiness already contains different state"
                )
            # The marker proves every item, route, metadata state, and manifest
            # was already durable. Let the status-store subclass heal its own
            # small checkpoint without reading any O(N) audit artifact.
            return state
        deferred = self.get_deferred_manifest(research_id)
        if (
            deferred is None
            or state.discovery.resolution.query != gateway.job.contract.query
            or state.discovery.resolution.source_hash != deferred.discovery_source_hash
            or state.discovery.status_partitions != deferred.status_partitions
            or state.discovery.document_bill_numbers != deferred.document_bill_numbers
        ):
            raise ValueError("metadata does not bind to the stored discovery")
        route_shards = DocumentRouteShard.build(state.manifest)
        readiness = DocumentBoundaryReadiness(
            gateway.job.query_fingerprint,
            gateway.job.index_revision,
            deferred.discovery_source_hash,
            canonical_hash(_encode(deferred)),
            canonical_hash(_encode(state)),
            canonical_hash(_encode(state.manifest)),
            state.manifest.fingerprint,
            len(state.manifest.items),
            len(route_shards),
        )
        stored_bill_discoveries = self.bill_discoveries_for(
            research_id,
            deferred.document_bill_numbers,
        )
        if state.manifest.bill_discoveries != stored_bill_discoveries:
            raise ValueError("metadata manifest does not bind to bill discoveries")
        stored = self._put_fixed(
            research_id,
            ArtifactKind.METADATA,
            _METADATA_STATE_KEY,
            "metadata",
            {"research_id": research_id},
            state,
            expires_at=gateway.job.expires_at,
        )
        routing_value = self._get_fixed(
            research_id,
            ArtifactKind.MANIFEST,
            _DOCUMENT_ROUTING_READY_KEY,
            "document_routing_readiness",
        )
        if routing_value is None:
            writes: list[tuple[str, str, Mapping[str, Any], Any]] = []
            for item in stored.manifest.items:
                logical_key = f"run/document-work-item/{item.work_id}"
                writes.append(
                    (
                        logical_key,
                        "document_work_item",
                        _hot_entity(
                            gateway,
                            readiness.manifest_hash,
                            logical_key,
                            item,
                            work_id=item.work_id,
                        ),
                        item,
                    )
                )
            for shard in route_shards:
                logical_key = f"run/document-route-shard/{shard.number}"
                writes.append(
                    (
                        logical_key,
                        "document_route_shard",
                        _hot_entity(
                            gateway,
                            readiness.manifest_hash,
                            logical_key,
                            shard,
                            shard=shard.number,
                        ),
                        shard,
                    )
                )
            self._put_manifest_artifacts_parallel(research_id, gateway, writes)
            self._put_fixed(
                research_id,
                ArtifactKind.MANIFEST,
                _DOCUMENT_ROUTING_READY_KEY,
                "document_routing_readiness",
                {"research_id": research_id},
                readiness,
                expires_at=gateway.job.expires_at,
            )
        elif (
            self._expect(
                routing_value,
                DocumentBoundaryReadiness,
                "document routing readiness",
            )
            != readiness
        ):
            raise ResearchRunConflictError(
                "immutable document routing readiness contains different state"
            )
        self._put_fixed(
            research_id,
            ArtifactKind.MANIFEST,
            "run/document-work-manifest",
            "document_work_manifest",
            {
                "query_fingerprint": gateway.job.query_fingerprint,
                "index_revision": gateway.job.index_revision,
                "manifest_fingerprint": stored.manifest.fingerprint,
                "discovery_source_hash": deferred.discovery_source_hash,
            },
            stored.manifest,
            expires_at=gateway.job.expires_at,
        )
        # Final write for the document boundary. No metadata, full manifest,
        # route, or work item is observable to workers before this marker.
        self._put_fixed(
            research_id,
            ArtifactKind.MANIFEST,
            "run/document-ready",
            "document_readiness",
            {"research_id": research_id},
            readiness,
            expires_at=gateway.job.expires_at,
        )
        return stored

    def get_metadata(self, research_id: str) -> MetadataStageState | None:
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return None
        readiness = self._document_readiness_or_adopt(research_id, gateway)
        if readiness is None:
            return None
        value = self._get_fixed(
            research_id,
            ArtifactKind.METADATA,
            _METADATA_STATE_KEY,
            "metadata",
        )
        if value is None:
            raise ResearchRunStorageError("ready metadata audit state is missing")
        state = self._expect(value, MetadataStageState, "metadata")
        if (
            canonical_hash(_encode(state)) != readiness.metadata_hash
            or state.discovery.resolution.query != gateway.job.contract.query
            or state.discovery.resolution.source_hash != readiness.discovery_source_hash
            or canonical_hash(_encode(state.manifest)) != readiness.manifest_hash
        ):
            raise ResearchRunStorageError("metadata discovery binding is invalid")
        return state

    def get_document_manifest(self, research_id: str) -> DocumentWorkManifest | None:
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return None
        readiness = self._document_readiness_or_adopt(research_id, gateway)
        if readiness is None:
            return None
        record = self._get_fixed_record(
            research_id,
            ArtifactKind.MANIFEST,
            "run/document-work-manifest",
            "document_work_manifest",
        )
        if record is None:
            raise ResearchRunStorageError("ready document manifest is missing")
        entity, value = record
        manifest = self._expect(value, DocumentWorkManifest, "document work manifest")
        if (
            entity.get("query_fingerprint") != gateway.job.query_fingerprint
            or entity.get("index_revision") != gateway.job.index_revision
            or entity.get("manifest_fingerprint") != manifest.fingerprint
            or entity.get("discovery_source_hash") != readiness.discovery_source_hash
            or manifest.fingerprint != readiness.manifest_fingerprint
            or canonical_hash(_encode(manifest)) != readiness.manifest_hash
        ):
            raise ResearchRunStorageError("document manifest binding is invalid")
        return manifest

    def get_document_item(self, research_id: str, work_id: str) -> DocumentWorkItem | None:
        if not work_id.strip():
            raise ValueError("document work_id is required")
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return None
        readiness = self._document_readiness_or_adopt(research_id, gateway)
        if readiness is None:
            return None
        logical_key = f"run/document-work-item/{work_id}"
        record = self._get_bound_hot_record(
            research_id,
            gateway,
            readiness.manifest_hash,
            logical_key,
            "document_work_item",
        )
        if record is None:
            return None
        entity, value = record
        item = self._expect(value, DocumentWorkItem, "document work item")
        if entity.get("work_id") != work_id or item.work_id != work_id:
            raise ResearchRunStorageError("document work item fixed identity is invalid")
        return item

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
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return ()
        readiness = self._document_readiness_or_adopt(research_id, gateway)
        if readiness is None:
            return ()
        if readiness.item_count != expected_total:
            raise ResearchRunStorageError("document route total binding is invalid")
        shard_numbers = tuple(
            range(start // ROUTING_SHARD_SIZE, ((stop - 1) // ROUTING_SHARD_SIZE) + 1)
        )

        def read_shard(number: int) -> DocumentRouteShard:
            logical_key = f"run/document-route-shard/{number}"
            record = self._get_bound_hot_record(
                research_id,
                gateway,
                readiness.manifest_hash,
                logical_key,
                "document_route_shard",
            )
            if record is None:
                raise ResearchRunStorageError("ready document route shard is missing")
            entity, value = record
            shard = self._expect(value, DocumentRouteShard, "document route shard")
            if (
                entity.get("shard") != number
                or shard.number != number
                or shard.total != expected_total
            ):
                raise ResearchRunStorageError("document route shard binding is invalid")
            return shard

        shards = self._bounded_map(read_shard, shard_numbers, "kbd-document-route-read")
        selected: list[DocumentWorkItem] = []
        positions: list[int] = []
        for shard in shards:
            for offset, item in enumerate(shard.items):
                position = shard.start_position + offset
                if start <= position < stop:
                    positions.append(position)
                    selected.append(item)
        if tuple(positions) != tuple(range(start, stop)):
            raise ResearchRunStorageError("document route range is incomplete")
        return tuple(selected)

    def claim_phase_finalization(
        self,
        research_id: str,
        phase: MetadataPhase,
    ) -> bool:
        """Acquire one crash-recoverable assembly claim for this phase.

        A generation remains active through the hosted function hard limit plus
        a 30-second completion grace period.
        Concurrent invocations race on one write-once key, so only one may read
        and decode all raw pages. After a crashed invocation's lease expires,
        the next fixed generation restores liveness without overwriting audit
        history or allowing an active 300-second function to overlap.
        """

        gateway = self._require_active(research_id)
        self._validate_clock()
        now = self._now()
        for generation in range(_FINALIZATION_CLAIM_GENERATIONS):
            logical_key = f"run/phase-finalization-claim/{phase.value}/{generation}"
            value = self._get_fixed(
                research_id,
                ArtifactKind.MANIFEST,
                logical_key,
                "phase_finalization_claim",
            )
            if value is not None:
                if not isinstance(value, Mapping):
                    raise ResearchRunStorageError("phase finalization claim is invalid")
                claimed_at = value.get("claimed_at")
                if (
                    value.get("query_fingerprint") != gateway.job.query_fingerprint
                    or value.get("index_revision") != gateway.job.index_revision
                    or value.get("phase") != phase.value
                    or value.get("generation") != generation
                    or not isinstance(claimed_at, datetime)
                    or claimed_at.tzinfo is None
                    or not re.fullmatch(r"[0-9a-f]{32}", str(value.get("owner") or ""))
                ):
                    raise ResearchRunStorageError("phase finalization claim binding is invalid")
                if (now - claimed_at).total_seconds() < _FINALIZATION_CLAIM_LEASE_SECONDS:
                    return False
                continue
            claim = {
                "query_fingerprint": gateway.job.query_fingerprint,
                "index_revision": gateway.job.index_revision,
                "phase": phase.value,
                "generation": generation,
                "claimed_at": now,
                # Distinct contenders must not collapse as an idempotent same-
                # payload write in backends that return success for duplicates.
                "owner": uuid.uuid4().hex,
            }
            payload = _record_payload(
                research_id,
                "phase_finalization_claim",
                {"phase": phase.value, "generation": generation},
                claim,
                expires_at=gateway.job.expires_at,
            )
            try:
                self.artifacts.write(
                    research_id,
                    ArtifactKind.MANIFEST,
                    payload,
                    logical_key=logical_key,
                )
                return True
            except ArtifactConflictError:
                # Another invocation won this exact generation. Re-read it on
                # the next loop turn only after moving through the same chain.
                return False
        raise ResearchRunStorageError("phase finalization claim generations are exhausted")

    def put_task_completion(self, task: ResearchTask) -> TaskCompletionReceipt:
        gateway = self._require_active(task.research_id)
        receipt = TaskCompletionReceipt.from_task(task)
        self._validate_task_receipt_binding(gateway, task, receipt)
        return self._put_fixed(
            task.research_id,
            ArtifactKind.MANIFEST,
            f"run/task-completion/{task.idempotency_key}",
            "task_completion",
            {
                "task_identity": task.idempotency_key,
                "stage": task.stage.value,
                "work_id": task.work_id,
            },
            receipt,
            expires_at=gateway.job.expires_at,
        )

    def get_task_completion(self, task: ResearchTask) -> TaskCompletionReceipt | None:
        gateway = self.get_gateway(task.research_id)
        if gateway is None:
            return None
        return self._get_task_completion(task, gateway)

    def _get_task_completion(
        self,
        task: ResearchTask,
        gateway: GatewayPlanState,
    ) -> TaskCompletionReceipt | None:
        record = self._get_fixed_record(
            task.research_id,
            ArtifactKind.MANIFEST,
            f"run/task-completion/{task.idempotency_key}",
            "task_completion",
        )
        if record is None:
            return None
        entity, value = record
        receipt = self._expect(value, TaskCompletionReceipt, "task completion")
        if (
            entity.get("task_identity") != task.idempotency_key
            or entity.get("stage") != task.stage.value
            or entity.get("work_id") != task.work_id
        ):
            raise ResearchRunStorageError("task completion identity is invalid")
        self._validate_task_receipt_binding(gateway, task, receipt)
        return receipt

    def task_completions_for(
        self,
        tasks: Sequence[ResearchTask],
    ) -> tuple[TaskCompletionReceipt, ...]:
        """Read compact planned receipts with bounded parallel artifact I/O."""

        planned = tuple(tasks)
        identities = tuple(task.idempotency_key for task in planned)
        if len(identities) != len(set(identities)):
            raise ValueError("task completion identities must be unique")
        if not planned:
            return ()
        research_id = planned[0].research_id
        if any(task.research_id != research_id for task in planned):
            raise ValueError("task completions must belong to one research run")
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return ()
        values = self._bounded_map(
            lambda task: self._get_task_completion(task, gateway),
            planned,
            "kbd-task-receipt-read",
        )
        return tuple(value for value in values if value is not None)

    def put_document_outcome(self, research_id: str, outcome: DocumentOutcome) -> DocumentOutcome:
        gateway = self._require_active(research_id)
        outcome = _compact_document_outcome(outcome)
        work_item = self.get_document_item(research_id, outcome.work_id)
        if work_item is None:
            raise ValueError("document outcome is absent from the stored manifest")
        if outcome.result is not None and (
            outcome.result.kind is not work_item.kind
            or outcome.result.official_url != work_item.official_url
        ):
            raise ValueError("document result does not match its manifest item")

        current_terminal = self._terminal_outcome(research_id, outcome.work_id)
        if current_terminal is not None:
            if _compact_document_outcome(current_terminal) != outcome:
                raise ResearchRunConflictError("terminal document outcome cannot be replaced")
            return current_terminal

        entity = {"work_id": outcome.work_id, "status": outcome.status.value}
        if outcome.terminal:
            return self._put_fixed(
                research_id,
                ArtifactKind.OUTCOME,
                f"run/document-terminal/{outcome.work_id}",
                "document_outcome",
                entity,
                outcome,
                expires_at=gateway.job.expires_at,
            )

        # Retry observations are append-only/content-addressed.  Distinct errors
        # remain inspectable; exact redeliveries collapse idempotently.
        return self._put_content_addressed(
            research_id,
            ArtifactKind.OUTCOME,
            "document_outcome",
            entity,
            outcome,
            expires_at=gateway.job.expires_at,
        )

    def get_document_outcome(self, research_id: str, work_id: str) -> DocumentOutcome | None:
        """Read one terminal outcome by its fixed logical key without a run scan."""

        if not work_id.strip():
            raise ValueError("document work_id is required")
        return self._terminal_outcome(research_id, work_id)

    def document_outcomes_for(
        self, research_id: str, work_ids: Sequence[str]
    ) -> tuple[DocumentOutcome, ...]:
        """Read planned terminal outcomes directly without listing retry history."""

        identifiers = tuple(work_ids)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("document outcome work ids must be unique")
        if not identifiers:
            return ()
        if len(identifiers) == 1 or self._page_read_concurrency == 1:
            values = tuple(
                self.get_document_outcome(research_id, work_id) for work_id in identifiers
            )
        else:
            with ThreadPoolExecutor(
                max_workers=min(self._page_read_concurrency, len(identifiers)),
                thread_name_prefix="kbd-outcome-read",
            ) as executor:
                values = tuple(
                    executor.map(
                        lambda work_id: self.get_document_outcome(research_id, work_id),
                        identifiers,
                    )
                )
        return tuple(value for value in values if value is not None)

    def document_outcomes(self, research_id: str) -> tuple[DocumentOutcome, ...]:
        history = self.document_outcome_history(research_id)
        by_work: dict[str, list[DocumentOutcome]] = {}
        for outcome in history:
            by_work.setdefault(outcome.work_id, []).append(outcome)
        current: list[DocumentOutcome] = []
        for work_id in sorted(by_work):
            candidates = by_work[work_id]
            terminal = [value for value in candidates if value.terminal]
            if len(terminal) > 1:
                raise ResearchRunConflictError("multiple terminal document outcomes exist")
            if terminal:
                current.append(terminal[0])
                continue
            # DocumentOutcome has no attempt timestamp.  Canonical hashing gives
            # a stable current retry observation independent of list/delivery order.
            current.append(max(candidates, key=lambda value: canonical_hash(_encode(value))))
        return tuple(current)

    def document_outcome_history(
        self, research_id: str, *, work_id: str | None = None
    ) -> tuple[DocumentOutcome, ...]:
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return ()
        manifest = self.get_document_manifest(research_id)
        allowed = {item.work_id for item in manifest.items} if manifest is not None else set()
        values: list[DocumentOutcome] = []
        seen: set[str] = set()
        for ref, entity, value in self._records(
            research_id, ArtifactKind.OUTCOME, "document_outcome"
        ):
            restored = self._expect(value, DocumentOutcome, "document outcome")
            if (
                entity.get("work_id") != restored.work_id
                or entity.get("status") != restored.status.value
            ):
                raise ResearchRunStorageError("document outcome identity is invalid")
            if restored.work_id not in allowed:
                raise ResearchRunStorageError("document outcome manifest binding is invalid")
            if work_id is not None and restored.work_id != work_id:
                continue
            fingerprint = canonical_hash(_encode(restored))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            values.append(restored)
            if restored.terminal and ref.logical_key != (
                f"run/document-terminal/{restored.work_id}"
            ):
                raise ResearchRunStorageError("terminal outcome lacks a write-once identity")
        return tuple(
            sorted(
                values,
                key=lambda value: (
                    value.work_id,
                    1 if value.terminal else 0,
                    canonical_hash(_encode(value)),
                ),
            )
        )

    def put_snapshot(self, research_id: str, snapshot: ResearchSnapshot) -> ResearchSnapshot:
        gateway = self._require_active(research_id)
        if (
            snapshot.research_id != research_id
            or snapshot.contract != gateway.job.contract
            or snapshot.query_fingerprint != gateway.job.query_fingerprint
            or snapshot.index_revision != gateway.job.index_revision
        ):
            raise ValueError("snapshot belongs to another research job")
        manifest, shards, lookup_buckets, text_shards = build_snapshot_index(snapshot)
        overview = build_overview_transport(snapshot)
        shard_writes: list[tuple[str, str, Mapping[str, Any], Any]] = []
        shard_writes.extend(
            (
                f"run/snapshot-text/shard/{item.number}",
                "evidence_text_shard",
                {"research_id": research_id, "shard": item.number},
                item,
            )
            for item in text_shards
        )
        shard_writes.extend(
            (
                f"run/snapshot-index/shard/{item.number}",
                "snapshot_index_shard",
                {"research_id": research_id, "shard": item.number},
                item,
            )
            for item in shards
        )
        shard_writes.extend(
            (
                f"run/snapshot-index/lookup/{item.number}",
                "snapshot_index_lookup",
                {"research_id": research_id, "bucket": item.number},
                item,
            )
            for item in lookup_buckets
        )
        shard_writes.extend(
            (
                f"run/overview/shard/{item.number}",
                "overview_group_shard",
                {"research_id": research_id, "shard": item.number},
                item,
            )
            for item in overview.shards
        )
        self._put_result_artifacts_parallel(
            research_id,
            gateway,
            tuple(shard_writes),
        )
        # Manifests are independent after all referenced shards are durable.
        # Publish both in parallel, then write the single readiness marker.
        self._put_result_artifacts_parallel(
            research_id,
            gateway,
            (
                (
                    "run/snapshot-index/manifest",
                    "snapshot_index_manifest",
                    {"research_id": research_id},
                    manifest,
                ),
                (
                    "run/overview/manifest",
                    "overview_manifest",
                    {"research_id": research_id},
                    overview.manifest,
                ),
            ),
        )
        # This summary is the readiness marker for the snapshot index and the
        # overview alike.  It must remain the final immutable write.
        self._put_fixed(
            research_id,
            ArtifactKind.RESULT_PAGE,
            "run/snapshot-summary",
            "snapshot_summary",
            {"research_id": research_id},
            ResearchSnapshotSummary.from_snapshot(snapshot),
            expires_at=gateway.job.expires_at,
        )
        return snapshot

    def _put_result_artifacts_parallel(
        self,
        research_id: str,
        gateway: GatewayPlanState,
        writes: Sequence[tuple[str, str, Mapping[str, Any], Any]],
    ) -> None:
        if not writes:
            return

        def put_one(spec: tuple[str, str, Mapping[str, Any], Any]) -> None:
            logical_key, record_type, entity, value = spec
            self._put_fixed(
                research_id,
                ArtifactKind.RESULT_PAGE,
                logical_key,
                record_type,
                entity,
                value,
                expires_at=gateway.job.expires_at,
            )

        if len(writes) == 1 or self._page_read_concurrency == 1:
            for write in writes:
                put_one(write)
            return
        with ThreadPoolExecutor(
            max_workers=min(self._page_read_concurrency, len(writes)),
            thread_name_prefix="kbd-result-write",
        ) as executor:
            tuple(executor.map(put_one, writes))

    def _put_manifest_artifacts_parallel(
        self,
        research_id: str,
        gateway: GatewayPlanState,
        writes: Sequence[tuple[str, str, Mapping[str, Any], Any]],
    ) -> None:
        """Write hot routing artifacts concurrently, capped at eight requests."""

        if not writes:
            return

        def put_one(spec: tuple[str, str, Mapping[str, Any], Any]) -> None:
            logical_key, record_type, entity, value = spec
            self._put_fixed(
                research_id,
                ArtifactKind.MANIFEST,
                logical_key,
                record_type,
                entity,
                value,
                expires_at=gateway.job.expires_at,
            )

        self._bounded_map(put_one, tuple(writes), "kbd-routing-write")

    def _bounded_map(
        self,
        function: Callable[[_Input], _Output],
        values: Sequence[_Input],
        thread_name_prefix: str,
    ) -> tuple[_Output, ...]:
        if not values:
            return ()
        max_workers = min(
            _ROUTING_IO_CONCURRENCY,
            self._page_read_concurrency,
            len(values),
        )
        if max_workers == 1:
            return tuple(function(value) for value in values)
        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        ) as executor:
            return tuple(executor.map(function, values))

    def get_snapshot_summary(self, research_id: str) -> ResearchSnapshotSummary | None:
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return None
        value = self._get_fixed(
            research_id,
            ArtifactKind.RESULT_PAGE,
            "run/snapshot-summary",
            "snapshot_summary",
        )
        if value is None:
            # The summary is the final readiness marker.  A queue retry of
            # put_snapshot fills any missing index shards after a crash; never
            # expose the giant full snapshot as ready before that completes.
            return None
        summary = self._expect(value, ResearchSnapshotSummary, "snapshot summary")
        if (
            summary.research_id != research_id
            or summary.query_fingerprint != gateway.job.query_fingerprint
            or summary.index_revision != gateway.job.index_revision
        ):
            raise ResearchRunStorageError("snapshot summary job binding is invalid")
        return summary

    def get_result_page(
        self,
        research_id: str,
        *,
        cursor: str | None = None,
        page_size: int = 20,
    ) -> dict[str, Any] | None:
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return None
        manifest = self._get_snapshot_index(research_id)
        if manifest is None:
            return None
        if (
            manifest.research_id != research_id
            or manifest.query_fingerprint != gateway.job.query_fingerprint
            or manifest.index_revision != gateway.job.index_revision
        ):
            raise ResearchRunStorageError("snapshot index job binding is invalid")

        start = 0
        loaded: dict[int, SnapshotIndexShard] = {}
        if cursor is not None:
            decoded = StableCursor.decode(cursor)
            if decoded.query_fingerprint != manifest.query_fingerprint:
                raise ValueError("cursor belongs to another research query")
            if decoded.index_revision != manifest.index_revision:
                raise ValueError("cursor belongs to another index revision")
            if decoded.page_size != page_size:
                raise ValueError("page_size must match the cursor")
            descriptor = _descriptor_for_cursor(manifest, decoded)
            shard = self._get_snapshot_index_shard(research_id, descriptor)
            loaded[shard.number] = shard
            for offset, entry in enumerate(shard.entries):
                if entry.sort_key == decoded.sort_key and entry.id == decoded.item_id:
                    start = shard.start_position + offset + 1
                    break
            else:
                raise ValueError("cursor item is absent from this immutable snapshot")

        stop = min(start + page_size, manifest.evidence_total)
        selected: list[EvidenceIndexEntry] = []
        if start < stop:
            first_shard = start // SNAPSHOT_INDEX_SHARD_SIZE
            last_shard = (stop - 1) // SNAPSHOT_INDEX_SHARD_SIZE
            for number in range(first_shard, last_shard + 1):
                descriptor = manifest.shards[number]
                current_shard = loaded.get(number)
                if current_shard is None:
                    current_shard = self._get_snapshot_index_shard(research_id, descriptor)
                local_start = (
                    max(start, current_shard.start_position) - current_shard.start_position
                )
                local_stop = min(stop, current_shard.end_position) - current_shard.start_position
                selected.extend(current_shard.entries[local_start:local_stop])

        next_cursor = None
        if stop < manifest.evidence_total:
            last = selected[-1]
            next_cursor = StableCursor(
                query_fingerprint=manifest.query_fingerprint,
                index_revision=manifest.index_revision,
                sort_key=last.sort_key,
                item_id=last.id,
                page_size=page_size,
            ).encode()
        page = EvidencePage(
            matched_total=manifest.evidence_total,
            returned_count=len(selected),
            returned_through=stop,
            next_cursor=next_cursor,
        )
        return ResearchResultIndexPage(manifest, page, tuple(selected)).to_dict()

    def get_overview_page(
        self,
        research_id: str,
        *,
        offset: int = 0,
        page_size: int = 20,
    ) -> dict[str, Any] | None:
        if offset < 0:
            raise ValueError("offset must not be negative")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        summary = self.get_snapshot_summary(research_id)
        if summary is None:
            return None
        manifest = self._get_overview_manifest(research_id)
        if manifest is None:
            raise ResearchRunStorageError("ready snapshot overview manifest is missing")
        _validate_overview_summary_binding(research_id, manifest, summary)
        required = overview_catalog_required_shards(
            manifest,
            offset=offset,
            page_size=page_size,
        )
        shards = tuple(self._get_overview_shard(research_id, descriptor) for descriptor in required)
        try:
            catalog = overview_catalog_page(
                manifest,
                shards,
                offset=offset,
                page_size=page_size,
            )
        except ValueError as exc:
            raise ResearchRunStorageError("overview catalog binding is invalid") from exc
        payload = manifest.to_dict()
        payload["catalog"] = catalog.to_dict()
        payload["core_full_text_required_ids"] = [
            route.evidence_id for route in manifest.core if not route.text_inline_complete
        ]
        return payload

    def get_evidence_index_entry(
        self,
        research_id: str,
        evidence_id: str,
    ) -> EvidenceIndexEntry | None:
        if not evidence_id.strip():
            raise ValueError("evidence_id is required")
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return None
        manifest = self._get_snapshot_index(research_id)
        if manifest is None:
            return None
        if (
            manifest.research_id != research_id
            or manifest.query_fingerprint != gateway.job.query_fingerprint
            or manifest.index_revision != gateway.job.index_revision
        ):
            raise ResearchRunStorageError("snapshot index job binding is invalid")
        bucket_number = snapshot_index_lookup_bucket(
            evidence_id,
            manifest.lookup_bucket_count,
        )
        value = self._get_fixed(
            research_id,
            ArtifactKind.RESULT_PAGE,
            f"run/snapshot-index/lookup/{bucket_number}",
            "snapshot_index_lookup",
        )
        if value is None:
            raise LookupError("evidence id is absent from this immutable snapshot")
        bucket = self._expect(value, SnapshotIndexLookupBucket, "snapshot index lookup")
        if bucket.number != bucket_number:
            raise ResearchRunStorageError("snapshot index lookup binding is invalid")
        shard_number = next(
            (
                number
                for candidate_id, number, _text_shard in bucket.entries
                if candidate_id == evidence_id
            ),
            None,
        )
        if shard_number is None:
            raise LookupError("evidence id is absent from this immutable snapshot")
        shard = self._get_snapshot_index_shard(
            research_id,
            manifest.shards[shard_number],
        )
        for entry in shard.entries:
            if entry.id == evidence_id:
                return entry
        raise ResearchRunStorageError("snapshot index evidence lookup is invalid")

    def get_overflow_evidence_record(
        self,
        research_id: str,
        evidence_id: str,
    ) -> EvidenceRecord | None:
        if not evidence_id.strip():
            raise ValueError("evidence_id is required")
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return None
        manifest = self._get_snapshot_index(research_id)
        if manifest is None:
            return None
        bucket_number = snapshot_index_lookup_bucket(
            evidence_id,
            manifest.lookup_bucket_count,
        )
        value = self._get_fixed(
            research_id,
            ArtifactKind.RESULT_PAGE,
            f"run/snapshot-index/lookup/{bucket_number}",
            "snapshot_index_lookup",
        )
        if value is None:
            raise LookupError("evidence id is absent from this immutable snapshot")
        bucket = self._expect(value, SnapshotIndexLookupBucket, "snapshot index lookup")
        if bucket.number != bucket_number:
            raise ResearchRunStorageError("snapshot index lookup binding is invalid")
        text_shard_number = next(
            (
                text_shard
                for candidate_id, _index_shard, text_shard in bucket.entries
                if candidate_id == evidence_id
            ),
            None,
        )
        if text_shard_number is None:
            raise LookupError("evidence id does not require external full-text delivery")
        text_shard = self._get_evidence_text_shard(research_id, text_shard_number)
        for evidence in text_shard.records:
            if evidence.id == evidence_id:
                return evidence
        raise ResearchRunStorageError("evidence text shard binding is invalid")

    def get_next_full_text_evidence_id(
        self,
        research_id: str,
        after_evidence_id: str,
    ) -> str | None:
        if not after_evidence_id.strip():
            raise ValueError("after_evidence_id is required")
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return None
        manifest = self._get_snapshot_index(research_id)
        if manifest is None:
            return None
        if (
            manifest.research_id != research_id
            or manifest.query_fingerprint != gateway.job.query_fingerprint
            or manifest.index_revision != gateway.job.index_revision
        ):
            raise ResearchRunStorageError("snapshot index job binding is invalid")
        bucket_number = snapshot_index_lookup_bucket(
            after_evidence_id,
            manifest.lookup_bucket_count,
        )
        value = self._get_fixed(
            research_id,
            ArtifactKind.RESULT_PAGE,
            f"run/snapshot-index/lookup/{bucket_number}",
            "snapshot_index_lookup",
        )
        if value is None:
            raise LookupError("evidence id is absent from this immutable snapshot")
        bucket = self._expect(value, SnapshotIndexLookupBucket, "snapshot index lookup")
        if bucket.number != bucket_number:
            raise ResearchRunStorageError("snapshot index lookup binding is invalid")
        shard_number = next(
            (
                number
                for candidate_id, number, _text_shard in bucket.entries
                if candidate_id == after_evidence_id
            ),
            None,
        )
        if shard_number is None:
            raise LookupError("evidence id is absent from this immutable snapshot")
        shard = self._get_snapshot_index_shard(
            research_id,
            manifest.shards[shard_number],
        )
        offset = next(
            (number for number, entry in enumerate(shard.entries) if entry.id == after_evidence_id),
            None,
        )
        if offset is None:
            raise ResearchRunStorageError("snapshot index evidence lookup is invalid")
        for entry in shard.entries[offset + 1 :]:
            if entry.inline_text is None:
                return entry.id
        for descriptor in manifest.shards[shard_number + 1 :]:
            next_shard = self._get_snapshot_index_shard(research_id, descriptor)
            for entry in next_shard.entries:
                if entry.inline_text is None:
                    return entry.id
        return None

    def get_next_core_evidence_id(
        self,
        research_id: str,
        after_evidence_id: str,
    ) -> str | None:
        if not after_evidence_id.strip():
            raise ValueError("after_evidence_id is required")
        summary = self.get_snapshot_summary(research_id)
        if summary is None:
            return None
        manifest = self._get_overview_manifest(research_id)
        if manifest is None:
            raise ResearchRunStorageError("ready snapshot overview manifest is missing")
        _validate_overview_summary_binding(research_id, manifest, summary)
        found = False
        for route in manifest.core:
            if found and not route.text_inline_complete:
                return route.evidence_id
            if route.evidence_id == after_evidence_id:
                found = True
        if not found:
            raise LookupError("evidence id is absent from the immutable core")
        return None

    def _get_evidence_text_shard(
        self,
        research_id: str,
        shard_number: int,
    ) -> EvidenceTextShard:
        value = self._get_fixed(
            research_id,
            ArtifactKind.RESULT_PAGE,
            f"run/snapshot-text/shard/{shard_number}",
            "evidence_text_shard",
        )
        if value is None:
            raise ResearchRunStorageError("evidence text shard is missing")
        shard = self._expect(value, EvidenceTextShard, "evidence text shard")
        if shard.number != shard_number:
            raise ResearchRunStorageError("evidence text shard binding is invalid")
        return shard

    def _get_overview_manifest(
        self,
        research_id: str,
    ) -> OverviewTransportManifest | None:
        record = self._get_fixed_record(
            research_id,
            ArtifactKind.RESULT_PAGE,
            "run/overview/manifest",
            "overview_manifest",
        )
        if record is None:
            return None
        entity, value = record
        manifest = self._expect(value, OverviewTransportManifest, "overview manifest")
        if entity.get("research_id") != research_id:
            raise ResearchRunStorageError("overview manifest entity binding is invalid")
        return manifest

    def _get_overview_shard(
        self,
        research_id: str,
        descriptor: OverviewGroupShardDescriptor,
    ) -> OverviewGroupShard:
        record = self._get_fixed_record(
            research_id,
            ArtifactKind.RESULT_PAGE,
            f"run/overview/shard/{descriptor.number}",
            "overview_group_shard",
        )
        if record is None:
            raise ResearchRunStorageError("overview group shard is missing")
        entity, value = record
        shard = self._expect(value, OverviewGroupShard, "overview group shard")
        if (
            entity.get("research_id") != research_id
            or entity.get("shard") != descriptor.number
            or OverviewGroupShardDescriptor.from_shard(shard) != descriptor
        ):
            raise ResearchRunStorageError("overview group shard binding is invalid")
        return shard

    def _get_snapshot_index(self, research_id: str) -> ResearchSnapshotIndex | None:
        value = self._get_fixed(
            research_id,
            ArtifactKind.RESULT_PAGE,
            "run/snapshot-index/manifest",
            "snapshot_index_manifest",
        )
        if value is None:
            return None
        return self._expect(value, ResearchSnapshotIndex, "snapshot index manifest")

    def _get_snapshot_index_shard(
        self,
        research_id: str,
        descriptor: SnapshotIndexShardDescriptor,
    ) -> SnapshotIndexShard:
        value = self._get_fixed(
            research_id,
            ArtifactKind.RESULT_PAGE,
            f"run/snapshot-index/shard/{descriptor.number}",
            "snapshot_index_shard",
        )
        if value is None:
            raise ResearchRunStorageError("snapshot index shard is missing")
        shard = self._expect(value, SnapshotIndexShard, "snapshot index shard")
        if (
            SnapshotIndexShardDescriptor.from_shard(shard) != descriptor
            or shard.number != descriptor.number
        ):
            raise ResearchRunStorageError("snapshot index shard binding is invalid")
        return shard

    def _partition(
        self,
        research_id: str,
        gateway: GatewayPlanState,
        phase: MetadataPhase,
        partition_id: str,
    ) -> Any:
        if phase is MetadataPhase.DISCOVERY:
            partitions = gateway.discovery_partitions
        else:
            partition = self.get_status_partition(research_id, partition_id)
            if partition is None:
                raise LookupError("bill-status partitions are not available")
            return partition
        for partition in partitions:
            if partition.partition_id == partition_id:
                return partition
        raise ValueError("metadata partition is outside the research plan")

    def _terminal_outcome(self, research_id: str, work_id: str) -> DocumentOutcome | None:
        value = self._get_fixed(
            research_id,
            ArtifactKind.OUTCOME,
            f"run/document-terminal/{work_id}",
            "document_outcome",
        )
        if value is None:
            return None
        outcome = self._expect(value, DocumentOutcome, "document outcome")
        if outcome.work_id != work_id or not outcome.terminal:
            raise ResearchRunStorageError("terminal document outcome identity is invalid")
        return outcome

    def _get_page(
        self,
        research_id: str,
        phase: MetadataPhase,
        partition_id: str,
        page_number: int,
    ) -> ApiPage | None:
        record = self._get_fixed_record(
            research_id,
            ArtifactKind.PARTITION,
            f"run/page/{phase.value}/{partition_id}/{page_number}",
            "page",
        )
        if record is None:
            return None
        entity, value = record
        page = self._expect(value, ApiPage, "metadata page")
        if (
            entity.get("phase") != phase.value
            or entity.get("partition_id") != partition_id
            or entity.get("page") != page_number
            or page.page != page_number
        ):
            raise ResearchRunStorageError("metadata page identity is invalid")
        return page

    def _get_page_readiness(
        self,
        research_id: str,
        gateway: GatewayPlanState,
        phase: MetadataPhase,
        partition: MetadataPartition,
        page_number: int,
    ) -> MetadataPageReadiness | None:
        record = self._get_fixed_record(
            research_id,
            ArtifactKind.MANIFEST,
            f"run/page-ready/{phase.value}/{partition.partition_id}/{page_number}",
            "page_readiness",
        )
        if record is None:
            return None
        entity, value = record
        readiness = self._expect(value, MetadataPageReadiness, "metadata page readiness")
        if (
            entity.get("phase") != phase.value
            or entity.get("partition_id") != partition.partition_id
            or entity.get("page") != page_number
            or readiness.query_fingerprint != gateway.job.query_fingerprint
            or readiness.index_revision != gateway.job.index_revision
            or readiness.phase is not phase
            or readiness.partition_id != partition.partition_id
            or readiness.dataset != partition.dataset
            or readiness.page != page_number
            or readiness.page_size != partition.page_size
        ):
            raise ResearchRunStorageError("metadata page readiness identity is invalid")
        return readiness

    def _required_discovery_first_pages(
        self,
        research_id: str,
        gateway: GatewayPlanState,
    ) -> tuple[MetadataPageReadiness, ...]:
        """Read every planned discovery page-one marker in plan order."""

        partitions = gateway.discovery_partitions
        if not partitions:
            raise ValueError("first-page preview requires discovery partitions")

        def read_first(partition: MetadataPartition) -> MetadataPageReadiness | None:
            return self._get_page_readiness(
                research_id,
                gateway,
                MetadataPhase.DISCOVERY,
                partition,
                1,
            )

        values = self._bounded_map(
            read_first,
            partitions,
            "kbd-first-page-preview-read",
        )
        if any(value is None for value in values):
            raise LookupError("not every discovery first page is ready")
        return cast(tuple[MetadataPageReadiness, ...], values)

    def _get_first_page_preview_readiness(
        self,
        research_id: str,
        gateway: GatewayPlanState,
    ) -> Mapping[str, Any] | None:
        value = self._get_fixed(
            research_id,
            ArtifactKind.MANIFEST,
            _FIRST_PAGE_PREVIEW_READY_KEY,
            "first_page_preview_readiness",
        )
        if value is None:
            return None
        expected_fields = {
            "query_fingerprint",
            "index_revision",
            "source_hash",
            "preview_hash",
            "first_pages_hash",
            "partition_count",
            "partitions_complete",
            "pages_expected",
            "pages_complete",
            "source_rows_expected",
            "source_rows_fetched",
        }
        if not isinstance(value, Mapping) or set(value) != expected_fields:
            raise ResearchRunStorageError("first-page preview readiness is invalid")
        if (
            value.get("query_fingerprint") != gateway.job.query_fingerprint
            or value.get("index_revision") != gateway.job.index_revision
            or any(
                not isinstance(value.get(name), str)
                or not re.fullmatch(r"[0-9a-f]{64}", cast(str, value.get(name)))
                for name in ("source_hash", "preview_hash", "first_pages_hash")
            )
        ):
            raise ResearchRunStorageError("first-page preview readiness binding is invalid")
        count_fields = (
            "partition_count",
            "partitions_complete",
            "pages_expected",
            "pages_complete",
            "source_rows_expected",
            "source_rows_fetched",
        )
        if any(type(value.get(name)) is not int for name in count_fields):
            raise ResearchRunStorageError("first-page preview readiness counts are invalid")
        partition_count = cast(int, value["partition_count"])
        partitions_complete = cast(int, value["partitions_complete"])
        pages_expected = cast(int, value["pages_expected"])
        pages_complete = cast(int, value["pages_complete"])
        source_rows_expected = cast(int, value["source_rows_expected"])
        source_rows_fetched = cast(int, value["source_rows_fetched"])
        if (
            partition_count != len(gateway.discovery_partitions)
            or pages_complete != partition_count
            or not 0 <= partitions_complete <= partition_count
            or pages_expected < pages_complete
            or source_rows_expected < 0
            or not 0 <= source_rows_fetched <= source_rows_expected
        ):
            raise ResearchRunStorageError("first-page preview readiness counts are invalid")
        return value

    def _get_bound_first_page_preview(
        self,
        research_id: str,
        gateway: GatewayPlanState,
        readiness: Mapping[str, Any],
    ) -> ProvisionalResearchOverview | None:
        record = self._get_fixed_record(
            research_id,
            ArtifactKind.RESOLUTION,
            _FIRST_PAGE_PREVIEW_KEY,
            "first_page_preview",
        )
        if record is None:
            return None
        entity, value = record
        preview = self._expect(value, ProvisionalResearchOverview, "first-page preview")
        if (
            set(entity)
            != {
                "query_fingerprint",
                "index_revision",
                "source_hash",
                "first_pages_hash",
            }
            or entity.get("query_fingerprint") != gateway.job.query_fingerprint
            or entity.get("index_revision") != gateway.job.index_revision
            or entity.get("source_hash") != readiness.get("source_hash")
            or entity.get("first_pages_hash") != readiness.get("first_pages_hash")
            or preview.query != gateway.job.contract.query
            or preview.source_hash != readiness.get("source_hash")
            or preview.source.source_complete is not False
            or preview.source.source_rows_expected != readiness.get("source_rows_expected")
            or preview.source.source_rows_fetched != readiness.get("source_rows_fetched")
            or canonical_hash(_encode(preview)) != readiness.get("preview_hash")
        ):
            raise ResearchRunStorageError("first-page preview binding is invalid")
        return preview

    def _first_page_preview_progress(
        self,
        research_id: str,
        gateway: GatewayPlanState,
    ) -> tuple[int, int, int, int]:
        readiness = self._get_first_page_preview_readiness(research_id, gateway)
        if readiness is None:
            raise ResearchRunStorageError("first-page preview readiness is missing")
        return (
            cast(int, readiness["partition_count"]),
            cast(int, readiness["partitions_complete"]),
            cast(int, readiness["pages_expected"]),
            cast(int, readiness["pages_complete"]),
        )

    def _get_gateway_any(self, research_id: str) -> GatewayPlanState | None:
        value = self._get_fixed(
            research_id,
            ArtifactKind.PLAN,
            "run/gateway",
            "gateway",
        )
        if value is None:
            return None
        state = self._expect(value, GatewayPlanState, "gateway")
        if state.job.id != research_id:
            raise ResearchRunStorageError("gateway research binding is invalid")
        return state

    def _get_discovery_readiness(
        self,
        research_id: str,
        gateway: GatewayPlanState,
    ) -> DiscoveryBoundaryReadiness | None:
        value = self._get_fixed(
            research_id,
            ArtifactKind.MANIFEST,
            "run/discovery-ready",
            "discovery_readiness",
        )
        if value is None:
            return None
        readiness = self._expect(value, DiscoveryBoundaryReadiness, "discovery readiness")
        if (
            readiness.query_fingerprint != gateway.job.query_fingerprint
            or readiness.index_revision != gateway.job.index_revision
        ):
            raise ResearchRunStorageError("discovery readiness job binding is invalid")
        return readiness

    def _get_document_readiness(
        self,
        research_id: str,
        gateway: GatewayPlanState,
    ) -> DocumentBoundaryReadiness | None:
        value = self._get_fixed(
            research_id,
            ArtifactKind.MANIFEST,
            "run/document-ready",
            "document_readiness",
        )
        if value is None:
            return None
        readiness = self._expect(value, DocumentBoundaryReadiness, "document readiness")
        if (
            readiness.query_fingerprint != gateway.job.query_fingerprint
            or readiness.index_revision != gateway.job.index_revision
        ):
            raise ResearchRunStorageError("document readiness job binding is invalid")
        return readiness

    def _discovery_readiness_or_adopt(
        self,
        research_id: str,
        gateway: GatewayPlanState,
    ) -> DiscoveryBoundaryReadiness | None:
        """Adopt a pre-v1.0 discovery boundary into fixed routing artifacts once."""

        readiness = self._get_discovery_readiness(research_id, gateway)
        if readiness is not None:
            return readiness
        value = self._get_fixed(
            research_id,
            ArtifactKind.RESOLUTION,
            _LEGACY_DISCOVERY_STATE_KEY,
            "discovery",
        )
        if value is None:
            return None
        legacy = self._expect(value, DiscoveryStageState, "legacy discovery")
        if legacy.resolution.query != gateway.job.contract.query:
            raise ResearchRunStorageError("legacy discovery contract binding is invalid")
        self.put_discovery(research_id, legacy)
        readiness = self._get_discovery_readiness(research_id, gateway)
        if readiness is None:
            raise ResearchRunStorageError("legacy discovery adoption did not become ready")
        return readiness

    def _document_readiness_or_adopt(
        self,
        research_id: str,
        gateway: GatewayPlanState,
    ) -> DocumentBoundaryReadiness | None:
        """Adopt a pre-v1.0 metadata boundary without rewriting its old key."""

        readiness = self._get_document_readiness(research_id, gateway)
        if readiness is not None:
            return readiness
        value = self._get_fixed(
            research_id,
            ArtifactKind.METADATA,
            _LEGACY_METADATA_STATE_KEY,
            "metadata",
        )
        if value is None:
            return None
        legacy = self._expect(value, MetadataStageState, "legacy metadata")
        if legacy.discovery.resolution.query != gateway.job.contract.query:
            raise ResearchRunStorageError("legacy metadata contract binding is invalid")
        self.put_discovery(research_id, legacy.discovery)
        self.put_metadata(research_id, legacy)
        readiness = self._get_document_readiness(research_id, gateway)
        if readiness is None:
            raise ResearchRunStorageError("legacy metadata adoption did not become ready")
        return readiness

    def _get_bound_hot_record(
        self,
        research_id: str,
        gateway: GatewayPlanState,
        boundary_hash: str,
        logical_key: str,
        record_type: str,
    ) -> tuple[Mapping[str, Any], Any] | None:
        record = self._get_fixed_record(
            research_id,
            ArtifactKind.MANIFEST,
            logical_key,
            record_type,
        )
        if record is None:
            return None
        entity, value = record
        if (
            entity.get("query_fingerprint") != gateway.job.query_fingerprint
            or entity.get("index_revision") != gateway.job.index_revision
            or entity.get("boundary_hash") != boundary_hash
            or entity.get("binding_hash") != _hot_binding_hash(boundary_hash, logical_key, value)
        ):
            raise ResearchRunStorageError("hot routing artifact binding is invalid")
        return entity, value

    @staticmethod
    def _validate_task_receipt_binding(
        gateway: GatewayPlanState,
        task: ResearchTask,
        receipt: TaskCompletionReceipt,
    ) -> None:
        if (
            task.research_id != gateway.job.id
            or task.query_fingerprint != gateway.job.query_fingerprint
            or task.index_revision != gateway.job.index_revision
            or receipt != TaskCompletionReceipt.from_task(task)
        ):
            raise ResearchRunStorageError("task completion job binding is invalid")

    def _require_active(self, research_id: str) -> GatewayPlanState:
        state = self._get_gateway_any(research_id)
        if state is None:
            raise LookupError("research gateway is not available")
        if self._expired(state):
            raise ResearchRunExpiredError("research run has expired")
        return state

    def _expired(self, state: GatewayPlanState) -> bool:
        self._validate_clock()
        return self._now() >= state.job.expires_at

    def _validate_clock(self) -> None:
        observed = self._now()
        if observed.tzinfo is None:
            raise ValueError("run store clock must return a timezone-aware datetime")

    def _put_fixed(
        self,
        research_id: str,
        kind: ArtifactKind,
        logical_key: str,
        record_type: str,
        entity: Mapping[str, Any],
        value: _Value,
        *,
        expires_at: datetime,
    ) -> _Value:
        payload = _record_payload(
            research_id,
            record_type,
            entity,
            value,
            expires_at=expires_at,
        )
        # The artifact backend owns atomic put-if-absent semantics. Keeping a
        # process-wide lock around network I/O serialized every supposedly
        # parallel result-shard PUT and recreated the 300-second timeout.
        for attempt in range(_WRITE_RETRIES):
            try:
                self.artifacts.write(research_id, kind, payload, logical_key=logical_key)
                return value
            except ArtifactConflictError:
                try:
                    existing = self._get_fixed(research_id, kind, logical_key, record_type)
                except (ArtifactIntegrityError, ArtifactBackendError):
                    existing = None
                if existing is not None:
                    if existing == value:
                        return cast(_Value, existing)
                    raise ResearchRunConflictError(
                        "immutable research artifact already contains different state"
                    ) from None
                if attempt + 1 < _WRITE_RETRIES:
                    time.sleep(_WRITE_RETRY_SECONDS)
                    continue
                raise ResearchRunStorageError(
                    "concurrent research artifact did not become readable"
                ) from None
        raise AssertionError("unreachable")

    def _put_content_addressed(
        self,
        research_id: str,
        kind: ArtifactKind,
        record_type: str,
        entity: Mapping[str, Any],
        value: _Value,
        *,
        expires_at: datetime,
    ) -> _Value:
        payload = _record_payload(
            research_id,
            record_type,
            entity,
            value,
            expires_at=expires_at,
        )
        with self._lock:
            for attempt in range(_WRITE_RETRIES):
                try:
                    self.artifacts.write(research_id, kind, payload)
                    return value
                except ArtifactConflictError:
                    # Content-addressed paths can only conflict during a create
                    # race (or a cryptographic collision).  Retry until the
                    # winning immutable bytes are readable and idempotent.
                    if attempt + 1 < _WRITE_RETRIES:
                        time.sleep(_WRITE_RETRY_SECONDS)
                        continue
                    raise ResearchRunStorageError(
                        "concurrent content-addressed artifact did not stabilize"
                    ) from None
        return value

    def _get_fixed(
        self,
        research_id: str,
        kind: ArtifactKind,
        logical_key: str,
        record_type: str,
    ) -> Any | None:
        record = self._get_fixed_record(
            research_id,
            kind,
            logical_key,
            record_type,
        )
        if record is None:
            return None
        _entity, value = record
        return value

    def _get_fixed_record(
        self,
        research_id: str,
        kind: ArtifactKind,
        logical_key: str,
        record_type: str,
    ) -> tuple[Mapping[str, Any], Any] | None:
        stored = self.artifacts.read_logical(research_id, kind, logical_key)
        if stored is None:
            return None
        decoded = self._decode_record(
            stored.ref,
            stored.payload,
            research_id,
            record_type,
        )
        if decoded is None:  # Only possible when ignore_unrelated=True.
            raise AssertionError("fixed research artifact was unexpectedly ignored")
        return decoded

    def _records(
        self, research_id: str, kind: ArtifactKind, record_type: str
    ) -> tuple[tuple[ArtifactRef, Mapping[str, Any], Any], ...]:
        records: list[tuple[ArtifactRef, Mapping[str, Any], Any]] = []
        for ref in self.artifacts.list(research_id, kind):
            stored = self.artifacts.read(ref)
            if stored is None:
                raise ResearchRunStorageError("listed research artifact is missing")
            decoded = self._decode_record(
                ref,
                stored.payload,
                research_id,
                record_type,
                ignore_unrelated=True,
            )
            if decoded is None:
                continue
            entity, value = decoded
            records.append((ref, entity, value))
        return tuple(records)

    @staticmethod
    def _decode_record(
        ref: ArtifactRef,
        payload: Any,
        research_id: str,
        record_type: str,
        *,
        ignore_unrelated: bool = False,
    ) -> tuple[Mapping[str, Any], Any] | None:
        if not isinstance(payload, Mapping) or payload.get("namespace") != _NAMESPACE:
            if ignore_unrelated:
                return None
            raise ResearchRunStorageError("fixed research artifact namespace is invalid")
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise ResearchRunStorageError("research run artifact schema is unsupported")
        if payload.get("research_id") != research_id or ref.research_id != research_id:
            raise ResearchRunStorageError("research run artifact binding is invalid")
        if payload.get("record_type") != record_type:
            if ignore_unrelated:
                return None
            raise ResearchRunStorageError("fixed research artifact type is invalid")
        entity = payload.get("entity")
        if not isinstance(entity, Mapping):
            raise ResearchRunStorageError("research run entity identity is invalid")
        raw_value = payload.get("value")
        try:
            value = _decode(raw_value)
        except (TypeError, ValueError, ImportError, AttributeError) as exc:
            raise ResearchRunStorageError("research run artifact value is invalid") from exc
        return cast(Mapping[str, Any], entity), value

    @staticmethod
    def _expect(value: Any, expected: type[_Value], label: str) -> _Value:
        if not isinstance(value, expected):
            raise ResearchRunStorageError(f"stored {label} has the wrong type")
        return value


def _descriptor_for_cursor(
    manifest: ResearchSnapshotIndex,
    cursor: StableCursor,
) -> SnapshotIndexShardDescriptor:
    key = (cursor.sort_key, cursor.item_id)
    for descriptor in manifest.shards:
        if (
            (
                descriptor.first_sort_key,
                descriptor.first_id,
            )
            <= key
            <= (
                descriptor.last_sort_key,
                descriptor.last_id,
            )
        ):
            return descriptor
    raise ValueError("cursor item is absent from this immutable snapshot")


def _validate_overview_summary_binding(
    research_id: str,
    manifest: OverviewTransportManifest,
    summary: ResearchSnapshotSummary,
) -> None:
    expected_axes = tuple(
        OverviewCoverageAxisSummary.from_coverage(entry)
        for entry in summary.coverage.entries
        if entry.evidence_type in summary.coverage.requested
    )
    manifest_evidence_types = tuple(
        sorted(
            (item.evidence_type for item in manifest.evidence_type_counts),
            key=lambda item: item.value,
        )
    )
    if (
        summary.research_id != research_id
        or manifest.research_id != summary.research_id
        or manifest.query_fingerprint != summary.query_fingerprint
        or manifest.index_revision != summary.index_revision
        or manifest.build_sha != summary.build_sha
        or manifest.coverage_requested != summary.coverage.requested
        or manifest.coverage_axes != expected_axes
        or manifest.complete != summary.coverage.complete
        or manifest.evidence_count != summary.evidence_total
        or manifest_evidence_types != summary.evidence_types
    ):
        raise ResearchRunStorageError("overview snapshot binding is invalid")


def _status_partition_key(partition_id: str) -> str:
    digest = hashlib.sha256(partition_id.encode()).hexdigest()
    return f"run/status-partition/{digest}"


def _hot_binding_hash(boundary_hash: str, logical_key: str, value: Any) -> str:
    value_hash = canonical_hash(_encode(value))
    return hashlib.sha256(f"{boundary_hash}\0{logical_key}\0{value_hash}".encode()).hexdigest()


def _compact_document_outcome(outcome: DocumentOutcome) -> DocumentOutcome:
    """Persist immutable document identity once, not full parsed text per run."""

    result = outcome.result
    if result is None or result.document is None:
        return outcome
    compact = replace(result, document=None)
    return replace(outcome, result=compact)


def _hot_entity(
    gateway: GatewayPlanState,
    boundary_hash: str,
    logical_key: str,
    value: Any,
    **identity: Any,
) -> dict[str, Any]:
    return {
        "query_fingerprint": gateway.job.query_fingerprint,
        "index_revision": gateway.job.index_revision,
        "boundary_hash": boundary_hash,
        "binding_hash": _hot_binding_hash(boundary_hash, logical_key, value),
        **identity,
    }


def _record_payload(
    research_id: str,
    record_type: str,
    entity: Mapping[str, Any],
    value: Any,
    *,
    expires_at: datetime,
) -> dict[str, Any]:
    if expires_at.tzinfo is None:
        raise ValueError("artifact expiry must be timezone-aware")
    return {
        "namespace": _NAMESPACE,
        "schema_version": _SCHEMA_VERSION,
        "research_id": research_id,
        "record_type": record_type,
        "entity": dict(entity),
        "expires_at": expires_at.isoformat(),
        "value": _encode(value),
    }


@lru_cache(maxsize=1)
def _type_registry() -> dict[str, type[Any]]:
    result: dict[str, type[Any]] = {}
    for module_name in _ALLOWED_TYPE_MODULES:
        module = importlib.import_module(module_name)
        for candidate in vars(module).values():
            if not isinstance(candidate, type) or candidate.__module__ != module_name:
                continue
            if not is_dataclass(candidate) and not issubclass(candidate, Enum):
                continue
            name = f"{module_name}:{candidate.__qualname__}"
            result[name] = candidate
    return result


def _encode(value: Any) -> Any:
    if isinstance(value, Enum):
        return {
            _TYPE_MARKER: "enum",
            "type": f"{type(value).__module__}:{type(value).__qualname__}",
            "value": _encode(value.value),
        }
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("run artifact datetimes must be timezone-aware")
        return {_TYPE_MARKER: "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {_TYPE_MARKER: "date", "value": value.isoformat()}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            _TYPE_MARKER: "dataclass",
            "type": f"{type(value).__module__}:{type(value).__qualname__}",
            "fields": {field.name: _encode(getattr(value, field.name)) for field in fields(value)},
        }
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("run artifact floats must be finite")
        return value
    if isinstance(value, tuple):
        return {_TYPE_MARKER: "tuple", "items": [_encode(item) for item in value]}
    if isinstance(value, list):
        return {_TYPE_MARKER: "list", "items": [_encode(item) for item in value]}
    if isinstance(value, Mapping):
        items = [(_encode(key), _encode(item)) for key, item in value.items()]
        items.sort(key=lambda pair: canonical_hash(pair[0]))
        return {_TYPE_MARKER: "mapping", "items": [[key, item] for key, item in items]}
    raise ValueError("run artifact contains an unsupported value type")


def _decode(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("encoded run value must be an object or primitive")
    marker = value.get(_TYPE_MARKER)
    if marker == "datetime":
        observed_at = datetime.fromisoformat(_required_string(value, "value"))
        if observed_at.tzinfo is None:
            raise ValueError("decoded run datetime is not timezone-aware")
        return observed_at
    if marker == "date":
        return date.fromisoformat(_required_string(value, "value"))
    if marker in {"tuple", "list"}:
        raw_items = value.get("items")
        if not isinstance(raw_items, list):
            raise ValueError("encoded run sequence is invalid")
        items = [_decode(item) for item in raw_items]
        return tuple(items) if marker == "tuple" else items
    if marker == "mapping":
        raw_items = value.get("items")
        if not isinstance(raw_items, list):
            raise ValueError("encoded run mapping is invalid")
        restored_mapping: dict[Any, Any] = {}
        for pair in raw_items:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("encoded run mapping item is invalid")
            key = _decode(pair[0])
            try:
                if key in restored_mapping:
                    raise ValueError("encoded run mapping has duplicate keys")
                restored_mapping[key] = _decode(pair[1])
            except TypeError as exc:
                raise ValueError("encoded run mapping key is unhashable") from exc
        return restored_mapping
    if marker in {"enum", "dataclass"}:
        type_name = _required_string(value, "type")
        try:
            target = _type_registry()[type_name]
        except KeyError as exc:
            raise ValueError("encoded run type is not allowed") from exc
        if marker == "enum":
            if not issubclass(target, Enum):
                raise ValueError("encoded run enum type is invalid")
            return target(_decode(value.get("value")))
        if not is_dataclass(target):
            raise ValueError("encoded run dataclass type is invalid")
        raw_fields = value.get("fields")
        if not isinstance(raw_fields, Mapping):
            raise ValueError("encoded run dataclass fields are invalid")
        init_fields = {field.name: field for field in fields(target) if field.init}
        encoded_fields = set(raw_fields)
        unknown = encoded_fields - set(init_fields)
        required_missing = {
            name
            for name, field in init_fields.items()
            if name not in encoded_fields
            and field.default is MISSING
            and field.default_factory is MISSING
        }
        if unknown or required_missing:
            raise ValueError("encoded run dataclass field set is invalid")
        # Append-only run artifacts can outlive a deployment.  A newer
        # dataclass may add an optional field with a declared default; old
        # artifacts must resume with exactly that default instead of becoming
        # unreadable.  Required fields and unknown fields remain fail-closed.
        arguments = {name: _decode(raw_fields[name]) for name in sorted(encoded_fields)}
        return target(**arguments)
    raise ValueError("encoded run value marker is invalid")


def _required_string(value: Mapping[str, Any], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str):
        raise ValueError("encoded run string field is invalid")
    return result


def _first_page_collection_source_hash(
    partitions: Sequence[MetadataPartition],
    first_pages: Sequence[MetadataPageReadiness],
) -> str:
    """Reproduce MetadataCollection.source_hash for page-one-only results."""

    if len(partitions) != len(first_pages):
        raise ValueError("first-page preview partition accounting is invalid")
    by_id = {item.partition_id: item for item in first_pages}
    if len(by_id) != len(first_pages):
        raise ValueError("first-page preview contains duplicate partitions")
    ordered = sorted(
        partitions,
        key=lambda item: (
            item.kind.value,
            item.dataset,
            tuple((name, str(value)) for name, value in item.parameters),
            item.partition_id,
        ),
    )
    collection_digest = hashlib.sha256()
    for partition in ordered:
        try:
            first = by_id[partition.partition_id]
        except KeyError as exc:
            raise ValueError("first-page preview is missing a planned partition") from exc
        result_digest = hashlib.sha256()
        result_digest.update(b"1:")
        result_digest.update(first.source_hash.encode())
        result_digest.update(b"\n")
        collection_digest.update(partition.partition_id.encode())
        collection_digest.update(b":")
        collection_digest.update(result_digest.hexdigest().encode())
        collection_digest.update(b"\n")
    return collection_digest.hexdigest()


def _candidate_bill_number(decision: CandidateDecision) -> str:
    raw = decision.candidate.get("BILL_NO", decision.candidate.get("bill_no"))
    bill_number = str(raw).strip() if raw is not None else ""
    if (
        decision.kind.value != "bill"
        or not decision.accepted
        or decision.candidate_id != f"bill:{bill_number}"
        or not re.fullmatch(r"\d{7}", bill_number)
    ):
        raise ValueError("accepted bill decision lacks an exact identity")
    return bill_number


def _compact_discovery_state(state: DiscoveryStageState) -> DiscoveryStageState:
    """Drop duplicated rows while retaining lossless page and decision audit.

    Every original official row remains in the immutable page artifacts named
    by ``collection.partitions``. Accepted candidates keep their complete
    normalized payload because finalization needs it. Rejected candidates keep
    their exact source identity plus every score/reason; their full official
    payload is recoverable from those provenance-bound pages.
    """

    resolution = MetadataResolution(
        query=state.resolution.query,
        source_hash=state.resolution.source_hash,
        criteria=state.resolution.criteria,
        bills=_compact_candidate_set(state.resolution.bills),
        meetings=_compact_candidate_set(state.resolution.meetings),
    )
    return replace(
        state,
        collection=_rowless_collection(state.collection),
        filtered_collection=_rowless_collection(state.filtered_collection),
        resolution=resolution,
    )


def _rowless_collection(collection: MetadataCollection) -> MetadataCollection:
    return MetadataCollection((), (), collection.partitions, collection.coverage)


def _compact_candidate_set(
    resolution: CandidateSetResolution,
) -> CandidateSetResolution:
    decisions = tuple(_compact_candidate(item) for item in resolution.decisions)
    by_id = {item.candidate_id: item for item in decisions}
    accepted = tuple(by_id[item.candidate_id] for item in resolution.accepted)
    return CandidateSetResolution(resolution.kind, decisions, accepted)


def _compact_candidate(decision: CandidateDecision) -> CandidateDecision:
    if decision.accepted:
        return decision
    if decision.kind is MetadataKind.BILL:
        identity: Mapping[str, Any] = {"BILL_NO": _candidate_bill_number_any(decision)}
    else:
        prefix = "meeting:"
        if not decision.candidate_id.startswith(prefix):
            raise ValueError("rejected meeting decision lacks an exact identity")
        official_url = decision.candidate_id.removeprefix(prefix).strip()
        if not official_url:
            raise ValueError("rejected meeting decision lacks an official URL")
        identity = {"PDF_LINK_URL": official_url}
    return replace(decision, candidate=identity)


def _candidate_bill_number_any(decision: CandidateDecision) -> str:
    raw = decision.candidate.get("BILL_NO", decision.candidate.get("bill_no"))
    bill_number = str(raw).strip() if raw is not None else ""
    if (
        decision.kind is not MetadataKind.BILL
        or decision.candidate_id != f"bill:{bill_number}"
        or not re.fullmatch(r"\d{7}", bill_number)
    ):
        raise ValueError("bill decision lacks an exact identity")
    return bill_number


__all__ = [
    "ArtifactResearchRunStore",
    "ResearchRunConflictError",
    "ResearchRunExpiredError",
    "ResearchRunStorageError",
]
