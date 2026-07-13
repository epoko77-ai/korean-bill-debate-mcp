"""Content-addressed corpus revisions, incremental indexing, and search."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Protocol

from .lexical import (
    CorpusSearchCandidate,
    CorpusSearchPage,
    LexicalMatchMode,
    lexical_term_frequencies,
    lexical_terms,
    paginate_candidates,
    query_fingerprint,
    rank_candidates,
)
from .models import (
    CORPUS_SCHEMA_VERSION,
    LEXICAL_INDEX_VERSION,
    CorpusDocument,
    CorpusDocumentIdentity,
    CorpusDocumentRef,
    CorpusEvidenceKind,
    CorpusIngestionFailure,
    CorpusLexicalIndexManifest,
    CorpusRevisionManifest,
    CorpusScopeCoverage,
    LexicalShardRef,
    document_object_key,
    document_set_hash,
    lexical_object_key,
    revision_manifest_key,
    shard_id_for_term,
)
from .serialization import canonical_json, decode_canonical_json
from .storage import CorpusObjectStore


class IncompleteCorpusRevisionError(RuntimeError):
    """A comprehensive query attempted to use an incomplete revision."""


class CorpusRepositoryIntegrityError(RuntimeError):
    """Stored corpus metadata, object hashes, or index bindings are invalid."""


class FullTextCorpusReader(Protocol):
    """Small engine-facing interface independent of filesystem or Blob storage."""

    def get_revision(self, revision_id: str) -> CorpusRevisionManifest | None: ...

    def get_document(self, reference: CorpusDocumentRef) -> CorpusDocument: ...

    def search_all(
        self,
        revision_id: str,
        query: str,
        *,
        match_mode: LexicalMatchMode = LexicalMatchMode.ANY,
        assembly_terms: Iterable[int] | None = None,
        evidence_kinds: Iterable[CorpusEvidenceKind] | None = None,
        require_complete: bool = True,
    ) -> tuple[CorpusSearchCandidate, ...]: ...


@dataclass(frozen=True, slots=True)
class _CoverageExpectation:
    expected_count: int | None
    failures: tuple[CorpusIngestionFailure, ...]


class CorpusRepository:
    """High-level corpus service over an immutable private object store."""

    def __init__(self, objects: CorpusObjectStore) -> None:
        self.objects = objects

    def put_document(self, document: CorpusDocument) -> CorpusDocumentRef:
        encoded = canonical_json(document.to_dict())
        object_hash = hashlib.sha256(encoded).hexdigest()
        key = document_object_key(object_hash)
        self.objects.put_immutable(key, encoded)
        return CorpusDocumentRef.from_document(document, object_hash=object_hash)

    def get_document(self, reference: CorpusDocumentRef) -> CorpusDocument:
        raw = self.objects.get(reference.object_key)
        if raw is None:
            raise CorpusRepositoryIntegrityError(
                "corpus revision refers to a missing document object"
            )
        if hashlib.sha256(raw).hexdigest() != reference.object_hash:
            raise CorpusRepositoryIntegrityError(
                "stored corpus document object hash does not match"
            )
        try:
            payload = decode_canonical_json(raw)
            if not isinstance(payload, dict):
                raise ValueError
            document = CorpusDocument.from_dict(payload)
            actual = CorpusDocumentRef.from_document(
                document,
                object_hash=reference.object_hash,
            )
        except (TypeError, ValueError, OverflowError):
            raise CorpusRepositoryIntegrityError(
                "stored corpus document object is invalid"
            ) from None
        if actual != reference:
            raise CorpusRepositoryIntegrityError(
                "stored corpus document does not match its manifest reference"
            )
        return document

    def begin_revision(
        self,
        *,
        assembly_terms: Iterable[int] | None = None,
        evidence_kinds: Iterable[CorpusEvidenceKind] | None = None,
        parent_revision_id: str | None = None,
    ) -> CorpusRevisionBuilder:
        parent = (
            self.require_revision(parent_revision_id)
            if parent_revision_id is not None
            else None
        )
        normalized_terms = _normalize_assembly_terms(
            assembly_terms if assembly_terms is not None else (
                parent.assembly_terms if parent else ()
            )
        )
        normalized_kinds = _normalize_evidence_kinds(
            evidence_kinds if evidence_kinds is not None else (
                parent.evidence_kinds if parent else ()
            )
        )
        if parent is not None:
            if not set(parent.assembly_terms).issubset(normalized_terms):
                raise ValueError(
                    "incremental revision cannot remove parent assembly terms"
                )
            if not set(parent.evidence_kinds).issubset(normalized_kinds):
                raise ValueError(
                    "incremental revision cannot remove parent evidence kinds"
                )
        return CorpusRevisionBuilder(
            self,
            assembly_terms=normalized_terms,
            evidence_kinds=normalized_kinds,
            parent=parent,
        )

    def put_revision(self, manifest: CorpusRevisionManifest) -> None:
        encoded = canonical_json(manifest.to_dict())
        self.objects.put_immutable(
            revision_manifest_key(manifest.revision_id),
            encoded,
        )

    def get_revision(self, revision_id: str) -> CorpusRevisionManifest | None:
        try:
            key = revision_manifest_key(revision_id)
        except ValueError:
            raise ValueError("revision_id must be a lowercase SHA-256 digest") from None
        raw = self.objects.get(key)
        if raw is None:
            return None
        try:
            payload = decode_canonical_json(raw)
            if not isinstance(payload, dict):
                raise ValueError
            manifest = CorpusRevisionManifest.from_dict(payload)
        except (TypeError, ValueError, OverflowError):
            raise CorpusRepositoryIntegrityError(
                "stored corpus revision manifest is invalid"
            ) from None
        if manifest.revision_id != revision_id:
            raise CorpusRepositoryIntegrityError(
                "stored corpus revision identity does not match its key"
            )
        return manifest

    def require_revision(self, revision_id: str) -> CorpusRevisionManifest:
        manifest = self.get_revision(revision_id)
        if manifest is None:
            raise LookupError("corpus revision does not exist")
        return manifest

    def search_all(
        self,
        revision_id: str,
        query: str,
        *,
        match_mode: LexicalMatchMode = LexicalMatchMode.ANY,
        assembly_terms: Iterable[int] | None = None,
        evidence_kinds: Iterable[CorpusEvidenceKind] | None = None,
        require_complete: bool = True,
    ) -> tuple[CorpusSearchCandidate, ...]:
        manifest = self.require_revision(revision_id)
        if require_complete and not manifest.complete:
            raise IncompleteCorpusRevisionError(
                "corpus revision is incomplete; comprehensive search is unavailable"
            )
        normalized_terms = lexical_terms(query)
        if not normalized_terms:
            raise ValueError("lexical query must contain at least one searchable term")
        selected_terms = _selected_assembly_terms(manifest, assembly_terms)
        selected_kinds = _selected_evidence_kinds(manifest, evidence_kinds)
        term_postings = self._postings_for_terms(manifest, normalized_terms)
        documents = {
            document.identity.identity_id: document
            for document in manifest.documents
        }
        try:
            return rank_candidates(
                documents=documents,
                term_postings=term_postings,
                normalized_terms=normalized_terms,
                match_mode=match_mode,
                allowed_assembly_terms=set(selected_terms),
                allowed_evidence_kinds=set(selected_kinds),
            )
        except ValueError:
            raise CorpusRepositoryIntegrityError(
                "corpus lexical index references an invalid document"
            ) from None

    def search_page(
        self,
        revision_id: str,
        query: str,
        *,
        page_size: int = 100,
        cursor: str | None = None,
        match_mode: LexicalMatchMode = LexicalMatchMode.ANY,
        assembly_terms: Iterable[int] | None = None,
        evidence_kinds: Iterable[CorpusEvidenceKind] | None = None,
        require_complete: bool = True,
    ) -> CorpusSearchPage:
        manifest = self.require_revision(revision_id)
        normalized_terms = lexical_terms(query)
        if not normalized_terms:
            raise ValueError("lexical query must contain at least one searchable term")
        selected_terms = _selected_assembly_terms(manifest, assembly_terms)
        selected_kinds = _selected_evidence_kinds(manifest, evidence_kinds)
        candidates = self.search_all(
            revision_id,
            query,
            match_mode=match_mode,
            assembly_terms=selected_terms,
            evidence_kinds=selected_kinds,
            require_complete=require_complete,
        )
        fingerprint = query_fingerprint(
            revision_id=revision_id,
            normalized_terms=normalized_terms,
            match_mode=match_mode,
            assembly_terms=selected_terms,
            evidence_kinds=selected_kinds,
        )
        return paginate_candidates(
            revision_id=revision_id,
            revision_complete=manifest.complete,
            normalized_terms=normalized_terms,
            match_mode=match_mode,
            candidates=candidates,
            fingerprint=fingerprint,
            page_size=page_size,
            cursor=cursor,
        )

    def _postings_for_terms(
        self,
        manifest: CorpusRevisionManifest,
        terms: tuple[str, ...],
    ) -> dict[str, dict[str, int]]:
        refs = {reference.shard_id: reference for reference in manifest.lexical_index.shards}
        loaded: dict[str, dict[str, dict[str, int]]] = {}
        result: dict[str, dict[str, int]] = {}
        for term in terms:
            shard_id = shard_id_for_term(term)
            reference = refs.get(shard_id)
            if reference is None:
                result[term] = {}
                continue
            shard = loaded.get(shard_id)
            if shard is None:
                shard = self._read_shard(reference)
                loaded[shard_id] = shard
            result[term] = dict(shard.get(term, {}))
        return result

    def _write_shard(
        self,
        shard_id: str,
        postings: dict[str, dict[str, int]],
    ) -> LexicalShardRef:
        if any(shard_id_for_term(term) != shard_id for term in postings):
            raise ValueError("lexical term does not belong to the requested shard")
        normalized = {
            term: [
                {"identity_id": identity_id, "frequency": frequency}
                for identity_id, frequency in sorted(values.items())
            ]
            for term, values in sorted(postings.items())
            if values
        }
        if not normalized:
            raise ValueError("empty lexical shards are not stored")
        payload = {
            "schema_version": CORPUS_SCHEMA_VERSION,
            "lexical_version": LEXICAL_INDEX_VERSION,
            "shard_id": shard_id,
            "postings": normalized,
        }
        encoded = canonical_json(payload)
        object_hash = hashlib.sha256(encoded).hexdigest()
        key = lexical_object_key(object_hash)
        self.objects.put_immutable(key, encoded)
        return LexicalShardRef(
            shard_id=shard_id,
            object_hash=object_hash,
            object_key=key,
            term_count=len(normalized),
            posting_count=sum(len(values) for values in normalized.values()),
        )

    def _read_shard(
        self,
        reference: LexicalShardRef,
    ) -> dict[str, dict[str, int]]:
        raw = self.objects.get(reference.object_key)
        if raw is None:
            raise CorpusRepositoryIntegrityError(
                "corpus revision refers to a missing lexical shard"
            )
        if hashlib.sha256(raw).hexdigest() != reference.object_hash:
            raise CorpusRepositoryIntegrityError(
                "stored lexical shard object hash does not match"
            )
        try:
            payload = decode_canonical_json(raw)
            if not isinstance(payload, dict):
                raise ValueError
            if (
                type(payload.get("schema_version")) is not int
                or payload.get("schema_version") != CORPUS_SCHEMA_VERSION
                or payload.get("lexical_version") != LEXICAL_INDEX_VERSION
                or payload.get("shard_id") != reference.shard_id
            ):
                raise ValueError
            raw_postings = payload.get("postings")
            if not isinstance(raw_postings, dict):
                raise ValueError
            postings: dict[str, dict[str, int]] = {}
            for term, raw_values in raw_postings.items():
                if (
                    not isinstance(term, str)
                    or not term
                    or shard_id_for_term(term) != reference.shard_id
                    or not isinstance(raw_values, list)
                ):
                    raise ValueError
                values: dict[str, int] = {}
                ordered_ids: list[str] = []
                for raw_value in raw_values:
                    if not isinstance(raw_value, dict):
                        raise ValueError
                    identity_id = raw_value.get("identity_id")
                    frequency = raw_value.get("frequency")
                    if (
                        not isinstance(identity_id, str)
                        or len(identity_id) != 64
                        or any(
                            character not in "0123456789abcdef"
                            for character in identity_id
                        )
                        or not isinstance(frequency, int)
                        or isinstance(frequency, bool)
                        or frequency < 1
                    ):
                        raise ValueError
                    if identity_id in values:
                        raise ValueError
                    values[identity_id] = frequency
                    ordered_ids.append(identity_id)
                if ordered_ids != sorted(ordered_ids):
                    raise ValueError
                postings[term] = values
            if list(raw_postings) != sorted(raw_postings):
                raise ValueError
            if len(postings) != reference.term_count or sum(
                len(values) for values in postings.values()
            ) != reference.posting_count:
                raise ValueError
            return postings
        except (TypeError, ValueError, OverflowError):
            raise CorpusRepositoryIntegrityError(
                "stored lexical shard is invalid"
            ) from None


class CorpusRevisionBuilder:
    """Incrementally upsert documents and publish a new immutable revision."""

    def __init__(
        self,
        repository: CorpusRepository,
        *,
        assembly_terms: tuple[int, ...],
        evidence_kinds: tuple[CorpusEvidenceKind, ...],
        parent: CorpusRevisionManifest | None,
    ) -> None:
        self.repository = repository
        self.assembly_terms = assembly_terms
        self.evidence_kinds = evidence_kinds
        self.parent = parent
        self._base_documents = {
            document.identity.identity_id: document
            for document in (parent.documents if parent else ())
        }
        self._documents = dict(self._base_documents)
        parent_coverage = {
            entry.scope_key: _CoverageExpectation(
                entry.expected_count,
                entry.failures,
            )
            for entry in (parent.coverage if parent else ())
        }
        self._coverage = {
            (term, kind): parent_coverage.get(
                (term, kind),
                _CoverageExpectation(None, ()),
            )
            for term in assembly_terms
            for kind in evidence_kinds
        }
        self._published = False

    def upsert_document(self, document: CorpusDocument) -> CorpusDocumentRef:
        self._ensure_mutable()
        self._require_scope(
            document.identity.assembly_term,
            document.identity.evidence_kind,
        )
        reference = self.repository.put_document(document)
        self._documents[document.identity.identity_id] = reference
        return reference

    def upsert_documents(
        self,
        documents: Iterable[CorpusDocument],
    ) -> tuple[CorpusDocumentRef, ...]:
        return tuple(self.upsert_document(document) for document in documents)

    def remove_document(self, identity: CorpusDocumentIdentity) -> None:
        self._ensure_mutable()
        self._require_scope(identity.assembly_term, identity.evidence_kind)
        self._documents.pop(identity.identity_id, None)

    def set_expected_count(
        self,
        assembly_term: int,
        evidence_kind: CorpusEvidenceKind,
        expected_count: int | None,
    ) -> None:
        self._ensure_mutable()
        scope = self._require_scope(assembly_term, evidence_kind)
        if expected_count is not None and (
            not isinstance(expected_count, int)
            or isinstance(expected_count, bool)
            or expected_count < 0
        ):
            raise ValueError("expected_count must be non-negative or unknown")
        current = self._coverage[scope]
        self._coverage[scope] = _CoverageExpectation(
            expected_count,
            current.failures,
        )

    def replace_failures(
        self,
        assembly_term: int,
        evidence_kind: CorpusEvidenceKind,
        failures: Iterable[CorpusIngestionFailure],
    ) -> None:
        self._ensure_mutable()
        scope = self._require_scope(assembly_term, evidence_kind)
        normalized = tuple(sorted(failures, key=lambda item: item.failure_key))
        if len({failure.failure_key for failure in normalized}) != len(normalized):
            raise ValueError("failure keys must be unique within a corpus scope")
        current = self._coverage[scope]
        self._coverage[scope] = _CoverageExpectation(
            current.expected_count,
            normalized,
        )

    def record_failure(
        self,
        assembly_term: int,
        evidence_kind: CorpusEvidenceKind,
        failure: CorpusIngestionFailure,
    ) -> None:
        self._ensure_mutable()
        scope = self._require_scope(assembly_term, evidence_kind)
        current = self._coverage[scope]
        by_key = {item.failure_key: item for item in current.failures}
        existing = by_key.get(failure.failure_key)
        if existing is not None and existing != failure:
            raise ValueError("failure key already records a different failure")
        by_key[failure.failure_key] = failure
        self._coverage[scope] = _CoverageExpectation(
            current.expected_count,
            tuple(by_key[key] for key in sorted(by_key)),
        )

    def clear_failure(
        self,
        assembly_term: int,
        evidence_kind: CorpusEvidenceKind,
        failure_key: str,
    ) -> None:
        self._ensure_mutable()
        scope = self._require_scope(assembly_term, evidence_kind)
        current = self._coverage[scope]
        self._coverage[scope] = _CoverageExpectation(
            current.expected_count,
            tuple(
                failure
                for failure in current.failures
                if failure.failure_key != failure_key
            ),
        )

    def publish(
        self,
        *,
        inventory_as_of: datetime,
        created_at: datetime | None = None,
    ) -> CorpusRevisionManifest:
        self._ensure_mutable()
        timestamp = created_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        if inventory_as_of.tzinfo is None:
            raise ValueError("inventory_as_of must be timezone-aware")
        if inventory_as_of.astimezone(UTC) > timestamp.astimezone(UTC):
            raise ValueError("inventory_as_of cannot be later than created_at")
        if self.parent is not None and timestamp.astimezone(UTC) < (
            self.parent.created_at.astimezone(UTC)
        ):
            raise ValueError("child revision cannot predate its parent")
        documents = tuple(
            self._documents[key] for key in sorted(self._documents)
        )
        coverage = self._build_coverage(documents)
        lexical_index = self._build_lexical_index(documents)
        manifest = CorpusRevisionManifest.create(
            created_at=timestamp,
            inventory_as_of=inventory_as_of,
            assembly_terms=self.assembly_terms,
            evidence_kinds=self.evidence_kinds,
            documents=documents,
            coverage=coverage,
            lexical_index=lexical_index,
            parent_revision_id=(self.parent.revision_id if self.parent else None),
        )
        self.repository.put_revision(manifest)
        self._published = True
        return manifest

    def _build_coverage(
        self,
        documents: tuple[CorpusDocumentRef, ...],
    ) -> tuple[CorpusScopeCoverage, ...]:
        counts: dict[tuple[int, CorpusEvidenceKind], int] = {
            scope: 0 for scope in self._coverage
        }
        for document in documents:
            scope = (
                document.identity.assembly_term,
                document.identity.evidence_kind,
            )
            counts[scope] += 1
        return tuple(
            CorpusScopeCoverage(
                assembly_term=term,
                evidence_kind=kind,
                expected_count=self._coverage[(term, kind)].expected_count,
                succeeded_count=counts[(term, kind)],
                failures=self._coverage[(term, kind)].failures,
            )
            for term, kind in sorted(
                self._coverage,
                key=lambda scope: (scope[0], scope[1].value),
            )
        )

    def _build_lexical_index(
        self,
        documents: tuple[CorpusDocumentRef, ...],
    ) -> CorpusLexicalIndexManifest:
        base_refs = {
            reference.shard_id: reference
            for reference in (
                self.parent.lexical_index.shards if self.parent else ()
            )
        }
        changed_ids = {
            identity_id
            for identity_id in set(self._base_documents) | set(self._documents)
            if self._base_documents.get(identity_id)
            != self._documents.get(identity_id)
        }
        affected_shards: set[str] = set()
        updated_refs = dict(base_refs)
        # A full Assembly term can contain thousands of large documents.  Do
        # not retain every document's term-frequency map in RAM.  Spool new
        # postings into 256 deterministic shard files, then materialize only
        # one existing/new shard at a time.  A crash can leave immutable,
        # unreferenced corpus objects but can never expose a partial manifest.
        with tempfile.TemporaryDirectory(prefix="kbd-corpus-index-") as directory:
            spool = _LexicalChangeSpool(Path(directory))
            try:
                for identity_id in sorted(changed_ids):
                    old_reference = self._base_documents.get(identity_id)
                    if old_reference is not None:
                        old_document = self.repository.get_document(old_reference)
                        old_frequencies = lexical_term_frequencies(
                            _searchable_text(old_document)
                        )
                        affected_shards.update(
                            shard_id_for_term(term) for term in old_frequencies
                        )
                    new_reference = self._documents.get(identity_id)
                    if new_reference is not None:
                        new_document = self.repository.get_document(new_reference)
                        new_frequencies = lexical_term_frequencies(
                            _searchable_text(new_document)
                        )
                        spool.add(identity_id, new_frequencies)
                        affected_shards.update(
                            shard_id_for_term(term) for term in new_frequencies
                        )
                spool.close_writers()

                for shard_id in sorted(affected_shards):
                    reference = base_refs.get(shard_id)
                    postings = (
                        self.repository._read_shard(reference)  # noqa: SLF001
                        if reference is not None
                        else {}
                    )
                    for term in tuple(postings):
                        remaining = {
                            identity_id: frequency
                            for identity_id, frequency in postings[term].items()
                            if identity_id not in changed_ids
                        }
                        if remaining:
                            postings[term] = remaining
                        else:
                            del postings[term]
                    for identity_id, frequencies in spool.iter_shard(shard_id):
                        for term, frequency in frequencies.items():
                            postings.setdefault(term, {})[identity_id] = frequency
                    if postings:
                        updated_refs[shard_id] = self.repository._write_shard(  # noqa: SLF001
                            shard_id,
                            postings,
                        )
                    else:
                        updated_refs.pop(shard_id, None)
            finally:
                spool.close_writers()

        references = tuple(updated_refs[key] for key in sorted(updated_refs))
        return CorpusLexicalIndexManifest(
            lexical_version=LEXICAL_INDEX_VERSION,
            document_count=len(documents),
            document_set_hash=document_set_hash(documents),
            term_count=sum(reference.term_count for reference in references),
            posting_count=sum(
                reference.posting_count for reference in references
            ),
            shards=references,
        )

    def _require_scope(
        self,
        assembly_term: int,
        evidence_kind: CorpusEvidenceKind,
    ) -> tuple[int, CorpusEvidenceKind]:
        scope = (assembly_term, evidence_kind)
        if scope not in self._coverage:
            raise ValueError("document or coverage is outside this revision scope")
        return scope

    def _ensure_mutable(self) -> None:
        if self._published:
            raise RuntimeError("a published corpus revision builder is immutable")


def _searchable_text(document: CorpusDocument) -> str:
    return f"{document.title}\n{document.text}" if document.title else document.text


class _LexicalChangeSpool:
    """Disk-backed new postings grouped by lexical shard."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._writers: dict[str, BinaryIO] = {}

    def add(self, identity_id: str, frequencies: dict[str, int]) -> None:
        by_shard: dict[str, list[tuple[str, int]]] = {}
        for term, frequency in frequencies.items():
            by_shard.setdefault(shard_id_for_term(term), []).append(
                (term, frequency)
            )
        for shard_id, terms in by_shard.items():
            writer = self._writers.get(shard_id)
            if writer is None:
                writer = (self.root / f"{shard_id}.jsonl").open("ab")
                self._writers[shard_id] = writer
            writer.write(
                canonical_json(
                    {
                        "identity_id": identity_id,
                        "terms": [list(item) for item in sorted(terms)],
                    }
                )
                + b"\n"
            )

    def close_writers(self) -> None:
        for writer in self._writers.values():
            if not writer.closed:
                writer.close()

    def iter_shard(self, shard_id: str) -> Iterable[tuple[str, dict[str, int]]]:
        path = self.root / f"{shard_id}.jsonl"
        if not path.exists():
            return
        with path.open("rb") as stream:
            for raw_line in stream:
                raw = raw_line.removesuffix(b"\n")
                payload = decode_canonical_json(raw)
                if not isinstance(payload, dict):
                    raise CorpusRepositoryIntegrityError(
                        "temporary lexical spool record is invalid"
                    )
                identity_id = payload.get("identity_id")
                raw_terms = payload.get("terms")
                if (
                    not isinstance(identity_id, str)
                    or len(identity_id) != 64
                    or not isinstance(raw_terms, list)
                ):
                    raise CorpusRepositoryIntegrityError(
                        "temporary lexical spool record is invalid"
                    )
                frequencies: dict[str, int] = {}
                for item in raw_terms:
                    if (
                        not isinstance(item, list)
                        or len(item) != 2
                        or not isinstance(item[0], str)
                        or not isinstance(item[1], int)
                        or isinstance(item[1], bool)
                        or item[1] < 1
                        or shard_id_for_term(item[0]) != shard_id
                        or item[0] in frequencies
                    ):
                        raise CorpusRepositoryIntegrityError(
                            "temporary lexical spool record is invalid"
                        )
                    frequencies[item[0]] = item[1]
                yield identity_id, frequencies


def _normalize_assembly_terms(values: Iterable[int]) -> tuple[int, ...]:
    raw_terms = tuple(values)
    if not raw_terms or any(
        not isinstance(term, int) or isinstance(term, bool) or term < 1
        for term in raw_terms
    ):
        raise ValueError("assembly_terms must contain positive integers")
    return tuple(sorted(set(raw_terms)))


def _normalize_evidence_kinds(
    values: Iterable[CorpusEvidenceKind],
) -> tuple[CorpusEvidenceKind, ...]:
    try:
        kinds = tuple(
            sorted(
                {CorpusEvidenceKind(value) for value in values},
                key=lambda item: item.value,
            )
        )
    except (TypeError, ValueError):
        raise ValueError("evidence_kinds contains an unsupported value") from None
    if not kinds:
        raise ValueError("evidence_kinds must not be empty")
    return kinds


def _selected_assembly_terms(
    manifest: CorpusRevisionManifest,
    requested: Iterable[int] | None,
) -> tuple[int, ...]:
    if requested is None:
        return manifest.assembly_terms
    selected = _normalize_assembly_terms(requested)
    if not set(selected).issubset(manifest.assembly_terms):
        raise ValueError("requested assembly_terms are outside the corpus revision")
    return selected


def _selected_evidence_kinds(
    manifest: CorpusRevisionManifest,
    requested: Iterable[CorpusEvidenceKind] | None,
) -> tuple[CorpusEvidenceKind, ...]:
    if requested is None:
        return manifest.evidence_kinds
    selected = _normalize_evidence_kinds(requested)
    if not set(selected).issubset(manifest.evidence_kinds):
        raise ValueError("requested evidence_kinds are outside the corpus revision")
    return selected
