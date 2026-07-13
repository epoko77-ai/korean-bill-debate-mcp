from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kasm.research.artifact_job_storage import ArtifactResearchJobStore
from kasm.research.artifacts import (
    ArtifactIntegrityError,
    ArtifactKind,
    ArtifactRef,
    FilesystemResearchArtifactStore,
    SecretMaterialError,
    StoredArtifact,
)
from kasm.research.contracts import (
    CoverageLedger,
    EvidenceCoverage,
    EvidenceType,
    ResearchContract,
    ResearchIntent,
)
from kasm.research.jobs import JobStatus, ResearchJobStore


class Clock:
    def __init__(self, value: datetime | None = None) -> None:
        self.value = value or datetime(2026, 7, 13, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


class RacingFilesystemStore(FilesystemResearchArtifactStore):
    def __init__(self, root: Path, barrier: threading.Barrier, prefix: str) -> None:
        super().__init__(root)
        self.barrier = barrier
        self.prefix = prefix

    def write(
        self,
        research_id: str,
        kind: ArtifactKind,
        payload: Any,
        *,
        logical_key: str | None = None,
    ) -> ArtifactRef:
        if logical_key is not None and logical_key.startswith(self.prefix):
            self.barrier.wait(timeout=5)
        return super().write(
            research_id,
            kind,
            payload,
            logical_key=logical_key,
        )


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


def contract(
    *, query: str = "AI 법안 처리 흐름과 정부 답변을 시간순으로 보여줘"
) -> ResearchContract:
    return ResearchContract(
        query=query,
        as_of=datetime(2026, 7, 13, 9, 30, tzinfo=UTC),
        date_from=date(2026, 1, 1),
        date_to=date(2026, 7, 13),
        assembly_term=22,
        committees=("과학기술정보방송통신위원회",),
        bill_numbers=("2219564",),
        evidence_types=(
            EvidenceType.BILLS,
            EvidenceType.SUBCOMMITTEE_MINUTES,
            EvidenceType.REVIEW_REPORTS,
            EvidenceType.GOVERNMENT_RESPONSES,
        ),
        intents=(ResearchIntent.TIMELINE, ResearchIntent.QUOTE_EVIDENCE),
        ordering="chronological",
        completeness="comprehensive",
    )


def coverage(*, complete: bool) -> CoverageLedger:
    requested = contract().evidence_types
    return CoverageLedger(
        requested=requested,
        entries=tuple(
            EvidenceCoverage(
                evidence_type=item,
                candidate_total=3,
                checked_count=3 if complete else 2,
                matched_count=2,
                pending_count=0 if complete else 1,
                gap_reasons=() if complete else ("one document is still unavailable",),
            )
            for item in requested
        ),
    )


def assert_job_store(_store: ResearchJobStore) -> None:
    """Static protocol assertion for mypy users."""


def event_refs(store: FilesystemResearchArtifactStore, research_id: str):
    return tuple(
        ref
        for ref in store.list(research_id, ArtifactKind.OUTCOME)
        if ref.logical_key is not None and ref.logical_key.startswith("job-event-v1-")
    )


def test_round_trip_is_canonical_and_survives_restart(tmp_path: Path) -> None:
    clock = Clock()
    artifacts = FilesystemResearchArtifactStore(tmp_path)
    first = ArtifactResearchJobStore(
        artifacts,
        now=clock,
        id_factory=lambda: "research_restart",
        creation_id_factory=lambda: "1" * 32,
    )
    assert_job_store(first)
    created = first.create(contract(query="  AI 법안 처리 흐름  "), "revision-1")
    assert created.contract.query == "AI 법안 처리 흐름"
    clock.value += timedelta(minutes=1)
    first.transition(created.id, JobStatus.RUNNING, stage="collecting", progress=0.5)
    clock.value += timedelta(minutes=1)
    completed = first.transition(
        created.id,
        JobStatus.COMPLETE,
        stage="complete",
        progress=1.0,
        coverage=coverage(complete=True),
    )

    restarted = ArtifactResearchJobStore(
        FilesystemResearchArtifactStore(tmp_path),
        now=clock,
    )
    restored = restarted.get(created.id)

    assert restored == completed
    assert restored is not None
    assert restored.status is JobStatus.COMPLETE
    assert restored.coverage == coverage(complete=True)
    assert len(event_refs(artifacts, created.id)) == 2


def test_job_fixed_state_and_history_skip_unrelated_outcome_reads(
    tmp_path: Path,
) -> None:
    clock = Clock()
    artifacts = CountingFilesystemStore(tmp_path)
    first = ArtifactResearchJobStore(
        artifacts,
        now=clock,
        id_factory=lambda: "research_bounded_history",
        creation_id_factory=lambda: "f" * 32,
    )
    job = first.create(contract(), "revision")
    clock.value += timedelta(seconds=1)
    running = first.transition(
        job.id,
        JobStatus.RUNNING,
        stage="collecting",
        progress=0.25,
    )
    for number in range(100):
        artifacts.write(job.id, ArtifactKind.OUTCOME, {"unrelated": number})
    artifacts.reset_counts()

    restored = ArtifactResearchJobStore(artifacts, now=clock).get(job.id)

    assert restored == running
    assert artifacts.list_calls == 1
    assert artifacts.logical_read_calls == 1
    # Only the single job event is read; 100 unrelated outcomes are not.
    assert artifacts.read_calls == 1

    artifacts.reset_counts()
    duplicate = ArtifactResearchJobStore(
        artifacts,
        now=clock,
        id_factory=lambda: job.id,
        creation_id_factory=lambda: "e" * 32,
    )
    with pytest.raises(ValueError, match="already exists"):
        duplicate.create(contract(), "revision")
    assert artifacts.list_calls == 0
    assert artifacts.read_calls == 0
    assert artifacts.logical_read_calls == 1


def test_duplicate_transition_intent_is_idempotent_across_instances(
    tmp_path: Path,
) -> None:
    setup_clock = Clock()
    setup = ArtifactResearchJobStore(
        FilesystemResearchArtifactStore(tmp_path),
        now=setup_clock,
        id_factory=lambda: "research_retry",
        creation_id_factory=lambda: "2" * 32,
    )
    job = setup.create(contract(), "revision")

    barrier = threading.Barrier(2)
    first_clock = Clock(setup_clock.value + timedelta(seconds=1))
    second_clock = Clock(setup_clock.value + timedelta(seconds=2))
    first = ArtifactResearchJobStore(
        RacingFilesystemStore(tmp_path, barrier, "job-event-v1-"),
        now=first_clock,
    )
    second = ArtifactResearchJobStore(
        RacingFilesystemStore(tmp_path, barrier, "job-event-v1-"),
        now=second_clock,
    )
    results = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def run(store: ArtifactResearchJobStore) -> None:
        try:
            value = store.transition(
                job.id,
                JobStatus.RUNNING,
                stage="collecting",
                progress=0.25,
            )
            with lock:
                results.append(value)
        except BaseException as exc:  # pragma: no cover - asserted below
            with lock:
                errors.append(exc)

    threads = (
        threading.Thread(target=run, args=(first,)),
        threading.Thread(target=run, args=(second,)),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]
    assert len(event_refs(FilesystemResearchArtifactStore(tmp_path), job.id)) == 1


def test_competing_complete_and_partial_events_resolve_to_verified_complete(
    tmp_path: Path,
) -> None:
    clock = Clock()
    setup = ArtifactResearchJobStore(
        FilesystemResearchArtifactStore(tmp_path),
        now=clock,
        id_factory=lambda: "research_terminal_race",
        creation_id_factory=lambda: "3" * 32,
    )
    job = setup.create(contract(), "revision")
    clock.value += timedelta(seconds=1)
    setup.transition(job.id, JobStatus.RUNNING, stage="collecting", progress=0.8)

    barrier = threading.Barrier(2)
    first = ArtifactResearchJobStore(
        RacingFilesystemStore(tmp_path, barrier, "job-event-v1-"),
        now=clock,
    )
    second = ArtifactResearchJobStore(
        RacingFilesystemStore(tmp_path, barrier, "job-event-v1-"),
        now=clock,
    )
    errors: list[BaseException] = []

    def finish(
        store: ArtifactResearchJobStore,
        status: JobStatus,
        ledger: CoverageLedger,
    ) -> None:
        try:
            store.transition(
                job.id,
                status,
                stage=status.value,
                progress=1.0,
                coverage=ledger,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = (
        threading.Thread(
            target=finish,
            args=(first, JobStatus.COMPLETE, coverage(complete=True)),
        ),
        threading.Thread(
            target=finish,
            args=(second, JobStatus.PARTIAL, coverage(complete=False)),
        ),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    final = ArtifactResearchJobStore(
        FilesystemResearchArtifactStore(tmp_path), now=clock
    ).get(job.id)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert final is not None
    assert final.status is JobStatus.COMPLETE
    assert final.coverage is not None and final.coverage.complete
    assert len(event_refs(FilesystemResearchArtifactStore(tmp_path), job.id)) == 3


def test_incomplete_coverage_can_never_be_written_as_complete(tmp_path: Path) -> None:
    clock = Clock()
    artifacts = FilesystemResearchArtifactStore(tmp_path)
    store = ArtifactResearchJobStore(
        artifacts,
        now=clock,
        id_factory=lambda: "research_no_false_complete",
        creation_id_factory=lambda: "4" * 32,
    )
    job = store.create(contract(), "revision")
    clock.value += timedelta(seconds=1)
    store.transition(job.id, JobStatus.RUNNING, stage="collecting", progress=0.8)
    before = event_refs(artifacts, job.id)

    with pytest.raises(ValueError, match="complete job requires complete evidence coverage"):
        store.transition(
            job.id,
            JobStatus.COMPLETE,
            stage="complete",
            progress=1.0,
            coverage=coverage(complete=False),
        )

    assert event_refs(artifacts, job.id) == before
    assert store.get(job.id).status is JobStatus.RUNNING  # type: ignore[union-attr]


def test_terminal_state_rejects_later_nonduplicate_transitions(tmp_path: Path) -> None:
    clock = Clock()
    store = ArtifactResearchJobStore(
        FilesystemResearchArtifactStore(tmp_path),
        now=clock,
        id_factory=lambda: "research_terminal",
        creation_id_factory=lambda: "5" * 32,
    )
    job = store.create(contract(), "revision")
    clock.value += timedelta(seconds=1)
    store.transition(job.id, JobStatus.RUNNING, stage="collecting", progress=0.5)
    clock.value += timedelta(seconds=1)
    store.transition(
        job.id,
        JobStatus.PARTIAL,
        stage="partial",
        progress=1.0,
        coverage=coverage(complete=False),
    )

    with pytest.raises(ValueError, match="invalid research job transition"):
        store.transition(job.id, JobStatus.RUNNING, stage="again", progress=0.9)


def test_ttl_expiry_is_derived_without_writing_an_event(tmp_path: Path) -> None:
    clock = Clock()
    artifacts = FilesystemResearchArtifactStore(tmp_path)
    store = ArtifactResearchJobStore(
        artifacts,
        now=clock,
        id_factory=lambda: "research_expiry",
        creation_id_factory=lambda: "6" * 32,
    )
    job = store.create(contract(), "revision", ttl=timedelta(minutes=5))
    refs_before = artifacts.list(job.id, ArtifactKind.OUTCOME)
    clock.value += timedelta(minutes=5)

    expired = ArtifactResearchJobStore(artifacts, now=clock).get(job.id)

    assert expired is not None
    assert expired.status is JobStatus.EXPIRED
    assert expired.stage == "expired"
    assert expired.updated_at == job.expires_at
    assert artifacts.list(job.id, ArtifactKind.OUTCOME) == refs_before
    with pytest.raises(ValueError, match="invalid research job transition"):
        store.transition(job.id, JobStatus.RUNNING, stage="late", progress=0.1)
    with pytest.raises(ValueError, match="derived"):
        store.transition(job.id, JobStatus.EXPIRED, stage="expired", progress=0.0)


def test_concurrent_duplicate_create_has_exactly_one_winner(tmp_path: Path) -> None:
    barrier = threading.Barrier(2)
    stores = (
        ArtifactResearchJobStore(
            RacingFilesystemStore(tmp_path, barrier, "job-state-v1"),
            id_factory=lambda: "research_create_race",
            creation_id_factory=lambda: "7" * 32,
        ),
        ArtifactResearchJobStore(
            RacingFilesystemStore(tmp_path, barrier, "job-state-v1"),
            id_factory=lambda: "research_create_race",
            creation_id_factory=lambda: "8" * 32,
        ),
    )
    outcomes: list[str] = []

    def create(store: ArtifactResearchJobStore) -> None:
        try:
            store.create(contract(), "revision")
            outcomes.append("created")
        except ValueError:
            outcomes.append("rejected")

    threads = tuple(threading.Thread(target=create, args=(store,)) for store in stores)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["created", "rejected"]
    assert stores[0].get("research_create_race") is not None


def test_job_artifacts_have_no_secret_fields_and_reject_secret_values(
    tmp_path: Path,
) -> None:
    artifacts = FilesystemResearchArtifactStore(tmp_path)
    store = ArtifactResearchJobStore(
        artifacts,
        id_factory=lambda: "research_secret_scan",
        creation_id_factory=lambda: "9" * 32,
    )
    job = store.create(contract(), "revision")

    with pytest.raises(SecretMaterialError):
        store.transition(
            job.id,
            JobStatus.FAILED,
            stage="failed",
            progress=0.0,
            error_code="upstream_rejected",
            error_message="sk-ant-api03-this-is-prohibited-secret-material",
        )

    combined = b"\n".join(path.read_bytes() for path in tmp_path.rglob("*.json")).lower()
    for forbidden in (
        b'"api_key"',
        b'"access_token"',
        b'"credential"',
        b'"capability"',
        b'"client_secret"',
    ):
        assert forbidden not in combined
    assert b"sk-ant-api03" not in combined
    assert event_refs(artifacts, job.id) == ()


def test_every_job_event_is_validated_and_unrelated_outcomes_are_ignored(
    tmp_path: Path,
) -> None:
    artifacts = FilesystemResearchArtifactStore(tmp_path)
    store = ArtifactResearchJobStore(
        artifacts,
        id_factory=lambda: "research_validation",
        creation_id_factory=lambda: "a" * 32,
    )
    job = store.create(contract(), "revision")
    artifacts.write_outcome(job.id, {"complete": False, "documents": 3})
    assert store.get(job.id) is not None

    bad_id = "b" * 64
    artifacts.write(
        job.id,
        ArtifactKind.OUTCOME,
        {
            "schema_version": 1,
            "artifact_type": "research_job_event_v1",
            "research_id": job.id,
            "event_id": bad_id,
        },
        logical_key=f"job-event-v1-{bad_id}",
    )

    with pytest.raises(ArtifactIntegrityError, match="event schema"):
        store.get(job.id)
