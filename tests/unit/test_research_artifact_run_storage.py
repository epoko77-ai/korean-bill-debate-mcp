from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

import kasm.research.artifact_run_storage as run_storage_codec
from kasm.adapters.korea.bills import BILL_STATUS_DATASET
from kasm.adapters.korea.client import ApiPage
from kasm.research.artifact_run_storage import (
    ArtifactResearchRunStore,
    ResearchRunConflictError,
    ResearchRunExpiredError,
)
from kasm.research.artifacts import (
    ArtifactKind,
    ArtifactRef,
    FilesystemResearchArtifactStore,
    StoredArtifact,
)
from kasm.research.collector import (
    CollectionCoverage,
    MetadataCollection,
    MetadataKind,
    MetadataPartition,
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
from kasm.research.partitioning import ResearchPartitionPlanner
from kasm.research.planner import plan_research
from kasm.research.resolver import resolve_metadata_candidates
from kasm.research.results import (
    EvidenceCitation,
    EvidenceRecord,
    ResearchSnapshot,
    ResearchSnapshotSummary,
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
        self.list_calls = 0

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
        return super().read_logical(research_id, kind, logical_key)

    def list(
        self, research_id: str, kind: ArtifactKind | None = None
    ) -> tuple[ArtifactRef, ...]:
        self.list_calls += 1
        return super().list(research_id, kind)

    def reset_counts(self) -> None:
        self.read_calls = 0
        self.logical_read_calls = 0
        self.list_calls = 0


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
            EvidenceCoverage(item, 1, 1, 1)
            for item in state.gateway.job.contract.evidence_types
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
            evidence_type=(
                EvidenceType.BILL_TEXT
                if number == 0
                else EvidenceType.REVIEW_REPORTS
            ),
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
    return ArtifactResearchRunStore(
        FilesystemResearchArtifactStore(root), now=lambda: state.now[0]
    )


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


def _seed_through_metadata(
    store: ArtifactResearchRunStore, state: FixtureState
) -> None:
    research_id = state.gateway.job.id
    store.put_gateway(research_id, state.gateway)
    store.put_discovery(research_id, state.discovery)
    store.put_bill_discovery(research_id, state.bill_discovery)
    store.put_metadata(research_id, state.metadata)


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
    first.put_discovery(research_id, state.discovery)
    first.put_bill_discovery(research_id, state.bill_discovery)
    first.put_metadata(research_id, state.metadata)
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
    assert restarted.pages(
        research_id, MetadataPhase.DISCOVERY, partition.partition_id
    ) == (page,)
    assert restarted.get_discovery(research_id) == state.discovery
    assert restarted.bill_discoveries(research_id) == (state.bill_discovery,)
    assert restarted.get_metadata(research_id) == state.metadata
    restored_outcome = restarted.document_outcomes(research_id)[0]
    assert restored_outcome == outcome
    assert restored_outcome.result is not None
    assert restored_outcome.result.document.full_text == text
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
        evidence=(
            replace(snapshot.evidence[0], metadata=(("work_id", "document-one"),)),
        ),
    )
    store.put_snapshot(research_id, snapshot)

    writes = artifacts.successful_logical_writes
    summary_position = writes.index("run/snapshot-summary")
    assert writes[-1] == "run/snapshot-summary"
    assert writes.index("run/overview/shard/0") < summary_position
    assert writes.index("run/overview/manifest") < summary_position


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
    assert durable.get_next_core_evidence_id(
        research_id, "document-record-0000"
    ) == "document-record-0001"
    # Gateway + readiness marker + overview manifest; no catalog/text shard.
    assert artifacts.logical_read_calls == 3
    assert artifacts.list_calls == 0
    assert artifacts.read_calls == 0

    memory = InMemoryResearchRunStore()
    memory.put_gateway(research_id, state.gateway)
    memory.put_snapshot(research_id, snapshot)
    assert memory.get_overview_page(
        research_id, offset=95, page_size=20
    ) == page
    assert memory.get_next_core_evidence_id(
        research_id, "document-record-0000"
    ) == "document-record-0001"


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
    crossing_cursor = store.get_result_page(research_id, page_size=100)["page"][
        "next_cursor"
    ]
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
    assert (
        store.get_next_full_text_evidence_id(research_id, "review:099")
        == "review:100"
    )
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
        rows = (
            {
                "BILL_NO": f"{page_number:07d}",
                "BILL_NAME": "장애인 이동권 보장 법률안",
            },
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
    assert store.put_document_outcome(research_id, terminal) == terminal
    assert store.get_document_outcome(research_id, state.work_item.work_id) == terminal

    assert store.document_outcomes(research_id) == (terminal,)
    history = store.document_outcome_history(research_id)
    assert len(history) == 3
    assert set(history) == {first_retry, second_retry, terminal}
    assert terminal.result is not None and terminal.result.document.full_text == text
    assert len(
        FilesystemResearchArtifactStore(tmp_path).list(research_id, ArtifactKind.OUTCOME)
    ) == 3
    conflicting = DocumentOutcome(
        state.work_item.work_id,
        DocumentOutcomeStatus.FAILED,
        error_code="permanent_parse_error",
    )
    with pytest.raises(ResearchRunConflictError, match="cannot be replaced"):
        store.put_document_outcome(research_id, conflicting)


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
        return store.put_page(
            research_id, MetadataPhase.DISCOVERY, partition.partition_id, page
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(write, (first, second)))

    assert results == (page, page)
    assert first.pages(
        research_id, MetadataPhase.DISCOVERY, partition.partition_id
    ) == (page,)
    assert len(
        FilesystemResearchArtifactStore(tmp_path).list(
            research_id, ArtifactKind.PARTITION
        )
    ) == 1


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
    assert len(
        FilesystemResearchArtifactStore(tmp_path).list(research_id, ArtifactKind.OUTCOME)
    ) == 1


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
    assert store.pages(
        research_id, MetadataPhase.DISCOVERY, partition.partition_id
    ) == ()
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


def test_status_snapshot_store_falls_back_for_legacy_runs(tmp_path: Path) -> None:
    state = _fixture()
    artifacts = CountingFilesystemStore(tmp_path)
    legacy = ArtifactResearchRunStore(artifacts, now=lambda: state.now[0])
    legacy.put_gateway(state.gateway.job.id, state.gateway)
    restarted = StatusSnapshotResearchRunStore(artifacts, now=lambda: state.now[0])

    assert restarted.get_status_view(state.gateway.job.id) is None
