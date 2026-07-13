"""Durable-job domain model for research that must outlive one MCP request."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from .contracts import CoverageLedger, ResearchContract


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    EXPIRED = "expired"


_TERMINAL = {JobStatus.COMPLETE, JobStatus.PARTIAL, JobStatus.FAILED, JobStatus.EXPIRED}
_TRANSITIONS = {
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.EXPIRED},
    JobStatus.RUNNING: {
        JobStatus.RUNNING,
        JobStatus.COMPLETE,
        JobStatus.PARTIAL,
        JobStatus.FAILED,
        JobStatus.EXPIRED,
    },
}


@dataclass(frozen=True, slots=True)
class ResearchJob:
    """Credential-free state for one resumable public-record investigation."""

    id: str
    contract: ResearchContract
    query_fingerprint: str
    index_revision: str
    status: JobStatus
    stage: str
    progress: float
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    coverage: CoverageLedger | None = None
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.query_fingerprint or not self.index_revision:
            raise ValueError("job id, query fingerprint, and index revision are required")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("job timestamps must be timezone-aware")
        if self.expires_at.tzinfo is None or self.expires_at <= self.created_at:
            raise ValueError("job expiry must be timezone-aware and after creation")
        if not 0.0 <= self.progress <= 1.0:
            raise ValueError("job progress must be between zero and one")
        if self.status is JobStatus.COMPLETE and (
            self.coverage is None or not self.coverage.complete
        ):
            raise ValueError("a complete job requires complete evidence coverage")
        if self.status is JobStatus.PARTIAL and self.coverage is None:
            raise ValueError("a partial job requires explicit evidence coverage")
        if self.coverage is not None and self.coverage.requested != self.contract.evidence_types:
            raise ValueError("job coverage must include every evidence type in its contract")
        if self.status in {JobStatus.COMPLETE, JobStatus.PARTIAL} and self.progress != 1.0:
            raise ValueError("a completed or partial job must report progress 1.0")
        if self.status is JobStatus.FAILED and not self.error_code:
            raise ValueError("a failed job requires an error code")

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL

    def public_payload(self) -> dict[str, object]:
        return {
            "research_id": self.id,
            "status": self.status.value,
            "stage": self.stage,
            "progress": self.progress,
            "index_revision": self.index_revision,
            "interpreted_scope": self.contract.canonical_payload(),
            "coverage": self.coverage.to_dict() if self.coverage else None,
            "error": (
                {"code": self.error_code, "message": self.error_message}
                if self.error_code
                else None
            ),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


class ResearchJobStore(Protocol):
    def create(
        self,
        contract: ResearchContract,
        index_revision: str,
        *,
        ttl: timedelta = timedelta(hours=1),
    ) -> ResearchJob: ...

    def get(self, research_id: str) -> ResearchJob | None: ...

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
    ) -> ResearchJob: ...


class InMemoryResearchJobStore:
    """Thread-safe reference adapter; production stores implement the same contract."""

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._jobs: dict[str, ResearchJob] = {}
        self._lock = threading.RLock()

    def create(
        self,
        contract: ResearchContract,
        index_revision: str,
        *,
        ttl: timedelta = timedelta(hours=1),
    ) -> ResearchJob:
        if ttl <= timedelta(0):
            raise ValueError("job ttl must be positive")
        created_at = self._now()
        identifier = f"research_{uuid.uuid4().hex}"
        job = ResearchJob(
            id=identifier,
            contract=contract,
            query_fingerprint=contract.fingerprint(index_revision),
            index_revision=index_revision,
            status=JobStatus.QUEUED,
            stage="queued",
            progress=0.0,
            created_at=created_at,
            updated_at=created_at,
            expires_at=created_at + ttl,
        )
        with self._lock:
            self._jobs[identifier] = job
        return job

    def get(self, research_id: str) -> ResearchJob | None:
        with self._lock:
            job = self._jobs.get(research_id)
            if job is None:
                return None
            if not job.terminal and self._now() >= job.expires_at:
                job = replace(
                    job,
                    status=JobStatus.EXPIRED,
                    stage="expired",
                    updated_at=self._now(),
                )
                self._jobs[research_id] = job
            return job

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
        with self._lock:
            current = self.get(research_id)
            if current is None:
                raise LookupError(f"research job not found: {research_id}")
            allowed = _TRANSITIONS.get(current.status, set())
            if status not in allowed:
                raise ValueError(f"invalid research job transition: {current.status} -> {status}")
            job = replace(
                current,
                status=status,
                stage=stage,
                progress=progress,
                coverage=coverage,
                error_code=error_code,
                error_message=error_message,
                updated_at=self._now(),
            )
            self._jobs[research_id] = job
            return job
