"""Small stage-boundary snapshots for bounded hosted status polling."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from .artifact_run_storage import ArtifactResearchRunStore, ResearchRunStorageError
from .artifacts import ArtifactKind
from .collector import MetadataCollection, MetadataPartition
from .engine import (
    DerivedResearchStatus,
    DiscoveryStageState,
    GatewayPlanState,
    MetadataStageState,
)
from .results import ResearchSnapshotSummary

_STATUS_KEYS: Final = {
    "gateway": "run/status/gateway-v1",
    "discovery": "run/status/discovery-v1",
    "metadata": "run/status/metadata-v1",
}
_STATUS_RECORD: Final = "status_checkpoint"


@dataclass(frozen=True, slots=True)
class BoundedResearchStatusView:
    derived: DerivedResearchStatus
    summary: ResearchSnapshotSummary | None


class StatusSnapshotResearchRunStore(ArtifactResearchRunStore):
    """Add three immutable checkpoints without changing source artifact storage."""

    def put_gateway(self, research_id: str, state: GatewayPlanState) -> GatewayPlanState:
        stored = super().put_gateway(research_id, state)
        self._put_status("gateway", stored, _gateway_status(stored))
        return stored

    def put_discovery(self, research_id: str, state: DiscoveryStageState) -> DiscoveryStageState:
        stored = super().put_discovery(research_id, state)
        gateway = self._required_status_gateway(research_id)
        self._put_status("discovery", gateway, _discovery_status(gateway, stored))
        return stored

    def put_metadata(self, research_id: str, state: MetadataStageState) -> MetadataStageState:
        stored = super().put_metadata(research_id, state)
        gateway = self._required_status_gateway(research_id)
        self._put_status("metadata", gateway, _metadata_status(gateway, stored))
        return stored

    def get_status_view(self, research_id: str) -> BoundedResearchStatusView | None:
        """Return a constant-read view, or ``None`` for a pre-checkpoint run."""

        gateway = self.get_gateway(research_id)
        if gateway is None:
            return None
        summary_value = self._get_fixed(
            research_id,
            ArtifactKind.RESULT_PAGE,
            "run/snapshot-summary",
            "snapshot_summary",
        )
        summary = (
            self._expect(summary_value, ResearchSnapshotSummary, "snapshot summary")
            if summary_value is not None
            else None
        )
        if summary is not None and (
            summary.research_id != research_id
            or summary.query_fingerprint != gateway.job.query_fingerprint
            or summary.index_revision != gateway.job.index_revision
        ):
            raise ResearchRunStorageError("snapshot summary job binding is invalid")

        checkpoint = self._latest_status(research_id, gateway)
        if checkpoint is None:
            return None
        if summary is not None:
            checkpoint = replace(
                checkpoint,
                stage="complete" if summary.coverage.complete else "partial",
                documents_complete=checkpoint.documents_expected,
                documents_failed=min(
                    checkpoint.documents_expected,
                    _failed_document_count(summary),
                ),
                overview_available=True,
                snapshot_ready=True,
                complete=summary.coverage.complete,
            )
        return BoundedResearchStatusView(checkpoint, summary)

    def _put_status(
        self,
        boundary: str,
        gateway: GatewayPlanState,
        status: DerivedResearchStatus,
    ) -> None:
        value = {
            "query_fingerprint": gateway.job.query_fingerprint,
            "index_revision": gateway.job.index_revision,
            "status": status,
        }
        self._put_fixed(
            gateway.job.id,
            ArtifactKind.MANIFEST,
            _STATUS_KEYS[boundary],
            _STATUS_RECORD,
            {"boundary": boundary},
            value,
            expires_at=gateway.job.expires_at,
        )

    def _latest_status(
        self,
        research_id: str,
        gateway: GatewayPlanState,
    ) -> DerivedResearchStatus | None:
        for boundary in ("metadata", "discovery", "gateway"):
            value = self._get_fixed(
                research_id,
                ArtifactKind.MANIFEST,
                _STATUS_KEYS[boundary],
                _STATUS_RECORD,
            )
            if value is None:
                continue
            if not isinstance(value, Mapping) or set(value) != {
                "query_fingerprint",
                "index_revision",
                "status",
            }:
                raise ResearchRunStorageError("research status checkpoint is invalid")
            if (
                value.get("query_fingerprint") != gateway.job.query_fingerprint
                or value.get("index_revision") != gateway.job.index_revision
            ):
                raise ResearchRunStorageError("research status checkpoint binding is invalid")
            status = self._expect(value.get("status"), DerivedResearchStatus, "research status")
            if status.research_id != research_id or status.snapshot_ready or status.complete:
                raise ResearchRunStorageError("research status checkpoint over-reports readiness")
            return status
        return None

    def _required_status_gateway(self, research_id: str) -> GatewayPlanState:
        gateway = self.get_gateway(research_id)
        if gateway is None:
            raise ResearchRunStorageError("research gateway vanished at a stage boundary")
        return gateway


def _gateway_status(gateway: GatewayPlanState) -> DerivedResearchStatus:
    partitions = len(gateway.discovery_partitions)
    return _status(
        gateway.job.id,
        "metadata_discovery",
        partitions=partitions,
        pages=partitions,
    )


def _discovery_status(
    gateway: GatewayPlanState,
    discovery: DiscoveryStageState,
) -> DerivedResearchStatus:
    partitions, partitions_done, pages, pages_done = _collection_progress(
        gateway.discovery_partitions,
        discovery.collection,
    )
    deferred = len(discovery.status_partitions)
    return _status(
        gateway.job.id,
        "deferred_metadata",
        partitions=partitions + deferred,
        partitions_done=partitions_done,
        pages=pages + deferred,
        pages_done=pages_done,
        bill_checks=len(discovery.document_bill_numbers),
        overview=True,
    )


def _metadata_status(
    gateway: GatewayPlanState,
    metadata: MetadataStageState,
) -> DerivedResearchStatus:
    discovery = metadata.discovery
    first = _collection_progress(gateway.discovery_partitions, discovery.collection)
    second = _collection_progress(discovery.status_partitions, metadata.status_collection)
    checks = len(discovery.document_bill_numbers)
    return _status(
        gateway.job.id,
        "documents",
        partitions=first[0] + second[0],
        partitions_done=first[1] + second[1],
        pages=first[2] + second[2],
        pages_done=first[3] + second[3],
        bill_checks=checks,
        bill_checks_done=min(checks, len(metadata.manifest.bill_discoveries)),
        documents=len(metadata.manifest.items),
        overview=True,
    )


def _status(
    research_id: str,
    stage: str,
    *,
    partitions: int,
    pages: int,
    partitions_done: int = 0,
    pages_done: int = 0,
    bill_checks: int = 0,
    bill_checks_done: int = 0,
    documents: int = 0,
    overview: bool = False,
) -> DerivedResearchStatus:
    return DerivedResearchStatus(
        research_id,
        stage,
        partitions,
        min(partitions, partitions_done),
        pages,
        min(pages, pages_done),
        bill_checks,
        min(bill_checks, bill_checks_done),
        documents,
        0,
        0,
        overview,
        False,
        False,
    )


def _collection_progress(
    planned: Sequence[MetadataPartition],
    collection: MetadataCollection,
) -> tuple[int, int, int, int]:
    provenance = {item.partition_id: item for item in collection.partitions}
    partitions_done = pages = pages_done = 0
    for partition in planned:
        item = provenance.get(partition.partition_id)
        if item is None:
            pages += 1
            continue
        first = next((page for page in item.pages if page.page == 1), None)
        expected = (
            max(1, math.ceil(first.total_count / partition.page_size))
            if first is not None and first.total_count is not None
            else max(1, len(item.pages))
        )
        observed = len({page.page for page in item.pages if page.page >= 1})
        pages += expected
        pages_done += min(expected, observed)
        partitions_done += int(item.complete and observed == expected)
    return len(planned), partitions_done, pages, pages_done


def _failed_document_count(summary: ResearchSnapshotSummary) -> int:
    work_ids: set[str] = set()
    for entry in summary.coverage.entries:
        for reason in entry.gap_reasons:
            if reason.startswith("document_failed:"):
                work_id, separator, _code = reason.removeprefix("document_failed:").rpartition(":")
                if separator and work_id:
                    work_ids.add(work_id)
    return len(work_ids)


__all__ = ["BoundedResearchStatusView", "StatusSnapshotResearchRunStore"]
