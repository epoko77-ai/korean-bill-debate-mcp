from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from kasm.research.contracts import (
    CoverageLedger,
    EvidenceCoverage,
    EvidenceType,
    ResearchContract,
    ResearchIntent,
)
from kasm.research.job_storage import SQLiteResearchJobStore
from kasm.research.jobs import JobStatus, ResearchJobStore


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 13, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def contract() -> ResearchContract:
    return ResearchContract(
        query="2026년 AI 법안을 시간순으로 보고 정부 답변을 인용해줘",
        as_of=datetime(2026, 7, 13, 9, 30, tzinfo=UTC),
        date_from=date(2026, 1, 1),
        date_to=date(2026, 7, 13),
        assembly_term=22,
        committees=("과학기술정보방송통신위원회",),
        bill_numbers=("2219564",),
        evidence_types=(
            EvidenceType.BILLS,
            EvidenceType.REVIEW_REPORTS,
            EvidenceType.GOVERNMENT_RESPONSES,
        ),
        intents=(ResearchIntent.TIMELINE, ResearchIntent.QUOTE_EVIDENCE),
        ordering="chronological",
        completeness="comprehensive",
    )


def complete_coverage() -> CoverageLedger:
    requested = contract().evidence_types
    return CoverageLedger(
        requested=requested,
        entries=tuple(
            EvidenceCoverage(
                evidence_type=item,
                candidate_total=2,
                checked_count=2,
                matched_count=1,
                gap_reasons=(),
            )
            for item in requested
        ),
    )


def partial_coverage() -> CoverageLedger:
    requested = contract().evidence_types
    return CoverageLedger(
        requested=requested,
        entries=tuple(
            EvidenceCoverage(
                evidence_type=item,
                candidate_total=2,
                checked_count=1,
                matched_count=1,
                pending_count=1,
                gap_reasons=("one official document remains pending",),
            )
            for item in requested
        ),
    )


def assert_store_protocol(_store: ResearchJobStore) -> None:
    """Static assertion that the SQLite adapter satisfies the public protocol."""


def test_round_trips_contract_intents_evidence_and_coverage_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "research.sqlite3"
    first = SQLiteResearchJobStore(path)
    assert_store_protocol(first)
    created = first.create(contract(), "index-2026-07-13")
    first.transition(created.id, JobStatus.RUNNING, stage="collecting", progress=0.4)
    completed = first.transition(
        created.id,
        JobStatus.COMPLETE,
        stage="complete",
        progress=1.0,
        coverage=complete_coverage(),
    )
    first.close()

    second = SQLiteResearchJobStore(path)
    restored = second.get(created.id)
    second.close()

    assert restored == completed
    assert restored is not None
    assert restored.contract == contract()
    assert restored.contract.intents == (
        ResearchIntent.TIMELINE,
        ResearchIntent.QUOTE_EVIDENCE,
    )
    assert restored.coverage == complete_coverage()


def test_duplicate_research_id_is_rejected_by_the_database(tmp_path: Path) -> None:
    store = SQLiteResearchJobStore(
        tmp_path / "research.sqlite3",
        id_factory=lambda: "research_fixed",
    )
    store.create(contract(), "revision-a")

    with pytest.raises(ValueError, match="already exists"):
        store.create(contract(), "revision-b")

    assert store.get("research_fixed") is not None


def test_get_atomically_expires_a_job_and_terminal_state_cannot_restart(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = SQLiteResearchJobStore(tmp_path / "research.sqlite3", now=clock)
    created = store.create(contract(), "revision", ttl=timedelta(minutes=5))
    clock.value += timedelta(minutes=5)

    with pytest.raises(ValueError, match="expired"):
        store.transition(
            created.id,
            JobStatus.RUNNING,
            stage="too-late",
            progress=0.1,
        )
    expired = store.get(created.id)

    assert expired is not None
    assert expired.status is JobStatus.EXPIRED
    assert expired.stage == "expired"
    with pytest.raises(ValueError, match="invalid research job transition"):
        store.transition(
            created.id,
            JobStatus.RUNNING,
            stage="restarted",
            progress=0.1,
        )


@pytest.mark.parametrize(
    ("terminal_status", "kwargs"),
    (
        (
            JobStatus.COMPLETE,
            {"coverage": complete_coverage()},
        ),
        (
            JobStatus.FAILED,
            {"error_code": "source_timeout", "error_message": "official source timed out"},
        ),
        (
            JobStatus.PARTIAL,
            {"coverage": partial_coverage()},
        ),
    ),
)
def test_terminal_jobs_reject_every_later_transition(
    tmp_path: Path,
    terminal_status: JobStatus,
    kwargs: dict[str, object],
) -> None:
    store = SQLiteResearchJobStore(tmp_path / f"{terminal_status}.sqlite3")
    job = store.create(contract(), "revision")
    store.transition(job.id, JobStatus.RUNNING, stage="running", progress=0.2)
    store.transition(
        job.id,
        terminal_status,
        stage=terminal_status.value,
        progress=(
            1.0
            if terminal_status in {JobStatus.COMPLETE, JobStatus.PARTIAL}
            else 0.2
        ),
        **kwargs,  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="invalid research job transition"):
        store.transition(job.id, JobStatus.RUNNING, stage="again", progress=0.5)


def test_competing_terminal_transitions_are_atomic_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "research.sqlite3"
    setup = SQLiteResearchJobStore(path)
    job = setup.create(contract(), "revision")
    setup.transition(job.id, JobStatus.RUNNING, stage="running", progress=0.5)
    setup.close()

    first = SQLiteResearchJobStore(path)
    second = SQLiteResearchJobStore(path)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    def complete() -> None:
        barrier.wait()
        try:
            first.transition(
                job.id,
                JobStatus.COMPLETE,
                stage="complete",
                progress=1.0,
                coverage=complete_coverage(),
            )
            outcome = "complete"
        except ValueError:
            outcome = "rejected"
        with outcomes_lock:
            outcomes.append(outcome)

    def fail() -> None:
        barrier.wait()
        try:
            second.transition(
                job.id,
                JobStatus.FAILED,
                stage="failed",
                progress=0.5,
                error_code="source_timeout",
            )
            outcome = "failed"
        except ValueError:
            outcome = "rejected"
        with outcomes_lock:
            outcomes.append(outcome)

    threads = (threading.Thread(target=complete), threading.Thread(target=fail))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes).count("rejected") == 1
    assert len(outcomes) == 2
    final = first.get(job.id)
    assert final is not None
    assert final.status in {JobStatus.COMPLETE, JobStatus.FAILED}


def test_dedicated_schema_has_no_credential_fields_or_generic_secret_payload(
    tmp_path: Path,
) -> None:
    path = tmp_path / "research.sqlite3"
    store = SQLiteResearchJobStore(path)
    job = store.create(contract(), "revision")
    store.close()

    connection = sqlite3.connect(path)
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(research_jobs_v1)")
    }
    contract_json = connection.execute(
        "SELECT contract_json FROM research_jobs_v1 WHERE research_id = ?",
        (job.id,),
    ).fetchone()[0]
    connection.close()

    forbidden_fragments = {"api", "oauth", "token", "llm", "secret", "credential"}
    assert not any(
        fragment in column.casefold()
        for column in columns
        for fragment in forbidden_fragments
    )
    assert set(json.loads(contract_json)) == {
        "query",
        "as_of",
        "date_from",
        "date_to",
        "assembly_term",
        "assembly_terms",
        "committees",
        "bill_numbers",
        "evidence_types",
        "intents",
        "ordering",
        "completeness",
    }
