"""SQLite persistence for resumable, credential-free research jobs."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .contracts import (
    CoverageLedger,
    EvidenceCoverage,
    EvidenceType,
    ResearchContract,
    ResearchIntent,
)
from .jobs import JobStatus, ResearchJob

_TABLE = "research_jobs_v1"
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

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    research_id TEXT PRIMARY KEY,
    contract_json TEXT NOT NULL,
    query_fingerprint TEXT NOT NULL,
    index_revision TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    progress REAL NOT NULL CHECK (progress >= 0.0 AND progress <= 1.0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    coverage_json TEXT,
    error_code TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS research_jobs_v1_expiry
    ON {_TABLE}(status, expires_at);
"""


class SQLiteResearchJobStore:
    """Thread-safe SQLite implementation of :class:`ResearchJobStore`.

    The table contains only public research scope and execution state.  It has
    no generic metadata/blob column, so credentials cannot accidentally become
    part of this persistence contract.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        now: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        self.path = str(path)
        self._now = now or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: f"research_{uuid.uuid4().hex}")
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
            timeout=busy_timeout_ms / 1000,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        self._connection.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self.initialize()

    def initialize(self) -> None:
        """Create the dedicated job table without touching the main app schema."""

        with self._lock:
            self._connection.executescript(_SCHEMA)

    def create(
        self,
        contract: ResearchContract,
        index_revision: str,
        *,
        ttl: timedelta = timedelta(hours=1),
    ) -> ResearchJob:
        if ttl <= timedelta(0):
            raise ValueError("job ttl must be positive")
        created_at = self._checked_now()
        identifier = self._id_factory()
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
        try:
            with self._write_transaction() as connection:
                connection.execute(
                    f"""
                    INSERT INTO {_TABLE} (
                        research_id, contract_json, query_fingerprint, index_revision,
                        status, stage, progress, created_at, updated_at, expires_at,
                        coverage_json, error_code, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _job_values(job),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"research id already exists: {identifier}") from exc
        return job

    def get(self, research_id: str) -> ResearchJob | None:
        """Return a job, atomically materializing TTL expiry when necessary."""

        # Most status polls are read-only and should not contend for SQLite's
        # single-writer lock.  Re-check under BEGIN IMMEDIATE only when expiry
        # might require a write.
        with self._lock:
            row = self._connection.execute(
                f"SELECT * FROM {_TABLE} WHERE research_id = ?",
                (research_id,),
            ).fetchone()
        if row is None:
            return None
        job = _job_from_row(row)
        if job.status in _TERMINAL or self._checked_now() < job.expires_at:
            return job

        with self._write_transaction() as connection:
            row = connection.execute(
                f"SELECT * FROM {_TABLE} WHERE research_id = ?",
                (research_id,),
            ).fetchone()
            if row is None:
                return None
            job = _job_from_row(row)
            now = self._checked_now()
            if job.status not in _TERMINAL and now >= job.expires_at:
                job = replace(
                    job,
                    status=JobStatus.EXPIRED,
                    stage="expired",
                    updated_at=now,
                )
                _update_job(connection, job, expected_status=row["status"])
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
        """Validate and commit a state transition in one write transaction."""

        expired_during_transition = False
        with self._write_transaction() as connection:
            row = connection.execute(
                f"SELECT * FROM {_TABLE} WHERE research_id = ?",
                (research_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"research job not found: {research_id}")
            current = _job_from_row(row)
            now = self._checked_now()
            if current.status not in _TERMINAL and now >= current.expires_at:
                expired = replace(
                    current,
                    status=JobStatus.EXPIRED,
                    stage="expired",
                    updated_at=now,
                )
                _update_job(connection, expired, expected_status=current.status.value)
                current = expired
                expired_during_transition = True

            allowed = _TRANSITIONS.get(current.status, set())
            if status not in allowed and not expired_during_transition:
                raise ValueError(
                    f"invalid research job transition: {current.status} -> {status}"
                )
            if not expired_during_transition:
                current = replace(
                    current,
                    status=status,
                    stage=stage,
                    progress=progress,
                    coverage=coverage,
                    error_code=error_code,
                    error_message=error_message,
                    updated_at=now,
                )
                _update_job(connection, current, expected_status=row["status"])

        if expired_during_transition:
            raise ValueError(f"invalid research job transition: expired -> {status}")
        return current

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SQLiteResearchJobStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        with suppress(AttributeError, sqlite3.ProgrammingError):
            self._connection.close()

    def _checked_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("job clock must return a timezone-aware datetime")
        return value

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()


def _update_job(
    connection: sqlite3.Connection,
    job: ResearchJob,
    *,
    expected_status: str,
) -> None:
    values = _job_values(job)
    result = connection.execute(
        f"""
        UPDATE {_TABLE} SET
            contract_json = ?, query_fingerprint = ?, index_revision = ?,
            status = ?, stage = ?, progress = ?, created_at = ?, updated_at = ?,
            expires_at = ?, coverage_json = ?, error_code = ?, error_message = ?
        WHERE research_id = ? AND status = ?
        """,
        (*values[1:], values[0], expected_status),
    )
    if result.rowcount != 1:
        raise RuntimeError("research job changed during atomic transition")


def _job_values(job: ResearchJob) -> tuple[object, ...]:
    return (
        job.id,
        _json_dump(_contract_to_payload(job.contract)),
        job.query_fingerprint,
        job.index_revision,
        job.status.value,
        job.stage,
        job.progress,
        job.created_at.isoformat(),
        job.updated_at.isoformat(),
        job.expires_at.isoformat(),
        _json_dump(_coverage_to_payload(job.coverage)) if job.coverage else None,
        job.error_code,
        job.error_message,
    )


def _job_from_row(row: sqlite3.Row) -> ResearchJob:
    contract_payload = _json_load_object(row["contract_json"], "contract")
    coverage_value = row["coverage_json"]
    return ResearchJob(
        id=str(row["research_id"]),
        contract=_contract_from_payload(contract_payload),
        query_fingerprint=str(row["query_fingerprint"]),
        index_revision=str(row["index_revision"]),
        status=JobStatus(str(row["status"])),
        stage=str(row["stage"]),
        progress=float(row["progress"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        expires_at=datetime.fromisoformat(str(row["expires_at"])),
        coverage=(
            _coverage_from_payload(_json_load_object(coverage_value, "coverage"))
            if coverage_value is not None
            else None
        ),
        error_code=str(row["error_code"]) if row["error_code"] is not None else None,
        error_message=(
            str(row["error_message"]) if row["error_message"] is not None else None
        ),
    )


def _contract_to_payload(contract: ResearchContract) -> dict[str, object]:
    return {
        "query": contract.query,
        "as_of": contract.as_of.isoformat(),
        "date_from": contract.date_from.isoformat() if contract.date_from else None,
        "date_to": contract.date_to.isoformat() if contract.date_to else None,
        "assembly_term": contract.assembly_term,
        "assembly_terms": list(contract.assembly_terms),
        "committees": list(contract.committees),
        "bill_numbers": list(contract.bill_numbers),
        "representative_proposer_names": list(
            contract.representative_proposer_names
        ),
        "co_proposer_names": list(contract.co_proposer_names),
        "proposer_names": list(contract.proposer_names),
        "evidence_types": [item.value for item in contract.evidence_types],
        "intents": [item.value for item in contract.intents],
        "ordering": contract.ordering,
        "completeness": contract.completeness,
    }


def _contract_from_payload(payload: dict[str, Any]) -> ResearchContract:
    date_from = payload.get("date_from")
    date_to = payload.get("date_to")
    return ResearchContract(
        query=str(payload["query"]),
        as_of=datetime.fromisoformat(str(payload["as_of"])),
        date_from=date.fromisoformat(str(date_from)) if date_from is not None else None,
        date_to=date.fromisoformat(str(date_to)) if date_to is not None else None,
        assembly_term=int(payload["assembly_term"]),
        assembly_terms=tuple(
            int(item)
            for item in payload.get(
                "assembly_terms",
                (payload["assembly_term"],),
            )
        ),
        committees=tuple(str(item) for item in payload["committees"]),
        bill_numbers=tuple(str(item) for item in payload["bill_numbers"]),
        representative_proposer_names=tuple(
            str(item) for item in payload.get("representative_proposer_names", ())
        ),
        co_proposer_names=tuple(
            str(item) for item in payload.get("co_proposer_names", ())
        ),
        proposer_names=tuple(
            str(item) for item in payload.get("proposer_names", ())
        ),
        evidence_types=tuple(EvidenceType(str(item)) for item in payload["evidence_types"]),
        intents=tuple(ResearchIntent(str(item)) for item in payload["intents"]),
        ordering=str(payload["ordering"]),
        completeness=str(payload["completeness"]),
    )


def _coverage_to_payload(coverage: CoverageLedger) -> dict[str, object]:
    return {
        "requested": [item.value for item in coverage.requested],
        "entries": [
            {
                "evidence_type": entry.evidence_type.value,
                "candidate_total": entry.candidate_total,
                "checked_count": entry.checked_count,
                "matched_count": entry.matched_count,
                "failed_count": entry.failed_count,
                "pending_count": entry.pending_count,
                "gap_reasons": list(entry.gap_reasons),
            }
            for entry in coverage.entries
        ],
    }


def _coverage_from_payload(payload: dict[str, Any]) -> CoverageLedger:
    entries_value = payload["entries"]
    if not isinstance(entries_value, list):
        raise ValueError("coverage entries must be a list")
    if any(not isinstance(entry, dict) for entry in entries_value):
        raise ValueError("each coverage entry must be an object")
    return CoverageLedger(
        requested=tuple(EvidenceType(str(item)) for item in payload["requested"]),
        entries=tuple(
            EvidenceCoverage(
                evidence_type=EvidenceType(str(entry["evidence_type"])),
                candidate_total=(
                    int(entry["candidate_total"])
                    if entry["candidate_total"] is not None
                    else None
                ),
                checked_count=int(entry["checked_count"]),
                matched_count=int(entry["matched_count"]),
                failed_count=int(entry["failed_count"]),
                pending_count=int(entry["pending_count"]),
                gap_reasons=tuple(str(item) for item in entry["gap_reasons"]),
            )
            for entry in entries_value
        ),
    )


def _json_dump(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load_object(value: object, label: str) -> dict[str, Any]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise ValueError(f"stored {label} payload must be an object")
    return decoded
