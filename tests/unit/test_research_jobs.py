from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kasm.research.contracts import (
    CoverageLedger,
    EvidenceCoverage,
    EvidenceType,
    ResearchContract,
)
from kasm.research.jobs import InMemoryResearchJobStore, JobStatus


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 13, 13, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def contract(*evidence_types: EvidenceType) -> ResearchContract:
    return ResearchContract(
        query="2219564번 의안의 상태·회의록·발언을 연결해줘",
        as_of=datetime(2026, 7, 13, 12, tzinfo=UTC),
        bill_numbers=("2219564",),
        evidence_types=evidence_types or tuple(EvidenceType),
    )


def complete_ledger() -> CoverageLedger:
    requested = (EvidenceType.BILLS, EvidenceType.SPEECHES)
    return CoverageLedger(
        requested=requested,
        entries=(
            EvidenceCoverage(EvidenceType.BILLS, 4, 4, 4),
            EvidenceCoverage(EvidenceType.SPEECHES, 12, 12, 7),
        ),
    )


def test_research_job_has_no_credential_field_and_exposes_scope() -> None:
    store = InMemoryResearchJobStore(now=Clock())

    job = store.create(contract(), "index-r1")

    payload = job.public_payload()
    assert payload["status"] == "queued"
    assert payload["interpreted_scope"]["bill_numbers"] == ["2219564"]  # type: ignore[index]
    assert "api_key" not in repr(job)
    assert "token" not in payload


def test_research_job_requires_valid_transitions_and_complete_coverage() -> None:
    store = InMemoryResearchJobStore(now=Clock())
    job = store.create(
        contract(EvidenceType.BILLS, EvidenceType.SPEECHES), "index-r1"
    )
    running = store.transition(
        job.id,
        JobStatus.RUNNING,
        stage="retrieving_documents",
        progress=0.4,
    )

    assert running.progress == 0.4
    completed = store.transition(
        job.id,
        JobStatus.COMPLETE,
        stage="complete",
        progress=1.0,
        coverage=complete_ledger(),
    )
    assert completed.terminal is True
    with pytest.raises(ValueError, match="invalid research job transition"):
        store.transition(
            job.id,
            JobStatus.RUNNING,
            stage="restart",
            progress=0.0,
        )


def test_partial_job_must_preserve_coverage_gaps() -> None:
    store = InMemoryResearchJobStore(now=Clock())
    job = store.create(contract(EvidenceType.REVIEW_REPORTS), "index-r1")
    store.transition(job.id, JobStatus.RUNNING, stage="parsing", progress=0.8)
    partial = CoverageLedger(
        requested=(EvidenceType.REVIEW_REPORTS,),
        entries=(
            EvidenceCoverage(
                EvidenceType.REVIEW_REPORTS,
                3,
                2,
                2,
                failed_count=1,
                gap_reasons=("one PDF was corrupt",),
            ),
        ),
    )

    result = store.transition(
        job.id,
        JobStatus.PARTIAL,
        stage="partial",
        progress=1.0,
        coverage=partial,
    )

    assert result.public_payload()["coverage"]["complete"] is False  # type: ignore[index]


def test_job_expires_without_being_claimed() -> None:
    clock = Clock()
    store = InMemoryResearchJobStore(now=clock)
    job = store.create(contract(), "index-r1", ttl=timedelta(minutes=5))

    clock.value += timedelta(minutes=6)

    expired = store.get(job.id)
    assert expired is not None
    assert expired.status is JobStatus.EXPIRED
