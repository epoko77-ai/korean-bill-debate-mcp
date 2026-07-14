"""Append-only research jobs backed by immutable execution artifacts.

The adapter deliberately avoids a mutable "current job" object.  A canonical
initial state and every transition intent are stored at write-once logical
paths.  Readers validate the complete event DAG and deterministically select a
safe current leaf, so separate serverless instances can race without turning
partial coverage into a false ``complete`` state.

Only public research scope and state are persisted.  Credentials and generic
payload fields are not part of this schema, and the underlying artifact store
recursively rejects secret material before writing bytes.
"""

from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .artifacts import (
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactKind,
    ArtifactRef,
    BaseResearchArtifactStore,
    canonical_hash,
)
from .contracts import (
    CoverageLedger,
    EvidenceCoverage,
    EvidenceType,
    ResearchContract,
    ResearchIntent,
)
from .jobs import JobStatus, ResearchJob

_SCHEMA_VERSION = 1
_INITIAL_TYPE = "research_job_initial_v1"
_EVENT_TYPE = "research_job_event_v1"
_INITIAL_KEY = "job-state-v1"
_EVENT_KEY_PREFIX = "job-event-v1-"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL = {JobStatus.COMPLETE, JobStatus.PARTIAL, JobStatus.FAILED, JobStatus.EXPIRED}
_TRANSITIONS = {
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.FAILED},
    JobStatus.RUNNING: {
        JobStatus.RUNNING,
        JobStatus.COMPLETE,
        JobStatus.PARTIAL,
        JobStatus.FAILED,
    },
}


@dataclass(frozen=True, slots=True)
class _TransitionTarget:
    status: JobStatus
    stage: str
    progress: float
    coverage: CoverageLedger | None
    error_code: str | None
    error_message: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "stage": self.stage,
            "progress": self.progress,
            "coverage": _coverage_to_payload(self.coverage),
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class _StoredEvent:
    event_id: str
    parent_state_hash: str
    result_state_hash: str
    occurred_at: datetime
    target: _TransitionTarget


@dataclass(frozen=True, slots=True)
class _History:
    initial: ResearchJob
    events: tuple[_StoredEvent, ...]
    artifact_kind: ArtifactKind


class ArtifactResearchJobStore:
    """Serverless-safe :class:`ResearchJobStore` over write-once artifacts.

    ``transition`` has no caller-supplied event identifier in the public
    protocol, so this adapter derives one from the canonical transition intent.
    Retrying the same intent is therefore idempotent even after another process
    has advanced the job.  Distinct concurrent intents remain as immutable DAG
    branches; readers prefer verified complete coverage, then explicit partial
    coverage, then failure, and never manufacture ``complete`` from an
    incomplete ledger.
    """

    def __init__(
        self,
        artifacts: BaseResearchArtifactStore,
        *,
        now: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        creation_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.artifacts = artifacts
        self._now = now or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: f"research_{uuid.uuid4().hex}")
        self._creation_id_factory = creation_id_factory or (lambda: uuid.uuid4().hex)
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
        revision = index_revision.strip()
        if not revision:
            raise ValueError("index_revision is required")
        canonical_contract = _contract_from_payload(contract.canonical_payload())
        created_at = self._checked_now()
        research_id = self._id_factory()
        creation_id = self._creation_id_factory()
        if not re.fullmatch(r"[0-9a-f]{32}", creation_id):
            raise ValueError("creation id must be 32 lowercase hexadecimal characters")
        job = ResearchJob(
            id=research_id,
            contract=canonical_contract,
            query_fingerprint=canonical_contract.fingerprint(revision),
            index_revision=revision,
            status=JobStatus.QUEUED,
            stage="queued",
            progress=0.0,
            created_at=created_at,
            updated_at=created_at,
            expires_at=created_at + ttl,
        )
        payload = _initial_payload(job, creation_id)
        with self._lock:
            if self._initial_ref(research_id) is not None:
                raise ValueError(f"research id already exists: {research_id}")
            try:
                self.artifacts.write(
                    research_id,
                    ArtifactKind.JOB_STATE,
                    payload,
                    logical_key=_INITIAL_KEY,
                )
            except ArtifactConflictError as exc:
                raise ValueError(f"research id already exists: {research_id}") from exc
        return job

    def get(self, research_id: str) -> ResearchJob | None:
        history = self._load_history(research_id)
        if history is None:
            return None
        current, _state_hash = _select_current(history)
        now = self._checked_now()
        if current.status not in _TERMINAL and now >= current.expires_at:
            return replace(
                current,
                status=JobStatus.EXPIRED,
                stage="expired",
                updated_at=max(current.expires_at, current.updated_at),
            )
        return current

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
        try:
            normalized_status = JobStatus(status)
        except ValueError as exc:
            raise ValueError("unknown research job status") from exc
        if normalized_status is JobStatus.EXPIRED:
            raise ValueError("expired status is derived from the job TTL")
        if not stage.strip():
            raise ValueError("job stage must not be empty")
        target = _TransitionTarget(
            normalized_status,
            stage.strip(),
            progress,
            coverage,
            error_code.strip() if error_code is not None else None,
            error_message.strip() if error_message is not None else None,
        )
        event_id = _event_id(research_id, target)

        with self._lock:
            history = self._load_history(research_id)
            if history is None:
                raise LookupError(f"research job not found: {research_id}")
            if any(item.event_id == event_id for item in history.events):
                current, _ = _select_current(history)
                return _derive_expiry(current, self._checked_now())

            current, parent_state_hash = _select_current(history)
            now = self._checked_now()
            current = _derive_expiry(current, now)
            if current.status in _TERMINAL:
                raise ValueError(
                    f"invalid research job transition: {current.status} -> {normalized_status}"
                )
            candidate = _apply_target(current, target, now)
            result_state_hash = _state_hash(candidate)
            event = _StoredEvent(
                event_id=event_id,
                parent_state_hash=parent_state_hash,
                result_state_hash=result_state_hash,
                occurred_at=now,
                target=target,
            )
            try:
                self.artifacts.write(
                    research_id,
                    history.artifact_kind,
                    _event_payload(research_id, event),
                    logical_key=f"{_EVENT_KEY_PREFIX}{event_id}",
                )
            except ArtifactConflictError:
                # Another instance won the write-once race for this derived
                # event ID.  Accept it only after the complete history parser
                # proves that it represents the identical transition intent.
                raced = self._load_history(research_id)
                if raced is None or not any(
                    item.event_id == event_id for item in raced.events
                ):
                    raise ArtifactIntegrityError(
                        "conflicting research job event could not be validated"
                    ) from None

            refreshed = self._load_history(research_id)
            if refreshed is None:
                raise ArtifactIntegrityError("research job disappeared after transition")
            result, _ = _select_current(refreshed)
            return _derive_expiry(result, self._checked_now())

    def _load_history(self, research_id: str) -> _History | None:
        artifact_kind = ArtifactKind.JOB_STATE
        initial_artifact = self.artifacts.read_logical(
            research_id,
            artifact_kind,
            _INITIAL_KEY,
        )
        if initial_artifact is None:
            # Deployments before the dedicated job-state namespace stored the
            # state DAG beside document outcomes.  Keep those jobs readable
            # and continue their event history in the legacy namespace, while
            # ensuring newly-created jobs never list unrelated outcomes.
            artifact_kind = ArtifactKind.OUTCOME
            initial_artifact = self.artifacts.read_logical(
                research_id,
                artifact_kind,
                _INITIAL_KEY,
            )
        if initial_artifact is None:
            orphan_refs = self.artifacts.list(research_id, ArtifactKind.JOB_STATE)
            if any(
                ref.logical_key is not None
                and ref.logical_key.startswith(_EVENT_KEY_PREFIX)
                for ref in orphan_refs
            ):
                raise ArtifactIntegrityError("research job events have no initial state")
            return None

        refs = self.artifacts.list(research_id, artifact_kind)
        event_refs = tuple(
            ref
            for ref in refs
            if ref.logical_key is not None
            and ref.logical_key.startswith(_EVENT_KEY_PREFIX)
        )
        initial_payload = initial_artifact.payload
        if not isinstance(initial_payload, Mapping):
            raise ArtifactIntegrityError("research job artifact payload must be an object")
        initial = _initial_from_payload(initial_payload, research_id)
        events = tuple(
            sorted(
                (
                    _event_from_payload(
                        self._read_payload(ref),
                        research_id,
                        ref.logical_key or "",
                    )
                    for ref in event_refs
                ),
                key=lambda item: item.event_id,
            )
        )
        identifiers = [item.event_id for item in events]
        if len(identifiers) != len(set(identifiers)):
            raise ArtifactIntegrityError("research job contains duplicate event identifiers")
        history = _History(initial, events, artifact_kind)
        _validate_history(history)
        return history

    def _initial_ref(self, research_id: str) -> ArtifactRef | None:
        stored = self.artifacts.read_logical(
            research_id,
            ArtifactKind.JOB_STATE,
            _INITIAL_KEY,
        )
        if stored is None:
            stored = self.artifacts.read_logical(
                research_id,
                ArtifactKind.OUTCOME,
                _INITIAL_KEY,
            )
        return stored.ref if stored is not None else None

    def _read_payload(self, ref: ArtifactRef) -> Mapping[str, Any]:
        stored = self.artifacts.read(ref)
        if stored is None:
            raise ArtifactIntegrityError("listed research job artifact is missing")
        if not isinstance(stored.payload, Mapping):
            raise ArtifactIntegrityError("research job artifact payload must be an object")
        return stored.payload

    def _checked_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("job clock must return a timezone-aware datetime")
        return value


def _derive_expiry(job: ResearchJob, now: datetime) -> ResearchJob:
    if job.status not in _TERMINAL and now >= job.expires_at:
        return replace(
            job,
            status=JobStatus.EXPIRED,
            stage="expired",
            updated_at=max(job.expires_at, job.updated_at),
        )
    return job


def _apply_target(
    current: ResearchJob,
    target: _TransitionTarget,
    occurred_at: datetime,
) -> ResearchJob:
    allowed = _TRANSITIONS.get(current.status, set())
    if target.status not in allowed:
        raise ValueError(f"invalid research job transition: {current.status} -> {target.status}")
    if occurred_at.tzinfo is None or occurred_at < current.updated_at:
        raise ValueError("job transition time must be timezone-aware and monotonic")
    if occurred_at >= current.expires_at:
        raise ValueError("job transition occurred after the derived TTL expiry")
    if target.error_code is not None and target.status is not JobStatus.FAILED:
        raise ValueError("error_code is only valid for a failed job")
    if target.error_message is not None and target.status is not JobStatus.FAILED:
        raise ValueError("error_message is only valid for a failed job")
    if target.error_message is not None and not target.error_message:
        raise ValueError("error_message must not be empty")
    return replace(
        current,
        status=target.status,
        stage=target.stage,
        progress=target.progress,
        coverage=target.coverage,
        error_code=target.error_code,
        error_message=target.error_message,
        updated_at=occurred_at,
    )


def _validate_history(history: _History) -> None:
    initial_hash = _state_hash(history.initial)
    states: dict[str, ResearchJob] = {initial_hash: history.initial}
    pending = list(history.events)
    while pending:
        progressed = False
        remaining: list[_StoredEvent] = []
        for event in pending:
            parent = states.get(event.parent_state_hash)
            if parent is None:
                remaining.append(event)
                continue
            try:
                result = _apply_target(parent, event.target, event.occurred_at)
            except ValueError as exc:
                raise ArtifactIntegrityError("research job contains an invalid transition") from exc
            if _state_hash(result) != event.result_state_hash:
                raise ArtifactIntegrityError("research job event result hash does not match")
            previous = states.get(event.result_state_hash)
            if previous is not None and previous != result:
                raise ArtifactIntegrityError("research job state hash collision detected")
            states[event.result_state_hash] = result
            progressed = True
        if not progressed:
            raise ArtifactIntegrityError("research job event graph has a dangling parent")
        pending = remaining


def _select_current(history: _History) -> tuple[ResearchJob, str]:
    initial_hash = _state_hash(history.initial)
    states: dict[str, ResearchJob] = {initial_hash: history.initial}
    parents: set[str] = set()
    pending = list(history.events)
    while pending:
        progressed = False
        remaining: list[_StoredEvent] = []
        for event in pending:
            parent = states.get(event.parent_state_hash)
            if parent is None:
                remaining.append(event)
                continue
            result = _apply_target(parent, event.target, event.occurred_at)
            states[event.result_state_hash] = result
            parents.add(event.parent_state_hash)
            progressed = True
        if not progressed:
            raise ArtifactIntegrityError("research job event graph has a dangling parent")
        pending = remaining
    leaves = tuple(
        (state_hash, job)
        for state_hash, job in states.items()
        if state_hash not in parents
    )
    if not leaves:
        raise ArtifactIntegrityError("research job event graph has no current state")
    state_hash, job = max(
        leaves,
        key=lambda item: (*_safe_state_priority(item[1]), item[0]),
    )
    return job, state_hash


def _safe_state_priority(job: ResearchJob) -> tuple[int, int, int, int, int, int, float]:
    status_priority = {
        JobStatus.QUEUED: 0,
        JobStatus.RUNNING: 1,
        JobStatus.EXPIRED: 2,
        JobStatus.FAILED: 3,
        JobStatus.PARTIAL: 4,
        JobStatus.COMPLETE: 5,
    }[job.status]
    coverage = job.coverage
    if coverage is None:
        return (status_priority, 0, 0, 0, 0, 0, job.progress)
    checked = sum(item.checked_count for item in coverage.entries)
    failed = sum(item.failed_count for item in coverage.entries)
    pending = sum(item.pending_count for item in coverage.entries)
    gaps = sum(len(item.gap_reasons) for item in coverage.entries)
    return (
        status_priority,
        int(coverage.complete),
        checked,
        -failed,
        -pending,
        -gaps,
        job.progress,
    )


def _initial_payload(job: ResearchJob, creation_id: str) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "artifact_type": _INITIAL_TYPE,
        "research_id": job.id,
        "creation_id": creation_id,
        "contract": job.contract.canonical_payload(),
        "query_fingerprint": job.query_fingerprint,
        "index_revision": job.index_revision,
        "created_at": job.created_at.isoformat(),
        "expires_at": job.expires_at.isoformat(),
        "initial_state": _dynamic_state_payload(job),
        "initial_state_hash": _state_hash(job),
    }


def _initial_from_payload(
    payload: Mapping[str, Any], research_id: str
) -> ResearchJob:
    expected = {
        "schema_version",
        "artifact_type",
        "research_id",
        "creation_id",
        "contract",
        "query_fingerprint",
        "index_revision",
        "created_at",
        "expires_at",
        "initial_state",
        "initial_state_hash",
    }
    if set(payload) != expected:
        raise ArtifactIntegrityError("research job initial-state schema is invalid")
    if payload.get("schema_version") != _SCHEMA_VERSION or payload.get(
        "artifact_type"
    ) != _INITIAL_TYPE:
        raise ArtifactIntegrityError("research job initial-state version is invalid")
    if payload.get("research_id") != research_id:
        raise ArtifactIntegrityError("research job initial-state identity does not match")
    creation_id = payload.get("creation_id")
    if not isinstance(creation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", creation_id):
        raise ArtifactIntegrityError("research job creation identity is invalid")
    contract_value = payload.get("contract")
    state_value = payload.get("initial_state")
    if not isinstance(contract_value, Mapping) or not isinstance(state_value, Mapping):
        raise ArtifactIntegrityError("research job initial-state objects are invalid")
    try:
        contract = _contract_from_payload(contract_value)
        created_at = _parse_datetime(payload.get("created_at"), "created_at")
        expires_at = _parse_datetime(payload.get("expires_at"), "expires_at")
        index_revision = _required_string(payload.get("index_revision"), "index_revision")
        fingerprint = _required_hash(payload.get("query_fingerprint"), "query_fingerprint")
        job = _job_from_dynamic_state(
            research_id=research_id,
            contract=contract,
            query_fingerprint=fingerprint,
            index_revision=index_revision,
            created_at=created_at,
            expires_at=expires_at,
            payload=state_value,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactIntegrityError("research job initial state is invalid") from exc
    if job.status is not JobStatus.QUEUED or job.stage != "queued" or job.progress != 0.0:
        raise ArtifactIntegrityError("research job must begin in queued state")
    if job.updated_at != job.created_at or job.coverage is not None:
        raise ArtifactIntegrityError("research job initial state contains mutable results")
    if job.error_code is not None or job.error_message is not None:
        raise ArtifactIntegrityError("research job initial state contains an error")
    if contract.fingerprint(index_revision) != fingerprint:
        raise ArtifactIntegrityError("research job query fingerprint does not match")
    stored_state_hash = _required_hash(
        payload.get("initial_state_hash"), "initial_state_hash"
    )
    if _state_hash(job) != stored_state_hash:
        raise ArtifactIntegrityError("research job initial state hash does not match")
    return job


def _event_payload(research_id: str, event: _StoredEvent) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "artifact_type": _EVENT_TYPE,
        "research_id": research_id,
        "event_id": event.event_id,
        "parent_state_hash": event.parent_state_hash,
        "result_state_hash": event.result_state_hash,
        "occurred_at": event.occurred_at.isoformat(),
        "target_state": event.target.to_payload(),
    }


def _event_from_payload(
    payload: Mapping[str, Any], research_id: str, logical_key: str
) -> _StoredEvent:
    expected = {
        "schema_version",
        "artifact_type",
        "research_id",
        "event_id",
        "parent_state_hash",
        "result_state_hash",
        "occurred_at",
        "target_state",
    }
    if set(payload) != expected:
        raise ArtifactIntegrityError("research job event schema is invalid")
    if payload.get("schema_version") != _SCHEMA_VERSION or payload.get(
        "artifact_type"
    ) != _EVENT_TYPE:
        raise ArtifactIntegrityError("research job event version is invalid")
    if payload.get("research_id") != research_id:
        raise ArtifactIntegrityError("research job event identity does not match")
    event_id = _required_hash(payload.get("event_id"), "event_id")
    if logical_key != f"{_EVENT_KEY_PREFIX}{event_id}":
        raise ArtifactIntegrityError("research job event logical identity does not match")
    target_value = payload.get("target_state")
    if not isinstance(target_value, Mapping):
        raise ArtifactIntegrityError("research job event target must be an object")
    try:
        target = _target_from_payload(target_value)
        occurred_at = _parse_datetime(payload.get("occurred_at"), "occurred_at")
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactIntegrityError("research job event target is invalid") from exc
    if _event_id(research_id, target) != event_id:
        raise ArtifactIntegrityError("research job event identifier does not match")
    return _StoredEvent(
        event_id=event_id,
        parent_state_hash=_required_hash(
            payload.get("parent_state_hash"), "parent_state_hash"
        ),
        result_state_hash=_required_hash(
            payload.get("result_state_hash"), "result_state_hash"
        ),
        occurred_at=occurred_at,
        target=target,
    )


def _target_from_payload(payload: Mapping[str, Any]) -> _TransitionTarget:
    expected = {
        "status",
        "stage",
        "progress",
        "coverage",
        "error_code",
        "error_message",
    }
    if set(payload) != expected:
        raise ValueError("transition target schema is invalid")
    coverage_value = payload.get("coverage")
    if coverage_value is not None and not isinstance(coverage_value, Mapping):
        raise ValueError("transition coverage must be an object")
    return _TransitionTarget(
        status=JobStatus(str(payload["status"])),
        stage=_required_string(payload["stage"], "stage"),
        progress=float(payload["progress"]),
        coverage=(
            _coverage_from_payload(coverage_value)
            if isinstance(coverage_value, Mapping)
            else None
        ),
        error_code=_optional_string(payload.get("error_code"), "error_code"),
        error_message=_optional_string(payload.get("error_message"), "error_message"),
    )


def _event_id(research_id: str, target: _TransitionTarget) -> str:
    return canonical_hash(
        {
            "schema_version": _SCHEMA_VERSION,
            "artifact_type": "research_job_transition_intent_v1",
            "research_id": research_id,
            "target_state": target.to_payload(),
        }
    )


def _state_hash(job: ResearchJob) -> str:
    return canonical_hash(
        {
            "schema_version": _SCHEMA_VERSION,
            "artifact_type": "research_job_state_v1",
            "research_id": job.id,
            "query_fingerprint": job.query_fingerprint,
            "index_revision": job.index_revision,
            "created_at": job.created_at.isoformat(),
            "expires_at": job.expires_at.isoformat(),
            "state": _dynamic_state_payload(job),
        }
    )


def _dynamic_state_payload(job: ResearchJob) -> dict[str, Any]:
    return {
        "status": job.status.value,
        "stage": job.stage,
        "progress": job.progress,
        "updated_at": job.updated_at.isoformat(),
        "coverage": _coverage_to_payload(job.coverage),
        "error_code": job.error_code,
        "error_message": job.error_message,
    }


def _job_from_dynamic_state(
    *,
    research_id: str,
    contract: ResearchContract,
    query_fingerprint: str,
    index_revision: str,
    created_at: datetime,
    expires_at: datetime,
    payload: Mapping[str, Any],
) -> ResearchJob:
    expected = {
        "status",
        "stage",
        "progress",
        "updated_at",
        "coverage",
        "error_code",
        "error_message",
    }
    if set(payload) != expected:
        raise ValueError("dynamic state schema is invalid")
    coverage_value = payload.get("coverage")
    if coverage_value is not None and not isinstance(coverage_value, Mapping):
        raise ValueError("dynamic state coverage must be an object")
    return ResearchJob(
        id=research_id,
        contract=contract,
        query_fingerprint=query_fingerprint,
        index_revision=index_revision,
        status=JobStatus(str(payload["status"])),
        stage=_required_string(payload["stage"], "stage"),
        progress=float(payload["progress"]),
        created_at=created_at,
        updated_at=_parse_datetime(payload["updated_at"], "updated_at"),
        expires_at=expires_at,
        coverage=(
            _coverage_from_payload(coverage_value)
            if isinstance(coverage_value, Mapping)
            else None
        ),
        error_code=_optional_string(payload.get("error_code"), "error_code"),
        error_message=_optional_string(payload.get("error_message"), "error_message"),
    )


def _contract_from_payload(payload: Mapping[str, Any]) -> ResearchContract:
    legacy_expected = {
        "query",
        "as_of",
        "date_from",
        "date_to",
        "assembly_term",
        "committees",
        "bill_numbers",
        "evidence_types",
        "intents",
        "ordering",
        "completeness",
    }
    expected = legacy_expected | {"assembly_terms"}
    if set(payload) not in (legacy_expected, expected):
        raise ValueError("research contract schema is invalid")
    committees = payload["committees"]
    assembly_terms = payload.get("assembly_terms", [payload["assembly_term"]])
    bill_numbers = payload["bill_numbers"]
    evidence_types = payload["evidence_types"]
    intents = payload["intents"]
    for value, label in (
        (assembly_terms, "assembly_terms"),
        (committees, "committees"),
        (bill_numbers, "bill_numbers"),
        (evidence_types, "evidence_types"),
        (intents, "intents"),
    ):
        if not isinstance(value, list):
            raise ValueError(f"research contract {label} must be a list")
    date_from = payload.get("date_from")
    date_to = payload.get("date_to")
    return ResearchContract(
        query=_required_string(payload["query"], "query"),
        as_of=_parse_datetime(payload["as_of"], "as_of"),
        date_from=date.fromisoformat(str(date_from)) if date_from is not None else None,
        date_to=date.fromisoformat(str(date_to)) if date_to is not None else None,
        assembly_term=int(payload["assembly_term"]),
        assembly_terms=tuple(int(item) for item in assembly_terms),
        committees=tuple(str(item) for item in committees),
        bill_numbers=tuple(str(item) for item in bill_numbers),
        evidence_types=tuple(EvidenceType(str(item)) for item in evidence_types),
        intents=tuple(ResearchIntent(str(item)) for item in intents),
        ordering=_required_string(payload["ordering"], "ordering"),
        completeness=_required_string(payload["completeness"], "completeness"),
    )


def _coverage_to_payload(coverage: CoverageLedger | None) -> dict[str, Any] | None:
    if coverage is None:
        return None
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


def _coverage_from_payload(payload: Mapping[str, Any]) -> CoverageLedger:
    if set(payload) != {"requested", "entries"}:
        raise ValueError("coverage schema is invalid")
    requested = payload["requested"]
    entries = payload["entries"]
    if not isinstance(requested, list) or not isinstance(entries, list):
        raise ValueError("coverage requested and entries must be lists")
    parsed_entries: list[EvidenceCoverage] = []
    expected_entry = {
        "evidence_type",
        "candidate_total",
        "checked_count",
        "matched_count",
        "failed_count",
        "pending_count",
        "gap_reasons",
    }
    for value in entries:
        if not isinstance(value, Mapping) or set(value) != expected_entry:
            raise ValueError("coverage entry schema is invalid")
        gap_reasons = value["gap_reasons"]
        if not isinstance(gap_reasons, list):
            raise ValueError("coverage gap_reasons must be a list")
        candidate_total = value["candidate_total"]
        parsed_entries.append(
            EvidenceCoverage(
                evidence_type=EvidenceType(str(value["evidence_type"])),
                candidate_total=(
                    int(candidate_total) if candidate_total is not None else None
                ),
                checked_count=int(value["checked_count"]),
                matched_count=int(value["matched_count"]),
                failed_count=int(value["failed_count"]),
                pending_count=int(value["pending_count"]),
                gap_reasons=tuple(str(item) for item in gap_reasons),
            )
        )
    return CoverageLedger(
        requested=tuple(EvidenceType(str(item)) for item in requested),
        entries=tuple(parsed_entries),
    )


def _parse_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO datetime")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, label)


def _required_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ArtifactIntegrityError(f"research job {label} is not a SHA-256 digest")
    return value


__all__ = ["ArtifactResearchJobStore"]
