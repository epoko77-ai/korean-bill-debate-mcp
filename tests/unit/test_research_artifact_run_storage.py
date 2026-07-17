from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Lock
from typing import Any

import pytest

import kasm.research.artifact_run_storage as run_storage_codec
from kasm.adapters.korea.bills import BILL_STATUS_DATASET
from kasm.adapters.korea.client import ApiPage, ApiResult
from kasm.research.artifact_run_storage import (
    ArtifactResearchRunStore,
    ResearchRunConflictError,
    ResearchRunExpiredError,
    ResearchRunStorageError,
)
from kasm.research.artifacts import (
    ArtifactBackendError,
    ArtifactKind,
    ArtifactRef,
    FilesystemResearchArtifactStore,
    StoredArtifact,
)
from kasm.research.collector import (
    CollectionCoverage,
    MetadataCollection,
    MetadataCollector,
    MetadataKind,
    MetadataPartition,
    PageProvenance,
    PartitionProvenance,
)
from kasm.research.contracts import CoverageLedger, EvidenceCoverage, EvidenceType
from kasm.research.document_worker import DocumentWorkResult
from kasm.research.documents import (
    OfficialDocumentKind,
    ParsedOfficialDocument,
    TextSegment,
)
from kasm.research.engine import (
    BillDocumentDiscovery,
    DiscoveryStageState,
    DocumentOutcome,
    DocumentOutcomeStatus,
    DocumentWorkItem,
    DocumentWorkManifest,
    FamilyFilterAccounting,
    GatewayPlanState,
    InMemoryResearchRunStore,
    MetadataPhase,
    MetadataStageState,
    StrictFilterReport,
)
from kasm.research.jobs import InMemoryResearchJobStore
from kasm.research.overview import (
    ProvisionalResearchOverview,
    ProvisionalSourceAccounting,
    build_provisional_research_overview,
)
from kasm.research.partitioning import ResearchPartitionPlanner
from kasm.research.planner import plan_research
from kasm.research.queue import ResearchTask, ResearchTaskStage
from kasm.research.resolver import (
    CandidateDecision,
    CandidateSetResolution,
    resolve_metadata_candidates,
)
from kasm.research.results import (
    EvidenceCitation,
    EvidenceRecord,
    ResearchSnapshot,
    ResearchSnapshotSummary,
)
from kasm.research.source_availability import (
    OfficialSourceAvailability,
    SourceAvailabilityState,
)
from kasm.research.status_storage import StatusSnapshotResearchRunStore

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
REVIEW_URL = "https://likms.assembly.go.kr/filegate/review.pdf?id=2219564"


@dataclass
class FixtureState:
    now: list[datetime]
    gateway: GatewayPlanState
    discovery: DiscoveryStageState
    bill_discovery: BillDocumentDiscovery
    metadata: MetadataStageState
    work_item: DocumentWorkItem


class CountingFilesystemStore(FilesystemResearchArtifactStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.read_calls = 0
        self.logical_read_calls = 0
        self.logical_read_bytes = 0
        self.list_calls = 0
        self.logical_reads: list[str] = []

    def read(self, ref: ArtifactRef) -> StoredArtifact | None:
        self.read_calls += 1
        return super().read(ref)

    def read_logical(
        self,
        research_id: str,
        kind: ArtifactKind,
        logical_key: str,
    ) -> StoredArtifact | None:
        self.logical_read_calls += 1
        self.logical_reads.append(logical_key)
        stored = super().read_logical(research_id, kind, logical_key)
        if stored is not None:
            self.logical_read_bytes += stored.ref.byte_size
        return stored

    def list(self, research_id: str, kind: ArtifactKind | None = None) -> tuple[ArtifactRef, ...]:
        self.list_calls += 1
        return super().list(research_id, kind)

    def reset_counts(self) -> None:
        self.read_calls = 0
        self.logical_read_calls = 0
        self.logical_read_bytes = 0
        self.list_calls = 0
        self.logical_reads.clear()


class RecordingFilesystemStore(CountingFilesystemStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.successful_logical_writes: list[str] = []

    def write(
        self,
        research_id: str,
        kind: ArtifactKind,
        payload: Any,
        *,
        logical_key: str | None = None,
    ) -> ArtifactRef:
        ref = super().write(
            research_id,
            kind,
            payload,
            logical_key=logical_key,
        )
        if logical_key is not None:
            self.successful_logical_writes.append(logical_key)
        return ref


class ConcurrentRecordingStore(RecordingFilesystemStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self._activity_lock = Lock()
        self.active_result_writes = 0
        self.max_active_result_writes = 0
        self.active_routing_writes = 0
        self.max_active_routing_writes = 0

    def write(
        self,
        research_id: str,
        kind: ArtifactKind,
        payload: Any,
        *,
        logical_key: str | None = None,
    ) -> ArtifactRef:
        tracked = bool(
            kind is ArtifactKind.RESULT_PAGE
            and logical_key is not None
            and ("/shard/" in logical_key or "/lookup/" in logical_key)
        )
        routing = bool(
            kind is ArtifactKind.MANIFEST
            and logical_key is not None
            and logical_key.startswith(
                (
                    "run/accepted-bill/",
                    "run/status-partition/",
                    "run/deferred-route-shard/",
                    "run/document-work-item/",
                    "run/document-route-shard/",
                )
            )
        )
        if tracked:
            with self._activity_lock:
                self.active_result_writes += 1
                self.max_active_result_writes = max(
                    self.max_active_result_writes,
                    self.active_result_writes,
                )
            time.sleep(0.01)
        if routing:
            with self._activity_lock:
                self.active_routing_writes += 1
                self.max_active_routing_writes = max(
                    self.max_active_routing_writes,
                    self.active_routing_writes,
                )
            time.sleep(0.005)
        try:
            return super().write(
                research_id,
                kind,
                payload,
                logical_key=logical_key,
            )
        finally:
            if tracked:
                with self._activity_lock:
                    self.active_result_writes -= 1
            if routing:
                with self._activity_lock:
                    self.active_routing_writes -= 1


class FailOneResultWriteStore(RecordingFilesystemStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.failed = False

    def write(
        self,
        research_id: str,
        kind: ArtifactKind,
        payload: Any,
        *,
        logical_key: str | None = None,
    ) -> ArtifactRef:
        if (
            not self.failed
            and kind is ArtifactKind.RESULT_PAGE
            and logical_key == "run/snapshot-index/shard/1"
        ):
            self.failed = True
            raise ArtifactBackendError("injected bounded shard failure")
        return super().write(
            research_id,
            kind,
            payload,
            logical_key=logical_key,
        )


class FailLogicalWriteOnceStore(RecordingFilesystemStore):
    def __init__(self, root: Path, target: str) -> None:
        super().__init__(root)
        self.target = target
        self.failed = False

    def write(
        self,
        research_id: str,
        kind: ArtifactKind,
        payload: Any,
        *,
        logical_key: str | None = None,
    ) -> ArtifactRef:
        if not self.failed and logical_key == self.target:
            self.failed = True
            raise ArtifactBackendError("injected boundary failure")
        return super().write(
            research_id,
            kind,
            payload,
            logical_key=logical_key,
        )


class FailStatusCheckpointOnceStore(StatusSnapshotResearchRunStore):
    def __init__(
        self,
        artifacts: FilesystemResearchArtifactStore,
        *,
        now: Any,
        boundary: str,
    ) -> None:
        super().__init__(artifacts, now=now)
        self.boundary = boundary
        self.failed = False

    def _put_status(self, boundary: str, gateway: Any, status: Any) -> None:
        if not self.failed and boundary == self.boundary:
            self.failed = True
            raise ArtifactBackendError("injected status checkpoint failure")
        super()._put_status(boundary, gateway, status)


def _coverage(*, bills: int = 0, meetings: int = 0) -> CollectionCoverage:
    return CollectionCoverage(
        partitions_expected=0,
        partitions_complete=0,
        source_rows_expected=bills + meetings,
        source_rows_fetched=bills + meetings,
        bill_source_rows=bills,
        bill_unique_records=bills,
        bill_duplicate_rows=0,
        bill_rejected_rows=0,
        meeting_source_rows=meetings,
        meeting_unique_pdfs=meetings,
        meeting_rows_merged=0,
        meeting_rejected_rows=0,
    )


def _fixture() -> FixtureState:
    now = [NOW]
    plan = plan_research(
        "2026-01-01부터 2026-07-13까지 2219564 보완수사권",
        as_of=NOW,
        evidence_types=(EvidenceType.BILLS, EvidenceType.REVIEW_REPORTS),
    )
    partition_plan = ResearchPartitionPlanner().plan(plan)
    jobs = InMemoryResearchJobStore(now=lambda: now[0])
    job = jobs.create(plan.contract, "index-v1", ttl=timedelta(hours=2))
    gateway = GatewayPlanState(
        job,
        plan,
        partition_plan,
        partition_plan.metadata_partitions,
    )
    bill = {
        "BILL_NO": "2219564",
        "BILL_NAME": "형사소송법 일부개정법률안",
        "PROPOSE_DT": "2026-06-01",
        "summary": "보완수사권을 정비한다.",
    }
    collection = MetadataCollection((bill,), (), (), _coverage(bills=1))
    resolution = resolve_metadata_candidates(plan, collection)
    status_partition = MetadataPartition.create(
        "bill-status:2219564",
        MetadataKind.BILL,
        BILL_STATUS_DATASET,
        parameters={"BILL_NO": "2219564", "AGE": 22},
    )
    discovery = DiscoveryStageState(
        collection,
        collection,
        StrictFilterReport(
            FamilyFilterAccounting(1, 1),
            FamilyFilterAccounting(0, 0),
        ),
        resolution,
        (status_partition,),
        ("2219564",),
    )
    work_item = DocumentWorkItem.create(
        OfficialDocumentKind.REVIEW_REPORT,
        REVIEW_URL,
        evidence_types=(EvidenceType.REVIEW_REPORTS,),
        related_bill_numbers=("2219564",),
    )
    bill_discovery = BillDocumentDiscovery("2219564", (work_item,))
    manifest = DocumentWorkManifest.create((work_item,), (bill_discovery,))
    metadata = MetadataStageState(
        discovery,
        MetadataCollection((), (), (), _coverage()),
        manifest,
        (),
    )
    return FixtureState(now, gateway, discovery, bill_discovery, metadata, work_item)


def _scaled_boundary(
    state: FixtureState,
    count: int,
) -> tuple[DiscoveryStageState, MetadataStageState, tuple[BillDocumentDiscovery, ...]]:
    if count < 1:
        raise ValueError("scaled boundary requires at least one bill")
    original = state.discovery.resolution.bills.accepted[0]
    numbers = ("2219564",) + tuple(f"{2200000 + offset:07d}" for offset in range(count - 1))
    decisions = (original,) + tuple(
        CandidateDecision(
            MetadataKind.BILL,
            f"bill:{number}",
            True,
            10,
            ("issue_exact",),
            (),
            {
                "BILL_NO": number,
                "BILL_NAME": "인공지능 산업 진흥법 일부개정법률안",
                "PROPOSE_DT": "2026-07-01",
                "summary": "인공지능 정책과 안전 기준을 정비한다.",
            },
        )
        for number in numbers[1:]
    )
    bills = CandidateSetResolution(MetadataKind.BILL, decisions, decisions)
    prototype = state.discovery.status_partitions[0]
    status_partitions = tuple(
        MetadataPartition.create(
            f"bill-status:{number}",
            MetadataKind.BILL,
            prototype.dataset,
            parameters={"BILL_NO": number, "AGE": 22},
        )
        for number in numbers
    )
    discovery = replace(
        state.discovery,
        resolution=replace(state.discovery.resolution, bills=bills),
        status_partitions=status_partitions,
        document_bill_numbers=numbers,
    )
    items = (state.work_item,) + tuple(
        DocumentWorkItem.create(
            OfficialDocumentKind.REVIEW_REPORT,
            f"https://likms.assembly.go.kr/filegate/review-{number}.pdf",
            evidence_types=(EvidenceType.REVIEW_REPORTS,),
            related_bill_numbers=(number,),
        )
        for number in numbers[1:]
    )
    discoveries = tuple(
        BillDocumentDiscovery(number, (item,)) for number, item in zip(numbers, items, strict=True)
    )
    manifest = DocumentWorkManifest.create(items, discoveries)
    metadata = replace(state.metadata, discovery=discovery, manifest=manifest)
    return discovery, metadata, discoveries


def _page(partition: MetadataPartition, number: int = 1) -> ApiPage:
    rows: tuple[dict[str, Any], ...] = (
        {
            "BILL_NO": "2219564",
            "BILL_NAME": "형사소송법 일부개정법률안",
        },
    )
    return ApiPage(
        partition.dataset,
        number,
        partition.page_size,
        1,
        rows,
        (
            f"https://open.assembly.go.kr/portal/openapi/{partition.dataset}"
            f"?KEY=%2A%2A%2A&pIndex={number}&pSize={partition.page_size}"
        ),
        hashlib.sha256(repr(rows).encode()).hexdigest(),
    )


def _first_page_preview_fixture(
    state: FixtureState,
) -> tuple[
    GatewayPlanState,
    tuple[tuple[MetadataPartition, ApiPage], ...],
    ProvisionalResearchOverview,
]:
    original = replace(state.gateway.discovery_partitions[0], page_size=1)
    second = MetadataPartition.create(
        "bill-metadata:preview-second",
        MetadataKind.BILL,
        original.dataset,
        parameters={"AGE": 21},
        page_size=1,
    )
    gateway = replace(state.gateway, discovery_partitions=(original, second))
    rows = (
        (
            {
                "BILL_NO": "2219564",
                "BILL_NAME": "형사소송법 일부개정법률안",
            },
        ),
        (
            {
                "BILL_NO": "2199999",
                "BILL_NAME": "다른 법률 일부개정법률안",
            },
        ),
    )
    pages = tuple(
        ApiPage(
            partition.dataset,
            1,
            partition.page_size,
            2,
            page_rows,
            (
                f"https://open.assembly.go.kr/portal/openapi/{partition.dataset}"
                f"?KEY=%2A%2A%2A&pIndex=1&pSize={partition.page_size}"
            ),
            hashlib.sha256(repr((partition.partition_id, page_rows)).encode()).hexdigest(),
        )
        for partition, page_rows in zip(gateway.discovery_partitions, rows, strict=True)
    )
    result_by_parameters = {
        partition.parameters: ApiResult(
            page.dataset,
            page.page_size,
            page.total_count or 0,
            page.rows,
            (page,),
        )
        for partition, page in zip(gateway.discovery_partitions, pages, strict=True)
    }

    class FirstPageClient:
        def fetch_all(
            self,
            _dataset: str,
            *,
            page_size: int,
            parameters: Mapping[str, str | int] | None = None,
            refresh: bool = False,
        ) -> ApiResult:
            del page_size, refresh
            return result_by_parameters[tuple(sorted((parameters or {}).items()))]

    collection = MetadataCollector(FirstPageClient()).collect(gateway.discovery_partitions)
    resolution = resolve_metadata_candidates(gateway.research_plan, collection)
    preview = build_provisional_research_overview(
        replace(
            state.discovery,
            collection=collection,
            filtered_collection=collection,
            filter_report=StrictFilterReport(
                FamilyFilterAccounting(2, 2),
                FamilyFilterAccounting(0, 0),
            ),
            resolution=resolution,
            status_partitions=(),
            document_bill_numbers=(),
        )
    )
    return gateway, tuple(zip(gateway.discovery_partitions, pages, strict=True)), preview


def _result(state: FixtureState, text: str) -> DocumentWorkResult:
    document = ParsedOfficialDocument(
        OfficialDocumentKind.REVIEW_REPORT,
        REVIEW_URL,
        "a" * 64,
        "parser-v1",
        NOW,
        (TextSegment("p.1", text),),
    )
    return DocumentWorkResult(
        document.kind,
        document.official_url,
        document.parser_version,
        len(text.encode()),
        1,
        len(text),
        document.source_hash,
        document.text_hash,
        False,
        "official/raw/" + document.source_hash,
        "official/parsed/" + document.source_hash + "/parser-v1.json",
        document,
    )


def _snapshot(state: FixtureState, text: str) -> ResearchSnapshot:
    coverage = CoverageLedger(
        state.gateway.job.contract.evidence_types,
        tuple(
            EvidenceCoverage(item, 1, 1, 1) for item in state.gateway.job.contract.evidence_types
        ),
    )
    record = EvidenceRecord(
        "review:2219564:p.1",
        EvidenceType.REVIEW_REPORTS,
        "2026-06-01:2219564:review:p.1",
        "전문위원 검토보고서",
        text,
        EvidenceCitation(REVIEW_URL, "p.1:1-120500", "a" * 64, NOW),
        (("bill_number", "2219564"),),
    )
    return ResearchSnapshot(
        state.gateway.job.id,
        state.gateway.job.contract,
        state.gateway.job.index_revision,
        "build-test",
        coverage,
        (record,),
    )


def _large_overview_snapshot(state: FixtureState) -> ResearchSnapshot:
    base = _snapshot(state, "기본 검토보고서")
    long_marker = "OVERVIEW_MUST_NOT_INLINE_THIS_TEXT::"
    records = tuple(
        EvidenceRecord(
            id=f"document-record-{number:04d}",
            evidence_type=(EvidenceType.BILL_TEXT if number == 0 else EvidenceType.REVIEW_REPORTS),
            sort_key=f"2026-06-01|{number:06d}",
            title=f"공식 문서 {number:04d}",
            text=(
                long_marker + ("가" * 120_000)
                if number in {0, 1}
                else f"완전한 짧은 공식 문서 {number}"
            ),
            citation=base.evidence[0].citation,
            metadata=(("work_id", f"document-{number:04d}"),),
        )
        for number in range(1_005)
    )
    return ResearchSnapshot(
        base.research_id,
        base.contract,
        base.index_revision,
        base.build_sha,
        base.coverage,
        records,
    )


def _store(root: Path, state: FixtureState) -> ArtifactResearchRunStore:
    return ArtifactResearchRunStore(FilesystemResearchArtifactStore(root), now=lambda: state.now[0])


def test_legacy_discovery_artifact_uses_new_optional_field_default() -> None:
    state = _fixture()
    encoded = run_storage_codec._encode(state.discovery)
    assert isinstance(encoded, dict)
    raw_fields = encoded["fields"]
    assert isinstance(raw_fields, dict)
    raw_fields.pop("corpus_recall")

    restored = run_storage_codec._decode(encoded)

    assert restored == state.discovery
    assert restored.corpus_recall is None


def test_source_availability_round_trip_and_legacy_default() -> None:
    availability = OfficialSourceAvailability(
        "bill_status",
        BILL_STATUS_DATASET,
        MetadataKind.BILL,
        1,
        1,
        1,
        0,
        0,
        SourceAvailabilityState.NO_RECORDS,
    )
    accounting = ProvisionalSourceAccounting(
        True,
        0,
        0,
        0,
        0,
        0,
        0,
        (availability,),
    )
    encoded = run_storage_codec._encode(accounting)

    assert run_storage_codec._decode(encoded) == accounting
    assert isinstance(encoded, dict)
    raw_fields = encoded["fields"]
    assert isinstance(raw_fields, dict)
    raw_fields.pop("source_availability")

    restored = run_storage_codec._decode(encoded)

    assert restored == replace(accounting, source_availability=())


def test_legacy_decoder_still_rejects_missing_required_or_unknown_fields() -> None:
    state = _fixture()
    missing_required = run_storage_codec._encode(state.discovery)
    assert isinstance(missing_required, dict)
    missing_fields = missing_required["fields"]
    assert isinstance(missing_fields, dict)
    missing_fields.pop("resolution")
    with pytest.raises(ValueError, match="field set"):
        run_storage_codec._decode(missing_required)

    unknown_field = run_storage_codec._encode(state.discovery)
    assert isinstance(unknown_field, dict)
    unknown_fields = unknown_field["fields"]
    assert isinstance(unknown_fields, dict)
    unknown_fields["unexpected"] = None
    with pytest.raises(ValueError, match="field set"):
        run_storage_codec._decode(unknown_field)


def _seed_through_metadata(store: ArtifactResearchRunStore, state: FixtureState) -> None:
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)
    store.put_discovery(research_id, state.discovery)
    store.put_bill_discovery(research_id, state.bill_discovery)
    store.put_metadata(research_id, state.metadata)


def _compact_outcome(outcome: DocumentOutcome) -> DocumentOutcome:
    assert outcome.result is not None
    return replace(
        outcome,
        result=replace(outcome.result, cache_hit=False, document=None),
    )


def test_every_run_stage_survives_restart_and_preserves_120k_text(tmp_path: Path) -> None:
    state = _fixture()
    first = _store(tmp_path, state)
    research_id = state.gateway.job.id
    partition = state.gateway.discovery_partitions[0]
    page = _page(partition)
    text = ("전문위원은 보완수사 절차와 통제 장치를 검토했습니다. " * 5000) + "끝"
    assert len(text) > 120_000

    first.put_gateway(research_id, state.gateway)
    first.put_page(research_id, MetadataPhase.DISCOVERY, partition.partition_id, page)
    stored_discovery = first.put_discovery(research_id, state.discovery)
    first.put_bill_discovery(research_id, state.bill_discovery)
    stored_metadata = first.put_metadata(research_id, state.metadata)
    outcome = DocumentOutcome(
        state.work_item.work_id,
        DocumentOutcomeStatus.SUCCEEDED,
        result=_result(state, text),
    )
    first.put_document_outcome(research_id, outcome)
    snapshot = _snapshot(state, text)
    first.put_snapshot(research_id, snapshot)

    restarted = _store(tmp_path, state)
    assert restarted.get_gateway(research_id) == state.gateway
    assert restarted.pages(research_id, MetadataPhase.DISCOVERY, partition.partition_id) == (page,)
    assert restarted.get_discovery(research_id) == stored_discovery
    assert stored_discovery.collection.bills == ()
    assert stored_discovery.collection.coverage == state.discovery.collection.coverage
    assert restarted.bill_discoveries(research_id) == (state.bill_discovery,)
    assert restarted.get_metadata(research_id) == stored_metadata
    assert stored_metadata.discovery == stored_discovery
    restored_outcome = restarted.document_outcomes(research_id)[0]
    assert restored_outcome == _compact_outcome(outcome)
    assert restored_outcome.result is not None
    assert restored_outcome.result.document is None
    terminal_refs = tuple(
        ref
        for ref in FilesystemResearchArtifactStore(tmp_path).list(
            research_id, ArtifactKind.OUTCOME
        )
        if ref.logical_key == f"run/document-terminal/{state.work_item.work_id}"
    )
    assert len(terminal_refs) == 1
    assert terminal_refs[0].byte_size < 10_000
    restored_evidence = restarted.get_overflow_evidence_record(
        research_id,
        snapshot.evidence[0].id,
    )
    assert restored_evidence == snapshot.evidence[0]
    assert restored_evidence is not None and restored_evidence.text == text
    assert restarted.get_snapshot_summary(research_id) == (
        ResearchSnapshotSummary.from_snapshot(snapshot)
    )


def test_single_bill_discovery_read_never_lists_sibling_artifacts(tmp_path: Path) -> None:
    state = _fixture()
    artifacts = CountingFilesystemStore(tmp_path)
    store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)
    store.put_discovery(research_id, state.discovery)
    store.put_bill_discovery(research_id, state.bill_discovery)
    artifacts.reset_counts()

    restored = store.get_bill_discovery(research_id, state.bill_discovery.bill_number)

    assert restored == state.bill_discovery
    assert artifacts.list_calls == 0
    assert artifacts.read_calls == 0
    assert artifacts.logical_read_calls == 1


def test_discovery_readiness_is_written_after_all_compact_views(tmp_path: Path) -> None:
    state = _fixture()
    artifacts = RecordingFilesystemStore(tmp_path)
    store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)

    store.put_discovery(research_id, state.discovery)

    writes = artifacts.successful_logical_writes
    ready = writes.index("run/discovery-ready")
    assert writes.index("run/discovery-v2") < ready
    assert writes.index("run/deferred-work-manifest") < ready
    assert writes.index("run/provisional-overview") < ready


def test_page_readiness_is_last_small_secret_free_write_and_retry_heals_gap(
    tmp_path: Path,
) -> None:
    state = _fixture()
    research_id = state.gateway.job.id
    partition = state.gateway.discovery_partitions[0]
    raw_key = f"run/page/discovery/{partition.partition_id}/1"
    ready_key = f"run/page-ready/discovery/{partition.partition_id}/1"
    artifacts = FailLogicalWriteOnceStore(tmp_path, ready_key)
    store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    store.put_gateway(research_id, state.gateway)
    source = _page(partition)

    with pytest.raises(ArtifactBackendError, match="boundary"):
        store.put_page(
            research_id,
            MetadataPhase.DISCOVERY,
            partition.partition_id,
            source,
        )

    assert (
        store.get_page(
            research_id,
            MetadataPhase.DISCOVERY,
            partition.partition_id,
            1,
        )
        == source
    )
    assert (
        store.page_readiness_for(
            research_id,
            MetadataPhase.DISCOVERY,
            partition.partition_id,
            (1,),
        )
        == ()
    )

    # A redelivery sees the immutable raw page, repeats the idempotent PUT, and
    # heals only the missing final marker; the official API need not be called.
    store.put_page(
        research_id,
        MetadataPhase.DISCOVERY,
        partition.partition_id,
        source,
    )
    assert artifacts.successful_logical_writes[-2:] == [raw_key, ready_key]
    readiness = store.page_readiness_for(
        research_id,
        MetadataPhase.DISCOVERY,
        partition.partition_id,
        (1,),
    )
    assert len(readiness) == 1
    assert readiness[0].source_hash == source.source_hash

    marker = artifacts.read_logical(research_id, ArtifactKind.MANIFEST, ready_key)
    assert marker is not None and marker.ref.byte_size < 4_096
    encoded = json.dumps(marker.payload, ensure_ascii=False, sort_keys=True)
    assert "rows" not in encoded
    assert "source_url" not in encoded
    assert "KEY=" not in encoded
    assert "credential" not in encoded.casefold()
    assert "형사소송법 일부개정법률안" not in encoded


def test_partial_discovery_boundary_is_hidden_until_last_readiness_write(
    tmp_path: Path,
) -> None:
    state = _fixture()
    artifacts = FailLogicalWriteOnceStore(tmp_path, "run/discovery-ready")
    store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)

    with pytest.raises(ArtifactBackendError, match="boundary"):
        store.put_discovery(research_id, state.discovery)

    assert "run/discovery-v2" in artifacts.successful_logical_writes
    assert "run/deferred-work-manifest" in artifacts.successful_logical_writes
    assert "run/provisional-overview" in artifacts.successful_logical_writes
    artifacts.reset_counts()
    assert store.get_discovery(research_id) is None
    assert store.get_deferred_manifest(research_id) is None
    assert store.get_provisional_overview(research_id) is None
    assert "run/discovery-v2" not in artifacts.logical_reads
    assert "run/deferred-work-manifest" not in artifacts.logical_reads
    assert "run/provisional-overview" not in artifacts.logical_reads

    store.put_discovery(research_id, state.discovery)

    assert store.get_deferred_manifest(research_id) is not None
    assert store.get_provisional_overview(research_id) is not None


def test_partial_metadata_boundary_is_hidden_until_document_readiness_write(
    tmp_path: Path,
) -> None:
    state = _fixture()
    artifacts = FailLogicalWriteOnceStore(
        tmp_path,
        "run/document-work-manifest",
    )
    store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)
    store.put_discovery(research_id, state.discovery)
    store.put_bill_discovery(research_id, state.bill_discovery)

    with pytest.raises(ArtifactBackendError, match="boundary"):
        store.put_metadata(research_id, state.metadata)

    assert "run/metadata-v2" in artifacts.successful_logical_writes
    assert "run/document-ready" not in artifacts.successful_logical_writes
    assert store.get_metadata(research_id) is None
    assert store.get_document_manifest(research_id) is None
    assert store.get_document_item(research_id, state.work_item.work_id) is None

    store.put_metadata(research_id, state.metadata)

    assert artifacts.successful_logical_writes[-1] == "run/document-ready"
    assert store.get_metadata(research_id) is not None
    assert store.get_document_manifest(research_id) == state.metadata.manifest


@pytest.mark.parametrize(
    "failed_key_factory",
    (
        lambda state: "run/accepted-bill/2219564",
        lambda state: (
            "run/status-partition/"
            + hashlib.sha256(state.discovery.status_partitions[0].partition_id.encode()).hexdigest()
        ),
        lambda state: "run/deferred-route-shard/0",
    ),
)
def test_partial_discovery_hot_routing_write_is_hidden_and_retry_heals(
    tmp_path: Path,
    failed_key_factory: Any,
) -> None:
    state = _fixture()
    failed_key = str(failed_key_factory(state))
    artifacts = FailLogicalWriteOnceStore(tmp_path, failed_key)
    store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)

    with pytest.raises(ArtifactBackendError, match="boundary"):
        store.put_discovery(research_id, state.discovery)

    assert "run/discovery-ready" not in artifacts.successful_logical_writes
    assert store.get_accepted_bill(research_id, "2219564") is None
    assert (
        store.get_status_partition(
            research_id,
            state.discovery.status_partitions[0].partition_id,
        )
        is None
    )
    store.put_discovery(research_id, state.discovery)
    assert artifacts.successful_logical_writes[-1] == "run/discovery-ready"
    assert store.get_accepted_bill(research_id, "2219564") is not None


@pytest.mark.parametrize(
    "failed_key",
    (
        "run/document-work-item/document_e819205bf0e1d7c47b9b935f8b635d6a7e9fb399341ab339e717f4616f490104",
        "run/document-route-shard/0",
        "run/document-ready",
    ),
)
def test_partial_document_hot_routing_write_is_hidden_and_retry_heals(
    tmp_path: Path,
    failed_key: str,
) -> None:
    state = _fixture()
    artifacts = FailLogicalWriteOnceStore(tmp_path, failed_key)
    store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)
    store.put_discovery(research_id, state.discovery)
    store.put_bill_discovery(research_id, state.bill_discovery)

    with pytest.raises(ArtifactBackendError, match="boundary"):
        store.put_metadata(research_id, state.metadata)

    assert "run/document-ready" not in artifacts.successful_logical_writes
    assert store.get_metadata(research_id) is None
    assert store.get_document_item(research_id, state.work_item.work_id) is None
    store.put_metadata(research_id, state.metadata)
    assert artifacts.successful_logical_writes[-1] == "run/document-ready"
    assert store.get_document_item(research_id, state.work_item.work_id) == state.work_item


def test_fixed_hot_reads_are_size_invariant_and_routes_are_exhaustive(
    tmp_path: Path,
) -> None:
    def publish_and_measure(
        root: Path,
        count: int,
        *,
        verify_routes: bool,
    ) -> dict[str, int]:
        state = _fixture()
        discovery, metadata, discoveries = _scaled_boundary(state, count)
        artifacts = CountingFilesystemStore(root)
        store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
        research_id = state.gateway.job.id
        store.put_gateway(research_id, state.gateway)
        store.put_discovery(research_id, discovery)
        for bill_discovery in discoveries:
            store.put_bill_discovery(research_id, bill_discovery)
        store.put_metadata(research_id, metadata)

        measurements: dict[str, int] = {}
        checks = (
            (
                "accepted",
                lambda: store.get_accepted_bill(research_id, "2219564"),
                "run/accepted-bill/2219564",
            ),
            (
                "status",
                lambda: store.get_status_partition(
                    research_id,
                    discovery.status_partitions[0].partition_id,
                ),
                (
                    "run/status-partition/"
                    + hashlib.sha256(
                        discovery.status_partitions[0].partition_id.encode()
                    ).hexdigest()
                ),
            ),
            (
                "document",
                lambda: store.get_document_item(research_id, state.work_item.work_id),
                f"run/document-work-item/{state.work_item.work_id}",
            ),
        )
        for label, getter, expected_key in checks:
            artifacts.reset_counts()
            assert getter() is not None
            expected_ready = "run/document-ready" if label == "document" else "run/discovery-ready"
            assert artifacts.logical_reads == [
                "run/gateway",
                expected_ready,
                expected_key,
            ]
            assert not {
                "run/deferred-work-manifest",
                "run/document-work-manifest",
                "run/discovery-v2",
                "run/metadata-v2",
            } & set(artifacts.logical_reads)
            measurements[label] = artifacts.logical_read_bytes

        if verify_routes:
            artifacts.reset_counts()
            deferred_total = len(discovery.status_partitions) + len(discovery.document_bill_numbers)
            deferred_routes = tuple(
                route
                for start in range(0, deferred_total, 4)
                for route in store.deferred_routes_for(
                    research_id,
                    start,
                    min(start + 4, deferred_total),
                    expected_total=deferred_total,
                )
            )
            assert tuple(route.position for route in deferred_routes) == tuple(
                range(deferred_total)
            )
            assert len({(route.kind, route.position) for route in deferred_routes}) == (
                deferred_total
            )
            document_routes = tuple(
                item
                for start in range(0, count, 4)
                for item in store.document_routes_for(
                    research_id,
                    start,
                    min(start + 4, count),
                    expected_total=count,
                )
            )
            assert len(document_routes) == count
            assert len({item.work_id for item in document_routes}) == count
            assert "run/deferred-work-manifest" not in artifacts.logical_reads
            assert "run/document-work-manifest" not in artifacts.logical_reads
        return measurements

    small = publish_and_measure(tmp_path / "small", 1, verify_routes=False)
    large = publish_and_measure(tmp_path / "large", 1_000, verify_routes=True)

    for label in ("accepted", "status", "document"):
        # Only decimal count fields in the tiny readiness marker can differ.
        assert abs(large[label] - small[label]) <= 128


def test_hot_routing_writes_are_parallel_capped_and_markers_are_last(
    tmp_path: Path,
) -> None:
    state = _fixture()
    discovery, metadata, discoveries = _scaled_boundary(state, 40)
    artifacts = ConcurrentRecordingStore(tmp_path)
    store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)

    store.put_discovery(research_id, discovery)
    assert 2 <= artifacts.max_active_routing_writes <= 8
    assert artifacts.successful_logical_writes[-1] == "run/discovery-ready"

    for bill_discovery in discoveries:
        store.put_bill_discovery(research_id, bill_discovery)
    artifacts.max_active_routing_writes = 0
    store.put_metadata(research_id, metadata)
    assert 2 <= artifacts.max_active_routing_writes <= 8
    assert artifacts.successful_logical_writes[-1] == "run/document-ready"


def test_discovery_final_marker_retry_does_not_rewrite_hot_routing_items(
    tmp_path: Path,
) -> None:
    state = _fixture()
    discovery, _metadata, _discoveries = _scaled_boundary(state, 40)
    artifacts = FailLogicalWriteOnceStore(tmp_path, "run/discovery-ready")
    store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)

    with pytest.raises(ArtifactBackendError, match="boundary"):
        store.put_discovery(research_id, discovery)
    assert "run/discovery-routing-ready" in artifacts.successful_logical_writes
    artifacts.successful_logical_writes.clear()

    store.put_discovery(research_id, discovery)

    assert artifacts.successful_logical_writes[-1] == "run/discovery-ready"
    assert not any(
        key.startswith(
            (
                "run/accepted-bill/",
                "run/status-partition/",
                "run/deferred-route-shard/",
            )
        )
        for key in artifacts.successful_logical_writes
    )


def test_document_final_marker_retry_does_not_rewrite_hot_routing_items(
    tmp_path: Path,
) -> None:
    state = _fixture()
    discovery, metadata, discoveries = _scaled_boundary(state, 40)
    artifacts = FailLogicalWriteOnceStore(tmp_path, "run/document-ready")
    store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)
    store.put_discovery(research_id, discovery)
    for bill_discovery in discoveries:
        store.put_bill_discovery(research_id, bill_discovery)

    with pytest.raises(ArtifactBackendError, match="boundary"):
        store.put_metadata(research_id, metadata)
    assert "run/document-routing-ready" in artifacts.successful_logical_writes
    artifacts.successful_logical_writes.clear()

    store.put_metadata(research_id, metadata)

    assert artifacts.successful_logical_writes[-1] == "run/document-ready"
    assert not any(
        key.startswith(("run/document-work-item/", "run/document-route-shard/"))
        for key in artifacts.successful_logical_writes
    )


def test_phase_finalization_claim_is_atomic_and_recovers_after_hard_limit(
    tmp_path: Path,
) -> None:
    state = _fixture()
    artifacts = FilesystemResearchArtifactStore(tmp_path)
    stores = tuple(ArtifactResearchRunStore(artifacts, now=lambda: state.now[0]) for _ in range(2))
    research_id = state.gateway.job.id
    stores[0].put_gateway(research_id, state.gateway)

    def claim(store: ArtifactResearchRunStore) -> bool:
        return store.claim_phase_finalization(research_id, MetadataPhase.DISCOVERY)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = tuple(executor.map(claim, stores))
    assert sorted(first) == [False, True]
    assert claim(stores[0]) is False
    assert claim(stores[1]) is False

    # The 30-second grace period prevents overlap with Vercel's 300-second hard
    # limit, but does not leave a crashed finalizer stranded for ten minutes.
    state.now[0] += timedelta(seconds=329)
    assert claim(stores[0]) is False
    assert claim(stores[1]) is False

    state.now[0] += timedelta(seconds=1)
    with ThreadPoolExecutor(max_workers=2) as executor:
        recovered = tuple(executor.map(claim, stores))
    assert sorted(recovered) == [False, True]


def test_compact_stage_boundaries_preserve_raw_pages_without_hot_giant_reads(
    tmp_path: Path,
) -> None:
    state = _fixture()
    research_id = state.gateway.job.id
    artifacts = CountingFilesystemStore(tmp_path)
    store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    store.put_gateway(research_id, state.gateway)

    accepted = state.discovery.resolution.bills.accepted[0]
    rejected_payload = {
        "BILL_NO": "2299999",
        "BILL_NAME": "관련 없는 대형 원본 후보",
        "FULL_OFFICIAL_ROW": "원" * 4_000_000,
    }
    rejected = CandidateDecision(
        MetadataKind.BILL,
        "bill:2299999",
        False,
        0,
        (),
        ("no_relevance_signal",),
        rejected_payload,
    )
    bills = CandidateSetResolution(
        MetadataKind.BILL,
        (accepted, rejected),
        (accepted,),
    )
    partition = state.gateway.discovery_partitions[0]
    raw_page = ApiPage(
        partition.dataset,
        1,
        partition.page_size,
        2,
        (dict(accepted.candidate), rejected_payload),
        "https://open.assembly.go.kr/portal/openapi/raw-audit?KEY=%2A%2A%2A",
        hashlib.sha256(repr(rejected_payload).encode()).hexdigest(),
    )
    provenance = PartitionProvenance(
        partition.partition_id,
        partition.kind,
        partition.dataset,
        partition.parameters,
        2,
        2,
        raw_page.source_hash,
        (
            PageProvenance(
                raw_page.page,
                raw_page.page_size,
                raw_page.total_count,
                len(raw_page.rows),
                raw_page.source_url,
                raw_page.source_hash,
            ),
        ),
    )
    coverage = replace(
        _coverage(bills=2),
        partitions_expected=1,
        partitions_complete=1,
    )
    collection = MetadataCollection(
        (dict(accepted.candidate), rejected_payload),
        (),
        (provenance,),
        coverage,
    )
    large_discovery = replace(
        state.discovery,
        collection=collection,
        filtered_collection=collection,
        filter_report=StrictFilterReport(
            FamilyFilterAccounting(2, 2),
            FamilyFilterAccounting(0, 0),
        ),
        resolution=replace(
            state.discovery.resolution,
            source_hash=collection.source_hash,
            bills=bills,
        ),
    )
    store.put_page(
        research_id,
        MetadataPhase.DISCOVERY,
        partition.partition_id,
        raw_page,
    )

    stored_discovery = store.put_discovery(research_id, large_discovery)
    assert stored_discovery.collection.bills == ()
    assert stored_discovery.filtered_collection.bills == ()
    stored_rejected = next(
        item for item in stored_discovery.resolution.bills.decisions if not item.accepted
    )
    assert stored_rejected.candidate == {"BILL_NO": "2299999"}
    assert stored_rejected.rejection_reasons == rejected.rejection_reasons
    changed_rejected = replace(
        rejected,
        score=1,
        match_reasons=("changed_audit_score",),
    )
    changed_bills = CandidateSetResolution(
        MetadataKind.BILL,
        (accepted, changed_rejected),
        (accepted,),
    )
    changed_discovery = replace(
        large_discovery,
        resolution=replace(large_discovery.resolution, bills=changed_bills),
    )
    with pytest.raises(ResearchRunConflictError, match="different state"):
        store.put_discovery(research_id, changed_discovery)
    assert (
        store.get_page(
            research_id,
            MetadataPhase.DISCOVERY,
            partition.partition_id,
            1,
        )
        == raw_page
    )

    refs = artifacts.list(research_id)
    discovery_ref = next(item for item in refs if item.logical_key == "run/discovery-v2")
    raw_page_ref = next(
        item
        for item in refs
        if item.logical_key == f"run/page/discovery/{partition.partition_id}/1"
    )
    assert discovery_ref.byte_size < 250_000
    assert raw_page_ref.byte_size > 4_000_000

    expected_overview = build_provisional_research_overview(large_discovery)
    artifacts.reset_counts()
    assert store.get_provisional_overview(research_id) == expected_overview
    assert artifacts.logical_reads == [
        "run/gateway",
        "run/discovery-ready",
        "run/provisional-overview",
    ]

    artifacts.reset_counts()
    deferred = store.get_deferred_manifest(research_id)
    assert deferred is not None
    assert deferred.accepted_bill_numbers == ("2219564",)
    assert store.get_accepted_bill(research_id, "2219564") == accepted
    assert "run/discovery-v2" not in artifacts.logical_reads
    assert "run/metadata-v2" not in artifacts.logical_reads

    store.put_bill_discovery(research_id, state.bill_discovery)
    large_metadata = replace(state.metadata, discovery=large_discovery)
    stored_metadata = store.put_metadata(research_id, large_metadata)
    assert stored_metadata.discovery.collection.bills == ()
    metadata_ref = next(
        item
        for item in artifacts.list(research_id, ArtifactKind.METADATA)
        if item.logical_key == "run/metadata-v2"
    )
    assert metadata_ref.byte_size < 500_000

    artifacts.reset_counts()
    assert store.get_document_item(research_id, state.work_item.work_id) == state.work_item
    assert "run/discovery-v2" not in artifacts.logical_reads
    assert "run/metadata-v2" not in artifacts.logical_reads
    assert artifacts.list_calls == 0


def test_compact_manifest_preserves_full_accepted_meeting_decisions(
    tmp_path: Path,
) -> None:
    state = _fixture()
    meeting_payload = {
        "PDF_LINK_URL": "https://record.assembly.go.kr/minutes/accepted.pdf",
        "TITLE": "법제사법위원회 법안심사제1소위원회",
        "CONF_DATE": "20260701",
        "AGENDA": "형사소송법 일부개정법률안",
    }
    meeting = CandidateDecision(
        MetadataKind.MEETING,
        f"meeting:{meeting_payload['PDF_LINK_URL']}",
        True,
        12,
        ("bill_number_exact",),
        (),
        meeting_payload,
    )
    meetings = CandidateSetResolution(
        MetadataKind.MEETING,
        (meeting,),
        (meeting,),
    )
    collection = MetadataCollection(
        state.discovery.collection.bills,
        (meeting_payload,),
        (),
        _coverage(bills=1, meetings=1),
    )
    discovery = replace(
        state.discovery,
        collection=collection,
        filtered_collection=collection,
        filter_report=StrictFilterReport(
            FamilyFilterAccounting(1, 1),
            FamilyFilterAccounting(1, 1),
        ),
        resolution=replace(state.discovery.resolution, meetings=meetings),
    )
    store = _store(tmp_path, state)
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)

    store.put_discovery(research_id, discovery)
    manifest = store.get_deferred_manifest(research_id)

    assert manifest is not None
    assert manifest.accepted_meetings == (meeting,)
    assert manifest.accepted_meetings[0].candidate == meeting_payload


def test_single_metadata_page_read_never_scans_partition(tmp_path: Path) -> None:
    state = _fixture()
    artifacts = CountingFilesystemStore(tmp_path)
    store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    research_id = state.gateway.job.id
    partition = state.gateway.discovery_partitions[0]
    expected = _page(partition)
    store.put_gateway(research_id, state.gateway)
    store.put_page(
        research_id,
        MetadataPhase.DISCOVERY,
        partition.partition_id,
        expected,
    )
    artifacts.reset_counts()

    restored = store.get_page(
        research_id,
        MetadataPhase.DISCOVERY,
        partition.partition_id,
        1,
    )

    assert restored == expected
    assert artifacts.list_calls == 0
    assert artifacts.read_calls == 0
    assert artifacts.logical_read_calls == 1


def test_snapshot_summary_hot_read_does_not_load_the_large_snapshot(tmp_path: Path) -> None:
    state = _fixture()
    artifacts = CountingFilesystemStore(tmp_path)
    store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)
    snapshot = _snapshot(state, "검토보고서 전체 원문 " * 20_000)
    store.put_snapshot(research_id, snapshot)
    result_artifacts = artifacts.list(research_id, ArtifactKind.RESULT_PAGE)
    assert all(item.logical_key != "run/snapshot" for item in result_artifacts)
    artifacts.reset_counts()

    summary = store.get_snapshot_summary(research_id)

    assert summary == ResearchSnapshotSummary.from_snapshot(snapshot)
    assert artifacts.list_calls == 0
    assert artifacts.read_calls == 0

    # One direct gateway lookup and one direct summary lookup.  The full
    # snapshot (and its potentially multi-megabyte evidence text) is untouched.
    assert artifacts.logical_read_calls == 2


def test_snapshot_readiness_is_written_only_after_overview_shards_and_manifest(
    tmp_path: Path,
) -> None:
    state = _fixture()
    artifacts = RecordingFilesystemStore(tmp_path)
    store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)

    snapshot = _snapshot(state, "검토보고서 전체 원문")
    snapshot = replace(
        snapshot,
        evidence=(replace(snapshot.evidence[0], metadata=(("work_id", "document-one"),)),),
    )
    store.put_snapshot(research_id, snapshot)

    writes = artifacts.successful_logical_writes
    summary_position = writes.index("run/snapshot-summary")
    assert writes[-1] == "run/snapshot-summary"
    assert writes.index("run/overview/shard/0") < summary_position
    assert writes.index("run/overview/manifest") < summary_position


def test_snapshot_shards_write_concurrently_but_readiness_remains_last(
    tmp_path: Path,
) -> None:
    state = _fixture()
    artifacts = ConcurrentRecordingStore(tmp_path)
    store = ArtifactResearchRunStore(
        artifacts,
        now=lambda: state.now[0],
        page_read_concurrency=8,
    )
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)

    store.put_snapshot(research_id, _large_overview_snapshot(state))

    assert artifacts.max_active_result_writes >= 2
    assert artifacts.successful_logical_writes[-1] == "run/snapshot-summary"
    assert store.get_snapshot_summary(research_id) is not None


def test_snapshot_retry_fills_missing_parallel_shard_before_readiness(
    tmp_path: Path,
) -> None:
    state = _fixture()
    artifacts = FailOneResultWriteStore(tmp_path)
    store = ArtifactResearchRunStore(
        artifacts,
        now=lambda: state.now[0],
        page_read_concurrency=8,
    )
    research_id = state.gateway.job.id
    snapshot = _large_overview_snapshot(state)
    store.put_gateway(research_id, state.gateway)

    with pytest.raises(ArtifactBackendError, match="injected"):
        store.put_snapshot(research_id, snapshot)
    assert store.get_snapshot_summary(research_id) is None
    assert "run/snapshot-summary" not in artifacts.successful_logical_writes
    assert any(
        key.startswith("run/snapshot-index/shard/") for key in artifacts.successful_logical_writes
    )

    store.put_snapshot(research_id, snapshot)

    assert store.get_snapshot_summary(research_id) == (
        ResearchSnapshotSummary.from_snapshot(snapshot)
    )
    assert artifacts.successful_logical_writes[-1] == "run/snapshot-summary"


def test_overview_hot_path_loads_only_overlapping_shards_and_matches_memory_store(
    tmp_path: Path,
) -> None:
    state = _fixture()
    snapshot = _large_overview_snapshot(state)
    artifacts = CountingFilesystemStore(tmp_path)
    durable = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    research_id = state.gateway.job.id
    durable.put_gateway(research_id, state.gateway)
    durable.put_snapshot(research_id, snapshot)
    artifacts.reset_counts()

    page = durable.get_overview_page(research_id, offset=95, page_size=20)

    assert page is not None
    assert page["entity_totals"]["documents"] == 1_005
    assert page["catalog"]["page"] == {
        "total": 1_005,
        "returned_count": 20,
        "returned_through": 115,
        "next_offset": 115,
        "complete": False,
    }
    assert len(page["catalog"]["groups"]) == 20
    assert page["core_full_text_required_ids"] == [
        "document-record-0000",
        "document-record-0001",
    ]
    encoded = json.dumps(page, ensure_ascii=False, sort_keys=True)
    assert "OVERVIEW_MUST_NOT_INLINE_THIS_TEXT" not in encoded
    assert "evidence_ids" not in encoded
    assert artifacts.list_calls == 0
    assert artifacts.read_calls == 0
    # Gateway + snapshot readiness + overview manifest + only two overlapping
    # 100-group shards.  The other nine shards and giant source text stay cold.
    assert artifacts.logical_read_calls == 5

    artifacts.reset_counts()
    assert (
        durable.get_next_core_evidence_id(research_id, "document-record-0000")
        == "document-record-0001"
    )
    # Gateway + readiness marker + overview manifest; no catalog/text shard.
    assert artifacts.logical_read_calls == 3
    assert artifacts.list_calls == 0
    assert artifacts.read_calls == 0

    memory = InMemoryResearchRunStore()
    memory.put_gateway(research_id, state.gateway)
    memory.put_snapshot(research_id, snapshot)
    assert memory.get_overview_page(research_id, offset=95, page_size=20) == page
    assert (
        memory.get_next_core_evidence_id(research_id, "document-record-0000")
        == "document-record-0001"
    )


def test_result_pages_read_only_bounded_compact_index_shards(tmp_path: Path) -> None:
    state = _fixture()
    artifacts = CountingFilesystemStore(tmp_path)
    store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)
    base = _snapshot(state, "검토보고서 " * 20_000)
    records = tuple(
        EvidenceRecord(
            id=f"review:{number:03d}",
            evidence_type=base.evidence[0].evidence_type,
            sort_key=f"2026-06-01|{number:03d}",
            title=f"검토보고서 {number}",
            text=(f"원문-{number}-" * 20_000),
            citation=base.evidence[0].citation,
            metadata=(
                ("document_kind", OfficialDocumentKind.REVIEW_REPORT.value),
                ("parser_version", "parser-v1"),
            ),
        )
        for number in range(237)
    )
    snapshot = ResearchSnapshot(
        base.research_id,
        base.contract,
        base.index_revision,
        base.build_sha,
        base.coverage,
        records,
    )
    store.put_snapshot(research_id, snapshot)
    result_artifacts = artifacts.list(research_id, ArtifactKind.RESULT_PAGE)
    assert all(item.logical_key != "run/snapshot" for item in result_artifacts)
    assert max(item.byte_size for item in result_artifacts) < 500_000
    artifacts.reset_counts()

    first = store.get_result_page(research_id, page_size=20)

    assert first is not None
    assert first["page"]["returned_count"] == 20
    assert first["page"]["matched_total"] == 237
    assert all("text" not in item for item in first["evidence"])
    assert artifacts.list_calls == 0
    assert artifacts.read_calls == 0
    # Gateway + compact manifest + only the one required 100-entry shard.
    assert artifacts.logical_read_calls == 3

    artifacts.reset_counts()
    crossing_cursor = store.get_result_page(research_id, page_size=100)["page"]["next_cursor"]
    artifacts.reset_counts()
    second = store.get_result_page(
        research_id,
        cursor=crossing_cursor,
        page_size=100,
    )
    assert second is not None
    assert second["page"]["returned_through"] == 200
    # Gateway + manifest + cursor shard + next shard.  The already-loaded
    # cursor shard is reused and the giant full snapshot is never opened.
    assert artifacts.logical_read_calls == 4

    artifacts.reset_counts()
    indexed = store.get_evidence_index_entry(research_id, "review:150")
    assert indexed is not None and indexed.inline_text is None
    # Gateway + compact manifest + one hashed lookup bucket + routed shard.
    assert artifacts.logical_read_calls == 4
    assert artifacts.list_calls == 0
    assert artifacts.read_calls == 0

    artifacts.reset_counts()
    overflow = store.get_overflow_evidence_record(research_id, "review:150")
    assert overflow == records[150]
    assert overflow is not None and overflow.text == ("원문-150-" * 20_000)
    # Gateway + manifest + one hashed lookup bucket + one bounded text shard.
    # The original parsed PDF and every unrelated evidence unit stay cold.
    assert artifacts.logical_read_calls == 4
    assert artifacts.list_calls == 0
    assert artifacts.read_calls == 0

    artifacts.reset_counts()
    assert store.get_next_full_text_evidence_id(research_id, "review:099") == "review:100"
    # Gateway + manifest + lookup + current shard + next shard.
    assert artifacts.logical_read_calls == 5


def test_fixed_and_partition_page_reads_are_bounded_by_target_pages(
    tmp_path: Path,
) -> None:
    state = _fixture()
    artifacts = CountingFilesystemStore(tmp_path)
    store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    research_id = state.gateway.job.id
    partition = state.gateway.discovery_partitions[0]
    store.put_gateway(research_id, state.gateway)
    for page_number in range(1, 4):
        row_count = partition.page_size if page_number < 3 else 1
        rows = tuple(
            {
                "BILL_NO": f"{(page_number - 1) * partition.page_size + index:07d}",
                "BILL_NAME": "장애인 이동권 보장 법률안",
            }
            for index in range(row_count)
        )
        page = ApiPage(
            partition.dataset,
            page_number,
            partition.page_size,
            partition.page_size * 2 + 1,
            rows,
            (
                f"https://open.assembly.go.kr/portal/openapi/{partition.dataset}"
                f"?KEY=%2A%2A%2A&pIndex={page_number}&pSize={partition.page_size}"
            ),
            hashlib.sha256(repr(rows).encode()).hexdigest(),
        )
        store.put_page(
            research_id,
            MetadataPhase.DISCOVERY,
            partition.partition_id,
            page,
        )
    for number in range(100):
        artifacts.write(
            research_id,
            ArtifactKind.PARTITION,
            {"unrelated": number},
        )
    artifacts.reset_counts()

    readiness = store.page_readiness_for(
        research_id,
        MetadataPhase.DISCOVERY,
        partition.partition_id,
        (1, 2, 3),
    )

    assert tuple(item.page for item in readiness) == (1, 2, 3)
    assert artifacts.list_calls == 0
    assert artifacts.read_calls == 0
    assert artifacts.logical_read_calls == 4
    assert sorted(artifacts.logical_reads) == sorted(
        [
            "run/gateway",
            *(
                f"run/page-ready/discovery/{partition.partition_id}/{number}"
                for number in range(1, 4)
            ),
        ]
    )
    assert not any(key.startswith("run/page/") for key in artifacts.logical_reads)

    artifacts.reset_counts()

    restored = store.pages(
        research_id,
        MetadataPhase.DISCOVERY,
        partition.partition_id,
    )

    assert tuple(page.page for page in restored) == (1, 2, 3)
    assert artifacts.list_calls == 0
    assert artifacts.read_calls == 0
    # One gateway read plus exactly the three planned page identities.
    assert artifacts.logical_read_calls == 4


def test_retryable_outcomes_are_append_only_then_terminal_becomes_current(
    tmp_path: Path,
) -> None:
    state = _fixture()
    store = _store(tmp_path, state)
    _seed_through_metadata(store, state)
    research_id = state.gateway.job.id
    first_retry = DocumentOutcome(
        state.work_item.work_id,
        DocumentOutcomeStatus.RETRYABLE_FAILURE,
        error_code="upstream_timeout",
        error_message="official source timed out",
    )
    second_retry = DocumentOutcome(
        state.work_item.work_id,
        DocumentOutcomeStatus.RETRYABLE_FAILURE,
        error_code="upstream_unavailable",
        error_message="official source unavailable",
    )
    text = "전문위원 검토 원문 " * 9000
    terminal = DocumentOutcome(
        state.work_item.work_id,
        DocumentOutcomeStatus.SUCCEEDED,
        result=_result(state, text),
    )

    assert store.get_document_outcome(research_id, state.work_item.work_id) is None
    assert store.put_document_outcome(research_id, first_retry) == first_retry
    assert store.put_document_outcome(research_id, first_retry) == first_retry
    assert store.put_document_outcome(research_id, second_retry) == second_retry
    assert store.get_document_outcome(research_id, state.work_item.work_id) is None
    compact_terminal = _compact_outcome(terminal)
    assert store.put_document_outcome(research_id, terminal) == compact_terminal
    assert terminal.result is not None
    cache_hit_delivery = replace(
        terminal,
        result=replace(terminal.result, cache_hit=True),
    )
    assert store.put_document_outcome(research_id, cache_hit_delivery) == compact_terminal
    assert store.get_document_outcome(research_id, state.work_item.work_id) == compact_terminal

    assert store.document_outcomes(research_id) == (compact_terminal,)
    history = store.document_outcome_history(research_id)
    assert len(history) == 3
    assert set(history) == {first_retry, second_retry, compact_terminal}
    assert (
        len(FilesystemResearchArtifactStore(tmp_path).list(research_id, ArtifactKind.OUTCOME)) == 3
    )
    conflicting = DocumentOutcome(
        state.work_item.work_id,
        DocumentOutcomeStatus.FAILED,
        error_code="permanent_parse_error",
    )
    with pytest.raises(ResearchRunConflictError, match="cannot be replaced"):
        store.put_document_outcome(research_id, conflicting)


def test_planned_terminal_outcomes_use_fixed_reads_and_ignore_retry_history(
    tmp_path: Path,
) -> None:
    state = _fixture()
    artifacts = CountingFilesystemStore(tmp_path)
    store = ArtifactResearchRunStore(
        artifacts,
        now=lambda: state.now[0],
        page_read_concurrency=2,
    )
    second_item = DocumentWorkItem.create(
        OfficialDocumentKind.BILL_TEXT,
        "https://likms.assembly.go.kr/filegate/bill-text.pdf?id=2219564",
        evidence_types=(EvidenceType.BILL_TEXT,),
        related_bill_numbers=("2219564",),
    )
    manifest = DocumentWorkManifest.create(
        (*state.metadata.manifest.items, second_item),
        state.metadata.manifest.bill_discoveries,
    )
    metadata = replace(state.metadata, manifest=manifest)
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)
    store.put_discovery(research_id, state.discovery)
    store.put_bill_discovery(research_id, state.bill_discovery)
    store.put_metadata(research_id, metadata)

    retry = DocumentOutcome(
        second_item.work_id,
        DocumentOutcomeStatus.RETRYABLE_FAILURE,
        error_code="upstream_timeout",
    )
    succeeded = DocumentOutcome(
        state.work_item.work_id,
        DocumentOutcomeStatus.SUCCEEDED,
        result=_result(state, "전문위원 검토 원문"),
    )
    failed = DocumentOutcome(
        second_item.work_id,
        DocumentOutcomeStatus.FAILED,
        error_code="unsupported_document",
    )
    store.put_document_outcome(research_id, retry)
    assert store.document_outcomes_for(research_id, (second_item.work_id,)) == ()
    store.put_document_outcome(research_id, succeeded)
    store.put_document_outcome(research_id, failed)

    artifacts.reset_counts()
    assert store.document_outcomes_for(
        research_id,
        (second_item.work_id, state.work_item.work_id),
    ) == (failed, _compact_outcome(succeeded))
    assert artifacts.list_calls == 0
    assert sorted(artifacts.logical_reads) == sorted(
        (
            f"run/document-terminal/{second_item.work_id}",
            f"run/document-terminal/{state.work_item.work_id}",
        )
    )
    with pytest.raises(ValueError, match="must be unique"):
        store.document_outcomes_for(
            research_id,
            (state.work_item.work_id, state.work_item.work_id),
        )


def test_task_completion_receipts_are_fixed_bound_and_outside_job_history(
    tmp_path: Path,
) -> None:
    state = _fixture()
    artifacts = CountingFilesystemStore(tmp_path)
    store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)
    task = ResearchTask(
        research_id,
        ResearchTaskStage.COLLECT_METADATA,
        "page:one",
        state.gateway.job.query_fingerprint,
        state.gateway.job.index_revision,
        payload=(("page", 1), ("work_kind", "metadata_page")),
        credential_capability="g" * 80,
    )

    receipt = store.put_task_completion(task)
    assert store.get_task_completion(task) == receipt
    # Credentials are deliberately outside the receipt identity; rotating the
    # encrypted delivery capability does not make completed public work new.
    rotated = replace(task, credential_capability="h" * 80)
    assert store.get_task_completion(rotated) == receipt
    assert receipt == type(receipt).from_task(rotated)
    artifacts.reset_counts()
    missing = replace(task, work_id="page:missing")
    assert store.task_completions_for((rotated, missing)) == (receipt,)
    assert artifacts.list_calls == 0
    assert artifacts.logical_reads.count("run/gateway") == 1
    assert sorted(artifacts.logical_reads) == sorted(
        (
            "run/gateway",
            f"run/task-completion/{rotated.idempotency_key}",
            f"run/task-completion/{missing.idempotency_key}",
        )
    )
    stored_bytes = b"\n".join(path.read_bytes() for path in tmp_path.rglob("*.json"))
    assert ("g" * 80).encode() not in stored_bytes
    assert b"credential_capability" not in stored_bytes

    changed_payload = replace(
        task,
        payload=(("page", 2), ("work_kind", "metadata_page")),
    )
    with pytest.raises(ResearchRunStorageError, match="binding"):
        store.get_task_completion(changed_payload)
    with pytest.raises(ResearchRunConflictError, match="different state"):
        store.put_task_completion(changed_payload)

    for number in range(100):
        store.put_task_completion(replace(task, work_id=f"page:{number + 2}"))
    assert artifacts.list(research_id, ArtifactKind.JOB_STATE) == ()
    receipt_refs = tuple(
        ref
        for ref in artifacts.list(research_id, ArtifactKind.MANIFEST)
        if (ref.logical_key or "").startswith("run/task-completion/")
    )
    assert len(receipt_refs) == 101


def test_task_side_effect_without_receipt_is_not_misreported_complete(
    tmp_path: Path,
) -> None:
    state = _fixture()
    store = _store(tmp_path, state)
    research_id = state.gateway.job.id
    partition = state.gateway.discovery_partitions[0]
    store.put_gateway(research_id, state.gateway)
    store.put_page(
        research_id,
        MetadataPhase.DISCOVERY,
        partition.partition_id,
        _page(partition),
    )
    task = ResearchTask(
        research_id,
        ResearchTaskStage.COLLECT_METADATA,
        "metadata-page-side-effect-only",
        state.gateway.job.query_fingerprint,
        state.gateway.job.index_revision,
    )

    assert store.get_task_completion(task) is None


def test_duplicate_concurrent_page_put_is_idempotent_across_store_instances(
    tmp_path: Path,
) -> None:
    state = _fixture()
    first = _store(tmp_path, state)
    second = _store(tmp_path, state)
    research_id = state.gateway.job.id
    first.put_gateway(research_id, state.gateway)
    partition = state.gateway.discovery_partitions[0]
    page = _page(partition)
    barrier = Barrier(2)

    def write(store: ArtifactResearchRunStore) -> ApiPage:
        barrier.wait()
        return store.put_page(research_id, MetadataPhase.DISCOVERY, partition.partition_id, page)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(write, (first, second)))

    assert results == (page, page)
    assert first.pages(research_id, MetadataPhase.DISCOVERY, partition.partition_id) == (page,)
    assert (
        len(FilesystemResearchArtifactStore(tmp_path).list(research_id, ArtifactKind.PARTITION))
        == 1
    )


def test_duplicate_concurrent_page_put_is_idempotent_on_one_unlocked_store(
    tmp_path: Path,
) -> None:
    state = _fixture()
    store = _store(tmp_path, state)
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)
    partition = state.gateway.discovery_partitions[0]
    page = _page(partition)
    barrier = Barrier(2)

    def write(_number: int) -> ApiPage:
        barrier.wait()
        return store.put_page(
            research_id,
            MetadataPhase.DISCOVERY,
            partition.partition_id,
            page,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(write, (1, 2)))

    assert results == (page, page)
    refs = FilesystemResearchArtifactStore(tmp_path).list(
        research_id,
        ArtifactKind.PARTITION,
    )
    assert len(refs) == 1


def test_duplicate_concurrent_retry_observation_is_one_append_only_event(
    tmp_path: Path,
) -> None:
    state = _fixture()
    first = _store(tmp_path, state)
    second = _store(tmp_path, state)
    _seed_through_metadata(first, state)
    research_id = state.gateway.job.id
    retry = DocumentOutcome(
        state.work_item.work_id,
        DocumentOutcomeStatus.RETRYABLE_FAILURE,
        error_code="upstream_timeout",
        error_message="official source timed out",
    )
    barrier = Barrier(2)

    def write(store: ArtifactResearchRunStore) -> DocumentOutcome:
        barrier.wait()
        return store.put_document_outcome(research_id, retry)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(write, (first, second)))

    assert results == (retry, retry)
    assert first.document_outcome_history(research_id) == (retry,)
    assert (
        len(FilesystemResearchArtifactStore(tmp_path).list(research_id, ArtifactKind.OUTCOME)) == 1
    )


def test_research_binding_conflicts_and_ttl_are_enforced_without_deletion(
    tmp_path: Path,
) -> None:
    state = _fixture()
    store = _store(tmp_path, state)
    research_id = state.gateway.job.id
    with pytest.raises(ValueError, match="another research id"):
        store.put_gateway("research_wrong", state.gateway)

    store.put_gateway(research_id, state.gateway)
    partition = state.gateway.discovery_partitions[0]
    store.put_page(
        research_id,
        MetadataPhase.DISCOVERY,
        partition.partition_id,
        _page(partition),
    )
    state.now[0] = state.gateway.job.expires_at + timedelta(seconds=1)

    assert store.get_gateway(research_id) is None
    assert store.pages(research_id, MetadataPhase.DISCOVERY, partition.partition_id) == ()
    with pytest.raises(ResearchRunExpiredError):
        store.put_page(
            research_id,
            MetadataPhase.DISCOVERY,
            partition.partition_id,
            _page(partition),
        )
    # Expiry hides and freezes the run; immutable evidence remains available to
    # the backend's configured retention/lifecycle policy.
    assert FilesystemResearchArtifactStore(tmp_path).list(research_id)


def test_artifacts_do_not_contain_a_queue_capability_or_api_key(tmp_path: Path) -> None:
    state = _fixture()
    store = _store(tmp_path, state)
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)
    encoded = b"\n".join(path.read_bytes() for path in tmp_path.rglob("*.json"))

    assert b"private-user-open-assembly-key" not in encoded
    assert b"credential_capability" not in encoded
    assert b"assembly_api_key" not in encoded


def test_status_snapshots_avoid_page_and_outcome_scans_and_remain_conservative(
    tmp_path: Path,
) -> None:
    state = _fixture()
    artifacts = CountingFilesystemStore(tmp_path)
    store = StatusSnapshotResearchRunStore(artifacts, now=lambda: state.now[0])
    _seed_through_metadata(store, state)
    research_id = state.gateway.job.id
    artifacts.reset_counts()

    running = store.get_status_view(research_id)

    assert running is not None
    assert running.summary is None
    assert running.derived.stage == "documents"
    assert running.derived.documents_expected == 1
    assert running.derived.documents_complete == 0
    assert running.derived.snapshot_ready is False
    assert running.derived.complete is False
    assert artifacts.list_calls == 0
    assert artifacts.read_calls == 0
    # Gateway, snapshot-summary miss, then the metadata checkpoint.  No page,
    # bill-discovery, or document-outcome artifact is opened by the poll.
    assert artifacts.logical_read_calls == 3

    outcome = DocumentOutcome(
        state.work_item.work_id,
        DocumentOutcomeStatus.SUCCEEDED,
        result=_result(state, "전문위원 검토 원문"),
    )
    store.put_document_outcome(research_id, outcome)
    snapshot = _snapshot(state, "전문위원 검토 원문")
    store.put_snapshot(research_id, snapshot)
    artifacts.reset_counts()

    terminal = store.get_status_view(research_id)

    assert terminal is not None
    assert terminal.summary == ResearchSnapshotSummary.from_snapshot(snapshot)
    assert terminal.derived.documents_complete == 1
    assert terminal.derived.snapshot_ready is True
    assert terminal.derived.complete is True
    assert artifacts.list_calls == 0
    assert artifacts.read_calls == 0
    assert artifacts.logical_read_calls == 3


def test_first_page_preview_is_bound_to_every_page_and_hidden_until_ready(
    tmp_path: Path,
) -> None:
    state = _fixture()
    gateway, partition_pages, preview = _first_page_preview_fixture(state)
    artifacts = FailLogicalWriteOnceStore(
        tmp_path,
        "run/first-page-preview-ready-v1",
    )
    store = StatusSnapshotResearchRunStore(artifacts, now=lambda: state.now[0])
    research_id = gateway.job.id
    store.put_gateway(research_id, gateway)
    first_partition, first_page = partition_pages[0]
    store.put_page(
        research_id,
        MetadataPhase.DISCOVERY,
        first_partition.partition_id,
        first_page,
    )

    with pytest.raises(LookupError, match="not every discovery first page"):
        store.put_first_page_preview(research_id, preview)

    second_partition, second_page = partition_pages[1]
    store.put_page(
        research_id,
        MetadataPhase.DISCOVERY,
        second_partition.partition_id,
        second_page,
    )
    with pytest.raises(ArtifactBackendError, match="boundary"):
        store.put_first_page_preview(research_id, preview)

    assert "run/first-page-preview" in artifacts.successful_logical_writes
    assert store.get_first_page_preview(research_id) is None
    assert store.get_provisional_overview(research_id) is None

    assert store.put_first_page_preview(research_id, preview) == preview
    assert store.get_first_page_preview(research_id) == preview
    assert store.get_provisional_overview(research_id) == preview
    with pytest.raises(ValueError, match="binding"):
        store.put_first_page_preview(
            research_id,
            replace(preview, source_hash="f" * 64),
        )
    with pytest.raises(ValueError, match="binding"):
        store.put_first_page_preview(
            research_id,
            replace(
                preview,
                source=replace(
                    preview.source,
                    source_rows_expected=(preview.source.source_rows_expected or 0) + 1,
                ),
            ),
        )


def test_preview_status_checkpoint_retry_heals_without_rewriting_payload(
    tmp_path: Path,
) -> None:
    state = _fixture()
    gateway, partition_pages, preview = _first_page_preview_fixture(state)
    artifacts = RecordingFilesystemStore(tmp_path)
    store = FailStatusCheckpointOnceStore(
        artifacts,
        now=lambda: state.now[0],
        boundary="preview",
    )
    research_id = gateway.job.id
    store.put_gateway(research_id, gateway)
    for partition, page in partition_pages:
        store.put_page(
            research_id,
            MetadataPhase.DISCOVERY,
            partition.partition_id,
            page,
        )

    with pytest.raises(ArtifactBackendError, match="checkpoint"):
        store.put_first_page_preview(research_id, preview)
    assert store.get_first_page_preview(research_id) == preview
    before = store.get_status_view(research_id)
    assert before is not None
    assert before.derived.overview_available is False

    store.put_first_page_preview(research_id, preview)

    healed = store.get_status_view(research_id)
    assert healed is not None
    assert healed.derived.stage == "metadata_discovery"
    assert healed.derived.overview_available is True
    assert healed.derived.metadata_partitions_expected == 2
    assert healed.derived.metadata_pages_expected == 4
    assert healed.derived.metadata_pages_complete == 2
    # Re-put is byte-identical and write-once; the recording store observes the
    # idempotent verification call but the artifact backend retains one object.
    preview_refs = tuple(
        ref
        for ref in artifacts.list(research_id, ArtifactKind.RESOLUTION)
        if ref.logical_key == "run/first-page-preview"
    )
    assert len(preview_refs) == 1


@pytest.mark.parametrize(
    ("failed_boundary", "expected_stage"),
    (("discovery", "deferred_metadata"), ("metadata", "documents")),
)
def test_stage_retry_heals_status_checkpoint_after_boundary_write(
    tmp_path: Path,
    failed_boundary: str,
    expected_stage: str,
) -> None:
    state = _fixture()
    artifacts = FilesystemResearchArtifactStore(tmp_path)
    store = FailStatusCheckpointOnceStore(
        artifacts,
        now=lambda: state.now[0],
        boundary=failed_boundary,
    )
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)

    if failed_boundary == "discovery":
        with pytest.raises(ArtifactBackendError, match="checkpoint"):
            store.put_discovery(research_id, state.discovery)
        assert store.get_deferred_manifest(research_id) is not None
        before = store.get_status_view(research_id)
        assert before is not None and before.derived.stage == "metadata_discovery"
        persisted = store.get_discovery(research_id)
        assert persisted is not None
        store.put_discovery(research_id, persisted)
    else:
        store.put_discovery(research_id, state.discovery)
        store.put_bill_discovery(research_id, state.bill_discovery)
        with pytest.raises(ArtifactBackendError, match="checkpoint"):
            store.put_metadata(research_id, state.metadata)
        assert store.get_document_manifest(research_id) is not None
        before = store.get_status_view(research_id)
        assert before is not None and before.derived.stage == "deferred_metadata"
        persisted_metadata = store.get_metadata(research_id)
        assert persisted_metadata is not None
        store.put_metadata(research_id, persisted_metadata)

    healed = store.get_status_view(research_id)
    assert healed is not None
    assert healed.derived.stage == expected_stage
    assert healed.derived.overview_available is True


def test_status_snapshot_store_falls_back_for_legacy_runs(tmp_path: Path) -> None:
    state = _fixture()
    artifacts = CountingFilesystemStore(tmp_path)
    legacy = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    legacy.put_gateway(state.gateway.job.id, state.gateway)
    restarted = StatusSnapshotResearchRunStore(artifacts, now=lambda: state.now[0])

    assert restarted.get_status_view(state.gateway.job.id) is None


def test_pre_v010_discovery_and_metadata_are_adopted_without_key_conflicts(
    tmp_path: Path,
) -> None:
    state = _fixture()
    research_id = state.gateway.job.id
    artifacts = FilesystemResearchArtifactStore(tmp_path)
    store = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    store.put_gateway(research_id, state.gateway)
    artifacts.write(
        research_id,
        ArtifactKind.RESOLUTION,
        run_storage_codec._record_payload(
            research_id,
            "discovery",
            {"research_id": research_id},
            state.discovery,
            expires_at=state.gateway.job.expires_at,
        ),
        logical_key="run/discovery",
    )

    deferred = store.get_deferred_manifest(research_id)

    assert deferred is not None
    assert store.get_discovery(research_id) is not None
    assert (
        artifacts.read_logical(research_id, ArtifactKind.RESOLUTION, "run/discovery-v2") is not None
    )
    store.put_bill_discovery(research_id, state.bill_discovery)
    artifacts.write(
        research_id,
        ArtifactKind.METADATA,
        run_storage_codec._record_payload(
            research_id,
            "metadata",
            {"research_id": research_id},
            state.metadata,
            expires_at=state.gateway.job.expires_at,
        ),
        logical_key="run/metadata",
    )

    assert store.get_document_item(research_id, state.work_item.work_id) == state.work_item
    assert store.get_metadata(research_id) is not None
    assert artifacts.read_logical(research_id, ArtifactKind.METADATA, "run/metadata-v2") is not None
