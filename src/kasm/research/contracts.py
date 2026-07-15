"""Research scope, coverage, and cursor contracts.

These values distinguish correctness of a claim from completeness of the
requested investigation.  A result can contain valid evidence while still
being incomplete; callers must never infer completion from a non-empty page.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any


class EvidenceType(StrEnum):
    """Evidence axes promised by the public research workflow."""

    BILLS = "bills"
    BILL_TEXT = "bill_text"
    BILL_STATUS = "bill_status"
    AGENDAS = "agendas"
    SUBCOMMITTEE_MINUTES = "subcommittee_minutes"
    REVIEW_REPORTS = "review_reports"
    SPEECHES = "speeches"
    SPEECH_CONTEXT = "speech_context"
    GOVERNMENT_RESPONSES = "government_responses"


DEFAULT_EVIDENCE_TYPES = tuple(EvidenceType)


class ResearchIntent(StrEnum):
    """Requested analysis modes; these never narrow the evidence contract."""

    DISCOVER = "discover"
    TRACK_STATUS = "track_status"
    TIMELINE = "timeline"
    EXPLAIN_ISSUES = "explain_issues"
    COMPARE_POSITIONS = "compare_positions"
    QUOTE_EVIDENCE = "quote_evidence"


@dataclass(frozen=True, slots=True)
class ResearchContract:
    """The explicit scope that a research result must satisfy."""

    query: str
    as_of: datetime
    date_from: date | None = None
    date_to: date | None = None
    assembly_term: int = 22
    assembly_terms: tuple[int, ...] = ()
    committees: tuple[str, ...] = ()
    bill_numbers: tuple[str, ...] = ()
    representative_proposer_names: tuple[str, ...] = ()
    co_proposer_names: tuple[str, ...] = ()
    proposer_names: tuple[str, ...] = ()
    evidence_types: tuple[EvidenceType, ...] = DEFAULT_EVIDENCE_TYPES
    intents: tuple[ResearchIntent, ...] = (ResearchIntent.DISCOVER,)
    ordering: str = "chronological"
    completeness: str = "comprehensive"

    def __post_init__(self) -> None:
        query = self.query.strip()
        if not query or len(query) > 500:
            raise ValueError("query must contain between 1 and 500 characters")
        if self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        if self.assembly_term < 1:
            raise ValueError("assembly_term must be positive")
        terms = self.assembly_terms or (self.assembly_term,)
        if any(term < 1 for term in terms):
            raise ValueError("assembly_terms must contain positive terms")
        if len(set(terms)) != len(terms):
            raise ValueError("assembly_terms must be unique")
        if tuple(sorted(terms)) != terms:
            raise ValueError("assembly_terms must be in chronological order")
        if self.assembly_term not in terms:
            raise ValueError("assembly_term must be included in assembly_terms")
        object.__setattr__(self, "assembly_terms", terms)
        if not self.evidence_types:
            raise ValueError("at least one evidence type is required")
        if len(set(self.evidence_types)) != len(self.evidence_types):
            raise ValueError("evidence types must be unique")
        if not self.intents:
            raise ValueError("at least one research intent is required")
        if len(set(self.intents)) != len(self.intents):
            raise ValueError("research intents must be unique")
        if any(not number.isdigit() or len(number) != 7 for number in self.bill_numbers):
            raise ValueError("bill numbers must contain exactly seven digits")
        for label, names in (
            ("representative proposer", self.representative_proposer_names),
            ("co-proposer", self.co_proposer_names),
            ("proposer", self.proposer_names),
        ):
            if len(names) != len(set(names)):
                raise ValueError(f"{label} names must be unique")
            if any(not name.strip() for name in names):
                raise ValueError(f"{label} names must not be empty")
        if self.ordering not in {"chronological", "relevance"}:
            raise ValueError("ordering must be chronological or relevance")
        if self.completeness not in {"comprehensive", "focused"}:
            raise ValueError("completeness must be comprehensive or focused")

    @classmethod
    def create(cls, query: str, **values: Any) -> ResearchContract:
        """Create a contract with an explicit UTC observation time."""

        return cls(query=query, as_of=datetime.now(UTC), **values)

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "query": self.query.strip(),
            "as_of": self.as_of.isoformat(),
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "assembly_term": self.assembly_term,
            "assembly_terms": list(self.assembly_terms),
            "committees": list(self.committees),
            "bill_numbers": list(self.bill_numbers),
            "representative_proposer_names": list(
                self.representative_proposer_names
            ),
            "co_proposer_names": list(self.co_proposer_names),
            "proposer_names": list(self.proposer_names),
            "evidence_types": [item.value for item in self.evidence_types],
            "intents": [item.value for item in self.intents],
            "ordering": self.ordering,
            "completeness": self.completeness,
        }

    def fingerprint(self, index_revision: str) -> str:
        payload = {"contract": self.canonical_payload(), "index_revision": index_revision}
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class EvidenceCoverage:
    """Coverage for one requested evidence axis."""

    evidence_type: EvidenceType
    candidate_total: int | None
    checked_count: int
    matched_count: int
    failed_count: int = 0
    pending_count: int = 0
    gap_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        counts = (
            self.checked_count,
            self.matched_count,
            self.failed_count,
            self.pending_count,
        )
        if self.candidate_total is not None and self.candidate_total < 0:
            raise ValueError("candidate_total must be non-negative")
        if any(count < 0 for count in counts):
            raise ValueError("coverage counts must be non-negative")
        if self.matched_count > self.checked_count:
            raise ValueError("matched_count cannot exceed checked_count")
        if self.candidate_total is not None:
            accounted = self.checked_count + self.failed_count + self.pending_count
            if accounted > self.candidate_total:
                raise ValueError("coverage cannot account for more than candidate_total")

    @property
    def complete(self) -> bool:
        return bool(
            self.candidate_total is not None
            and self.failed_count == 0
            and self.pending_count == 0
            and self.checked_count == self.candidate_total
            and not self.gap_reasons
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "evidence_type": self.evidence_type.value,
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class CoverageLedger:
    """Coverage for every evidence type required by a research contract."""

    requested: tuple[EvidenceType, ...]
    entries: tuple[EvidenceCoverage, ...]

    def __post_init__(self) -> None:
        if len(set(self.requested)) != len(self.requested):
            raise ValueError("requested evidence types must be unique")
        entry_types = tuple(entry.evidence_type for entry in self.entries)
        if len(set(entry_types)) != len(entry_types):
            raise ValueError("coverage entries must be unique by evidence type")
        missing = set(self.requested) - set(entry_types)
        if missing:
            labels = ", ".join(sorted(item.value for item in missing))
            raise ValueError(f"coverage entries are missing requested evidence: {labels}")

    @property
    def complete(self) -> bool:
        by_type = {entry.evidence_type: entry for entry in self.entries}
        return all(by_type[item].complete for item in self.requested)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": [item.value for item in self.requested],
            "complete": self.complete,
            "evidence": {
                entry.evidence_type.value: entry.to_dict() for entry in self.entries
            },
        }


@dataclass(frozen=True, slots=True)
class StableCursor:
    """A reproducible result cursor bound to one query and index revision."""

    query_fingerprint: str
    index_revision: str
    sort_key: str
    item_id: str
    page_size: int

    def __post_init__(self) -> None:
        if len(self.query_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.query_fingerprint
        ):
            raise ValueError("query_fingerprint must be a SHA-256 hex digest")
        if not self.index_revision or not self.sort_key or not self.item_id:
            raise ValueError("cursor revision, sort key, and item id are required")
        if not 1 <= self.page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")

    def encode(self) -> str:
        payload = json.dumps(
            asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        checksum = hashlib.sha256(payload).digest()[:12]
        return base64.urlsafe_b64encode(checksum + payload).rstrip(b"=").decode()

    @classmethod
    def decode(cls, value: str) -> StableCursor:
        try:
            padded = value + "=" * (-len(value) % 4)
            raw = base64.urlsafe_b64decode(padded.encode())
            checksum, payload = raw[:12], raw[12:]
            if len(checksum) != 12 or hashlib.sha256(payload).digest()[:12] != checksum:
                raise ValueError("cursor checksum does not match")
            decoded = json.loads(payload)
            if not isinstance(decoded, dict):
                raise ValueError("cursor payload must be an object")
            return cls(**decoded)
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid research cursor") from exc


@dataclass(frozen=True, slots=True)
class EvidencePage:
    """Pagination accounting that makes silent truncation invalid."""

    matched_total: int
    returned_count: int
    returned_through: int
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        if min(self.matched_total, self.returned_count, self.returned_through) < 0:
            raise ValueError("page counts must be non-negative")
        if self.returned_count > self.returned_through:
            raise ValueError("returned_count cannot exceed returned_through")
        if self.returned_through > self.matched_total:
            raise ValueError("returned_through cannot exceed matched_total")
        if self.returned_through < self.matched_total and not self.next_cursor:
            raise ValueError("an incomplete evidence page requires next_cursor")
        if self.returned_through == self.matched_total and self.next_cursor:
            raise ValueError("a complete evidence page must not include next_cursor")

    @property
    def complete(self) -> bool:
        return self.returned_through == self.matched_total

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "complete": self.complete}
