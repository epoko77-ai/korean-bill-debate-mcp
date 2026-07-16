from __future__ import annotations

import hashlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any

import pytest

import kasm.research.engine as engine_module
from kasm.adapters.korea.bills import BILL_DATASET, BILL_STATUS_DATASET
from kasm.adapters.korea.client import ApiPage
from kasm.adapters.korea.sources import DATASET_BY_SOURCE, MeetingSource
from kasm.research.collector import (
    CollectionCoverage,
    MetadataCollection,
    MetadataPartition,
)
from kasm.research.contracts import (
    CoverageLedger,
    EvidenceCoverage,
    EvidenceType,
    ResearchContract,
)
from kasm.research.credentials import ResearchCredential
from kasm.research.document_worker import DocumentWorkResult, TransientDocumentError
from kasm.research.documents import OfficialDocumentKind, ParsedOfficialDocument, TextSegment
from kasm.research.engine import (
    BillDocumentDiscovery,
    DeferredWorkManifest,
    DiscoveryStageState,
    DocumentOutcome,
    DocumentOutcomeStatus,
    DocumentWorkItem,
    DocumentWorkManifest,
    FamilyFilterAccounting,
    FinalizationContext,
    GatewayPlanState,
    InMemoryResearchRunStore,
    MetadataPhase,
    MetadataStageState,
    ResearchEngine,
    StrictFilterReport,
)
from kasm.research.jobs import InMemoryResearchJobStore, JobStatus, ResearchJob
from kasm.research.partitioning import ResearchPartitionPlan, ResearchPartitionPlanner
from kasm.research.planner import ResearchContractPlanner, ResearchPlan, plan_research
from kasm.research.queue import LeasedResearchTask, ResearchTask, ResearchTaskStage
from kasm.research.resolver import MetadataCandidateResolver, resolve_metadata_candidates
from kasm.research.results import ResearchSnapshot

AS_OF = datetime(2026, 7, 13, 9, 30, tzinfo=UTC)
REVIEW_URL = "https://likms.assembly.go.kr/filegate/review.pdf?id=2219564"
MINUTES_URL = "https://record.assembly.go.kr/minutes/ai.pdf"

Responder = Callable[[str, int, int, dict[str, str | int]], ApiPage]


def test_criteria_hash_binds_exact_proposer_role_and_name() -> None:
    representative = plan_research(
        "김남근 의원이 대표발의한 인공지능 법안",
        as_of=AS_OF,
    )
    co_proposer = plan_research(
        "김남근 의원이 공동발의한 인공지능 법안",
        as_of=AS_OF,
    )
    resolver = MetadataCandidateResolver()
    empty = MetadataCollection(
        bills=(),
        meetings=(),
        partitions=(),
        coverage=CollectionCoverage(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    )
    representative_criteria = resolver.resolve(representative, empty).criteria
    co_proposer_criteria = resolver.resolve(co_proposer, empty).criteria

    assert engine_module._criteria_hash(  # type: ignore[attr-defined]
        representative_criteria
    ) != engine_module._criteria_hash(co_proposer_criteria)  # type: ignore[attr-defined]


class Queue:
    def __init__(self) -> None:
        self.tasks: list[ResearchTask] = []
        self._keys: set[str] = set()
        self.delays: dict[str, int] = {}

    def publish(
        self,
        task: ResearchTask,
        *,
        retention_seconds: int = 86_400,
        delay_seconds: int = 0,
    ) -> str:
        assert retention_seconds >= 60
        assert delay_seconds >= 0
        if task.idempotency_key not in self._keys:
            self._keys.add(task.idempotency_key)
            self.tasks.append(task)
            self.delays[task.idempotency_key] = delay_seconds
        return f"message-{task.idempotency_key[:12]}"

    def receive(
        self,
        *,
        max_messages: int = 1,
        visibility_timeout_seconds: int = 300,
    ) -> tuple[LeasedResearchTask, ...]:
        del max_messages, visibility_timeout_seconds
        return ()

    def acknowledge(self, receipt_handle: str) -> None:
        del receipt_handle

    def extend(self, receipt_handle: str, visibility_timeout_seconds: int) -> None:
        del receipt_handle, visibility_timeout_seconds


class Credentials:
    capability = "g" * 120

    def __init__(self) -> None:
        self.issued_keys: list[str] = []

    def issue(
        self,
        *,
        research_id: str,
        query_fingerprint: str,
        assembly_api_key: str,
        ttl_seconds: int = 3600,
    ) -> str:
        assert research_id and len(query_fingerprint) == 64 and ttl_seconds >= 60
        self.issued_keys.append(assembly_api_key)
        return self.capability

    def reveal(
        self,
        token: str,
        *,
        research_id: str,
        query_fingerprint: str,
    ) -> ResearchCredential:
        assert token == self.capability
        return ResearchCredential(
            research_id=research_id,
            query_fingerprint=query_fingerprint,
            assembly_api_key="user-open-assembly-key",
            expires_at=2_000_000_000.0,
        )


class LostProcessLocalJobs:
    """Models a new hosted invocation whose process-local status DB disappeared."""

    def create(
        self,
        contract: ResearchContract,
        index_revision: str,
        *,
        ttl: timedelta = timedelta(hours=1),
    ) -> ResearchJob:
        del contract, index_revision, ttl
        raise AssertionError("a worker must not create another research job")

    def get(self, research_id: str) -> ResearchJob | None:
        del research_id
        return None

    def transition(
        self,
        research_id: str,
        status: JobStatus,
        *,
        stage: str,
        progress: float,
        coverage: CoverageLedger | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ResearchJob:
        del (
            research_id,
            status,
            stage,
            progress,
            coverage,
            error_code,
            error_message,
        )
        raise AssertionError("immutable artifacts, not local status, drive workers")


class PageClient:
    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.calls: list[tuple[str, int, int, dict[str, str | int]]] = []

    def fetch_page(
        self,
        dataset: str,
        *,
        page: int = 1,
        page_size: int = 100,
        parameters: Any = None,
        refresh: bool = False,
    ) -> ApiPage:
        assert refresh is False
        values = dict(parameters or {})
        self.calls.append((dataset, page, page_size, values))
        return self.responder(dataset, page, page_size, values)


class BillDocuments:
    def __init__(self, *, failure: str | None = None) -> None:
        self.failure = failure
        self.bills: list[str] = []

    def discover_one(self, plan: ResearchPlan, bill: Any) -> BillDocumentDiscovery:
        assert plan.contract.query
        number = str(bill.candidate["BILL_NO"])
        self.bills.append(number)
        if self.failure:
            return BillDocumentDiscovery(number, failure_reason=self.failure)
        return BillDocumentDiscovery(
            number,
            (
                DocumentWorkItem.create(
                    OfficialDocumentKind.REVIEW_REPORT,
                    REVIEW_URL.replace("2219564", number),
                    evidence_types=(EvidenceType.REVIEW_REPORTS,),
                    related_bill_numbers=(number,),
                ),
            ),
        )


class ManyBillDocuments(BillDocuments):
    def discover_one(self, plan: ResearchPlan, bill: Any) -> BillDocumentDiscovery:
        assert plan.contract.query
        number = str(bill.candidate["BILL_NO"])
        self.bills.append(number)
        return BillDocumentDiscovery(
            number,
            tuple(
                DocumentWorkItem.create(
                    OfficialDocumentKind.REVIEW_REPORT,
                    f"{REVIEW_URL}&part={part}",
                    evidence_types=(EvidenceType.REVIEW_REPORTS,),
                    related_bill_numbers=(number,),
                )
                for part in range(10)
            ),
        )


class CompleteExactBillDocuments(BillDocuments):
    def discover_one(self, plan: ResearchPlan, bill: Any) -> BillDocumentDiscovery:
        assert plan.contract.query
        number = str(bill.candidate["BILL_NO"])
        self.bills.append(number)
        return BillDocumentDiscovery(
            number,
            (
                DocumentWorkItem.create(
                    OfficialDocumentKind.BILL_TEXT,
                    f"https://likms.assembly.go.kr/bill/original.pdf?id={number}",
                    evidence_types=(EvidenceType.BILL_TEXT,),
                    related_bill_numbers=(number,),
                ),
                DocumentWorkItem.create(
                    OfficialDocumentKind.REVIEW_REPORT,
                    REVIEW_URL.replace("2219564", number),
                    evidence_types=(EvidenceType.REVIEW_REPORTS,),
                    related_bill_numbers=(number,),
                ),
            ),
        )


class ManifestReadGuardRunStore(InMemoryResearchRunStore):
    """Fail when an engine hot path falls back to a full run manifest."""

    def __init__(self) -> None:
        super().__init__()
        self.forbid_global_manifest_reads = False
        self.deferred_manifest_reads = 0
        self.document_manifest_reads = 0

    def get_deferred_manifest(self, research_id: str) -> DeferredWorkManifest | None:
        self.deferred_manifest_reads += 1
        if self.forbid_global_manifest_reads:
            raise AssertionError("hot path read the full deferred manifest")
        return super().get_deferred_manifest(research_id)

    def get_document_manifest(self, research_id: str) -> DocumentWorkManifest | None:
        self.document_manifest_reads += 1
        if self.forbid_global_manifest_reads:
            raise AssertionError("hot path read the full document manifest")
        return super().get_document_manifest(research_id)


class Worker:
    def __init__(self) -> None:
        self.calls: list[tuple[OfficialDocumentKind, str]] = []

    def process(
        self,
        kind: OfficialDocumentKind,
        official_url: str,
        *,
        refresh: bool = False,
    ) -> DocumentWorkResult:
        assert refresh is False
        self.calls.append((kind, official_url))
        document = ParsedOfficialDocument(
            kind=kind,
            official_url=official_url,
            source_hash="d" * 64,
            parser_version="test-parser-v1",
            parsed_at=AS_OF,
            segments=(TextSegment("p.1", "전문위원 검토 원문"),),
        )
        return DocumentWorkResult(
            kind=kind,
            official_url=official_url,
            parser_version=document.parser_version,
            byte_count=100,
            page_count=1,
            character_count=len(document.full_text),
            source_hash=document.source_hash,
            text_hash=document.text_hash,
            cache_hit=False,
            raw_object_key="official/raw/test",
            parsed_object_key="official/parsed/test.json",
            document=document,
        )


class TransientThenWorker(Worker):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def process(
        self,
        kind: OfficialDocumentKind,
        official_url: str,
        *,
        refresh: bool = False,
    ) -> DocumentWorkResult:
        self.attempts += 1
        if self.attempts == 1:
            raise TransientDocumentError("temporary upstream timeout", code="network_error")
        return super().process(kind, official_url, refresh=refresh)


class CompleteExactWorker(Worker):
    def process(
        self,
        kind: OfficialDocumentKind,
        official_url: str,
        *,
        refresh: bool = False,
    ) -> DocumentWorkResult:
        if kind is not OfficialDocumentKind.MINUTES:
            return super().process(kind, official_url, refresh=refresh)
        assert refresh is False
        self.calls.append((kind, official_url))
        document = ParsedOfficialDocument(
            kind=kind,
            official_url=official_url,
            source_hash="e" * 64,
            parser_version="test-parser-v1",
            parsed_at=AS_OF,
            segments=(
                TextSegment(
                    "p.1",
                    (
                        "1. 형사소송법 일부개정법률안\n"
                        "○김철수 위원: 보완수사권 통제 방안에 대한 정부 입장은 무엇입니까?\n"
                        "○박영희 장관: 사후 통제 절차를 함께 두겠습니다."
                    ),
                ),
            ),
        )
        return DocumentWorkResult(
            kind=kind,
            official_url=official_url,
            parser_version=document.parser_version,
            byte_count=len(document.full_text.encode()),
            page_count=1,
            character_count=len(document.full_text),
            source_hash=document.source_hash,
            text_hash=document.text_hash,
            cache_hit=False,
            raw_object_key="official/raw/minutes",
            parsed_object_key="official/parsed/minutes.json",
            document=document,
        )


class Finalizer:
    def __init__(self) -> None:
        self.contexts: list[FinalizationContext] = []

    def build(self, context: FinalizationContext) -> ResearchSnapshot:
        self.contexts.append(context)
        entries = []
        for evidence_type in context.job.contract.evidence_types:
            reasons = tuple(
                gap.reason for gap in context.coverage_gaps if evidence_type in gap.evidence_types
            )
            entries.append(
                EvidenceCoverage(
                    evidence_type,
                    candidate_total=1,
                    checked_count=0 if reasons else 1,
                    matched_count=0 if reasons else 1,
                    failed_count=1 if reasons else 0,
                    gap_reasons=reasons,
                )
            )
        coverage = CoverageLedger(context.job.contract.evidence_types, tuple(entries))
        return ResearchSnapshot(
            research_id=context.job.id,
            contract=context.job.contract,
            index_revision=context.job.index_revision,
            build_sha="test-build",
            coverage=coverage,
            evidence=(),
        )


class ReducedPartitionPlanner(ResearchPartitionPlanner):
    """Keep one bill-discovery partition so fan-out cardinality is easy to prove."""

    def plan(self, research_plan: ResearchPlan) -> ResearchPartitionPlan:
        original = super().plan(research_plan)
        selected = next(
            item for item in original.planned_partitions if item.source.value == "bill_metadata"
        )
        return replace(original, planned_partitions=(selected,))


def collection(
    *, bills: tuple[dict[str, Any], ...] = (), meetings: tuple[dict[str, Any], ...] = ()
) -> MetadataCollection:
    return MetadataCollection(
        bills,
        meetings,
        (),
        CollectionCoverage(
            partitions_expected=0,
            partitions_complete=0,
            source_rows_expected=len(bills) + len(meetings),
            source_rows_fetched=len(bills) + len(meetings),
            bill_source_rows=len(bills),
            bill_unique_records=len(bills),
            bill_duplicate_rows=0,
            bill_rejected_rows=0,
            meeting_source_rows=len(meetings),
            meeting_unique_pdfs=len(meetings),
            meeting_rows_merged=0,
            meeting_rejected_rows=0,
        ),
    )


def page(
    dataset: str,
    number: int,
    page_size: int,
    total: int,
    rows: list[dict[str, Any]],
) -> ApiPage:
    source_hash = hashlib.sha256(repr((dataset, number, rows)).encode()).hexdigest()
    return ApiPage(
        dataset,
        number,
        page_size,
        total,
        tuple(rows),
        (
            f"https://open.assembly.go.kr/portal/openapi/{dataset}?"
            f"KEY=%2A%2A%2A&pIndex={number}&pSize={page_size}"
        ),
        source_hash,
    )


def engine(
    responder: Responder,
    *,
    partition_planner: ResearchPartitionPlanner | None = None,
    bill_documents: BillDocuments | None = None,
    document_worker: Worker | None = None,
    run_store: InMemoryResearchRunStore | None = None,
    fanout_chunk_size: int = 8,
) -> tuple[
    ResearchEngine,
    Queue,
    PageClient,
    InMemoryResearchJobStore,
    InMemoryResearchRunStore,
    Finalizer,
]:
    queue = Queue()
    client = PageClient(responder)
    jobs = InMemoryResearchJobStore()
    runs = run_store or InMemoryResearchRunStore()
    finalizer = Finalizer()

    def client_factory(key: str) -> PageClient:
        assert key == "user-open-assembly-key"
        return client

    value = ResearchEngine(
        index_revision="index-test",
        planner=ResearchContractPlanner(),
        partition_planner=partition_planner or ResearchPartitionPlanner(),
        jobs=jobs,
        queue=queue,
        credentials=Credentials(),
        page_client_factory=client_factory,
        resolver=MetadataCandidateResolver(),
        bill_documents=bill_documents or BillDocuments(),
        document_worker=document_worker or Worker(),
        finalizer=finalizer,
        runs=runs,
        fanout_chunk_size=fanout_chunk_size,
    )
    return value, queue, client, jobs, runs, finalizer


def exact_responder(
    dataset: str, number: int, page_size: int, parameters: dict[str, str | int]
) -> ApiPage:
    assert number == 1
    bill_number = str(parameters["BILL_NO"])
    if dataset == BILL_STATUS_DATASET:
        rows = [{"BILL_NO": bill_number, "PROC_RESULT": "위원회 심사"}]
    else:
        rows = [
            {
                "BILL_NO": bill_number,
                "BILL_NAME": "형사소송법 일부개정법률안",
                "summary": "보완수사권을 정비한다.",
                "PROPOSE_DT": "2026-06-01",
            }
        ]
    return page(dataset, number, page_size, 1, rows)


def task_with(queue: Queue, **payload: object) -> ResearchTask:
    return next(
        task
        for task in queue.tasks
        if all(dict(task.payload).get(name) == value for name, value in payload.items())
    )


def record_queue_publications(
    monkeypatch: pytest.MonkeyPatch,
    queue: Queue,
) -> list[ResearchTask]:
    attempts: list[ResearchTask] = []
    original = queue.publish

    def recording_publish(
        task: ResearchTask,
        *,
        retention_seconds: int = 86_400,
        delay_seconds: int = 0,
    ) -> str:
        attempts.append(task)
        return original(
            task,
            retention_seconds=retention_seconds,
            delay_seconds=delay_seconds,
        )

    monkeypatch.setattr(queue, "publish", recording_publish)
    return attempts


def process_phase_barrier(
    value: ResearchEngine,
    queue: Queue,
    phase: str,
    *,
    attempt: int = 1,
) -> None:
    value.process_metadata_task(
        task_with(
            queue,
            work_kind="phase_barrier",
            phase=phase,
            attempt=attempt,
        )
    )


def process_finalize_barrier(
    value: ResearchEngine,
    queue: Queue,
    *,
    attempt: int = 1,
) -> None:
    value.process_finalize_task(
        task_with(
            queue,
            work_kind="document_finalize_barrier",
            attempt=attempt,
        )
    )


def process_document(value: ResearchEngine, task: ResearchTask) -> DocumentOutcome:
    outcome = value.process_document_task(task)
    value.complete_task(task)
    return outcome


def test_gateway_is_network_free_and_returns_secret_free_receipt() -> None:
    value, queue, client, _jobs, _runs, _finalizer = engine(exact_responder)

    receipt = value.gateway(
        "2026-01-01부터 2026-07-13까지 2219564 보완수사권",
        assembly_api_key="private-user-key",
        as_of=AS_OF,
        evidence_types=(
            EvidenceType.BILLS,
            EvidenceType.BILL_STATUS,
            EvidenceType.REVIEW_REPORTS,
        ),
    )

    assert client.calls == []
    assert receipt.metadata_task_count == 1
    assert len(queue.tasks) == 2
    assert dict(queue.tasks[0].payload)["page"] == 1
    barrier = task_with(queue, work_kind="phase_barrier", attempt=1)
    assert dict(barrier.payload)["phase"] == "discovery"
    assert queue.delays[barrier.idempotency_key] == value._barrier_delay_seconds(1)
    derived = value.derive_status(receipt.research_id)
    assert derived.stage == "metadata_discovery"
    assert derived.metadata_partitions_expected == 1
    assert derived.metadata_pages_expected == 1
    assert derived.metadata_pages_complete == 0
    assert derived.overview_available is False
    public = repr(receipt.to_dict())
    assert "private-user-key" not in public
    assert Credentials.capability not in public


def test_exact_identifier_gateway_replaces_56_way_scan_with_seven_bounded_tasks() -> None:
    value, queue, client, _jobs, runs, _finalizer = engine(exact_responder)

    receipt = value.gateway(
        "2219564 법안 상태·회의록·검토보고서",
        assembly_api_key="private-user-key",
        as_of=AS_OF,
    )

    assert client.calls == []
    assert receipt.metadata_task_count == 7
    gateway = runs.get_gateway(receipt.research_id)
    assert gateway is not None
    meeting_partitions = tuple(
        partition for partition in gateway.discovery_partitions if partition.kind.value == "meeting"
    )
    assert len(meeting_partitions) == 6
    assert all(
        partition.parameters_dict().get("SUB_NAME") == "2219564" for partition in meeting_partitions
    )
    assert {partition.parameters_dict().get("CONF_DATE") for partition in meeting_partitions} == {
        "2024",
        "2025",
        "2026",
    }
    direct = tuple(
        task
        for task in queue.tasks
        if dict(task.payload).get("work_kind") == "metadata_page"
        and dict(task.payload).get("phase") == "discovery"
    )
    assert len(direct) == 7
    coordinators = [
        task for task in queue.tasks if dict(task.payload).get("work_kind") == "discovery_fanout"
    ]
    assert coordinators == []
    assert len(queue.tasks) == 8
    assert (
        len(
            [task for task in queue.tasks if dict(task.payload).get("work_kind") == "phase_barrier"]
        )
        == 1
    )


def test_exact_fast_path_produces_overview_and_terminal_result_with_bounded_work() -> None:
    def responder(
        dataset: str,
        number: int,
        page_size: int,
        parameters: dict[str, str | int],
    ) -> ApiPage:
        assert number == 1
        if dataset == BILL_DATASET:
            assert parameters == {"AGE": 22, "BILL_NO": "2219564"}
            rows = [
                {
                    "BILL_NO": "2219564",
                    "BILL_ID": "PRC_EXACT",
                    "BILL_NAME": "형사소송법 일부개정법률안",
                    "AGE": 22,
                    "PROPOSE_DT": "2026-06-26",
                }
            ]
        elif dataset == BILL_STATUS_DATASET:
            assert parameters == {"AGE": 22, "BILL_NO": "2219564"}
            rows = [
                {
                    "BILL_NO": "2219564",
                    "AGE": 22,
                    "PROC_RESULT": "위원회 심사",
                }
            ]
        elif dataset == DATASET_BY_SOURCE[MeetingSource.PLENARY]:
            assert parameters == {
                "DAE_NUM": 22,
                "CONF_DATE": "2026",
                "SUB_NAME": "2219564",
            }
            rows = []
        else:
            assert dataset == DATASET_BY_SOURCE[MeetingSource.COMMITTEE]
            assert parameters == {
                "DAE_NUM": 22,
                "CONF_DATE": "2026",
                "SUB_NAME": "2219564",
            }
            # Even if the upstream filter over-returns a same-title neighbor,
            # the exact resolver gate must keep only the requested identifier.
            rows = [
                {
                    "DAE_NUM": 22,
                    "CONF_ID": "exact-meeting",
                    "CONF_DATE": "2026-07-08",
                    "COMM_NAME": "법제사법위원회",
                    "TITLE": "제22대 법제사법위원회 제1차 회의",
                    "SUB_NAME": ("형사소송법 일부개정법률안(의안번호 2219564)"),
                    "PDF_LINK_URL": "https://record.assembly.go.kr/exact.pdf",
                },
                {
                    "DAE_NUM": 22,
                    "CONF_ID": "wrong-meeting",
                    "CONF_DATE": "2026-07-08",
                    "COMM_NAME": "법제사법위원회",
                    "TITLE": "제22대 법제사법위원회 제2차 회의",
                    "SUB_NAME": ("형사소송법 일부개정법률안(의안번호 2219614)"),
                    "PDF_LINK_URL": "https://record.assembly.go.kr/wrong.pdf",
                },
            ]
        return page(dataset, number, page_size, len(rows), rows)

    documents = CompleteExactBillDocuments()
    worker = CompleteExactWorker()
    value, queue, client, jobs, runs, _finalizer = engine(
        responder,
        bill_documents=documents,
        document_worker=worker,
    )
    receipt = value.gateway(
        "2026년 1월부터 현재까지 2219564 법안 상태·회의록·검토보고서",
        assembly_api_key="private-user-key",
        as_of=AS_OF,
    )

    discovery_pages = tuple(
        task
        for task in queue.tasks
        if dict(task.payload).get("work_kind") == "metadata_page"
        and dict(task.payload).get("phase") == "discovery"
    )
    assert receipt.metadata_task_count == len(discovery_pages) == 3
    for task in discovery_pages:
        value.process_metadata_task(task)
    assert runs.get_first_page_preview(receipt.research_id) is None
    process_phase_barrier(value, queue, "discovery")

    discovery = runs.get_discovery(receipt.research_id)
    assert discovery is not None
    assert [item.candidate_id for item in discovery.resolution.meetings.accepted] == [
        "meeting:https://record.assembly.go.kr/exact.pdf"
    ]
    wrong = next(
        item
        for item in discovery.resolution.meetings.decisions
        if item.candidate_id == "meeting:https://record.assembly.go.kr/wrong.pdf"
    )
    assert wrong.rejection_reasons == ("bill_no_mismatch",)
    first = value.derive_status(receipt.research_id)
    assert first.overview_available is True
    assert first.stage == "deferred_metadata"

    for task in tuple(queue.tasks):
        payload = dict(task.payload)
        if payload.get("phase") == "bill_status" or payload.get("work_kind") == ("bill_documents"):
            value.process_metadata_task(task)
    process_phase_barrier(value, queue, "bill_status")
    for task in tuple(queue.tasks):
        if task.stage is ResearchTaskStage.HYDRATE_DOCUMENT:
            process_document(value, task)
    process_finalize_barrier(value, queue)

    snapshot = runs.get_snapshot(receipt.research_id)
    assert snapshot is not None and snapshot.coverage.complete
    assert jobs.get(receipt.research_id).status is JobStatus.COMPLETE  # type: ignore[union-attr]
    assert len(client.calls) == 4
    assert documents.bills == ["2219564"]
    assert len(worker.calls) == 3


def test_phase_barrier_rechecks_with_unique_attempts_and_single_assembly_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, queue, _client, _jobs, runs, _finalizer = engine(exact_responder)
    receipt = value.gateway(
        "2219564 보완수사권",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(EvidenceType.BILLS,),
    )
    original = value._try_assemble_collection
    phase_checks: list[MetadataPhase] = []

    def counted_assembly(
        research_id: str,
        phase: MetadataPhase,
        partitions: tuple[MetadataPartition, ...],
    ) -> MetadataCollection | None:
        phase_checks.append(phase)
        return original(research_id, phase, partitions)

    monkeypatch.setattr(value, "_try_assemble_collection", counted_assembly)

    process_phase_barrier(value, queue, "discovery", attempt=1)

    assert phase_checks == []
    assert runs.get_discovery(receipt.research_id) is None
    retry = task_with(
        queue,
        work_kind="phase_barrier",
        phase="discovery",
        attempt=2,
    )
    assert retry.work_id == "phase_barrier:discovery:2"
    assert queue.delays[retry.idempotency_key] == value._barrier_delay_seconds(2)

    value.process_metadata_task(task_with(queue, work_kind="metadata_page", page=1))

    assert phase_checks == []
    process_phase_barrier(value, queue, "discovery", attempt=2)
    assert phase_checks == [MetadataPhase.DISCOVERY]
    assert runs.get_discovery(receipt.research_id) is not None


def test_concurrent_duplicate_barriers_allow_only_one_full_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, queue, _client, _jobs, runs, _finalizer = engine(exact_responder)
    receipt = value.gateway(
        "2219564 보완수사권",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(EvidenceType.BILLS,),
    )
    value.process_metadata_task(task_with(queue, work_kind="metadata_page", page=1))
    barrier_task = task_with(queue, work_kind="phase_barrier", attempt=1)
    rendezvous = Barrier(2)
    original_complete = value._phase_complete
    original_assembly = value._try_assemble_collection
    assemblies: list[MetadataPhase] = []

    def synchronized_complete(research_id: str, phase: MetadataPhase) -> bool:
        result = original_complete(research_id, phase)
        rendezvous.wait(timeout=5)
        return result

    def counted_assembly(
        research_id: str,
        phase: MetadataPhase,
        partitions: tuple[MetadataPartition, ...],
    ) -> MetadataCollection | None:
        assemblies.append(phase)
        return original_assembly(research_id, phase, partitions)

    monkeypatch.setattr(value, "_phase_complete", synchronized_complete)
    monkeypatch.setattr(value, "_try_assemble_collection", counted_assembly)
    with ThreadPoolExecutor(max_workers=2) as executor:
        tuple(executor.map(value.process_metadata_task, (barrier_task, barrier_task)))

    assert assemblies == [MetadataPhase.DISCOVERY]
    assert runs.get_discovery(receipt.research_id) is not None
    assert (
        len(
            [
                task
                for task in queue.tasks
                if dict(task.payload).get("work_kind") == "phase_barrier"
                and dict(task.payload).get("attempt") == 2
            ]
        )
        == 1
    )


def test_broad_gateway_opens_one_fixed_coordinator_wave_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def empty_responder(
        dataset: str,
        number: int,
        page_size: int,
        _parameters: dict[str, str | int],
    ) -> ApiPage:
        assert number == 1
        return page(dataset, number, page_size, 0, [])

    value, queue, client, _jobs, _runs, _finalizer = engine(
        empty_responder,
        fanout_chunk_size=16,
    )

    receipt = value.gateway(
        "2025년부터 현재까지 AI 입법",
        assembly_api_key="private-user-key",
        as_of=AS_OF,
    )

    assert client.calls == []
    assert receipt.metadata_task_count > value.direct_fanout_limit
    coordinators = [
        task for task in queue.tasks if dict(task.payload).get("work_kind") == "discovery_fanout"
    ]
    assert len(coordinators) == 1
    first = coordinators[0]
    assert dict(first.payload) == {
        "work_kind": "discovery_fanout",
        "expected_total": receipt.metadata_task_count,
        "start": 0,
        "stop": 16,
    }

    fixed_attempts = record_queue_publications(monkeypatch, queue)
    value.process_metadata_task(first)
    assert fixed_attempts
    assert all(
        dict(task.payload).get("work_kind") == "metadata_page" for task in fixed_attempts
    )
    first_children = [
        task
        for task in queue.tasks
        if task.research_id == receipt.research_id
        and dict(task.payload).get("work_kind") == "metadata_page"
    ]
    assert len(first_children) == 16

    barrier = task_with(
        queue,
        work_kind="metadata_window_barrier",
        phase="discovery",
        attempt=1,
    )
    value.process_metadata_task(barrier)
    assert [
        task for task in queue.tasks if dict(task.payload).get("work_kind") == "discovery_fanout"
    ] == [first]

    for child in first_children:
        value.process_metadata_task(child)
    completed_first_barrier = task_with(
        queue,
        work_kind="metadata_window_barrier",
        phase="discovery",
        attempt=2,
    )
    value.process_metadata_task(completed_first_barrier)
    coordinators = [
        task for task in queue.tasks if dict(task.payload).get("work_kind") == "discovery_fanout"
    ]
    assert [
        (dict(task.payload)["start"], dict(task.payload)["stop"]) for task in coordinators
    ] == [(0, 16), (16, 32)]
    assert all(
        dict(task.payload)["expected_total"] == receipt.metadata_task_count
        for task in coordinators
    )
    unique_publications = len(queue.tasks)
    value.process_metadata_task(completed_first_barrier)
    assert len(queue.tasks) == unique_publications
    assert [
        (dict(task.payload)["start"], dict(task.payload)["stop"])
        for task in queue.tasks
        if dict(task.payload).get("work_kind") == "discovery_fanout"
    ] == [(0, 16), (16, 32)]

    second = coordinators[1]
    second_barrier = task_with(
        queue,
        work_kind="metadata_window_barrier",
        phase="discovery",
        start=16,
        stop=32,
        attempt=1,
    )
    assert queue.delays[second_barrier.idempotency_key] == value._barrier_delay_seconds(1)
    publications_before = len(queue.tasks)
    value.process_metadata_task(second)
    publications_after_first_delivery = len(queue.tasks)
    value.process_metadata_task(second)
    assert publications_after_first_delivery - publications_before == 16
    assert len(queue.tasks) == publications_after_first_delivery
    assert not [
        task
        for task in fixed_attempts
        if dict(task.payload).get("work_kind") == "discovery_fanout"
        and dict(task.payload).get("start") == 32
    ]
    assert len(client.calls) == 16


def test_legacy_wide_discovery_coordinator_chains_before_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, queue, _client, _jobs, _runs, _finalizer = engine(exact_responder)
    receipt = value.gateway(
        "2025년부터 현재까지 AI 입법",
        assembly_api_key="private-user-key",
        as_of=AS_OF,
    )
    seed = task_with(queue, work_kind="discovery_fanout", start=0)
    legacy = ResearchTask(
        research_id=seed.research_id,
        stage=seed.stage,
        work_id=f"discovery_fanout:0:{receipt.metadata_task_count}",
        query_fingerprint=seed.query_fingerprint,
        index_revision=seed.index_revision,
        payload=(
            ("work_kind", "discovery_fanout"),
            ("start", 0),
            ("stop", receipt.metadata_task_count),
        ),
        credential_capability=seed.credential_capability,
    )
    attempts = record_queue_publications(monkeypatch, queue)

    value.process_metadata_task(legacy)

    assert dict(attempts[0].payload) == {
        "work_kind": "discovery_fanout",
        "start": value.fanout_chunk_size,
        "stop": receipt.metadata_task_count,
    }
    assert all(dict(task.payload).get("work_kind") == "metadata_page" for task in attempts[1:])


def test_barrier_recheck_delay_is_capped_at_ten_seconds() -> None:
    value, _queue, _client, _jobs, _runs, _finalizer = engine(exact_responder)

    assert value._barrier_delay_seconds(1) == 1
    assert value._barrier_delay_seconds(9) == 9
    assert value._barrier_delay_seconds(10) == 10
    assert value._barrier_delay_seconds(10_000) == 10


def test_gateway_rejects_credential_ttl_shorter_than_queued_task_retention() -> None:
    value, _queue, _client, _jobs, _runs, _finalizer = engine(exact_responder)

    with pytest.raises(ValueError, match="task retention <= credential TTL <= job TTL"):
        value.gateway(
            "2219564 보완수사권",
            assembly_api_key="key",
            as_of=AS_OF,
            evidence_types=(EvidenceType.BILLS,),
            credential_ttl_seconds=3600,
        )


def test_poisoned_task_retry_budget_marks_job_failed_with_sanitized_error() -> None:
    value, queue, _client, jobs, _runs, _finalizer = engine(exact_responder)
    receipt = value.gateway(
        "2219564 보완수사권",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(EvidenceType.BILLS,),
    )
    task = queue.tasks[0]

    failed = value.fail_task(task, error_code="task_retry_budget_exhausted")

    assert failed.status is JobStatus.FAILED
    assert failed.stage == "collect_metadata_failed"
    assert failed.error_code == "task_retry_budget_exhausted"
    assert "key" not in (failed.error_message or "")
    assert jobs.get(receipt.research_id) == failed


def test_completed_task_receipt_prevents_late_retry_budget_failure() -> None:
    value, queue, _client, jobs, _runs, _finalizer = engine(exact_responder)
    receipt = value.gateway(
        "2219564 보완수사권",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(EvidenceType.BILLS,),
    )
    task = queue.tasks[0]

    completion = value.complete_task(task)
    assert value.task_completed(task) is True
    assert completion.work_id == task.work_id

    preserved = value.fail_task(task, error_code="task_retry_budget_exhausted")

    assert preserved.status is JobStatus.QUEUED
    assert jobs.get(receipt.research_id) == preserved


def test_retry_budget_marker_acknowledges_a_run_absent_after_retention() -> None:
    value, queue, _client, _jobs, _runs, _finalizer = engine(exact_responder)
    value.gateway(
        "2219564 보완수사권",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(EvidenceType.BILLS,),
    )
    expired = replace(queue.tasks[0], research_id="expired-run")

    assert value.fail_task(expired, error_code="task_retry_budget_exhausted") is None


def test_memory_task_receipt_rejects_same_identity_with_changed_payload() -> None:
    value, queue, _client, _jobs, _runs, _finalizer = engine(exact_responder)
    value.gateway(
        "2219564 보완수사권",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(EvidenceType.BILLS,),
    )
    task = queue.tasks[0]
    value.complete_task(task)
    changed = replace(task, payload=((*task.payload, ("changed", True))))

    with pytest.raises(ValueError, match="binding"):
        value.task_completed(changed)


def test_worker_recovers_plan_and_identity_when_process_local_job_store_is_empty() -> None:
    value, queue, client, _jobs, runs, finalizer = engine(exact_responder)
    receipt = value.gateway(
        "2026-01-01부터 2026-07-13까지 2219564 보완수사권",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(
            EvidenceType.BILLS,
            EvidenceType.BILL_STATUS,
            EvidenceType.REVIEW_REPORTS,
        ),
    )
    worker_invocation = ResearchEngine(
        index_revision="index-test",
        planner=ResearchContractPlanner(),
        partition_planner=ResearchPartitionPlanner(),
        jobs=LostProcessLocalJobs(),
        queue=queue,
        credentials=Credentials(),
        page_client_factory=lambda _key: client,
        resolver=MetadataCandidateResolver(),
        bill_documents=BillDocuments(),
        document_worker=Worker(),
        finalizer=finalizer,
        runs=runs,
    )

    worker_invocation.process_metadata_task(queue.tasks[0])
    process_phase_barrier(worker_invocation, queue, "discovery")

    assert runs.get_discovery(receipt.research_id) is not None
    derived = worker_invocation.derive_status(receipt.research_id)
    assert derived.stage == "deferred_metadata"
    assert derived.overview_available is True


def test_active_page_task_uses_immutable_gateway_identity_without_status_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, queue, _client, jobs, runs, _finalizer = engine(exact_responder)
    value.gateway(
        "2219564 보완수사권",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(EvidenceType.BILLS,),
    )

    def unexpected_job_read(_research_id: str) -> ResearchJob | None:
        raise AssertionError("an active page task must not read mutable job history")

    def unexpected_snapshot_read(_research_id: str) -> Any:
        raise AssertionError("an unexpired task must not probe snapshot storage")

    monkeypatch.setattr(jobs, "get", unexpected_job_read)
    monkeypatch.setattr(runs, "get_snapshot_summary", unexpected_snapshot_read)

    value.process_metadata_task(
        task_with(queue, work_kind="metadata_page", phase="discovery", page=1)
    )


def test_one_worker_fetches_only_one_page_then_fans_out_every_remaining_page() -> None:
    def responder(
        dataset: str,
        number: int,
        page_size: int,
        _parameters: dict[str, str | int],
    ) -> ApiPage:
        assert number == 1
        rows = [
            {
                "BILL_NO": f"{2200000 + index:07d}",
                "BILL_NAME": f"인공지능 법안 {index}",
                "PROPOSE_DT": "2026-06-01",
            }
            for index in range(100)
        ]
        return page(dataset, 1, page_size, 237, rows)

    value, queue, client, _jobs, _runs, _finalizer = engine(
        responder,
        partition_planner=ReducedPartitionPlanner(),
    )
    value.gateway(
        "최근 AI 입법",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(EvidenceType.BILLS,),
    )

    value.process_metadata_task(queue.tasks[0])

    assert len(client.calls) == 1
    follow_ups = [
        task
        for task in queue.tasks
        if dict(task.payload).get("work_kind") == "metadata_page"
        and dict(task.payload).get("page") in {2, 3}
    ]
    assert [dict(task.payload)["page"] for task in follow_ups] == [2, 3]
    assert all(dict(task.payload)["expected_total"] == 237 for task in follow_ups)


def test_large_page_expansion_opens_one_bounded_page_wave_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def responder(
        dataset: str,
        number: int,
        page_size: int,
        _parameters: dict[str, str | int],
    ) -> ApiPage:
        start = (number - 1) * page_size
        rows = [
            {
                "BILL_NO": f"{2210000 + start + index:07d}",
                "BILL_NAME": f"인공지능 법안 {start + index}",
                "PROPOSE_DT": "2026-06-01",
            }
            for index in range(page_size)
        ]
        return page(dataset, number, page_size, 1_500, rows)

    value, queue, client, _jobs, runs, _finalizer = engine(
        responder,
        partition_planner=ReducedPartitionPlanner(),
    )
    value.gateway(
        "최근 AI 입법",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(EvidenceType.BILLS,),
    )

    def forbidden_scan(
        _research_id: str,
        _phase: MetadataPhase,
        _partition_id: str,
    ) -> tuple[ApiPage, ...]:
        raise AssertionError("page work must use its fixed O(1) identity")

    monkeypatch.setattr(runs, "pages", forbidden_scan)
    value.process_metadata_task(queue.tasks[0])

    def forbidden_raw_page_read(
        _research_id: str,
        _phase: MetadataPhase,
        _partition_id: str,
        _page_number: int,
    ) -> ApiPage | None:
        raise AssertionError("page fan-out must read the small readiness marker")

    original_get_page = runs.get_page
    monkeypatch.setattr(runs, "get_page", forbidden_raw_page_read)

    assert len(client.calls) == 1
    assert not [
        task
        for task in queue.tasks
        if dict(task.payload).get("work_kind") == "metadata_page"
        and dict(task.payload).get("page") != 1
    ]
    initial_coordinators = [
        task for task in queue.tasks if dict(task.payload).get("work_kind") == "page_fanout"
    ]
    assert [
        (dict(task.payload)["start"], dict(task.payload)["stop"]) for task in initial_coordinators
    ] == [(0, 8)]
    assert all(
        dict(task.payload)["stop"] - dict(task.payload)["start"] <= value.fanout_chunk_size
        for task in initial_coordinators
    )
    first = initial_coordinators[0]
    value.process_metadata_task(first)
    first_follow_ups = [
        task
        for task in queue.tasks
        if dict(task.payload).get("work_kind") == "metadata_page"
        and dict(task.payload).get("page") != 1
    ]
    assert [dict(task.payload)["page"] for task in first_follow_ups] == list(range(2, 10))
    first_barrier = task_with(
        queue,
        work_kind="page_window_barrier",
        phase="discovery",
        start=0,
        stop=8,
        attempt=1,
    )
    original_readiness = runs.page_readiness_for

    def tampered_first_readiness(
        research_id: str,
        phase: MetadataPhase,
        partition_id: str,
        page_numbers: tuple[int, ...],
    ) -> tuple[engine_module.MetadataPageReadiness, ...]:
        values = original_readiness(research_id, phase, partition_id, page_numbers)
        if page_numbers == (1,):
            return (replace(values[0], page=2),)
        return values

    monkeypatch.setattr(runs, "page_readiness_for", tampered_first_readiness)
    with pytest.raises(ValueError, match="does not match its stored first page"):
        value.process_metadata_task(first_barrier)
    monkeypatch.setattr(runs, "page_readiness_for", original_readiness)
    value.process_metadata_task(first_barrier)
    assert [
        task for task in queue.tasks if dict(task.payload).get("work_kind") == "page_fanout"
    ] == [first]

    monkeypatch.setattr(runs, "get_page", original_get_page)
    for task in first_follow_ups:
        value.process_metadata_task(task)
    value.process_metadata_task(
        task_with(
            queue,
            work_kind="page_window_barrier",
            phase="discovery",
            start=0,
            stop=8,
            attempt=2,
        )
    )
    coordinators = [
        task for task in queue.tasks if dict(task.payload).get("work_kind") == "page_fanout"
    ]
    assert [
        (dict(task.payload)["start"], dict(task.payload)["stop"]) for task in coordinators
    ] == [(0, 8), (8, 14)]
    second_barrier = task_with(
        queue,
        work_kind="page_window_barrier",
        phase="discovery",
        start=8,
        stop=14,
        attempt=1,
    )
    assert queue.delays[second_barrier.idempotency_key] == value._barrier_delay_seconds(1)

    second = coordinators[1]
    publications_before = len(queue.tasks)
    value.process_metadata_task(second)
    publications_after_first_delivery = len(queue.tasks)
    value.process_metadata_task(second)
    assert publications_after_first_delivery - publications_before == 6
    assert len(queue.tasks) == publications_after_first_delivery
    follow_ups = [
        task
        for task in queue.tasks
        if dict(task.payload).get("work_kind") == "metadata_page"
        and dict(task.payload).get("page") != 1
    ]
    assert [dict(task.payload)["page"] for task in follow_ups] == list(range(2, 16))

    seed = coordinators[0]
    seed_payload = dict(seed.payload)
    seed_expected_total = seed_payload["expected_total"]
    assert isinstance(seed_expected_total, int)
    legacy = ResearchTask(
        research_id=seed.research_id,
        stage=seed.stage,
        work_id=(f"page_fanout:discovery:{seed_payload['partition_id']}:0:{len(follow_ups)}"),
        query_fingerprint=seed.query_fingerprint,
        index_revision=seed.index_revision,
        payload=(
            ("work_kind", "page_fanout"),
            ("phase", "discovery"),
            ("partition_id", str(seed_payload["partition_id"])),
            ("expected_total", seed_expected_total),
            ("start", 0),
            ("stop", len(follow_ups)),
        ),
        credential_capability=seed.credential_capability,
    )
    attempts = record_queue_publications(monkeypatch, queue)

    value.process_metadata_task(legacy)

    first_attempt = dict(attempts[0].payload)
    assert first_attempt["work_kind"] == "page_fanout"
    assert first_attempt["start"] == value.fanout_chunk_size
    assert first_attempt["stop"] == len(follow_ups)
    assert [dict(task.payload)["page"] for task in attempts[1:]] == list(range(2, 10))


def test_dynamic_two_page_discovery_waits_on_markers_then_assembles_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "BILL_NO": f"{2210000 + index:07d}",
            "BILL_NAME": f"인공지능 산업 진흥 제{index}호 법안",
            "PROPOSE_DT": "2026-06-01",
        }
        for index in range(101)
    ]
    rows.extend(
        (
            {
                "BILL_NO": "2219001",
                "BILL_NAME": "인공지능 과거 법안",
                "PROPOSE_DT": "2025-01-01",
            },
            {
                "BILL_NO": "2219002",
                "BILL_NAME": "인공지능 날짜 누락 법안",
            },
        )
    )

    def responder(
        dataset: str,
        number: int,
        page_size: int,
        _parameters: dict[str, str | int],
    ) -> ApiPage:
        start = (number - 1) * page_size
        selected = rows[start : start + page_size]
        return page(dataset, number, page_size, len(rows), selected)

    guarded_runs = ManifestReadGuardRunStore()
    value, queue, client, _jobs, runs, _finalizer = engine(
        responder,
        partition_planner=ReducedPartitionPlanner(),
        run_store=guarded_runs,
    )
    receipt = value.gateway(
        "최근 AI 입법",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(
            EvidenceType.BILLS,
            EvidenceType.BILL_STATUS,
            EvidenceType.REVIEW_REPORTS,
        ),
    )
    original = value._try_assemble_collection
    assembled_phases: list[MetadataPhase] = []

    def counted_assembly(
        research_id: str,
        phase: MetadataPhase,
        partitions: tuple[MetadataPartition, ...],
    ) -> MetadataCollection | None:
        assembled_phases.append(phase)
        return original(research_id, phase, partitions)

    monkeypatch.setattr(value, "_try_assemble_collection", counted_assembly)

    value.process_metadata_task(queue.tasks[0])

    preview = runs.get_first_page_preview(receipt.research_id)
    assert preview is not None
    assert preview.accepted_total == 100
    assert preview.source.source_complete is False
    assert preview.source.source_rows_expected == 103
    assert preview.source.source_rows_fetched == 100
    assert value.derive_status(receipt.research_id).overview_available is True
    process_phase_barrier(value, queue, "discovery", attempt=1)

    assert assembled_phases == []
    assert runs.get_discovery(receipt.research_id) is None

    second_page = task_with(queue, work_kind="metadata_page", page=2)
    value.process_metadata_task(second_page)
    process_phase_barrier(value, queue, "discovery", attempt=2)

    discovery = runs.get_discovery(receipt.research_id)
    assert assembled_phases == [MetadataPhase.DISCOVERY]
    assert discovery is not None
    assert discovery.filter_report.bills.source_count == 103
    assert discovery.filter_report.bills.kept_count == 103
    assert discovery.filter_report.bills.outside_date_count == 0
    assert discovery.filter_report.bills.missing_date_count == 0
    assert discovery.resolution.bills.accepted_count == 103
    complete_overview = runs.get_provisional_overview(receipt.research_id)
    assert complete_overview is not None
    assert complete_overview.accepted_total == 103
    assert complete_overview.source.source_complete is True
    initial_fanout_tasks = [
        task for task in queue.tasks if dict(task.payload).get("work_kind") == "deferred_fanout"
    ]
    assert len(initial_fanout_tasks) == 1
    assert dict(initial_fanout_tasks[0].payload) == {
        "work_kind": "deferred_fanout",
        "expected_total": 206,
        "start": 0,
        "stop": value.fanout_chunk_size,
    }
    assert not [
        task
        for task in queue.tasks
        if dict(task.payload).get("work_kind") == "metadata_page"
        and dict(task.payload).get("phase") == "bill_status"
    ]
    guarded_runs.deferred_manifest_reads = 0
    guarded_runs.document_manifest_reads = 0
    guarded_runs.forbid_global_manifest_reads = True
    value.process_metadata_task(initial_fanout_tasks[0])
    fanout_tasks = [
        task for task in queue.tasks if dict(task.payload).get("work_kind") == "deferred_fanout"
    ]
    assert fanout_tasks == initial_fanout_tasks
    status_tasks = [
        task
        for task in queue.tasks
        if dict(task.payload).get("phase") == "bill_status" and dict(task.payload).get("page") == 1
    ]
    assert len(status_tasks) == value.fanout_chunk_size
    for task in status_tasks:
        value.process_metadata_task(task)
    first_window_barrier = task_with(
        queue,
        work_kind="metadata_window_barrier",
        phase="bill_status",
        start=0,
        stop=value.fanout_chunk_size,
        attempt=1,
    )
    value.process_metadata_task(first_window_barrier)
    assert [
        task for task in queue.tasks if dict(task.payload).get("work_kind") == "deferred_fanout"
    ] == initial_fanout_tasks

    status_page_twos = [
        task
        for task in queue.tasks
        if dict(task.payload).get("phase") == "bill_status"
        and dict(task.payload).get("page") == 2
    ]
    assert len(status_page_twos) == value.fanout_chunk_size
    for task in status_page_twos:
        value.process_metadata_task(task)
    value.process_metadata_task(
        task_with(
            queue,
            work_kind="metadata_window_barrier",
            phase="bill_status",
            start=0,
            stop=value.fanout_chunk_size,
            attempt=2,
        )
    )
    fanout_tasks = [
        task for task in queue.tasks if dict(task.payload).get("work_kind") == "deferred_fanout"
    ]
    assert [
        (dict(task.payload)["start"], dict(task.payload)["stop"]) for task in fanout_tasks
    ] == [
        (0, value.fanout_chunk_size),
        (value.fanout_chunk_size, value.fanout_chunk_size * 2),
    ]
    next_window_barrier = task_with(
        queue,
        work_kind="metadata_window_barrier",
        phase="bill_status",
        start=value.fanout_chunk_size,
        stop=value.fanout_chunk_size * 2,
        attempt=1,
    )
    assert queue.delays[next_window_barrier.idempotency_key] == value._barrier_delay_seconds(1)
    assert len(client.calls) == 2 + (value.fanout_chunk_size * 2)
    assert guarded_runs.deferred_manifest_reads == 0
    assert guarded_runs.document_manifest_reads == 0

    seed = initial_fanout_tasks[0]
    legacy = ResearchTask(
        research_id=seed.research_id,
        stage=seed.stage,
        work_id="deferred_fanout:0:206",
        query_fingerprint=seed.query_fingerprint,
        index_revision=seed.index_revision,
        payload=(
            ("work_kind", "deferred_fanout"),
            ("start", 0),
            ("stop", 206),
        ),
        credential_capability=seed.credential_capability,
    )
    attempts = record_queue_publications(monkeypatch, queue)

    value.process_metadata_task(legacy)

    assert dict(attempts[0].payload) == {
        "work_kind": "deferred_fanout",
        "expected_total": 206,
        "start": value.fanout_chunk_size,
        "stop": 206,
    }
    assert all(
        dict(task.payload).get("work_kind") in {"metadata_page", "bill_documents"}
        for task in attempts[1:]
    )


def test_deferred_wave_waits_across_status_and_bill_document_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bills = [
        {
            "BILL_NO": f"{2210000 + index:07d}",
            "BILL_NAME": f"인공지능 산업 진흥 제{index}호 법안",
            "PROPOSE_DT": "2026-06-01",
        }
        for index in range(20)
    ]

    def responder(
        dataset: str,
        number: int,
        page_size: int,
        parameters: dict[str, str | int],
    ) -> ApiPage:
        assert number == 1
        if dataset == BILL_STATUS_DATASET:
            bill_number = str(parameters["BILL_NO"])
            return page(
                dataset,
                number,
                page_size,
                1,
                [{"BILL_NO": bill_number, "PROC_RESULT": "위원회 심사"}],
            )
        return page(dataset, number, page_size, len(bills), bills)

    value, queue, _client, _jobs, _runs, _finalizer = engine(
        responder,
        partition_planner=ReducedPartitionPlanner(),
        fanout_chunk_size=16,
    )
    receipt = value.gateway(
        "최근 AI 입법",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(
            EvidenceType.BILLS,
            EvidenceType.BILL_STATUS,
            EvidenceType.REVIEW_REPORTS,
        ),
    )
    value.process_metadata_task(
        task_with(queue, work_kind="metadata_page", phase="discovery", page=1)
    )
    process_phase_barrier(value, queue, "discovery")

    first = task_with(queue, work_kind="deferred_fanout", start=0, stop=16)
    value.process_metadata_task(first)
    first_status_tasks = [
        task
        for task in queue.tasks
        if task.research_id == receipt.research_id
        and dict(task.payload).get("phase") == "bill_status"
        and dict(task.payload).get("page") == 1
    ]
    assert len(first_status_tasks) == 16
    for task in first_status_tasks:
        value.process_metadata_task(task)
    value.process_metadata_task(
        task_with(
            queue,
            work_kind="metadata_window_barrier",
            phase="bill_status",
            start=0,
            stop=16,
            attempt=1,
        )
    )

    second = task_with(queue, work_kind="deferred_fanout", start=16, stop=32)
    attempts = record_queue_publications(monkeypatch, queue)
    value.process_metadata_task(second)
    second_children = tuple(attempts)
    assert len(second_children) == 16
    assert not [
        task
        for task in second_children
        if dict(task.payload).get("work_kind") == "deferred_fanout"
    ]
    second_status = [
        task for task in second_children if dict(task.payload).get("phase") == "bill_status"
    ]
    second_documents = [
        task
        for task in second_children
        if dict(task.payload).get("work_kind") == "bill_documents"
    ]
    assert len(second_status) == 4
    assert len(second_documents) == 12
    for task in second_status:
        value.process_metadata_task(task)
    for task in second_documents[:-1]:
        value.process_metadata_task(task)

    mixed_barrier = task_with(
        queue,
        work_kind="metadata_window_barrier",
        phase="bill_status",
        start=16,
        stop=32,
        attempt=1,
    )
    value.process_metadata_task(mixed_barrier)
    assert not [
        task
        for task in queue.tasks
        if dict(task.payload).get("work_kind") == "deferred_fanout"
        and dict(task.payload).get("start") == 32
    ]
    unique_publications = len(queue.tasks)
    value.process_metadata_task(mixed_barrier)
    assert len(queue.tasks) == unique_publications

    value.process_metadata_task(second_documents[-1])
    value.process_metadata_task(
        task_with(
            queue,
            work_kind="metadata_window_barrier",
            phase="bill_status",
            start=16,
            stop=32,
            attempt=2,
        )
    )
    third = task_with(queue, work_kind="deferred_fanout", start=32, stop=40)
    assert dict(third.payload)["expected_total"] == 40
    third_barrier = task_with(
        queue,
        work_kind="metadata_window_barrier",
        phase="bill_status",
        start=32,
        stop=40,
        attempt=1,
    )
    assert queue.delays[third_barrier.idempotency_key] == value._barrier_delay_seconds(1)


def test_legacy_upfront_deferred_shards_allow_credentialless_phase_barrier() -> None:
    bills = [
        {
            "BILL_NO": f"{2210000 + index:07d}",
            "BILL_NAME": f"인공지능 산업 진흥 제{index}호 법안",
            "PROPOSE_DT": "2026-06-01",
        }
        for index in range(20)
    ]

    def responder(
        dataset: str,
        number: int,
        page_size: int,
        _parameters: dict[str, str | int],
    ) -> ApiPage:
        assert number == 1
        return page(dataset, number, page_size, len(bills), bills)

    value, queue, _client, _jobs, runs, _finalizer = engine(
        responder,
        partition_planner=ReducedPartitionPlanner(),
        fanout_chunk_size=16,
    )
    receipt = value.gateway(
        "최근 AI 입법",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(
            EvidenceType.BILLS,
            EvidenceType.BILL_STATUS,
            EvidenceType.REVIEW_REPORTS,
        ),
    )
    value.process_metadata_task(
        task_with(queue, work_kind="metadata_page", phase="discovery", page=1)
    )
    process_phase_barrier(value, queue, "discovery")

    seed = task_with(queue, work_kind="deferred_fanout", start=0, stop=16)
    gateway = runs.get_gateway(receipt.research_id)
    assert gateway is not None
    value._publish_fanout(
        gateway.job,
        "deferred_fanout",
        16,
        32,
        expected_total=40,
        credential_capability=seed.credential_capability,
    )
    value._publish_fanout(
        gateway.job,
        "deferred_fanout",
        32,
        40,
        expected_total=40,
        credential_capability=seed.credential_capability,
    )
    legacy_barrier = ResearchTask(
        research_id=receipt.research_id,
        stage=ResearchTaskStage.COLLECT_METADATA,
        work_id="phase_barrier:bill_status:1",
        query_fingerprint=gateway.job.query_fingerprint,
        index_revision=gateway.job.index_revision,
        payload=(
            ("work_kind", "phase_barrier"),
            ("phase", "bill_status"),
            ("attempt", 1),
        ),
    )

    value.process_metadata_task(legacy_barrier)

    assert [
        (dict(task.payload)["start"], dict(task.payload)["stop"])
        for task in queue.tasks
        if dict(task.payload).get("work_kind") == "deferred_fanout"
    ] == [(0, 16), (16, 32), (32, 40)]
    retry = task_with(
        queue,
        work_kind="phase_barrier",
        phase="bill_status",
        attempt=2,
    )
    assert retry.credential_capability is None


def test_exact_status_document_pipeline_completes_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, queue, _client, jobs, runs, finalizer = engine(exact_responder)
    receipt = value.gateway(
        "2026-01-01부터 2026-07-13까지 2219564 보완수사권",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(
            EvidenceType.BILLS,
            EvidenceType.BILL_STATUS,
            EvidenceType.REVIEW_REPORTS,
        ),
    )
    value.process_metadata_task(queue.tasks[0])
    process_phase_barrier(value, queue, "discovery")
    value.process_metadata_task(task_with(queue, work_kind="bill_documents"))
    original = value._try_assemble_collection
    assembled_phases: list[MetadataPhase] = []

    def counted_assembly(
        research_id: str,
        phase: MetadataPhase,
        partitions: tuple[MetadataPartition, ...],
    ) -> MetadataCollection | None:
        assembled_phases.append(phase)
        return original(research_id, phase, partitions)

    monkeypatch.setattr(value, "_try_assemble_collection", counted_assembly)

    process_phase_barrier(value, queue, "bill_status", attempt=1)
    assert assembled_phases == []
    assert runs.get_document_manifest(receipt.research_id) is None

    value.process_metadata_task(task_with(queue, phase="bill_status", page=1))
    process_phase_barrier(value, queue, "bill_status", attempt=2)
    assert assembled_phases == [MetadataPhase.BILL_STATUS]
    document_task = next(
        task for task in queue.tasks if task.stage is ResearchTaskStage.HYDRATE_DOCUMENT
    )

    outcome = process_document(value, document_task)
    repeated = process_document(value, document_task)
    process_finalize_barrier(value, queue)

    assert outcome.status is DocumentOutcomeStatus.SUCCEEDED
    assert repeated == outcome
    assert len(finalizer.contexts) == 1
    snapshot = runs.get_snapshot(receipt.research_id)
    assert snapshot is not None and snapshot.coverage.complete
    job = jobs.get(receipt.research_id)
    assert job is not None and job.status is JobStatus.COMPLETE

    def forbidden_run_scan(_research_id: str) -> tuple[object, ...]:
        raise AssertionError("derived status must use exact planned identities")

    monkeypatch.setattr(runs, "bill_discoveries", forbidden_run_scan)
    monkeypatch.setattr(runs, "document_outcomes", forbidden_run_scan)
    derived = value.derive_status(receipt.research_id)
    assert derived.snapshot_ready and derived.complete
    assert derived.overview_available is True
    assert derived.documents_expected == 1
    assert derived.documents_complete == 1


def test_hot_workers_and_document_fanout_never_read_global_manifests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guarded_runs = ManifestReadGuardRunStore()
    worker = Worker()
    value, queue, _client, _jobs, _runs, _finalizer = engine(
        exact_responder,
        bill_documents=ManyBillDocuments(),
        document_worker=worker,
        run_store=guarded_runs,
    )
    value.gateway(
        "2219564 보완수사권",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(
            EvidenceType.BILLS,
            EvidenceType.BILL_STATUS,
            EvidenceType.REVIEW_REPORTS,
        ),
    )
    value.process_metadata_task(task_with(queue, work_kind="metadata_page", page=1))
    process_phase_barrier(value, queue, "discovery")

    guarded_runs.deferred_manifest_reads = 0
    guarded_runs.document_manifest_reads = 0
    guarded_runs.forbid_global_manifest_reads = True
    value.process_metadata_task(task_with(queue, phase="bill_status", page=1))
    value.process_metadata_task(task_with(queue, work_kind="bill_documents"))
    assert guarded_runs.deferred_manifest_reads == 0
    assert guarded_runs.document_manifest_reads == 0

    # A phase barrier may perform the one full audit/finalization read. Once it
    # has published fixed document routes, fan-out and workers must stay O(1).
    guarded_runs.forbid_global_manifest_reads = False
    process_phase_barrier(value, queue, "bill_status")
    guarded_runs.deferred_manifest_reads = 0
    guarded_runs.document_manifest_reads = 0
    guarded_runs.forbid_global_manifest_reads = True

    initial_fanouts = [
        task for task in queue.tasks if dict(task.payload).get("work_kind") == "document_fanout"
    ]
    assert [
        (dict(task.payload)["start"], dict(task.payload)["stop"]) for task in initial_fanouts
    ] == [(0, 10)]
    assert all(dict(task.payload)["expected_total"] == 10 for task in initial_fanouts)
    assert all(dict(task.payload)["receipt_gated"] is True for task in initial_fanouts)
    assert not any(
        dict(task.payload).get("work_kind") == "document_finalize_barrier"
        for task in queue.tasks
    )

    original_publish = queue.publish
    hydration_attempts = 0

    def fail_during_first_window(
        task: ResearchTask,
        *,
        retention_seconds: int = 86_400,
        delay_seconds: int = 0,
    ) -> str:
        nonlocal hydration_attempts
        if task.stage is ResearchTaskStage.HYDRATE_DOCUMENT:
            hydration_attempts += 1
            if hydration_attempts == 2:
                raise RuntimeError("injected document queue failure")
        return original_publish(
            task,
            retention_seconds=retention_seconds,
            delay_seconds=delay_seconds,
        )

    monkeypatch.setattr(queue, "publish", fail_during_first_window)
    with pytest.raises(RuntimeError, match="injected document queue failure"):
        value.process_metadata_task(initial_fanouts[0])
    assert sum(task.stage is ResearchTaskStage.HYDRATE_DOCUMENT for task in queue.tasks) == 1
    assert initial_fanouts == [
        task for task in queue.tasks if dict(task.payload).get("work_kind") == "document_fanout"
    ]
    assert not any(
        dict(task.payload).get("work_kind") == "document_finalize_barrier"
        for task in queue.tasks
    )
    monkeypatch.setattr(queue, "publish", original_publish)

    value.process_metadata_task(initial_fanouts[0])
    first_window = [
        task for task in queue.tasks if task.stage is ResearchTaskStage.HYDRATE_DOCUMENT
    ]
    assert len(first_window) == value.fanout_chunk_size
    first_barrier = task_with(
        queue,
        work_kind="document_window_barrier",
        start=0,
        stop=value.fanout_chunk_size,
        attempt=1,
    )
    value.process_metadata_task(first_barrier)
    assert not any(
        dict(task.payload).get("work_kind") == "document_fanout"
        and dict(task.payload).get("start") == value.fanout_chunk_size
        for task in queue.tasks
    )

    for task in first_window:
        assert process_document(value, task).status is DocumentOutcomeStatus.SUCCEEDED
    value.process_metadata_task(
        task_with(
            queue,
            work_kind="document_window_barrier",
            start=0,
            stop=value.fanout_chunk_size,
            attempt=2,
        )
    )
    second_fanout = task_with(
        queue,
        work_kind="document_fanout",
        start=value.fanout_chunk_size,
        stop=10,
    )
    value.process_metadata_task(second_fanout)
    document_tasks = [
        task for task in queue.tasks if task.stage is ResearchTaskStage.HYDRATE_DOCUMENT
    ]
    assert len(document_tasks) == 10
    second_window = document_tasks[value.fanout_chunk_size :]
    for task in second_window:
        assert process_document(value, task).status is DocumentOutcomeStatus.SUCCEEDED
    value.process_metadata_task(
        task_with(
            queue,
            work_kind="document_window_barrier",
            start=value.fanout_chunk_size,
            stop=10,
            attempt=1,
        )
    )
    finalize_tasks = [
        task
        for task in queue.tasks
        if dict(task.payload).get("work_kind") == "document_finalize_barrier"
    ]
    assert len(finalize_tasks) == 1
    assert dict(finalize_tasks[0].payload)["attempt"] == 1
    assert dict(finalize_tasks[0].payload)["receipts_verified"] is True
    assert finalize_tasks[0].work_id.endswith(":verified")
    assert len(worker.calls) == 10
    assert guarded_runs.deferred_manifest_reads == 0
    assert guarded_runs.document_manifest_reads == 0

    original_task_completions_for = guarded_runs.task_completions_for

    def forbidden_global_receipt_scan(
        _tasks: tuple[ResearchTask, ...],
    ) -> tuple[object, ...]:
        raise AssertionError("receipt-gated finalization must not re-read every receipt")

    guarded_runs.forbid_global_manifest_reads = False
    monkeypatch.setattr(
        guarded_runs,
        "task_completions_for",
        forbidden_global_receipt_scan,
    )
    value.process_finalize_task(finalize_tasks[0])
    assert guarded_runs.get_snapshot_summary(finalize_tasks[0].research_id) is not None
    monkeypatch.setattr(
        guarded_runs,
        "task_completions_for",
        original_task_completions_for,
    )
    guarded_runs.forbid_global_manifest_reads = True

    seed = initial_fanouts[0]
    legacy = ResearchTask(
        research_id=seed.research_id,
        stage=seed.stage,
        work_id="document_fanout:0:10",
        query_fingerprint=seed.query_fingerprint,
        index_revision=seed.index_revision,
        payload=(
            ("work_kind", "document_fanout"),
            ("start", 0),
            ("stop", 10),
        ),
    )
    attempts = record_queue_publications(monkeypatch, queue)

    value.process_metadata_task(legacy)

    assert all(
        task.stage is ResearchTaskStage.HYDRATE_DOCUMENT
        for task in attempts[: value.fanout_chunk_size]
    )
    assert dict(attempts[-1].payload) == {
        "attempt": 1,
        "chain_next": True,
        "expected_total": 10,
        "start": 0,
        "stop": value.fanout_chunk_size,
        "work_kind": "document_window_barrier",
    }

    # A coordinator published by the previous fixed-shard deployment remains
    # valid, but its completed non-final shard must not duplicate the already
    # published sibling windows.
    attempts.clear()
    fixed = ResearchTask(
        research_id=seed.research_id,
        stage=seed.stage,
        work_id=f"document_fanout:0:{value.fanout_chunk_size}",
        query_fingerprint=seed.query_fingerprint,
        index_revision=seed.index_revision,
        payload=(
            ("work_kind", "document_fanout"),
            ("expected_total", 10),
            ("start", 0),
            ("stop", value.fanout_chunk_size),
        ),
    )
    value.process_metadata_task(fixed)
    assert dict(attempts[-1].payload)["work_kind"] == "document_window_barrier"
    assert dict(attempts[-1].payload)["chain_next"] is False
    fixed_barrier = attempts[-1]
    attempts.clear()
    value.process_metadata_task(fixed_barrier)
    assert attempts == []


def test_bill_document_task_uses_single_identity_lookup_not_sibling_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, queue, _client, _jobs, runs, _finalizer = engine(exact_responder)
    value.gateway(
        "2219564 보완수사권",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(EvidenceType.BILLS, EvidenceType.REVIEW_REPORTS),
    )
    value.process_metadata_task(task_with(queue, work_kind="metadata_page", page=1))
    process_phase_barrier(value, queue, "discovery")
    bill_task = task_with(queue, work_kind="bill_documents")
    original_scan = runs.bill_discoveries

    def forbidden_scan(_research_id: str) -> tuple[BillDocumentDiscovery, ...]:
        raise AssertionError("a single bill task must not scan sibling discoveries")

    monkeypatch.setattr(runs, "bill_discoveries", forbidden_scan)
    first = value.process_metadata_task(bill_task)
    repeated = value.process_metadata_task(bill_task)
    monkeypatch.setattr(runs, "bill_discoveries", original_scan)

    assert first == repeated
    assert isinstance(first, BillDocumentDiscovery)
    assert first.bill_number == "2219564"


def test_document_tasks_and_finalize_barrier_use_exact_terminal_lookups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = Worker()
    value, queue, _client, jobs, runs, _finalizer = engine(
        exact_responder,
        document_worker=worker,
    )
    receipt = value.gateway(
        "2219564 보완수사권",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(
            EvidenceType.BILLS,
            EvidenceType.BILL_STATUS,
            EvidenceType.REVIEW_REPORTS,
        ),
    )
    value.process_metadata_task(task_with(queue, work_kind="metadata_page", page=1))
    process_phase_barrier(value, queue, "discovery")
    value.process_metadata_task(task_with(queue, phase="bill_status", page=1))
    value.process_metadata_task(task_with(queue, work_kind="bill_documents"))
    process_phase_barrier(value, queue, "bill_status")
    document_task = next(
        task for task in queue.tasks if task.stage is ResearchTaskStage.HYDRATE_DOCUMENT
    )
    original = runs.document_outcomes_for
    outcome_reads: list[tuple[str, ...]] = []

    def forbidden_history_scan(_research_id: str) -> tuple[DocumentOutcome, ...]:
        raise AssertionError("finalize barrier must not scan outcome history")

    def counted_document_outcomes_for(
        research_id: str, work_ids: tuple[str, ...]
    ) -> tuple[DocumentOutcome, ...]:
        outcome_reads.append(work_ids)
        return original(research_id, work_ids)

    monkeypatch.setattr(runs, "document_outcomes", forbidden_history_scan)
    monkeypatch.setattr(runs, "document_outcomes_for", counted_document_outcomes_for)

    process_finalize_barrier(value, queue, attempt=1)
    assert outcome_reads == []
    current = jobs.get(receipt.research_id)
    assert current is not None
    assert current.stage == "documents"
    assert current.progress == 0.4
    retry = task_with(
        queue,
        work_kind="document_finalize_barrier",
        attempt=2,
    )
    assert retry.work_id == "document_finalize_barrier:documents:2"
    assert queue.delays[retry.idempotency_key] == value._barrier_delay_seconds(2)

    first = process_document(value, document_task)
    repeated = process_document(value, document_task)

    assert first == repeated
    assert len(worker.calls) == 1
    assert outcome_reads == []
    current = jobs.get(receipt.research_id)
    assert current is not None
    assert current.stage == "documents"
    assert current.progress == 0.4
    process_finalize_barrier(value, queue, attempt=2)
    assert outcome_reads == [(document_task.work_id,)]
    assert runs.get_snapshot(receipt.research_id) is not None

    # If the final barrier's queue ACK is lost after the snapshot marker was
    # written, redelivery must not read the large document outcomes again.
    process_finalize_barrier(value, queue, attempt=2)
    assert outcome_reads == [(document_task.work_id,)]


def test_cross_term_date_scope_collects_every_term_without_a_false_gap() -> None:
    value, queue, _client, jobs, runs, finalizer = engine(exact_responder)
    receipt = value.gateway(
        "2020-01-01부터 2025-12-31까지 2219564 법안",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(EvidenceType.BILLS,),
    )

    for task in tuple(queue.tasks):
        value.process_metadata_task(task)
    process_finalize_barrier(value, queue)

    snapshot = runs.get_snapshot(receipt.research_id)
    assert snapshot is not None and snapshot.coverage.complete
    assert not finalizer.contexts[0].coverage_gaps
    job = jobs.get(receipt.research_id)
    assert job is not None and job.status is JobStatus.COMPLETE


def test_explicit_assembly_term_clipping_finishes_partial_with_explicit_gap() -> None:
    value, queue, _client, jobs, runs, finalizer = engine(exact_responder)
    receipt = value.gateway(
        "2020-01-01부터 2025-12-31까지 2219564 법안",
        assembly_api_key="key",
        as_of=AS_OF,
        assembly_term=22,
        evidence_types=(EvidenceType.BILLS,),
    )

    value.process_metadata_task(queue.tasks[0])
    process_phase_barrier(value, queue, "discovery")
    process_finalize_barrier(value, queue)

    expected = "requested_date_scope_not_fully_represented:date_from_clipped_to_assembly_term_start"
    snapshot = runs.get_snapshot(receipt.research_id)
    assert snapshot is not None and not snapshot.coverage.complete
    assert expected in snapshot.coverage.entries[0].gap_reasons
    assert expected in {gap.reason for gap in finalizer.contexts[0].coverage_gaps}
    job = jobs.get(receipt.research_id)
    assert job is not None and job.status is JobStatus.PARTIAL


def test_cross_term_status_requests_use_each_accepted_bills_own_term() -> None:
    def responder(
        dataset: str,
        number: int,
        page_size: int,
        parameters: dict[str, str | int],
    ) -> ApiPage:
        assert number == 1
        assembly_term = int(parameters["AGE"])
        bill_number = "2119001" if assembly_term == 21 else "2219001"
        if dataset == BILL_STATUS_DATASET:
            assert parameters["BILL_NO"] == bill_number
            rows = [{"BILL_NO": bill_number, "AGE": assembly_term, "PROC_RESULT": "심사"}]
        else:
            rows = [
                {
                    "BILL_NO": bill_number,
                    "AGE": assembly_term,
                    "BILL_NAME": "인공지능 산업 진흥 법률안",
                    "PROPOSE_DT": ("2023-06-01" if assembly_term == 21 else "2025-06-01"),
                }
            ]
        return page(dataset, number, page_size, 1, rows)

    value, queue, client, jobs, runs, _finalizer = engine(responder)
    receipt = value.gateway(
        "2020년 5월 30일부터 현재까지 인공지능 입법",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(EvidenceType.BILLS, EvidenceType.BILL_STATUS),
    )

    for task in tuple(queue.tasks):
        value.process_metadata_task(task)
    status_tasks = [
        task
        for task in queue.tasks
        if dict(task.payload).get("work_kind") == "metadata_page"
        and dict(task.payload).get("phase") == "bill_status"
    ]
    assert len(status_tasks) == 2
    for task in status_tasks:
        value.process_metadata_task(task)
    process_phase_barrier(value, queue, "bill_status")
    process_finalize_barrier(value, queue)

    status_parameters = {
        tuple(sorted(parameters.items()))
        for dataset, _page, _size, parameters in client.calls
        if dataset == BILL_STATUS_DATASET
    }
    assert status_parameters == {
        (("AGE", 21), ("BILL_NO", "2119001")),
        (("AGE", 22), ("BILL_NO", "2219001")),
    }
    snapshot = runs.get_snapshot(receipt.research_id)
    assert snapshot is not None and snapshot.coverage.complete is False
    assert all(
        "full_text_corpus:corpus_recall_provider_unconfigured" in entry.gap_reasons
        for entry in snapshot.coverage.entries
    )
    job = jobs.get(receipt.research_id)
    assert job is not None and job.status is JobStatus.PARTIAL


def test_document_discovery_failure_finishes_partial_with_explicit_coverage_gap() -> None:
    documents = BillDocuments(failure="review_index_timeout")
    value, queue, _client, jobs, runs, finalizer = engine(
        exact_responder,
        bill_documents=documents,
    )
    receipt = value.gateway(
        "2026-01-01부터 2026-07-13까지 2219564 보완수사권",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(
            EvidenceType.BILLS,
            EvidenceType.BILL_STATUS,
            EvidenceType.REVIEW_REPORTS,
        ),
    )
    value.process_metadata_task(queue.tasks[0])
    process_phase_barrier(value, queue, "discovery")
    value.process_metadata_task(task_with(queue, phase="bill_status", page=1))
    value.process_metadata_task(task_with(queue, work_kind="bill_documents"))
    process_phase_barrier(value, queue, "bill_status")
    process_finalize_barrier(value, queue)

    snapshot = runs.get_snapshot(receipt.research_id)
    assert snapshot is not None and not snapshot.coverage.complete
    reason = "bill_document_discovery_failed:2219564:review_index_timeout"
    review = next(
        entry
        for entry in snapshot.coverage.entries
        if entry.evidence_type is EvidenceType.REVIEW_REPORTS
    )
    assert reason in review.gap_reasons
    assert reason in {gap.reason for gap in finalizer.contexts[0].coverage_gaps}
    job = jobs.get(receipt.research_id)
    assert job is not None and job.status is JobStatus.PARTIAL


def test_transient_document_failure_is_not_acked_and_redelivery_can_finalize() -> None:
    worker = TransientThenWorker()
    value, queue, _client, jobs, runs, finalizer = engine(
        exact_responder,
        document_worker=worker,
    )
    receipt = value.gateway(
        "2026-01-01부터 2026-07-13까지 2219564 보완수사권",
        assembly_api_key="key",
        as_of=AS_OF,
        evidence_types=(
            EvidenceType.BILLS,
            EvidenceType.BILL_STATUS,
            EvidenceType.REVIEW_REPORTS,
        ),
    )
    value.process_metadata_task(queue.tasks[0])
    process_phase_barrier(value, queue, "discovery")
    value.process_metadata_task(task_with(queue, phase="bill_status", page=1))
    value.process_metadata_task(task_with(queue, work_kind="bill_documents"))
    process_phase_barrier(value, queue, "bill_status")
    document_task = next(
        task for task in queue.tasks if task.stage is ResearchTaskStage.HYDRATE_DOCUMENT
    )

    with pytest.raises(TransientDocumentError, match="temporary upstream timeout"):
        process_document(value, document_task)

    first = runs.document_outcomes(receipt.research_id)[0]
    assert first.status is DocumentOutcomeStatus.RETRYABLE_FAILURE
    assert runs.get_snapshot(receipt.research_id) is None
    outcome = process_document(value, document_task)
    process_finalize_barrier(value, queue)

    assert outcome.status is DocumentOutcomeStatus.SUCCEEDED
    assert worker.attempts == 2
    assert len(finalizer.contexts) == 1
    assert runs.get_snapshot(receipt.research_id) is not None
    job = jobs.get(receipt.research_id)
    assert job is not None and job.status is JobStatus.COMPLETE


def test_finalizer_receives_page_aware_transcript_with_cross_page_sequence() -> None:
    research_plan = plan_research(
        "2026-07-01부터 2026-07-13까지 보완수사권 회의록",
        as_of=AS_OF,
        evidence_types=(
            EvidenceType.SPEECHES,
            EvidenceType.SPEECH_CONTEXT,
            EvidenceType.GOVERNMENT_RESPONSES,
        ),
    )
    meeting_row = {
        "DAE_NUM": 22,
        "CONF_ID": "meeting-1",
        "CONF_DATE": "2026-07-02",
        "COMM_NAME": "법제사법위원회 법안심사소위원회",
        "TITLE": "법제사법위원회 법안심사소위원회",
        "PDF_LINK_URL": MINUTES_URL,
        "agenda_text": "보완수사권 형사소송법",
    }
    metadata_collection = collection(meetings=(meeting_row,))
    resolution = resolve_metadata_candidates(research_plan, metadata_collection)
    assert resolution.meetings.accepted_count == 1
    partition_plan = ResearchPartitionPlanner().plan(research_plan)
    discovery = DiscoveryStageState(
        metadata_collection,
        metadata_collection,
        StrictFilterReport(
            FamilyFilterAccounting(0, 0),
            FamilyFilterAccounting(1, 1),
        ),
        resolution,
        (),
        (),
    )
    minute_item = DocumentWorkItem.create(
        OfficialDocumentKind.MINUTES,
        MINUTES_URL,
        evidence_types=research_plan.contract.evidence_types,
    )
    manifest = DocumentWorkManifest.create((minute_item,), ())
    metadata = MetadataStageState(discovery, collection(), manifest, ())

    jobs = InMemoryResearchJobStore()
    job = jobs.create(research_plan.contract, "index-test")
    jobs.transition(job.id, JobStatus.RUNNING, stage="documents", progress=0.4)
    runs = InMemoryResearchRunStore()
    runs.put_gateway(
        job.id,
        GatewayPlanState(
            job,
            research_plan,
            partition_plan,
            partition_plan.metadata_partitions,
        ),
    )
    runs.put_metadata(job.id, metadata)
    document = ParsedOfficialDocument(
        OfficialDocumentKind.MINUTES,
        MINUTES_URL,
        "a" * 64,
        "pypdf-layout-v1",
        AS_OF,
        (
            TextSegment(
                "p.1",
                "1. 형사소송법 일부개정법률안\n○김철수 위원: 정부 입장은 무엇입니까?",
            ),
            TextSegment(
                "p.2",
                "○박영희 장관: 정부는 통제 장치가 필요하다고 봅니다.",
            ),
        ),
    )
    result = DocumentWorkResult(
        OfficialDocumentKind.MINUTES,
        MINUTES_URL,
        document.parser_version,
        200,
        2,
        len(document.full_text),
        document.source_hash,
        document.text_hash,
        False,
        "official/raw/minutes",
        "official/parsed/minutes.json",
        document,
    )
    runs.put_document_outcome(
        job.id,
        DocumentOutcome(
            minute_item.work_id,
            DocumentOutcomeStatus.SUCCEEDED,
            result=result,
        ),
    )
    finalizer = Finalizer()
    value = ResearchEngine(
        index_revision="index-test",
        planner=ResearchContractPlanner(),
        partition_planner=ResearchPartitionPlanner(),
        jobs=jobs,
        queue=Queue(),
        credentials=Credentials(),
        page_client_factory=lambda _key: PageClient(exact_responder),
        resolver=MetadataCandidateResolver(),
        bill_documents=BillDocuments(),
        document_worker=Worker(),
        finalizer=finalizer,
        runs=runs,
    )

    value.try_finalize(job.id)

    transcript = finalizer.contexts[0].transcripts[0]
    assert [speech.sequence for speech in transcript.speeches] == [1, 2]
    assert transcript.speeches[0].source_locator is not None
    assert transcript.speeches[1].source_locator is not None
    assert transcript.speeches[0].source_locator.startswith("p.1:")
    assert transcript.speeches[1].source_locator.startswith("p.2:")
    assert transcript.speeches[0].next_speech_id == transcript.speeches[1].id
