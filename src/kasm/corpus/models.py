"""Immutable contracts for the durable official-document corpus.

The research pipeline must be able to distinguish "some useful documents were
indexed" from "the requested official universe was indexed completely".  The
contracts in this module therefore bind every corpus revision to:

* an explicit National Assembly term and evidence-kind matrix;
* exact official document identities and URLs;
* immutable source, parsed-text, and stored-object hashes; and
* expected, succeeded, failed, and unaccounted document counts.

Completeness is deliberately fail closed.  An unknown expected count, one
failed source, one unaccounted source, or an index/document-set mismatch makes
the revision incomplete.
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Final

CORPUS_SCHEMA_VERSION: Final = 2
LEXICAL_INDEX_VERSION: Final = "kbd-ko-lexical-v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_OFFICIAL_HOST_SUFFIX = ".assembly.go.kr"
_SENSITIVE_QUERY_NAMES = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "key",
    "password",
    "secret",
    "token",
}


class CorpusEvidenceKind(StrEnum):
    """Official full-text axes required for exhaustive topical recall."""

    BILL_ORIGINAL = "bill_original"
    REVIEW_REPORT = "review_report"
    MINUTES = "minutes"


@dataclass(frozen=True, slots=True)
class CorpusDocumentIdentity:
    """Stable identity assigned by the official source, not by search text."""

    assembly_term: int
    evidence_kind: CorpusEvidenceKind
    official_identifier: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.assembly_term, int)
            or isinstance(self.assembly_term, bool)
            or self.assembly_term < 1
        ):
            raise ValueError("assembly_term must be positive")
        _validate_bounded_text(
            self.official_identifier,
            field="official_identifier",
            maximum=512,
        )

    @property
    def identity_id(self) -> str:
        return _hash_payload(self.to_dict())

    @property
    def sort_key(self) -> tuple[int, str, str, str]:
        return (
            self.assembly_term,
            self.evidence_kind.value,
            self.official_identifier,
            self.identity_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "assembly_term": self.assembly_term,
            "evidence_kind": self.evidence_kind.value,
            "official_identifier": self.official_identifier,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CorpusDocumentIdentity:
        return cls(
            assembly_term=_expect_int(payload, "assembly_term"),
            evidence_kind=CorpusEvidenceKind(
                _expect_str(payload, "evidence_kind")
            ),
            official_identifier=_expect_str(payload, "official_identifier"),
        )


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    """Lossless parsed text for one immutable official source observation."""

    identity: CorpusDocumentIdentity
    official_url: str
    source_hash: str
    parser_version: str
    text: str
    observed_at: datetime
    title: str = ""
    document_date: date | None = None
    related_bill_numbers: tuple[str, ...] = ()
    committee: str = ""

    def __post_init__(self) -> None:
        _validate_official_url(self.official_url)
        _validate_sha256(self.source_hash, field="source_hash")
        _validate_bounded_text(
            self.parser_version,
            field="parser_version",
            maximum=128,
        )
        if not self.text.strip():
            raise ValueError("corpus document text must not be empty")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.title:
            _validate_bounded_text(self.title, field="title", maximum=2_000)
        _validate_related_bill_numbers(
            self.related_bill_numbers,
            assembly_term=self.identity.assembly_term,
        )
        object.__setattr__(
            self,
            "related_bill_numbers",
            tuple(sorted(set(self.related_bill_numbers))),
        )
        if self.committee:
            _validate_bounded_text(self.committee, field="committee", maximum=500)

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def text_characters(self) -> int:
        return len(self.text)

    @property
    def text_bytes(self) -> int:
        return len(self.text.encode("utf-8"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CORPUS_SCHEMA_VERSION,
            "identity": self.identity.to_dict(),
            "official_url": self.official_url,
            "source_hash": self.source_hash,
            "parser_version": self.parser_version,
            "observed_at": self.observed_at.astimezone(UTC).isoformat(),
            "title": self.title,
            "document_date": (
                self.document_date.isoformat() if self.document_date else None
            ),
            "related_bill_numbers": list(self.related_bill_numbers),
            "committee": self.committee,
            "text": self.text,
            "text_hash": self.text_hash,
            "text_characters": self.text_characters,
            "text_bytes": self.text_bytes,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CorpusDocument:
        if type(payload.get("schema_version")) is not int or (
            payload.get("schema_version") != CORPUS_SCHEMA_VERSION
        ):
            raise ValueError("unsupported corpus document schema")
        raw_identity = payload.get("identity")
        if not isinstance(raw_identity, dict):
            raise ValueError("corpus document identity must be an object")
        raw_document_date = payload.get("document_date")
        if raw_document_date is not None and not isinstance(raw_document_date, str):
            raise ValueError("document_date must be an ISO date or null")
        raw_related_bills = payload.get("related_bill_numbers")
        if not isinstance(raw_related_bills, list) or any(
            not isinstance(value, str) for value in raw_related_bills
        ):
            raise ValueError("related_bill_numbers must be a string list")
        result = cls(
            identity=CorpusDocumentIdentity.from_dict(raw_identity),
            official_url=_expect_str(payload, "official_url"),
            source_hash=_expect_str(payload, "source_hash"),
            parser_version=_expect_str(payload, "parser_version"),
            observed_at=datetime.fromisoformat(_expect_str(payload, "observed_at")),
            title=_expect_str(payload, "title"),
            document_date=(
                date.fromisoformat(raw_document_date)
                if raw_document_date is not None
                else None
            ),
            related_bill_numbers=tuple(raw_related_bills),
            committee=_expect_str(payload, "committee"),
            text=_expect_str(payload, "text"),
        )
        if payload.get("text_hash") != result.text_hash:
            raise ValueError("corpus document text hash does not match")
        if _expect_int(payload, "text_characters") != result.text_characters:
            raise ValueError("corpus document character count does not match")
        if _expect_int(payload, "text_bytes") != result.text_bytes:
            raise ValueError("corpus document byte count does not match")
        return result


@dataclass(frozen=True, slots=True)
class CorpusDocumentRef:
    """Manifest-safe pointer to a full-text object."""

    identity: CorpusDocumentIdentity
    official_url: str
    source_hash: str
    parser_version: str
    observed_at: datetime
    text_hash: str
    text_characters: int
    text_bytes: int
    object_hash: str
    object_key: str
    title: str = ""
    document_date: date | None = None
    related_bill_numbers: tuple[str, ...] = ()
    committee: str = ""

    def __post_init__(self) -> None:
        _validate_official_url(self.official_url)
        _validate_sha256(self.source_hash, field="source_hash")
        _validate_sha256(self.text_hash, field="text_hash")
        _validate_sha256(self.object_hash, field="object_hash")
        _validate_bounded_text(
            self.parser_version,
            field="parser_version",
            maximum=128,
        )
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if (
            not isinstance(self.text_characters, int)
            or isinstance(self.text_characters, bool)
            or not isinstance(self.text_bytes, int)
            or isinstance(self.text_bytes, bool)
            or self.text_characters < 1
            or self.text_bytes < 1
        ):
            raise ValueError("corpus document text sizes must be positive")
        if self.object_key != document_object_key(self.object_hash):
            raise ValueError("document object key does not match object_hash")
        if self.title:
            _validate_bounded_text(self.title, field="title", maximum=2_000)
        _validate_related_bill_numbers(
            self.related_bill_numbers,
            assembly_term=self.identity.assembly_term,
        )
        object.__setattr__(
            self,
            "related_bill_numbers",
            tuple(sorted(set(self.related_bill_numbers))),
        )
        if self.committee:
            _validate_bounded_text(self.committee, field="committee", maximum=500)

    @classmethod
    def from_document(
        cls,
        document: CorpusDocument,
        *,
        object_hash: str,
    ) -> CorpusDocumentRef:
        return cls(
            identity=document.identity,
            official_url=document.official_url,
            source_hash=document.source_hash,
            parser_version=document.parser_version,
            observed_at=document.observed_at,
            text_hash=document.text_hash,
            text_characters=document.text_characters,
            text_bytes=document.text_bytes,
            object_hash=object_hash,
            object_key=document_object_key(object_hash),
            title=document.title,
            document_date=document.document_date,
            related_bill_numbers=document.related_bill_numbers,
            committee=document.committee,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "official_url": self.official_url,
            "source_hash": self.source_hash,
            "parser_version": self.parser_version,
            "observed_at": self.observed_at.astimezone(UTC).isoformat(),
            "text_hash": self.text_hash,
            "text_characters": self.text_characters,
            "text_bytes": self.text_bytes,
            "object_hash": self.object_hash,
            "object_key": self.object_key,
            "title": self.title,
            "document_date": (
                self.document_date.isoformat() if self.document_date else None
            ),
            "related_bill_numbers": list(self.related_bill_numbers),
            "committee": self.committee,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CorpusDocumentRef:
        raw_identity = payload.get("identity")
        if not isinstance(raw_identity, dict):
            raise ValueError("corpus document ref identity must be an object")
        raw_document_date = payload.get("document_date")
        if raw_document_date is not None and not isinstance(raw_document_date, str):
            raise ValueError("document_date must be an ISO date or null")
        raw_related_bills = payload.get("related_bill_numbers")
        if not isinstance(raw_related_bills, list) or any(
            not isinstance(value, str) for value in raw_related_bills
        ):
            raise ValueError("related_bill_numbers must be a string list")
        return cls(
            identity=CorpusDocumentIdentity.from_dict(raw_identity),
            official_url=_expect_str(payload, "official_url"),
            source_hash=_expect_str(payload, "source_hash"),
            parser_version=_expect_str(payload, "parser_version"),
            observed_at=datetime.fromisoformat(_expect_str(payload, "observed_at")),
            text_hash=_expect_str(payload, "text_hash"),
            text_characters=_expect_int(payload, "text_characters"),
            text_bytes=_expect_int(payload, "text_bytes"),
            object_hash=_expect_str(payload, "object_hash"),
            object_key=_expect_str(payload, "object_key"),
            title=_expect_str(payload, "title"),
            document_date=(
                date.fromisoformat(raw_document_date)
                if raw_document_date is not None
                else None
            ),
            related_bill_numbers=tuple(raw_related_bills),
            committee=_expect_str(payload, "committee"),
        )


@dataclass(frozen=True, slots=True)
class CorpusIngestionFailure:
    """Public, retryable accounting identity for one failed official source."""

    failure_key: str
    reason_code: str
    official_identifier: str | None = None

    def __post_init__(self) -> None:
        _validate_bounded_text(self.failure_key, field="failure_key", maximum=512)
        if not _SAFE_CODE.fullmatch(self.reason_code):
            raise ValueError("reason_code must be a stable lowercase code")
        if self.official_identifier is not None:
            _validate_bounded_text(
                self.official_identifier,
                field="official_identifier",
                maximum=512,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_key": self.failure_key,
            "reason_code": self.reason_code,
            "official_identifier": self.official_identifier,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CorpusIngestionFailure:
        raw_identifier = payload.get("official_identifier")
        if raw_identifier is not None and not isinstance(raw_identifier, str):
            raise ValueError("failure official_identifier must be a string or null")
        return cls(
            failure_key=_expect_str(payload, "failure_key"),
            reason_code=_expect_str(payload, "reason_code"),
            official_identifier=raw_identifier,
        )


@dataclass(frozen=True, slots=True)
class CorpusScopeCoverage:
    """Coverage accounting for one assembly-term/evidence-kind cell."""

    assembly_term: int
    evidence_kind: CorpusEvidenceKind
    expected_count: int | None
    succeeded_count: int
    failures: tuple[CorpusIngestionFailure, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.assembly_term, int)
            or isinstance(self.assembly_term, bool)
            or self.assembly_term < 1
        ):
            raise ValueError("assembly_term must be positive")
        if self.expected_count is not None and (
            not isinstance(self.expected_count, int)
            or isinstance(self.expected_count, bool)
            or self.expected_count < 0
        ):
            raise ValueError("expected_count must be non-negative or unknown")
        if (
            not isinstance(self.succeeded_count, int)
            or isinstance(self.succeeded_count, bool)
            or self.succeeded_count < 0
        ):
            raise ValueError("succeeded_count must be non-negative")
        keys = tuple(failure.failure_key for failure in self.failures)
        if len(keys) != len(set(keys)):
            raise ValueError("failure keys must be unique within a corpus scope")
        if self.expected_count is not None and (
            self.succeeded_count + self.failed_count > self.expected_count
        ):
            raise ValueError("corpus coverage accounts for more than expected_count")

    @property
    def failed_count(self) -> int:
        return len(self.failures)

    @property
    def unaccounted_count(self) -> int | None:
        if self.expected_count is None:
            return None
        return self.expected_count - self.succeeded_count - self.failed_count

    @property
    def complete(self) -> bool:
        return bool(
            self.expected_count is not None
            and self.failed_count == 0
            and self.unaccounted_count == 0
            and self.succeeded_count == self.expected_count
        )

    @property
    def scope_key(self) -> tuple[int, CorpusEvidenceKind]:
        return (self.assembly_term, self.evidence_kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "assembly_term": self.assembly_term,
            "evidence_kind": self.evidence_kind.value,
            "expected_count": self.expected_count,
            "succeeded_count": self.succeeded_count,
            "failed_count": self.failed_count,
            "unaccounted_count": self.unaccounted_count,
            "failures": [failure.to_dict() for failure in self.failures],
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CorpusScopeCoverage:
        raw_expected = payload.get("expected_count")
        if raw_expected is not None and (
            not isinstance(raw_expected, int) or isinstance(raw_expected, bool)
        ):
            raise ValueError("expected_count must be an integer or null")
        raw_failures = payload.get("failures")
        if not isinstance(raw_failures, list):
            raise ValueError("coverage failures must be a list")
        result = cls(
            assembly_term=_expect_int(payload, "assembly_term"),
            evidence_kind=CorpusEvidenceKind(
                _expect_str(payload, "evidence_kind")
            ),
            expected_count=raw_expected,
            succeeded_count=_expect_int(payload, "succeeded_count"),
            failures=tuple(
                CorpusIngestionFailure.from_dict(_expect_dict(item))
                for item in raw_failures
            ),
        )
        _expect_derived(payload, "failed_count", result.failed_count)
        _expect_derived(payload, "unaccounted_count", result.unaccounted_count)
        _expect_derived(payload, "complete", result.complete)
        return result


@dataclass(frozen=True, slots=True)
class LexicalShardRef:
    """One immutable hash-partitioned inverted-index shard."""

    shard_id: str
    object_hash: str
    object_key: str
    term_count: int
    posting_count: int

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{2}", self.shard_id):
            raise ValueError("lexical shard_id must be two lowercase hex digits")
        _validate_sha256(self.object_hash, field="object_hash")
        if self.object_key != lexical_object_key(self.object_hash):
            raise ValueError("lexical object key does not match object_hash")
        if (
            not isinstance(self.term_count, int)
            or isinstance(self.term_count, bool)
            or not isinstance(self.posting_count, int)
            or isinstance(self.posting_count, bool)
            or self.term_count < 1
            or self.posting_count < self.term_count
        ):
            raise ValueError("lexical shard counts are invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "shard_id": self.shard_id,
            "object_hash": self.object_hash,
            "object_key": self.object_key,
            "term_count": self.term_count,
            "posting_count": self.posting_count,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LexicalShardRef:
        return cls(
            shard_id=_expect_str(payload, "shard_id"),
            object_hash=_expect_str(payload, "object_hash"),
            object_key=_expect_str(payload, "object_key"),
            term_count=_expect_int(payload, "term_count"),
            posting_count=_expect_int(payload, "posting_count"),
        )


@dataclass(frozen=True, slots=True)
class CorpusLexicalIndexManifest:
    """Binding between a revision's document set and its inverted index."""

    lexical_version: str
    document_count: int
    document_set_hash: str
    term_count: int
    posting_count: int
    shards: tuple[LexicalShardRef, ...]

    def __post_init__(self) -> None:
        if self.lexical_version != LEXICAL_INDEX_VERSION:
            raise ValueError("unsupported lexical index version")
        _validate_sha256(self.document_set_hash, field="document_set_hash")
        counts = (self.document_count, self.term_count, self.posting_count)
        if any(
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            for count in counts
        ):
            raise ValueError("lexical index counts must be non-negative")
        if self.posting_count < self.term_count:
            raise ValueError("posting_count cannot be lower than term_count")
        shard_ids = tuple(shard.shard_id for shard in self.shards)
        if tuple(sorted(shard_ids)) != shard_ids or len(set(shard_ids)) != len(
            shard_ids
        ):
            raise ValueError("lexical shards must be unique and sorted")
        if sum(shard.term_count for shard in self.shards) != self.term_count:
            raise ValueError("lexical shard term counts do not match manifest")
        if sum(shard.posting_count for shard in self.shards) != self.posting_count:
            raise ValueError("lexical shard posting counts do not match manifest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "lexical_version": self.lexical_version,
            "document_count": self.document_count,
            "document_set_hash": self.document_set_hash,
            "term_count": self.term_count,
            "posting_count": self.posting_count,
            "shards": [shard.to_dict() for shard in self.shards],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CorpusLexicalIndexManifest:
        raw_shards = payload.get("shards")
        if not isinstance(raw_shards, list):
            raise ValueError("lexical shards must be a list")
        return cls(
            lexical_version=_expect_str(payload, "lexical_version"),
            document_count=_expect_int(payload, "document_count"),
            document_set_hash=_expect_str(payload, "document_set_hash"),
            term_count=_expect_int(payload, "term_count"),
            posting_count=_expect_int(payload, "posting_count"),
            shards=tuple(
                LexicalShardRef.from_dict(_expect_dict(item))
                for item in raw_shards
            ),
        )


@dataclass(frozen=True, slots=True)
class CorpusRevisionManifest:
    """Immutable, content-addressed description of one searchable corpus."""

    revision_id: str
    created_at: datetime
    inventory_as_of: datetime
    assembly_terms: tuple[int, ...]
    evidence_kinds: tuple[CorpusEvidenceKind, ...]
    documents: tuple[CorpusDocumentRef, ...]
    coverage: tuple[CorpusScopeCoverage, ...]
    lexical_index: CorpusLexicalIndexManifest
    parent_revision_id: str | None = None

    def __post_init__(self) -> None:
        _validate_sha256(self.revision_id, field="revision_id")
        if self.parent_revision_id is not None:
            _validate_sha256(self.parent_revision_id, field="parent_revision_id")
            if self.parent_revision_id == self.revision_id:
                raise ValueError("a corpus revision cannot be its own parent")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        if self.inventory_as_of.tzinfo is None:
            raise ValueError("inventory_as_of must be timezone-aware")
        if self.inventory_as_of.astimezone(UTC) > self.created_at.astimezone(UTC):
            raise ValueError("inventory_as_of cannot be later than revision creation")
        if not self.assembly_terms or any(
            not isinstance(term, int)
            or isinstance(term, bool)
            or term < 1
            for term in self.assembly_terms
        ):
            raise ValueError("assembly_terms must contain positive terms")
        if tuple(sorted(self.assembly_terms)) != self.assembly_terms or len(
            set(self.assembly_terms)
        ) != len(self.assembly_terms):
            raise ValueError("assembly_terms must be unique and sorted")
        if not self.evidence_kinds:
            raise ValueError("evidence_kinds must not be empty")
        kind_values = tuple(kind.value for kind in self.evidence_kinds)
        if tuple(sorted(kind_values)) != kind_values or len(set(kind_values)) != len(
            kind_values
        ):
            raise ValueError("evidence_kinds must be unique and sorted")
        identities = tuple(document.identity.identity_id for document in self.documents)
        if tuple(sorted(identities)) != identities or len(set(identities)) != len(
            identities
        ):
            raise ValueError("corpus documents must be unique and sorted by identity_id")
        expected_scopes = {
            (term, kind)
            for term in self.assembly_terms
            for kind in self.evidence_kinds
        }
        actual_scopes = {entry.scope_key for entry in self.coverage}
        if actual_scopes != expected_scopes or len(actual_scopes) != len(self.coverage):
            raise ValueError("coverage must contain the complete term/kind matrix")
        coverage_sort = tuple(
            (entry.assembly_term, entry.evidence_kind.value)
            for entry in self.coverage
        )
        if tuple(sorted(coverage_sort)) != coverage_sort:
            raise ValueError("coverage entries must be sorted")
        for document in self.documents:
            scope = (
                document.identity.assembly_term,
                document.identity.evidence_kind,
            )
            if scope not in expected_scopes:
                raise ValueError("corpus document is outside the revision scope")
        by_scope = {
            scope: sum(
                1
                for document in self.documents
                if (
                    document.identity.assembly_term,
                    document.identity.evidence_kind,
                )
                == scope
            )
            for scope in expected_scopes
        }
        for entry in self.coverage:
            if by_scope[entry.scope_key] != entry.succeeded_count:
                raise ValueError("coverage succeeded_count does not match documents")
            successes = {
                document.identity.official_identifier
                for document in self.documents
                if (
                    document.identity.assembly_term,
                    document.identity.evidence_kind,
                )
                == entry.scope_key
            }
            failed_ids = {
                failure.official_identifier
                for failure in entry.failures
                if failure.official_identifier is not None
            }
            if successes & failed_ids:
                raise ValueError("one official identifier cannot succeed and fail")
        if self.lexical_index.document_count != len(self.documents):
            raise ValueError("lexical index document count does not match revision")
        if self.lexical_index.document_set_hash != document_set_hash(self.documents):
            raise ValueError("lexical index is not bound to this document set")
        if self.revision_id != _hash_payload(self._identity_payload()):
            raise ValueError("revision_id does not match immutable manifest content")

    @property
    def complete(self) -> bool:
        return bool(
            set(self.evidence_kinds) == set(CorpusEvidenceKind)
            and len(self.evidence_kinds) == len(CorpusEvidenceKind)
            and self.lexical_index.document_count == len(self.documents)
            and self.lexical_index.document_set_hash
            == document_set_hash(self.documents)
            and all(entry.complete for entry in self.coverage)
        )

    @classmethod
    def create(
        cls,
        *,
        created_at: datetime,
        inventory_as_of: datetime,
        assembly_terms: tuple[int, ...],
        evidence_kinds: tuple[CorpusEvidenceKind, ...],
        documents: tuple[CorpusDocumentRef, ...],
        coverage: tuple[CorpusScopeCoverage, ...],
        lexical_index: CorpusLexicalIndexManifest,
        parent_revision_id: str | None = None,
    ) -> CorpusRevisionManifest:
        provisional = {
            "schema_version": CORPUS_SCHEMA_VERSION,
            "created_at": created_at.astimezone(UTC).isoformat(),
            "inventory_as_of": inventory_as_of.astimezone(UTC).isoformat(),
            "parent_revision_id": parent_revision_id,
            "assembly_terms": list(assembly_terms),
            "evidence_kinds": [kind.value for kind in evidence_kinds],
            "documents": [document.to_dict() for document in documents],
            "coverage": [entry.to_dict() for entry in coverage],
            "lexical_index": lexical_index.to_dict(),
        }
        return cls(
            revision_id=_hash_payload(provisional),
            created_at=created_at,
            inventory_as_of=inventory_as_of,
            assembly_terms=assembly_terms,
            evidence_kinds=evidence_kinds,
            documents=documents,
            coverage=coverage,
            lexical_index=lexical_index,
            parent_revision_id=parent_revision_id,
        )

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": CORPUS_SCHEMA_VERSION,
            "created_at": self.created_at.astimezone(UTC).isoformat(),
            "inventory_as_of": self.inventory_as_of.astimezone(UTC).isoformat(),
            "parent_revision_id": self.parent_revision_id,
            "assembly_terms": list(self.assembly_terms),
            "evidence_kinds": [kind.value for kind in self.evidence_kinds],
            "documents": [document.to_dict() for document in self.documents],
            "coverage": [entry.to_dict() for entry in self.coverage],
            "lexical_index": self.lexical_index.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._identity_payload(),
            "revision_id": self.revision_id,
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CorpusRevisionManifest:
        if type(payload.get("schema_version")) is not int or (
            payload.get("schema_version") != CORPUS_SCHEMA_VERSION
        ):
            raise ValueError("unsupported corpus revision schema")
        raw_terms = payload.get("assembly_terms")
        raw_kinds = payload.get("evidence_kinds")
        raw_documents = payload.get("documents")
        raw_coverage = payload.get("coverage")
        raw_index = payload.get("lexical_index")
        if not isinstance(raw_terms, list) or any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in raw_terms
        ):
            raise ValueError("assembly_terms must be an integer list")
        if not isinstance(raw_kinds, list) or any(
            not isinstance(value, str) for value in raw_kinds
        ):
            raise ValueError("evidence_kinds must be a string list")
        if not isinstance(raw_documents, list):
            raise ValueError("documents must be a list")
        if not isinstance(raw_coverage, list):
            raise ValueError("coverage must be a list")
        if not isinstance(raw_index, dict):
            raise ValueError("lexical_index must be an object")
        raw_parent = payload.get("parent_revision_id")
        if raw_parent is not None and not isinstance(raw_parent, str):
            raise ValueError("parent_revision_id must be a string or null")
        result = cls(
            revision_id=_expect_str(payload, "revision_id"),
            created_at=datetime.fromisoformat(_expect_str(payload, "created_at")),
            inventory_as_of=datetime.fromisoformat(
                _expect_str(payload, "inventory_as_of")
            ),
            parent_revision_id=raw_parent,
            assembly_terms=tuple(raw_terms),
            evidence_kinds=tuple(CorpusEvidenceKind(value) for value in raw_kinds),
            documents=tuple(
                CorpusDocumentRef.from_dict(_expect_dict(item))
                for item in raw_documents
            ),
            coverage=tuple(
                CorpusScopeCoverage.from_dict(_expect_dict(item))
                for item in raw_coverage
            ),
            lexical_index=CorpusLexicalIndexManifest.from_dict(raw_index),
        )
        _expect_derived(payload, "complete", result.complete)
        return result


def document_object_key(object_hash: str) -> str:
    _validate_sha256(object_hash, field="object_hash")
    return f"objects/documents/{object_hash[:2]}/sha256-{object_hash}.json"


def lexical_object_key(object_hash: str) -> str:
    _validate_sha256(object_hash, field="object_hash")
    return f"objects/lexical/{object_hash[:2]}/sha256-{object_hash}.json"


def revision_manifest_key(revision_id: str) -> str:
    _validate_sha256(revision_id, field="revision_id")
    return f"revisions/{revision_id}/manifest.json"


def document_set_hash(documents: tuple[CorpusDocumentRef, ...]) -> str:
    return _hash_payload(
        [
            {
                "identity_id": document.identity.identity_id,
                "object_hash": document.object_hash,
                "text_hash": document.text_hash,
            }
            for document in sorted(
                documents,
                key=lambda item: item.identity.identity_id,
            )
        ]
    )


def shard_id_for_term(term: str) -> str:
    if not term:
        raise ValueError("lexical term must not be empty")
    return hashlib.sha256(term.encode("utf-8")).hexdigest()[:2]


def _validate_official_url(value: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError as exc:
        raise ValueError("official_url is invalid") from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or not (
            hostname == "assembly.go.kr"
            or hostname.endswith(_OFFICIAL_HOST_SUFFIX)
        )
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or bool(parsed.fragment)
    ):
        raise ValueError("official_url must be credential-free National Assembly HTTPS")
    for raw_name, _value in query:
        name = re.sub(r"[^a-z0-9]+", "_", raw_name.casefold()).strip("_")
        if (
            name in _SENSITIVE_QUERY_NAMES
            or name.endswith("_token")
            or name.endswith("_api_key")
            or name.endswith("_secret")
        ):
            raise ValueError("official_url must not contain credential parameters")


def _validate_bounded_text(value: str, *, field: str, maximum: int) -> None:
    if not value or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{field} must contain between 1 and {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field} must not contain control characters")


def _validate_related_bill_numbers(
    values: tuple[str, ...],
    *,
    assembly_term: int,
) -> None:
    if any(not re.fullmatch(r"\d{7}", value) for value in values):
        raise ValueError("related bill numbers must contain exactly seven digits")
    if len(values) != len(set(values)):
        raise ValueError("related bill numbers must be unique")
    if any(int(value[:2]) != assembly_term for value in values):
        raise ValueError("related bill number belongs to another Assembly term")


def _validate_sha256(value: str, *, field: str) -> None:
    if not _SHA256.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


def _hash_payload(payload: Any) -> str:
    from .serialization import canonical_json

    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _expect_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("corpus payload item must be an object")
    return value


def _expect_str(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _expect_int(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    return value


def _expect_derived(payload: dict[str, Any], field: str, expected: object) -> None:
    actual = payload.get(field)
    if type(actual) is not type(expected) or actual != expected:
        raise ValueError(f"derived {field} does not match corpus payload")
