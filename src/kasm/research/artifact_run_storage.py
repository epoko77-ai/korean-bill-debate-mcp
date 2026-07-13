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

import importlib
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import MISSING, fields, is_dataclass
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
from .contracts import EvidencePage, StableCursor
from .engine import (
    BillDocumentDiscovery,
    DiscoveryStageState,
    DocumentOutcome,
    GatewayPlanState,
    MetadataPhase,
    MetadataStageState,
)
from .overview_transport import (
    OverviewCoverageAxisSummary,
    OverviewGroupShard,
    OverviewGroupShardDescriptor,
    OverviewTransportManifest,
    build_overview_transport,
    overview_catalog_page,
    overview_catalog_required_shards,
)
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
    "kasm.research.relevance",
    "kasm.research.resolver",
    "kasm.research.results",
    "kasm.research.transcript_evidence",
    "kasm.search.bilingual",
    "kasm.search.terminology",
)

_Value = TypeVar("_Value")


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
    ) -> None:
        self.artifacts = artifacts
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()

    def put_gateway(
        self, research_id: str, state: GatewayPlanState
    ) -> GatewayPlanState:
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
        return self._put_fixed(
            research_id,
            ArtifactKind.PARTITION,
            f"run/page/{phase.value}/{partition_id}/{page.page}",
            "page",
            entity,
            page,
            expires_at=gateway.job.expires_at,
        )

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
        values = [first]
        for page_number in range(2, expected_pages + 1):
            value = self._get_page(
                research_id,
                phase,
                partition_id,
                page_number,
            )
            if value is not None:
                values.append(value)
        return tuple(values)

    def put_discovery(
        self, research_id: str, state: DiscoveryStageState
    ) -> DiscoveryStageState:
        gateway = self._require_active(research_id)
        if state.resolution.query != gateway.job.contract.query:
            raise ValueError("discovery belongs to another research contract")
        return self._put_fixed(
            research_id,
            ArtifactKind.RESOLUTION,
            "run/discovery",
            "discovery",
            {"research_id": research_id},
            state,
            expires_at=gateway.job.expires_at,
        )

    def get_discovery(self, research_id: str) -> DiscoveryStageState | None:
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return None
        value = self._get_fixed(
            research_id,
            ArtifactKind.RESOLUTION,
            "run/discovery",
            "discovery",
        )
        if value is None:
            return None
        state = self._expect(value, DiscoveryStageState, "discovery")
        if state.resolution.query != gateway.job.contract.query:
            raise ResearchRunStorageError("discovery contract binding is invalid")
        return state

    def put_bill_discovery(
        self, research_id: str, outcome: BillDocumentDiscovery
    ) -> BillDocumentDiscovery:
        gateway = self._require_active(research_id)
        discovery = self.get_discovery(research_id)
        if discovery is None:
            raise LookupError("research discovery is not available")
        if outcome.bill_number not in discovery.document_bill_numbers:
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

    def bill_discoveries(
        self, research_id: str
    ) -> tuple[BillDocumentDiscovery, ...]:
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return ()
        discovery = self.get_discovery(research_id)
        allowed = set(discovery.document_bill_numbers) if discovery is not None else set()
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

    def put_metadata(
        self, research_id: str, state: MetadataStageState
    ) -> MetadataStageState:
        gateway = self._require_active(research_id)
        discovery = self.get_discovery(research_id)
        if discovery is None or state.discovery != discovery:
            raise ValueError("metadata does not bind to the stored discovery")
        stored_bill_discoveries = self.bill_discoveries(research_id)
        if state.manifest.bill_discoveries != stored_bill_discoveries:
            raise ValueError("metadata manifest does not bind to bill discoveries")
        return self._put_fixed(
            research_id,
            ArtifactKind.METADATA,
            "run/metadata",
            "metadata",
            {"research_id": research_id},
            state,
            expires_at=gateway.job.expires_at,
        )

    def get_metadata(self, research_id: str) -> MetadataStageState | None:
        gateway = self.get_gateway(research_id)
        if gateway is None:
            return None
        value = self._get_fixed(
            research_id,
            ArtifactKind.METADATA,
            "run/metadata",
            "metadata",
        )
        if value is None:
            return None
        state = self._expect(value, MetadataStageState, "metadata")
        discovery = self.get_discovery(research_id)
        if discovery is None or state.discovery != discovery:
            raise ResearchRunStorageError("metadata discovery binding is invalid")
        return state

    def put_document_outcome(
        self, research_id: str, outcome: DocumentOutcome
    ) -> DocumentOutcome:
        gateway = self._require_active(research_id)
        metadata = self.get_metadata(research_id)
        if metadata is None:
            raise LookupError("research metadata is not available")
        item_by_id = {item.work_id: item for item in metadata.manifest.items}
        try:
            work_item = item_by_id[outcome.work_id]
        except KeyError as exc:
            raise ValueError("document outcome is absent from the stored manifest") from exc
        if outcome.result is not None and (
            outcome.result.kind is not work_item.kind
            or outcome.result.official_url != work_item.official_url
        ):
            raise ValueError("document result does not match its manifest item")

        current_terminal = self._terminal_outcome(research_id, outcome.work_id)
        if current_terminal is not None:
            if current_terminal != outcome:
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
        metadata = self.get_metadata(research_id)
        allowed = (
            {item.work_id for item in metadata.manifest.items}
            if metadata is not None
            else set()
        )
        values: list[DocumentOutcome] = []
        seen: set[str] = set()
        for ref, entity, value in self._records(
            research_id, ArtifactKind.OUTCOME, "document_outcome"
        ):
            restored = self._expect(value, DocumentOutcome, "document outcome")
            if entity.get("work_id") != restored.work_id or entity.get(
                "status"
            ) != restored.status.value:
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

    def put_snapshot(
        self, research_id: str, snapshot: ResearchSnapshot
    ) -> ResearchSnapshot:
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
        for text_shard in text_shards:
            self._put_fixed(
                research_id,
                ArtifactKind.RESULT_PAGE,
                f"run/snapshot-text/shard/{text_shard.number}",
                "evidence_text_shard",
                {"research_id": research_id, "shard": text_shard.number},
                text_shard,
                expires_at=gateway.job.expires_at,
            )
        for shard in shards:
            self._put_fixed(
                research_id,
                ArtifactKind.RESULT_PAGE,
                f"run/snapshot-index/shard/{shard.number}",
                "snapshot_index_shard",
                {"research_id": research_id, "shard": shard.number},
                shard,
                expires_at=gateway.job.expires_at,
            )
        for bucket in lookup_buckets:
            self._put_fixed(
                research_id,
                ArtifactKind.RESULT_PAGE,
                f"run/snapshot-index/lookup/{bucket.number}",
                "snapshot_index_lookup",
                {"research_id": research_id, "bucket": bucket.number},
                bucket,
                expires_at=gateway.job.expires_at,
            )
        self._put_fixed(
            research_id,
            ArtifactKind.RESULT_PAGE,
            "run/snapshot-index/manifest",
            "snapshot_index_manifest",
            {"research_id": research_id},
            manifest,
            expires_at=gateway.job.expires_at,
        )
        for overview_shard in overview.shards:
            self._put_fixed(
                research_id,
                ArtifactKind.RESULT_PAGE,
                f"run/overview/shard/{overview_shard.number}",
                "overview_group_shard",
                {"research_id": research_id, "shard": overview_shard.number},
                overview_shard,
                expires_at=gateway.job.expires_at,
            )
        self._put_fixed(
            research_id,
            ArtifactKind.RESULT_PAGE,
            "run/overview/manifest",
            "overview_manifest",
            {"research_id": research_id},
            overview.manifest,
            expires_at=gateway.job.expires_at,
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
                    current_shard = self._get_snapshot_index_shard(
                        research_id, descriptor
                    )
                local_start = (
                    max(start, current_shard.start_position)
                    - current_shard.start_position
                )
                local_stop = (
                    min(stop, current_shard.end_position)
                    - current_shard.start_position
                )
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
        shards = tuple(
            self._get_overview_shard(research_id, descriptor)
            for descriptor in required
        )
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
            route.evidence_id
            for route in manifest.core
            if not route.text_inline_complete
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
            (
                number
                for number, entry in enumerate(shard.entries)
                if entry.id == after_evidence_id
            ),
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
            discovery = self.get_discovery(research_id)
            if discovery is None:
                raise LookupError("bill-status partitions are not available")
            partitions = discovery.status_partitions
        for partition in partitions:
            if partition.partition_id == partition_id:
                return partition
        raise ValueError("metadata partition is outside the research plan")

    def _terminal_outcome(
        self, research_id: str, work_id: str
    ) -> DocumentOutcome | None:
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
        with self._lock:
            for attempt in range(_WRITE_RETRIES):
                try:
                    self.artifacts.write(
                        research_id, kind, payload, logical_key=logical_key
                    )
                    return value
                except ArtifactConflictError:
                    try:
                        existing = self._get_fixed(
                            research_id, kind, logical_key, record_type
                        )
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
            raise ResearchRunStorageError(
                "research run artifact value is invalid"
            ) from exc
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
            descriptor.first_sort_key,
            descriptor.first_id,
        ) <= key <= (
            descriptor.last_sort_key,
            descriptor.last_id,
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
        init_fields = {
            field.name: field for field in fields(target) if field.init
        }
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
        arguments = {
            name: _decode(raw_fields[name]) for name in sorted(encoded_fields)
        }
        return target(**arguments)
    raise ValueError("encoded run value marker is invalid")


def _required_string(value: Mapping[str, Any], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str):
        raise ValueError("encoded run string field is invalid")
    return result


__all__ = [
    "ArtifactResearchRunStore",
    "ResearchRunConflictError",
    "ResearchRunExpiredError",
    "ResearchRunStorageError",
]
