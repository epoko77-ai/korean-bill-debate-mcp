from __future__ import annotations

import hashlib
import time
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

from kasm.adapters.korea.client import ApiPage
from kasm.corpus import (
    CorpusDocument,
    CorpusDocumentIdentity,
    CorpusEvidenceKind,
    CorpusObjectConflictError,
    CorpusRepository,
)
from kasm.research.contracts import EvidenceType
from kasm.research.corpus_runtime import RevisionCorpusRecallProvider
from kasm.research.credentials import ResearchCredential
from kasm.research.engine import (
    BillDocumentDiscovery,
    CorpusRecallStatus,
    InMemoryResearchRunStore,
    ResearchEngine,
)
from kasm.research.finalizer import ConnectedResearchFinalizer
from kasm.research.jobs import InMemoryResearchJobStore, JobStatus
from kasm.research.partitioning import ResearchPartitionPlan, ResearchPartitionPlanner
from kasm.research.planner import ResearchContractPlanner, ResearchPlan
from kasm.research.queue import LeasedResearchTask, ResearchTask
from kasm.research.resolver import MetadataCandidateResolver

AS_OF = datetime(2026, 7, 14, 5, 0, tzinfo=UTC)
VISIBLE_BILL = "2219001"
HIDDEN_BILL = "2219002"


class MemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_immutable(self, key: str, content: bytes) -> None:
        existing = self.objects.get(key)
        if existing is not None and existing != content:
            raise CorpusObjectConflictError("conflict")
        self.objects.setdefault(key, content)

    def get(self, key: str) -> bytes | None:
        return self.objects.get(key)


class Queue:
    def __init__(self) -> None:
        self.tasks: list[ResearchTask] = []
        self.keys: set[str] = set()

    def publish(
        self,
        task: ResearchTask,
        *,
        retention_seconds: int = 86_400,
        delay_seconds: int = 0,
    ) -> str:
        assert retention_seconds >= 60 and delay_seconds >= 0
        if task.idempotency_key not in self.keys:
            self.keys.add(task.idempotency_key)
            self.tasks.append(task)
        return f"message-{len(self.tasks)}"

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
    capability = "c" * 120

    def issue(
        self,
        *,
        research_id: str,
        query_fingerprint: str,
        assembly_api_key: str,
        ttl_seconds: int = 3600,
    ) -> str:
        del research_id, query_fingerprint, assembly_api_key, ttl_seconds
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
            assembly_api_key="user-key",
            expires_at=time.time() + 86_400,
        )


class PageClient:
    def __init__(self, rows: tuple[dict[str, Any], ...]) -> None:
        self.rows = rows

    def fetch_page(
        self,
        dataset: str,
        *,
        page: int = 1,
        page_size: int = 100,
        parameters: Any = None,
        refresh: bool = False,
    ) -> ApiPage:
        del parameters
        assert page == 1 and refresh is False
        source_hash = hashlib.sha256(repr(self.rows).encode()).hexdigest()
        return ApiPage(
            dataset=dataset,
            page=page,
            page_size=page_size,
            total_count=len(self.rows),
            rows=self.rows,
            source_url=(
                f"https://open.assembly.go.kr/portal/openapi/{dataset}?"
                f"KEY=%2A%2A%2A&pIndex=1&pSize={page_size}"
            ),
            source_hash=source_hash,
        )


class NoBillDocuments:
    def discover_one(
        self,
        plan: ResearchPlan,
        bill: Any,
    ) -> BillDocumentDiscovery:
        del plan, bill
        raise AssertionError("BILLS-only research must not discover PDFs")


class NoDocumentWorker:
    def process(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("BILLS-only research must not hydrate PDFs")


class OneBillPartitionPlanner(ResearchPartitionPlanner):
    def plan(self, research_plan: ResearchPlan) -> ResearchPartitionPlan:
        planned = super().plan(research_plan)
        bill_partition = next(
            item
            for item in planned.planned_partitions
            if item.source.value == "bill_metadata"
        )
        return replace(planned, planned_partitions=(bill_partition,))


def _row(number: str, title: str, summary: str) -> dict[str, Any]:
    return {
        "BILL_NO": number,
        "BILL_NAME": title,
        "AGE": 22,
        "PROPOSER": "김의원",
        "COMMITTEE": "법제사법위원회",
        "PROPOSE_DT": "2026-06-01",
        "summary": summary,
        "DETAIL_LINK": (
            "https://likms.assembly.go.kr/bill/billDetail.do?"
            f"billId={number}"
        ),
    }


ROWS = (
    _row(VISIBLE_BILL, "인공지능 안전법안", "AI 안전 의무를 정한다."),
    _row(HIDDEN_BILL, "해양사고 조사법안", "인용 조문을 정비한다."),
)


def _corpus(
    hidden_bill: str = HIDDEN_BILL,
) -> tuple[CorpusRepository, RevisionCorpusRecallProvider]:
    repository = CorpusRepository(MemoryObjectStore())
    documents = tuple(
        CorpusDocument(
            identity=CorpusDocumentIdentity(
                22,
                CorpusEvidenceKind.BILL_ORIGINAL,
                f"bill:{number}:original",
            ),
            official_url=(
                "https://likms.assembly.go.kr/bill/download.do?"
                f"billNo={number}"
            ),
            source_hash=hashlib.sha256(number.encode()).hexdigest(),
            parser_version="pypdf-layout-v1",
            text=(
                "인공지능 안전과 AI 책임을 규정한다."
                if number == hidden_bill
                else "다른 분야의 일반 규정"
            ),
            observed_at=AS_OF,
            title="공식 의안 원문",
            document_date=date(2026, 6, 1),
            related_bill_numbers=(number,),
            committee="법제사법위원회",
        )
        for number in (VISIBLE_BILL, HIDDEN_BILL)
    )
    builder = repository.begin_revision(
        assembly_terms=(22,),
        evidence_kinds=tuple(CorpusEvidenceKind),
    )
    builder.upsert_documents(documents)
    for kind in CorpusEvidenceKind:
        builder.set_expected_count(
            22,
            kind,
            len(documents) if kind is CorpusEvidenceKind.BILL_ORIGINAL else 0,
        )
    manifest = builder.publish(created_at=AS_OF, inventory_as_of=AS_OF)
    return repository, RevisionCorpusRecallProvider(
        repository,
        revision_id=manifest.revision_id,
    )


def _engine(
    *,
    provider: RevisionCorpusRecallProvider | None,
    jobs: InMemoryResearchJobStore | None = None,
    runs: InMemoryResearchRunStore | None = None,
    queue: Queue | None = None,
) -> tuple[
    ResearchEngine,
    InMemoryResearchJobStore,
    InMemoryResearchRunStore,
    Queue,
]:
    actual_jobs = jobs or InMemoryResearchJobStore()
    actual_runs = runs or InMemoryResearchRunStore()
    actual_queue = queue or Queue()
    client = PageClient(ROWS)
    engine = ResearchEngine(
        index_revision="research-test-v1",
        planner=ResearchContractPlanner(),
        partition_planner=OneBillPartitionPlanner(),
        jobs=actual_jobs,
        queue=actual_queue,
        credentials=Credentials(),
        page_client_factory=lambda key: client,
        resolver=MetadataCandidateResolver(),
        bill_documents=NoBillDocuments(),
        document_worker=NoDocumentWorker(),
        finalizer=ConnectedResearchFinalizer(build_sha="test-build"),
        runs=actual_runs,
        corpus_recall_provider=provider,
    )
    return engine, actual_jobs, actual_runs, actual_queue


def _run(engine: ResearchEngine, queue: Queue, query: str = "AI 입법") -> str:
    receipt = engine.gateway(
        query,
        assembly_api_key="user-key",
        as_of=AS_OF,
        evidence_types=(EvidenceType.BILLS,),
    )
    assert len(queue.tasks) == 2
    engine.process_metadata_task(
        next(
            task
            for task in queue.tasks
            if dict(task.payload).get("work_kind") == "metadata_page"
        )
    )
    engine.process_metadata_task(
        next(
            task
            for task in queue.tasks
            if dict(task.payload).get("work_kind") == "phase_barrier"
        )
    )
    engine.process_finalize_task(
        next(
            task
            for task in queue.tasks
            if dict(task.payload).get("work_kind") == "document_finalize_barrier"
        )
    )
    return receipt.research_id


def test_verified_revision_widens_only_exact_hidden_identity_and_completes() -> None:
    _repository, provider = _corpus()
    engine, jobs, runs, queue = _engine(provider=provider)

    research_id = _run(engine, queue)

    discovery = runs.get_discovery(research_id)
    assert discovery is not None
    assert discovery.corpus_recall is not None
    assert discovery.corpus_recall.status is CorpusRecallStatus.VERIFIED
    assert tuple(
        item.candidate_id for item in discovery.resolution.bills.accepted
    ) == ("bill:2219001", "bill:2219002")
    hidden = next(
        item
        for item in discovery.resolution.bills.accepted
        if item.candidate_id == f"bill:{HIDDEN_BILL}"
    )
    assert "corpus_exact_identity" in hidden.match_reasons
    snapshot = runs.get_snapshot(research_id)
    assert snapshot is not None and snapshot.coverage.complete is True
    assert jobs.get(research_id).status is JobStatus.COMPLETE  # type: ignore[union-attr]
    assert engine.index_revision.endswith(provider.binding_id)


def test_missing_provider_never_claims_complete_topical_recall() -> None:
    engine, jobs, runs, queue = _engine(provider=None)

    research_id = _run(engine, queue)

    discovery = runs.get_discovery(research_id)
    assert discovery is not None and discovery.corpus_recall is not None
    assert discovery.corpus_recall.status is CorpusRecallStatus.UNAVAILABLE
    assert tuple(
        item.candidate_id for item in discovery.resolution.bills.accepted
    ) == (f"bill:{VISIBLE_BILL}",)
    snapshot = runs.get_snapshot(research_id)
    assert snapshot is not None and snapshot.coverage.complete is False
    reasons = snapshot.coverage.entries[0].gap_reasons
    assert "full_text_corpus:corpus_recall_provider_unconfigured" in reasons
    assert jobs.get(research_id).status is JobStatus.PARTIAL  # type: ignore[union-attr]


def test_structured_only_query_marks_corpus_not_required() -> None:
    _repository, provider = _corpus()
    engine, jobs, runs, queue = _engine(provider=provider)

    research_id = _run(engine, queue, "법제사법위원회")

    discovery = runs.get_discovery(research_id)
    assert discovery is not None and discovery.corpus_recall is not None
    assert discovery.corpus_recall.status is CorpusRecallStatus.NOT_REQUIRED
    snapshot = runs.get_snapshot(research_id)
    assert snapshot is not None and snapshot.coverage.complete is True
    assert jobs.get(research_id).status is JobStatus.COMPLETE  # type: ignore[union-attr]


def test_worker_with_another_corpus_revision_fails_closed_in_flight() -> None:
    _repository_a, provider_a = _corpus(HIDDEN_BILL)
    gateway, jobs, runs, queue = _engine(provider=provider_a)
    receipt = gateway.gateway(
        "AI 입법",
        assembly_api_key="user-key",
        as_of=AS_OF,
        evidence_types=(EvidenceType.BILLS,),
    )
    _repository_b, provider_b = _corpus(VISIBLE_BILL)
    worker, _jobs, _runs, _queue = _engine(
        provider=provider_b,
        jobs=jobs,
        runs=runs,
        queue=queue,
    )

    worker.process_metadata_task(
        next(
            task
            for task in queue.tasks
            if dict(task.payload).get("work_kind") == "metadata_page"
        )
    )
    worker.process_metadata_task(
        next(
            task
            for task in queue.tasks
            if dict(task.payload).get("work_kind") == "phase_barrier"
        )
    )
    worker.process_finalize_task(
        next(
            task
            for task in queue.tasks
            if dict(task.payload).get("work_kind") == "document_finalize_barrier"
        )
    )

    discovery = runs.get_discovery(receipt.research_id)
    assert discovery is not None and discovery.corpus_recall is not None
    assert discovery.corpus_recall.status is CorpusRecallStatus.INCOMPLETE
    assert discovery.corpus_recall.gap_reasons == (
        "corpus_runtime_index_revision_mismatch",
    )
    assert tuple(
        item.candidate_id for item in discovery.resolution.bills.accepted
    ) == (f"bill:{VISIBLE_BILL}",)
    assert jobs.get(receipt.research_id).status is JobStatus.PARTIAL  # type: ignore[union-attr]
