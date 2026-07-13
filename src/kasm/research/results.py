"""Stable, lossless pages over a completed or explicitly partial investigation."""

from __future__ import annotations

import hashlib
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .contracts import CoverageLedger, EvidencePage, EvidenceType, ResearchContract, StableCursor

_OFFICIAL_HOSTS = {
    "open.assembly.go.kr",
    "record.assembly.go.kr",
    "likms.assembly.go.kr",
}
PUBLIC_INLINE_TEXT_CHARACTERS = 4_000
SNAPSHOT_INDEX_SHARD_SIZE = 100
SNAPSHOT_INDEX_LOOKUP_BUCKETS = 64
SNAPSHOT_TEXT_SHARD_CHARACTERS = 200_000
SNAPSHOT_TEXT_SHARD_RECORDS = 20


@dataclass(frozen=True, slots=True)
class EvidenceCitation:
    """A verifiable locator into one official National Assembly source."""

    official_url: str
    source_locator: str
    source_hash: str
    retrieved_at: datetime

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.official_url)
        if parsed.scheme != "https" or parsed.hostname not in _OFFICIAL_HOSTS:
            raise ValueError("evidence citation must use an official Assembly HTTPS URL")
        if not self.source_locator.strip():
            raise ValueError("source_locator is required")
        if len(self.source_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.source_hash
        ):
            raise ValueError("source_hash must be a SHA-256 hex digest")
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")

    def to_dict(self) -> dict[str, str]:
        return {
            "official_url": self.official_url,
            "source_locator": self.source_locator,
            "source_hash": self.source_hash,
            "retrieved_at": self.retrieved_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """One complete evidence unit; its text is never shortened by this layer."""

    id: str
    evidence_type: EvidenceType
    sort_key: str
    title: str
    text: str
    citation: EvidenceCitation
    metadata: tuple[tuple[str, str | int | float | bool | None], ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.sort_key.strip() or not self.title.strip():
            raise ValueError("evidence id, sort key, and title are required")
        if not self.text.strip():
            raise ValueError("evidence text must not be empty")
        names = [name for name, _value in self.metadata]
        if len(names) != len(set(names)) or any(not name.strip() for name in names):
            raise ValueError("evidence metadata names must be non-empty and unique")
        object.__setattr__(self, "metadata", tuple(sorted(self.metadata)))

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "evidence_type": self.evidence_type.value,
            "sort_key": self.sort_key,
            "title": self.title,
            "text": self.text,
            "text_characters": len(self.text),
            "text_hash": self.text_hash,
            "metadata": dict(self.metadata),
            "citation": self.citation.to_dict(),
        }

    def to_index_dict(
        self,
        *,
        inline_text_characters: int = PUBLIC_INLINE_TEXT_CHARACTERS,
    ) -> dict[str, Any]:
        """Return bounded discovery metadata without losing the stored text.

        Short evidence remains convenient to read inline.  Longer evidence is
        represented by its exact hash and size and must be exhausted through
        ``get_evidence_document``.  No preview substring is returned, so a
        caller can never mistake a shortened value for the complete source.
        """

        if inline_text_characters < 0:
            raise ValueError("inline_text_characters must not be negative")
        payload = self.to_dict()
        if len(self.text) <= inline_text_characters:
            payload["text_inline_complete"] = True
            return payload
        payload.pop("text")
        payload.update(
            {
                "text_inline_complete": False,
                "text_delivery": "get_evidence_document",
            }
        )
        return payload


@dataclass(frozen=True, slots=True)
class EvidenceIndexEntry:
    """Compact public evidence metadata stored separately from source text."""

    id: str
    evidence_type: EvidenceType
    sort_key: str
    title: str
    text_characters: int
    text_hash: str
    citation: EvidenceCitation
    metadata: tuple[tuple[str, str | int | float | bool | None], ...] = ()
    inline_text: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.sort_key.strip() or not self.title.strip():
            raise ValueError("evidence index identity is required")
        if self.text_characters < 1:
            raise ValueError("evidence index text size must be positive")
        if len(self.text_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.text_hash
        ):
            raise ValueError("evidence index text_hash must be a SHA-256 hex digest")
        if self.inline_text is not None:
            if len(self.inline_text) != self.text_characters:
                raise ValueError("inline evidence text size does not match its index")
            if hashlib.sha256(self.inline_text.encode()).hexdigest() != self.text_hash:
                raise ValueError("inline evidence text hash does not match its index")
        names = [name for name, _value in self.metadata]
        if len(names) != len(set(names)) or any(not name.strip() for name in names):
            raise ValueError("evidence index metadata names must be non-empty and unique")
        object.__setattr__(self, "metadata", tuple(sorted(self.metadata)))

    @classmethod
    def from_record(
        cls,
        record: EvidenceRecord,
        *,
        inline_text_characters: int = PUBLIC_INLINE_TEXT_CHARACTERS,
    ) -> EvidenceIndexEntry:
        if inline_text_characters < 0:
            raise ValueError("inline_text_characters must not be negative")
        return cls(
            id=record.id,
            evidence_type=record.evidence_type,
            sort_key=record.sort_key,
            title=record.title,
            text_characters=len(record.text),
            text_hash=record.text_hash,
            citation=record.citation,
            metadata=record.metadata,
            inline_text=(
                record.text if len(record.text) <= inline_text_characters else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "evidence_type": self.evidence_type.value,
            "sort_key": self.sort_key,
            "title": self.title,
            "text_characters": self.text_characters,
            "text_hash": self.text_hash,
            "metadata": dict(self.metadata),
            "citation": self.citation.to_dict(),
            "text_inline_complete": self.inline_text is not None,
        }
        if self.inline_text is None:
            payload["text_delivery"] = "get_evidence_document"
        else:
            payload["text"] = self.inline_text
        return payload


@dataclass(frozen=True, slots=True)
class SnapshotIndexShard:
    """A bounded immutable slice of the public evidence index."""

    number: int
    start_position: int
    entries: tuple[EvidenceIndexEntry, ...]

    def __post_init__(self) -> None:
        if self.number < 0 or self.start_position < 0 or not self.entries:
            raise ValueError("snapshot index shard identity is invalid")
        if len(self.entries) > SNAPSHOT_INDEX_SHARD_SIZE:
            raise ValueError("snapshot index shard exceeds its bounded size")
        ordered = tuple(sorted(self.entries, key=lambda item: (item.sort_key, item.id)))
        if ordered != self.entries:
            raise ValueError("snapshot index shard entries must be ordered")

    @property
    def end_position(self) -> int:
        return self.start_position + len(self.entries)


@dataclass(frozen=True, slots=True)
class SnapshotIndexShardDescriptor:
    """Small routing metadata for one evidence-index shard."""

    number: int
    start_position: int
    end_position: int
    first_sort_key: str
    first_id: str
    last_sort_key: str
    last_id: str

    @classmethod
    def from_shard(cls, shard: SnapshotIndexShard) -> SnapshotIndexShardDescriptor:
        return cls(
            number=shard.number,
            start_position=shard.start_position,
            end_position=shard.end_position,
            first_sort_key=shard.entries[0].sort_key,
            first_id=shard.entries[0].id,
            last_sort_key=shard.entries[-1].sort_key,
            last_id=shard.entries[-1].id,
        )


@dataclass(frozen=True, slots=True)
class SnapshotIndexLookupBucket:
    """Bounded hash routing from public evidence ids to compact shards."""

    number: int
    entries: tuple[tuple[str, int, int | None], ...]

    def __post_init__(self) -> None:
        if self.number < 0 or not self.entries:
            raise ValueError("snapshot index lookup bucket identity is invalid")
        ids = [item_id for item_id, _index_shard, _text_shard in self.entries]
        if len(ids) != len(set(ids)) or any(not item_id for item_id in ids):
            raise ValueError("snapshot index lookup bucket contains invalid ids")
        if any(
            index_shard < 0 or (text_shard is not None and text_shard < 0)
            for _item_id, index_shard, text_shard in self.entries
        ):
            raise ValueError("snapshot index lookup bucket contains an invalid shard")
        if tuple(sorted(self.entries)) != self.entries:
            raise ValueError("snapshot index lookup bucket entries must be ordered")


@dataclass(frozen=True, slots=True)
class EvidenceTextShard:
    """Bounded exact evidence text stored apart from the discovery index."""

    number: int
    records: tuple[EvidenceRecord, ...]

    def __post_init__(self) -> None:
        if self.number < 0 or not self.records:
            raise ValueError("evidence text shard identity is invalid")
        if len(self.records) > SNAPSHOT_TEXT_SHARD_RECORDS:
            raise ValueError("evidence text shard contains too many records")
        ids = [record.id for record in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence text shard contains duplicate records")
        ordered = tuple(sorted(self.records, key=lambda item: (item.sort_key, item.id)))
        if ordered != self.records:
            raise ValueError("evidence text shard records must be ordered")
        if (
            len(self.records) > 1
            and sum(len(record.text) for record in self.records)
            > SNAPSHOT_TEXT_SHARD_CHARACTERS
        ):
            raise ValueError("evidence text shard exceeds its bounded character size")


@dataclass(frozen=True, slots=True)
class ResearchSnapshotIndex:
    """Tiny routing manifest for paginating without loading source texts."""

    research_id: str
    query_fingerprint: str
    index_revision: str
    build_sha: str
    coverage: CoverageLedger
    evidence_total: int
    shards: tuple[SnapshotIndexShardDescriptor, ...]
    full_text_required_total: int
    first_full_text_id: str | None
    lookup_bucket_count: int = SNAPSHOT_INDEX_LOOKUP_BUCKETS
    inline_text_characters: int = PUBLIC_INLINE_TEXT_CHARACTERS

    def __post_init__(self) -> None:
        if not self.research_id or not self.index_revision or not self.build_sha:
            raise ValueError("snapshot index identity is required")
        if len(self.query_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.query_fingerprint
        ):
            raise ValueError("snapshot index query fingerprint is invalid")
        if (
            self.evidence_total < 0
            or not 0 <= self.full_text_required_total <= self.evidence_total
            or self.inline_text_characters < 0
            or not 1 <= self.lookup_bucket_count <= 1024
        ):
            raise ValueError("snapshot index sizes must not be negative")
        if (self.full_text_required_total == 0) != (self.first_full_text_id is None):
            raise ValueError("snapshot index full-text routing is inconsistent")
        position = 0
        previous_last: tuple[str, str] | None = None
        for number, shard in enumerate(self.shards):
            if (
                shard.number != number
                or shard.start_position != position
                or shard.end_position <= shard.start_position
            ):
                raise ValueError("snapshot index shard coverage is not contiguous")
            first = (shard.first_sort_key, shard.first_id)
            last = (shard.last_sort_key, shard.last_id)
            if first > last or (previous_last is not None and previous_last >= first):
                raise ValueError("snapshot index shard boundaries are not ordered")
            previous_last = last
            position = shard.end_position
        if position != self.evidence_total:
            raise ValueError("snapshot index does not account for every evidence record")


@dataclass(frozen=True, slots=True)
class ResearchResultIndexPage:
    """One bounded page produced from the compact evidence index."""

    manifest: ResearchSnapshotIndex
    page: EvidencePage
    evidence: tuple[EvidenceIndexEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        required = [item.id for item in self.evidence if item.inline_text is None]
        return {
            "research_id": self.manifest.research_id,
            "query_fingerprint": self.manifest.query_fingerprint,
            "index_revision": self.manifest.index_revision,
            "build_sha": self.manifest.build_sha,
            "complete": self.manifest.coverage.complete and self.page.complete,
            "coverage": self.manifest.coverage.to_dict(),
            "page": self.page.to_dict(),
            "evidence": [item.to_dict() for item in self.evidence],
            "full_text_required_ids": required,
            "full_text_required_count": len(required),
            "inline_text_character_limit": self.manifest.inline_text_characters,
            "full_text_required_total": self.manifest.full_text_required_total,
            "first_full_text_required_id": self.manifest.first_full_text_id,
        }


def build_snapshot_index(
    snapshot: ResearchSnapshot,
    *,
    inline_text_characters: int = PUBLIC_INLINE_TEXT_CHARACTERS,
) -> tuple[
    ResearchSnapshotIndex,
    tuple[SnapshotIndexShard, ...],
    tuple[SnapshotIndexLookupBucket, ...],
    tuple[EvidenceTextShard, ...],
]:
    """Split public discovery metadata from potentially huge official text."""

    entries = tuple(
        EvidenceIndexEntry.from_record(
            item,
            inline_text_characters=inline_text_characters,
        )
        for item in snapshot.evidence
    )
    shards = tuple(
        SnapshotIndexShard(
            number=start // SNAPSHOT_INDEX_SHARD_SIZE,
            start_position=start,
            entries=entries[start : start + SNAPSHOT_INDEX_SHARD_SIZE],
        )
        for start in range(0, len(entries), SNAPSHOT_INDEX_SHARD_SIZE)
    )
    text_shards = _build_evidence_text_shards(snapshot, inline_text_characters)
    text_shard_by_id = {
        record.id: shard.number for shard in text_shards for record in shard.records
    }
    lookup_values: list[list[tuple[str, int, int | None]]] = [
        [] for _number in range(SNAPSHOT_INDEX_LOOKUP_BUCKETS)
    ]
    for shard in shards:
        for entry in shard.entries:
            bucket_number = snapshot_index_lookup_bucket(
                entry.id,
                SNAPSHOT_INDEX_LOOKUP_BUCKETS,
            )
            lookup_values[bucket_number].append(
                (entry.id, shard.number, text_shard_by_id.get(entry.id))
            )
    lookup_buckets = tuple(
        SnapshotIndexLookupBucket(number, tuple(sorted(values)))
        for number, values in enumerate(lookup_values)
        if values
    )
    full_text_ids = tuple(entry.id for entry in entries if entry.inline_text is None)
    manifest = ResearchSnapshotIndex(
        research_id=snapshot.research_id,
        query_fingerprint=snapshot.query_fingerprint,
        index_revision=snapshot.index_revision,
        build_sha=snapshot.build_sha,
        coverage=snapshot.coverage,
        evidence_total=len(entries),
        shards=tuple(SnapshotIndexShardDescriptor.from_shard(shard) for shard in shards),
        full_text_required_total=len(full_text_ids),
        first_full_text_id=full_text_ids[0] if full_text_ids else None,
        lookup_bucket_count=SNAPSHOT_INDEX_LOOKUP_BUCKETS,
        inline_text_characters=inline_text_characters,
    )
    return manifest, shards, lookup_buckets, text_shards


def _build_evidence_text_shards(
    snapshot: ResearchSnapshot,
    inline_text_characters: int,
) -> tuple[EvidenceTextShard, ...]:
    shards: list[EvidenceTextShard] = []
    current: list[EvidenceRecord] = []
    current_characters = 0

    def flush() -> None:
        nonlocal current, current_characters
        if current:
            shards.append(EvidenceTextShard(len(shards), tuple(current)))
            current = []
            current_characters = 0

    for record in snapshot.evidence:
        if len(record.text) <= inline_text_characters:
            continue
        if current and (
            len(current) >= SNAPSHOT_TEXT_SHARD_RECORDS
            or current_characters + len(record.text) > SNAPSHOT_TEXT_SHARD_CHARACTERS
        ):
            flush()
        current.append(record)
        current_characters += len(record.text)
    flush()
    return tuple(shards)


def snapshot_index_lookup_bucket(evidence_id: str, bucket_count: int) -> int:
    if not evidence_id or not 1 <= bucket_count <= 1024:
        raise ValueError("snapshot index lookup scope is invalid")
    digest = hashlib.sha256(evidence_id.encode()).digest()
    return int.from_bytes(digest[:4], "big") % bucket_count


@dataclass(frozen=True, slots=True)
class ResearchResultPage:
    """One reproducible page with explicit total and coverage accounting."""

    research_id: str
    query_fingerprint: str
    index_revision: str
    build_sha: str
    coverage: CoverageLedger
    page: EvidencePage
    evidence: tuple[EvidenceRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "research_id": self.research_id,
            "query_fingerprint": self.query_fingerprint,
            "index_revision": self.index_revision,
            "build_sha": self.build_sha,
            "complete": self.coverage.complete and self.page.complete,
            "coverage": self.coverage.to_dict(),
            "page": self.page.to_dict(),
            "evidence": [item.to_dict() for item in self.evidence],
        }

    def to_index_dict(
        self,
        *,
        inline_text_characters: int = PUBLIC_INLINE_TEXT_CHARACTERS,
    ) -> dict[str, Any]:
        """Return a transport-bounded evidence index with lossless drill-down."""

        payload = {
            "research_id": self.research_id,
            "query_fingerprint": self.query_fingerprint,
            "index_revision": self.index_revision,
            "build_sha": self.build_sha,
            "complete": self.coverage.complete and self.page.complete,
            "coverage": self.coverage.to_dict(),
            "page": self.page.to_dict(),
            "evidence": [
                item.to_index_dict(inline_text_characters=inline_text_characters)
                for item in self.evidence
            ],
        }
        required = [
            item.id for item in self.evidence if len(item.text) > inline_text_characters
        ]
        payload["full_text_required_ids"] = required
        payload["full_text_required_count"] = len(required)
        payload["inline_text_character_limit"] = inline_text_characters
        return payload


@dataclass(frozen=True, slots=True)
class ResearchSnapshot:
    """Immutable research output bound to one contract and index revision."""

    research_id: str
    contract: ResearchContract
    index_revision: str
    build_sha: str
    coverage: CoverageLedger
    evidence: tuple[EvidenceRecord, ...]

    def __post_init__(self) -> None:
        if not self.research_id.strip() or not self.index_revision.strip():
            raise ValueError("research_id and index_revision are required")
        if not self.build_sha.strip():
            raise ValueError("build_sha is required")
        if self.coverage.requested != self.contract.evidence_types:
            raise ValueError("snapshot coverage must match the research contract")
        identifiers = [item.id for item in self.evidence]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("evidence identifiers must be unique")
        ordered = tuple(sorted(self.evidence, key=lambda item: (item.sort_key, item.id)))
        object.__setattr__(self, "evidence", ordered)

    @property
    def query_fingerprint(self) -> str:
        return self.contract.fingerprint(self.index_revision)

    def page(
        self,
        *,
        cursor: str | None = None,
        page_size: int = 20,
    ) -> ResearchResultPage:
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        start = 0
        if cursor:
            decoded = StableCursor.decode(cursor)
            if decoded.query_fingerprint != self.query_fingerprint:
                raise ValueError("cursor belongs to another research query")
            if decoded.index_revision != self.index_revision:
                raise ValueError("cursor belongs to another index revision")
            if decoded.page_size != page_size:
                raise ValueError("page_size must match the cursor")
            start = self._cursor_position(decoded) + 1

        selected = self.evidence[start : start + page_size]
        returned_through = start + len(selected)
        next_cursor = None
        if returned_through < len(self.evidence):
            last = selected[-1]
            next_cursor = StableCursor(
                query_fingerprint=self.query_fingerprint,
                index_revision=self.index_revision,
                sort_key=last.sort_key,
                item_id=last.id,
                page_size=page_size,
            ).encode()
        accounting = EvidencePage(
            matched_total=len(self.evidence),
            returned_count=len(selected),
            returned_through=returned_through,
            next_cursor=next_cursor,
        )
        return ResearchResultPage(
            research_id=self.research_id,
            query_fingerprint=self.query_fingerprint,
            index_revision=self.index_revision,
            build_sha=self.build_sha,
            coverage=self.coverage,
            page=accounting,
            evidence=selected,
        )

    def _cursor_position(self, cursor: StableCursor) -> int:
        for position, item in enumerate(self.evidence):
            if item.sort_key == cursor.sort_key and item.id == cursor.item_id:
                return position
        raise ValueError("cursor item is absent from this immutable snapshot")


@dataclass(frozen=True, slots=True)
class ResearchSnapshotSummary:
    """Small completion marker used by hot status-polling paths."""

    research_id: str
    query_fingerprint: str
    index_revision: str
    build_sha: str
    coverage: CoverageLedger
    evidence_total: int
    evidence_types: tuple[EvidenceType, ...]

    def __post_init__(self) -> None:
        if not self.research_id or not self.index_revision or not self.build_sha:
            raise ValueError("snapshot summary identity is required")
        if self.evidence_total < 0:
            raise ValueError("snapshot summary evidence_total must not be negative")
        if len(set(self.evidence_types)) != len(self.evidence_types):
            raise ValueError("snapshot summary evidence types must be unique")

    @classmethod
    def from_snapshot(cls, snapshot: ResearchSnapshot) -> ResearchSnapshotSummary:
        return cls(
            research_id=snapshot.research_id,
            query_fingerprint=snapshot.query_fingerprint,
            index_revision=snapshot.index_revision,
            build_sha=snapshot.build_sha,
            coverage=snapshot.coverage,
            evidence_total=len(snapshot.evidence),
            evidence_types=tuple(
                sorted(
                    {item.evidence_type for item in snapshot.evidence},
                    key=lambda item: item.value,
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "research_id": self.research_id,
            "query_fingerprint": self.query_fingerprint,
            "index_revision": self.index_revision,
            "build_sha": self.build_sha,
            "coverage": self.coverage.to_dict(),
            "evidence_total": self.evidence_total,
            "evidence_types": [item.value for item in self.evidence_types],
        }


def snapshot_payload(snapshot: ResearchSnapshot) -> dict[str, Any]:
    """Return small snapshot metadata without copying potentially huge evidence text."""

    return {
        **ResearchSnapshotSummary.from_snapshot(snapshot).to_dict(),
        "contract": snapshot.contract.canonical_payload(),
    }


__all__ = [
    "PUBLIC_INLINE_TEXT_CHARACTERS",
    "SNAPSHOT_INDEX_SHARD_SIZE",
    "SNAPSHOT_INDEX_LOOKUP_BUCKETS",
    "SNAPSHOT_TEXT_SHARD_CHARACTERS",
    "SNAPSHOT_TEXT_SHARD_RECORDS",
    "EvidenceCitation",
    "EvidenceIndexEntry",
    "EvidenceRecord",
    "EvidenceTextShard",
    "ResearchResultIndexPage",
    "ResearchResultPage",
    "ResearchSnapshot",
    "ResearchSnapshotIndex",
    "ResearchSnapshotSummary",
    "SnapshotIndexShard",
    "SnapshotIndexShardDescriptor",
    "SnapshotIndexLookupBucket",
    "build_snapshot_index",
    "snapshot_index_lookup_bucket",
    "snapshot_payload",
]
