"""Resumable parse/index/publish workflow for an official corpus inventory.

Every completed document is written to the content-addressed corpus store
before its checkpoint outcome is committed.  Reruns therefore skip successful
work, retry transient failures, and can safely recover from interruption at any
document boundary.  Publication is a separate fail-closed operation: no
revision is created or activated until every inventory scope is complete and
every scheduled document has a verified successful outcome.
"""

from __future__ import annotations

import contextlib
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from kasm.research.corpus_bridge import (
    ExactCorpusWorkDescriptor,
    corpus_document_from_parsed,
)
from kasm.research.document_worker import DocumentWorkerError, OfficialDocumentWorker

from .inventory import CorpusInventoryManifest
from .models import (
    CorpusDocumentRef,
    CorpusEvidenceKind,
    CorpusRevisionManifest,
)
from .repository import CorpusRepository
from .serialization import canonical_hash, canonical_json, decode_canonical_json

BUILD_CHECKPOINT_SCHEMA_VERSION: Final = 1
ACTIVATION_SCHEMA_VERSION: Final = 1
_REASON_CODE: Final = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


class CorpusBuildError(RuntimeError):
    """A sanitized, operator-actionable build failure."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        if not _REASON_CODE.fullmatch(code):
            raise ValueError("corpus build error code is invalid")
        self.code = code


class CorpusBuildOutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"


@dataclass(frozen=True, slots=True)
class CorpusBuildOutcome:
    """Checkpoint-safe state for one exact inventory work item."""

    work_id: str
    identity_id: str
    status: CorpusBuildOutcomeStatus
    attempts: int
    updated_at: datetime
    document: CorpusDocumentRef | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if not self.work_id.strip() or not _SHA256.fullmatch(self.identity_id):
            raise ValueError("corpus build outcome identity is invalid")
        if self.attempts < 1 or self.updated_at.tzinfo is None:
            raise ValueError("corpus build outcome attempt metadata is invalid")
        if self.status is CorpusBuildOutcomeStatus.SUCCEEDED:
            if self.document is None or self.failure_code is not None:
                raise ValueError("successful corpus outcome requires only a document")
        elif (
            self.document is not None
            or self.failure_code is None
            or not _REASON_CODE.fullmatch(self.failure_code)
        ):
            raise ValueError("failed corpus outcome requires one stable reason code")

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "identity_id": self.identity_id,
            "status": self.status.value,
            "attempts": self.attempts,
            "updated_at": self.updated_at.astimezone(UTC).isoformat(),
            "document": self.document.to_dict() if self.document else None,
            "failure_code": self.failure_code,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CorpusBuildOutcome:
        raw_document = payload.get("document")
        if raw_document is not None and not isinstance(raw_document, dict):
            raise ValueError("checkpoint outcome document is invalid")
        raw_failure = payload.get("failure_code")
        if raw_failure is not None and not isinstance(raw_failure, str):
            raise ValueError("checkpoint outcome failure code is invalid")
        return cls(
            work_id=_text(payload, "work_id"),
            identity_id=_text(payload, "identity_id"),
            status=CorpusBuildOutcomeStatus(_text(payload, "status")),
            attempts=_integer(payload, "attempts"),
            updated_at=datetime.fromisoformat(_text(payload, "updated_at")),
            document=(
                CorpusDocumentRef.from_dict(raw_document)
                if raw_document is not None
                else None
            ),
            failure_code=raw_failure,
        )


@dataclass(frozen=True, slots=True)
class CorpusBuildScopeAccounting:
    """Expected/succeeded/failed/unaccounted build state for one scope."""

    assembly_term: int
    evidence_kind: CorpusEvidenceKind
    expected_count: int | None
    succeeded_count: int
    failed_count: int
    unaccounted_count: int | None

    @property
    def complete(self) -> bool:
        return bool(
            self.expected_count is not None
            and self.succeeded_count == self.expected_count
            and self.failed_count == 0
            and self.unaccounted_count == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "assembly_term": self.assembly_term,
            "evidence_kind": self.evidence_kind.value,
            "expected_count": self.expected_count,
            "succeeded_count": self.succeeded_count,
            "failed_count": self.failed_count,
            "unaccounted_count": self.unaccounted_count,
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class CorpusBuildCheckpoint:
    """Mutable progress represented by a hash-protected canonical snapshot."""

    checkpoint_hash: str
    inventory: CorpusInventoryManifest
    parser_version: str
    parent_revision_id: str | None
    created_at: datetime
    updated_at: datetime
    outcomes: tuple[CorpusBuildOutcome, ...] = ()

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.checkpoint_hash):
            raise ValueError("checkpoint_hash is invalid")
        if not self.parser_version.strip() or len(self.parser_version) > 128:
            raise ValueError("checkpoint parser_version is invalid")
        if self.parent_revision_id is not None and not _SHA256.fullmatch(
            self.parent_revision_id
        ):
            raise ValueError("checkpoint parent revision id is invalid")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("checkpoint timestamps must be timezone-aware")
        if self.updated_at.astimezone(UTC) < self.created_at.astimezone(UTC):
            raise ValueError("checkpoint update cannot predate creation")
        work_ids = tuple(item.work_id for item in self.outcomes)
        if tuple(sorted(work_ids)) != work_ids or len(set(work_ids)) != len(work_ids):
            raise ValueError("checkpoint outcomes must be unique and sorted")
        inventory_by_work = {item.work_item.work_id: item for item in self.inventory.items}
        for outcome in self.outcomes:
            item = inventory_by_work.get(outcome.work_id)
            if item is None or outcome.identity_id != item.identity.identity_id:
                raise ValueError("checkpoint outcome is outside its inventory")
            if outcome.document is not None and (
                outcome.document.identity != item.identity
                or outcome.document.official_url != item.work_item.official_url
                or outcome.document.parser_version != self.parser_version
            ):
                raise ValueError("checkpoint document does not match inventory and parser")
        if self.checkpoint_hash != canonical_hash(self._identity_payload()):
            raise ValueError("checkpoint hash does not match its content")

    @classmethod
    def create(
        cls,
        inventory: CorpusInventoryManifest,
        *,
        parser_version: str,
        parent_revision_id: str | None = None,
        created_at: datetime | None = None,
        outcomes: tuple[CorpusBuildOutcome, ...] = (),
    ) -> CorpusBuildCheckpoint:
        timestamp = created_at or datetime.now(UTC)
        return cls._from_parts(
            inventory=inventory,
            parser_version=parser_version,
            parent_revision_id=parent_revision_id,
            created_at=timestamp,
            updated_at=timestamp,
            outcomes=outcomes,
        )

    @classmethod
    def merge_inventory(
        cls,
        inventory: CorpusInventoryManifest,
        *,
        parser_version: str,
        existing: CorpusBuildCheckpoint | None,
        parent_revision_id: str | None = None,
        updated_at: datetime | None = None,
    ) -> CorpusBuildCheckpoint:
        """Retain progress only for descriptors identical across inventories."""

        timestamp = updated_at or datetime.now(UTC)
        if (
            existing is None
            or existing.parser_version != parser_version
            or existing.parent_revision_id != parent_revision_id
            or existing.inventory.inventory_id != inventory.inventory_id
        ):
            return cls.create(
                inventory,
                parser_version=parser_version,
                parent_revision_id=parent_revision_id,
                created_at=timestamp,
            )
        old_items = {
            item.work_item.work_id: item.to_dict() for item in existing.inventory.items
        }
        new_items = {item.work_item.work_id: item for item in inventory.items}
        retained = tuple(
            outcome
            for outcome in existing.outcomes
            if outcome.work_id in new_items
            and old_items.get(outcome.work_id) == new_items[outcome.work_id].to_dict()
        )
        return cls._from_parts(
            inventory=inventory,
            parser_version=parser_version,
            parent_revision_id=parent_revision_id,
            created_at=existing.created_at,
            updated_at=timestamp,
            outcomes=retained,
        )

    @classmethod
    def _from_parts(
        cls,
        *,
        inventory: CorpusInventoryManifest,
        parser_version: str,
        parent_revision_id: str | None,
        created_at: datetime,
        updated_at: datetime,
        outcomes: tuple[CorpusBuildOutcome, ...],
    ) -> CorpusBuildCheckpoint:
        normalized = tuple(sorted(outcomes, key=lambda item: item.work_id))
        payload = _checkpoint_payload(
            inventory=inventory,
            parser_version=parser_version,
            parent_revision_id=parent_revision_id,
            created_at=created_at,
            updated_at=updated_at,
            outcomes=normalized,
        )
        return cls(
            checkpoint_hash=canonical_hash(payload),
            inventory=inventory,
            parser_version=parser_version,
            parent_revision_id=parent_revision_id,
            created_at=created_at,
            updated_at=updated_at,
            outcomes=normalized,
        )

    def with_outcome(
        self,
        outcome: CorpusBuildOutcome,
        *,
        updated_at: datetime | None = None,
    ) -> CorpusBuildCheckpoint:
        by_work = {item.work_id: item for item in self.outcomes}
        by_work[outcome.work_id] = outcome
        return self._from_parts(
            inventory=self.inventory,
            parser_version=self.parser_version,
            parent_revision_id=self.parent_revision_id,
            created_at=self.created_at,
            updated_at=updated_at or datetime.now(UTC),
            outcomes=tuple(by_work.values()),
        )

    @property
    def accounting(self) -> tuple[CorpusBuildScopeAccounting, ...]:
        outcomes = {item.work_id: item for item in self.outcomes}
        result: list[CorpusBuildScopeAccounting] = []
        for scope in self.inventory.coverage:
            scoped_items = tuple(
                item
                for item in self.inventory.items
                if item.assembly_term == scope.assembly_term
                and item.evidence_kind is scope.evidence_kind
            )
            succeeded = sum(
                outcomes.get(item.work_item.work_id) is not None
                and outcomes[item.work_item.work_id].status
                is CorpusBuildOutcomeStatus.SUCCEEDED
                for item in scoped_items
            )
            work_failures = sum(
                outcomes.get(item.work_item.work_id) is not None
                and outcomes[item.work_item.work_id].status
                is not CorpusBuildOutcomeStatus.SUCCEEDED
                for item in scoped_items
            )
            failed = scope.gap_count + work_failures
            unaccounted = (
                None
                if scope.expected_count is None
                else scope.expected_count - succeeded - failed
            )
            if unaccounted is not None and unaccounted < 0:
                raise ValueError("checkpoint accounting exceeds expected inventory")
            result.append(
                CorpusBuildScopeAccounting(
                    scope.assembly_term,
                    scope.evidence_kind,
                    scope.expected_count,
                    succeeded,
                    failed,
                    unaccounted,
                )
            )
        return tuple(result)

    @property
    def complete(self) -> bool:
        return self.inventory.complete and all(item.complete for item in self.accounting)

    def summary(self) -> dict[str, Any]:
        total = len(self.inventory.items)
        expected = (
            None
            if any(item.expected_count is None for item in self.inventory.coverage)
            else sum(
                item.expected_count or 0 for item in self.inventory.coverage
            )
        )
        succeeded = sum(
            item.status is CorpusBuildOutcomeStatus.SUCCEEDED for item in self.outcomes
        )
        retryable = sum(
            item.status is CorpusBuildOutcomeStatus.RETRYABLE_FAILURE
            for item in self.outcomes
        )
        permanent = sum(
            item.status is CorpusBuildOutcomeStatus.PERMANENT_FAILURE
            for item in self.outcomes
        )
        return {
            "checkpoint_hash": self.checkpoint_hash,
            "inventory_id": self.inventory.inventory_id,
            "inventory_complete": self.inventory.complete,
            "documents_expected": expected,
            "documents_scheduled": total,
            "documents_succeeded": succeeded,
            "documents_retryable_failed": retryable,
            "documents_permanent_failed": permanent,
            "documents_unattempted": total - len(self.outcomes),
            "complete": self.complete,
            "coverage": [item.to_dict() for item in self.accounting],
        }

    def _identity_payload(self) -> dict[str, Any]:
        return _checkpoint_payload(
            inventory=self.inventory,
            parser_version=self.parser_version,
            parent_revision_id=self.parent_revision_id,
            created_at=self.created_at,
            updated_at=self.updated_at,
            outcomes=self.outcomes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_hash": self.checkpoint_hash,
            **self._identity_payload(),
            "summary": self.summary(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CorpusBuildCheckpoint:
        if payload.get("schema_version") != BUILD_CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported corpus build checkpoint schema")
        raw_inventory = payload.get("inventory")
        raw_outcomes = payload.get("outcomes")
        if not isinstance(raw_inventory, dict) or not isinstance(raw_outcomes, list):
            raise ValueError("corpus build checkpoint content is invalid")
        raw_parent = payload.get("parent_revision_id")
        if raw_parent is not None and not isinstance(raw_parent, str):
            raise ValueError("checkpoint parent revision is invalid")
        result = cls(
            checkpoint_hash=_text(payload, "checkpoint_hash"),
            inventory=CorpusInventoryManifest.from_dict(raw_inventory),
            parser_version=_text(payload, "parser_version"),
            parent_revision_id=raw_parent,
            created_at=datetime.fromisoformat(_text(payload, "created_at")),
            updated_at=datetime.fromisoformat(_text(payload, "updated_at")),
            outcomes=tuple(
                CorpusBuildOutcome.from_dict(_mapping(item, "checkpoint outcome"))
                for item in raw_outcomes
            ),
        )
        if payload.get("summary") != result.summary():
            raise ValueError("checkpoint summary does not match checkpoint content")
        return result


class CorpusBuildRunner:
    """Execute every inventory item, checkpointing after every attempt."""

    def __init__(
        self,
        repository: CorpusRepository,
        worker: OfficialDocumentWorker,
        *,
        checkpoint_writer: Callable[[CorpusBuildCheckpoint], None],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repository = repository
        self.worker = worker
        self.checkpoint_writer = checkpoint_writer
        self.clock = clock

    def run(
        self,
        checkpoint: CorpusBuildCheckpoint,
        *,
        attempts_per_item: int = 3,
        retry_permanent: bool = False,
        refresh_documents: bool = False,
    ) -> CorpusBuildCheckpoint:
        if attempts_per_item < 1:
            raise ValueError("attempts_per_item must be positive")
        current = checkpoint
        outcomes = {item.work_id: item for item in current.outcomes}
        for inventory_item in current.inventory.items:
            prior = outcomes.get(inventory_item.work_item.work_id)
            if prior is not None and prior.status is CorpusBuildOutcomeStatus.SUCCEEDED:
                continue
            if (
                prior is not None
                and prior.status is CorpusBuildOutcomeStatus.PERMANENT_FAILURE
                and not retry_permanent
            ):
                continue
            for _attempt in range(attempts_per_item):
                previous_attempts = prior.attempts if prior else 0
                try:
                    result = self.worker.process(
                        inventory_item.work_item.kind,
                        inventory_item.work_item.official_url,
                        refresh=refresh_documents,
                    )
                except DocumentWorkerError as exc:
                    status = (
                        CorpusBuildOutcomeStatus.RETRYABLE_FAILURE
                        if exc.retryable
                        else CorpusBuildOutcomeStatus.PERMANENT_FAILURE
                    )
                    prior = CorpusBuildOutcome(
                        inventory_item.work_item.work_id,
                        inventory_item.identity.identity_id,
                        status,
                        previous_attempts + 1,
                        self.clock(),
                        failure_code=exc.code,
                    )
                    current = current.with_outcome(prior, updated_at=self.clock())
                    self.checkpoint_writer(current)
                    outcomes[prior.work_id] = prior
                    if status is CorpusBuildOutcomeStatus.PERMANENT_FAILURE:
                        break
                    continue

                if (
                    result.parser_version != current.parser_version
                    or result.document.parser_version != current.parser_version
                ):
                    prior = CorpusBuildOutcome(
                        inventory_item.work_item.work_id,
                        inventory_item.identity.identity_id,
                        CorpusBuildOutcomeStatus.PERMANENT_FAILURE,
                        previous_attempts + 1,
                        self.clock(),
                        failure_code="parser_version_mismatch",
                    )
                    current = current.with_outcome(prior, updated_at=self.clock())
                    self.checkpoint_writer(current)
                    outcomes[prior.work_id] = prior
                    break

                bridged = corpus_document_from_parsed(
                    ExactCorpusWorkDescriptor(
                        inventory_item.work_item,
                        inventory_item.assembly_term,
                        inventory_item.official_identifier,
                        title=inventory_item.title,
                        document_date=inventory_item.document_date,
                        committee=inventory_item.committee,
                    ),
                    result.document,
                )
                if bridged.document is None:
                    assert bridged.gap is not None
                    prior = CorpusBuildOutcome(
                        inventory_item.work_item.work_id,
                        inventory_item.identity.identity_id,
                        CorpusBuildOutcomeStatus.PERMANENT_FAILURE,
                        previous_attempts + 1,
                        self.clock(),
                        failure_code=f"bridge.{bridged.gap.code.value}",
                    )
                else:
                    reference = self.repository.put_document(bridged.document)
                    prior = CorpusBuildOutcome(
                        inventory_item.work_item.work_id,
                        inventory_item.identity.identity_id,
                        CorpusBuildOutcomeStatus.SUCCEEDED,
                        previous_attempts + 1,
                        self.clock(),
                        document=reference,
                    )
                current = current.with_outcome(prior, updated_at=self.clock())
                self.checkpoint_writer(current)
                outcomes[prior.work_id] = prior
                break
        return current


def publish_complete_revision(
    repository: CorpusRepository,
    checkpoint: CorpusBuildCheckpoint,
    *,
    created_at: datetime | None = None,
) -> CorpusRevisionManifest:
    """Publish only a fully accounted revision; incomplete state remains private."""

    if not checkpoint.complete:
        raise CorpusBuildError(
            "corpus build is incomplete; publication was refused",
            code="build_incomplete",
        )
    timestamp = created_at or datetime.now(UTC)
    parent = (
        repository.require_revision(checkpoint.parent_revision_id)
        if checkpoint.parent_revision_id is not None
        else None
    )
    if parent is not None and not parent.complete:
        raise CorpusBuildError(
            "parent corpus revision is incomplete; publication was refused",
            code="parent_revision_incomplete",
        )
    terms = tuple(
        sorted(
            set(checkpoint.inventory.assembly_terms)
            | (set(parent.assembly_terms) if parent else set())
        )
    )
    builder = repository.begin_revision(
        assembly_terms=terms,
        evidence_kinds=tuple(CorpusEvidenceKind),
        parent_revision_id=checkpoint.parent_revision_id,
    )
    target_scopes = {
        (term, kind)
        for term in checkpoint.inventory.assembly_terms
        for kind in CorpusEvidenceKind
    }
    target_identities = {item.identity.identity_id for item in checkpoint.inventory.items}
    if parent is not None:
        for reference in parent.documents:
            scope = (
                reference.identity.assembly_term,
                reference.identity.evidence_kind,
            )
            if scope in target_scopes and reference.identity.identity_id not in target_identities:
                builder.remove_document(reference.identity)

    outcomes = {item.work_id: item for item in checkpoint.outcomes}
    for item in checkpoint.inventory.items:
        outcome = outcomes[item.work_item.work_id]
        assert outcome.document is not None
        document = repository.get_document(outcome.document)
        builder.upsert_document(document)
    for coverage in checkpoint.inventory.coverage:
        assert coverage.expected_count is not None
        builder.set_expected_count(
            coverage.assembly_term,
            coverage.evidence_kind,
            coverage.expected_count,
        )
        builder.replace_failures(
            coverage.assembly_term,
            coverage.evidence_kind,
            (),
        )
    manifest = builder.publish(
        inventory_as_of=checkpoint.inventory.inventory_as_of,
        created_at=timestamp,
    )
    if not manifest.complete:
        # The id is deliberately not included in this error.  An unexpected
        # internal invariant failure cannot become an activation candidate.
        raise CorpusBuildError(
            "published corpus failed its completeness invariant",
            code="published_revision_incomplete",
        )
    return manifest


def write_checkpoint(path: str | Path, checkpoint: CorpusBuildCheckpoint) -> None:
    _atomic_private_write(Path(path), canonical_json(checkpoint.to_dict()))


def read_checkpoint(path: str | Path) -> CorpusBuildCheckpoint:
    payload = decode_canonical_json(Path(path).read_bytes())
    if not isinstance(payload, dict):
        raise ValueError("corpus build checkpoint must be an object")
    return CorpusBuildCheckpoint.from_dict(payload)


def write_complete_revision_manifest(
    path: str | Path,
    manifest: CorpusRevisionManifest,
) -> None:
    if not manifest.complete:
        raise CorpusBuildError(
            "incomplete corpus revisions cannot be exported",
            code="revision_export_incomplete",
        )
    _atomic_private_write(Path(path), canonical_json(manifest.to_dict()))


def write_activation(
    path: str | Path,
    manifest: CorpusRevisionManifest,
    *,
    inventory_id: str,
) -> None:
    """Write an operator activation pointer only for a complete revision."""

    if not manifest.complete:
        raise CorpusBuildError(
            "incomplete corpus revisions cannot be activated",
            code="revision_activation_incomplete",
        )
    payload = {
        "schema_version": ACTIVATION_SCHEMA_VERSION,
        "environment_variable": "KBD_RESEARCH_CORPUS_REVISION",
        "revision_id": manifest.revision_id,
        "inventory_id": inventory_id,
        "inventory_as_of": manifest.inventory_as_of.astimezone(UTC).isoformat(),
        "complete": True,
    }
    _atomic_private_write(Path(path), canonical_json(payload))


def _checkpoint_payload(
    *,
    inventory: CorpusInventoryManifest,
    parser_version: str,
    parent_revision_id: str | None,
    created_at: datetime,
    updated_at: datetime,
    outcomes: tuple[CorpusBuildOutcome, ...],
) -> dict[str, Any]:
    return {
        "schema_version": BUILD_CHECKPOINT_SCHEMA_VERSION,
        "inventory": inventory.to_dict(),
        "parser_version": parser_version,
        "parent_revision_id": parent_revision_id,
        "created_at": created_at.astimezone(UTC).isoformat(),
        "updated_at": updated_at.astimezone(UTC).isoformat(),
        "outcomes": [item.to_dict() for item in outcomes],
    }


def _atomic_private_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return value


def _text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _integer(payload: Mapping[str, Any], field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    return value


__all__ = [
    "ACTIVATION_SCHEMA_VERSION",
    "BUILD_CHECKPOINT_SCHEMA_VERSION",
    "CorpusBuildCheckpoint",
    "CorpusBuildError",
    "CorpusBuildOutcome",
    "CorpusBuildOutcomeStatus",
    "CorpusBuildRunner",
    "CorpusBuildScopeAccounting",
    "publish_complete_revision",
    "read_checkpoint",
    "write_activation",
    "write_checkpoint",
    "write_complete_revision_manifest",
]
