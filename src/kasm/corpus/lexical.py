"""Deterministic, exhaustive lexical candidate retrieval for Korean text."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from .models import CorpusDocumentIdentity, CorpusDocumentRef, CorpusEvidenceKind
from .serialization import canonical_json

_WORD: Final = re.compile(r"[0-9A-Za-z_가-힣]+", re.UNICODE)
_PARTICLES: Final = (
    "에게서",
    "으로부터",
    "에서부터",
    "이라고",
    "이라는",
    "으로",
    "에서",
    "에게",
    "까지",
    "부터",
    "처럼",
    "보다",
    "이나",
    "라도",
    "하고",
    "이며",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "에",
    "의",
    "와",
    "과",
    "로",
)
_CURSOR_SCHEMA: Final = 1


class LexicalMatchMode(StrEnum):
    """How normalized query terms are combined."""

    ANY = "any"
    ALL = "all"


def lexical_terms(text: str) -> tuple[str, ...]:
    """Return stable token forms with conservative Korean particle stripping.

    Both the exact surface and stripped base are retained.  This preserves
    literal recall while allowing ``보완수사권을`` to match ``보완수사권``.
    No vocabulary-size or result-count cutoff is applied.
    """

    terms: set[str] = set()
    for match in _WORD.finditer(unicodedata.normalize("NFKC", text).casefold()):
        raw = match.group()
        terms.add(raw)
        for particle in _PARTICLES:
            if raw.endswith(particle) and len(raw) >= len(particle) + 2:
                terms.add(raw[: -len(particle)])
                break
    return tuple(sorted(terms))


def lexical_term_frequencies(text: str) -> dict[str, int]:
    """Count exact and particle-stripped forms without truncating the document."""

    frequencies: Counter[str] = Counter()
    for match in _WORD.finditer(unicodedata.normalize("NFKC", text).casefold()):
        raw = match.group()
        frequencies[raw] += 1
        for particle in _PARTICLES:
            if raw.endswith(particle) and len(raw) >= len(particle) + 2:
                base = raw[: -len(particle)]
                if base != raw:
                    frequencies[base] += 1
                break
    return dict(sorted(frequencies.items()))


@dataclass(frozen=True, slots=True)
class CorpusSearchCandidate:
    """One matching official identity; no source text is duplicated here."""

    identity: CorpusDocumentIdentity
    official_url: str
    title: str
    document_date: str | None
    matched_terms: tuple[str, ...]
    occurrence_count: int

    def __post_init__(self) -> None:
        if not self.matched_terms:
            raise ValueError("search candidate must contain matched terms")
        if tuple(sorted(set(self.matched_terms))) != self.matched_terms:
            raise ValueError("matched_terms must be unique and sorted")
        if (
            not isinstance(self.occurrence_count, int)
            or isinstance(self.occurrence_count, bool)
            or self.occurrence_count < len(self.matched_terms)
        ):
            raise ValueError("occurrence_count cannot be lower than matched term count")

    @property
    def matched_term_count(self) -> int:
        return len(self.matched_terms)

    @property
    def identity_id(self) -> str:
        return self.identity.identity_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "identity_id": self.identity_id,
            "official_url": self.official_url,
            "title": self.title,
            "document_date": self.document_date,
            "matched_terms": list(self.matched_terms),
            "matched_term_count": self.matched_term_count,
            "occurrence_count": self.occurrence_count,
        }


@dataclass(frozen=True, slots=True)
class CorpusSearchPage:
    """Cursor page with explicit accounting for the complete candidate set."""

    revision_id: str
    normalized_terms: tuple[str, ...]
    match_mode: LexicalMatchMode
    total_matching_candidates: int
    offset: int
    candidates: tuple[CorpusSearchCandidate, ...]
    next_cursor: str | None
    revision_complete: bool

    def __post_init__(self) -> None:
        if not self.normalized_terms:
            raise ValueError("normalized search terms must not be empty")
        if (
            not isinstance(self.total_matching_candidates, int)
            or isinstance(self.total_matching_candidates, bool)
            or not isinstance(self.offset, int)
            or isinstance(self.offset, bool)
            or self.total_matching_candidates < 0
            or self.offset < 0
        ):
            raise ValueError("search page counts must be non-negative")
        if self.accounted_count > self.total_matching_candidates:
            raise ValueError("search page accounts for more than the candidate total")
        if self.exhausted != (self.next_cursor is None):
            raise ValueError("search cursor does not match page exhaustion")

    @property
    def returned_count(self) -> int:
        return len(self.candidates)

    @property
    def accounted_count(self) -> int:
        return self.offset + self.returned_count

    @property
    def remaining_count(self) -> int:
        return self.total_matching_candidates - self.accounted_count

    @property
    def exhausted(self) -> bool:
        return self.accounted_count == self.total_matching_candidates

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "revision_complete": self.revision_complete,
            "normalized_terms": list(self.normalized_terms),
            "match_mode": self.match_mode.value,
            "total_matching_candidates": self.total_matching_candidates,
            "offset": self.offset,
            "returned_count": self.returned_count,
            "accounted_count": self.accounted_count,
            "remaining_count": self.remaining_count,
            "exhausted": self.exhausted,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "next_cursor": self.next_cursor,
        }


@dataclass(frozen=True, slots=True)
class _SearchCursor:
    revision_id: str
    query_fingerprint: str
    offset: int
    page_size: int

    def encode(self) -> str:
        payload = canonical_json(
            {
                "schema_version": _CURSOR_SCHEMA,
                "revision_id": self.revision_id,
                "query_fingerprint": self.query_fingerprint,
                "offset": self.offset,
                "page_size": self.page_size,
            }
        )
        checksum = hashlib.sha256(payload).digest()[:16]
        return base64.urlsafe_b64encode(checksum + payload).rstrip(b"=").decode()

    @classmethod
    def decode(cls, value: str) -> _SearchCursor:
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            checksum, payload = raw[:16], raw[16:]
            if len(checksum) != 16 or hashlib.sha256(payload).digest()[:16] != checksum:
                raise ValueError
            decoded = json.loads(payload)
            if canonical_json(decoded) != payload:
                raise ValueError
            if not isinstance(decoded, dict):
                raise ValueError
            if type(decoded.get("schema_version")) is not int or (
                decoded.get("schema_version") != _CURSOR_SCHEMA
            ):
                raise ValueError
            revision_id = decoded.get("revision_id")
            query_fingerprint = decoded.get("query_fingerprint")
            offset = decoded.get("offset")
            page_size = decoded.get("page_size")
            if (
                not isinstance(revision_id, str)
                or not isinstance(query_fingerprint, str)
                or not isinstance(offset, int)
                or isinstance(offset, bool)
                or not isinstance(page_size, int)
                or isinstance(page_size, bool)
                or offset < 0
                or not 1 <= page_size <= 500
            ):
                raise ValueError
            result = cls(revision_id, query_fingerprint, offset, page_size)
            if result.encode() != value:
                raise ValueError
            return result
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            raise ValueError("invalid corpus search cursor") from None


def query_fingerprint(
    *,
    revision_id: str,
    normalized_terms: tuple[str, ...],
    match_mode: LexicalMatchMode,
    assembly_terms: tuple[int, ...],
    evidence_kinds: tuple[CorpusEvidenceKind, ...],
) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "revision_id": revision_id,
                "normalized_terms": list(normalized_terms),
                "match_mode": match_mode.value,
                "assembly_terms": list(assembly_terms),
                "evidence_kinds": [kind.value for kind in evidence_kinds],
            }
        )
    ).hexdigest()


def rank_candidates(
    *,
    documents: dict[str, CorpusDocumentRef],
    term_postings: dict[str, dict[str, int]],
    normalized_terms: tuple[str, ...],
    match_mode: LexicalMatchMode,
    allowed_assembly_terms: set[int],
    allowed_evidence_kinds: set[CorpusEvidenceKind],
) -> tuple[CorpusSearchCandidate, ...]:
    """Return every matching identity in a reproducible order."""

    matches: dict[str, dict[str, int]] = {}
    for term in normalized_terms:
        for identity_id, frequency in term_postings.get(term, {}).items():
            document = documents.get(identity_id)
            if document is None:
                raise ValueError("lexical posting references an unknown document")
            if (
                document.identity.assembly_term not in allowed_assembly_terms
                or document.identity.evidence_kind not in allowed_evidence_kinds
            ):
                continue
            matches.setdefault(identity_id, {})[term] = frequency
    required = set(normalized_terms)
    candidates: list[CorpusSearchCandidate] = []
    for identity_id, frequencies in matches.items():
        if match_mode is LexicalMatchMode.ALL and set(frequencies) != required:
            continue
        document = documents[identity_id]
        candidates.append(
            CorpusSearchCandidate(
                identity=document.identity,
                official_url=document.official_url,
                title=document.title,
                document_date=(
                    document.document_date.isoformat()
                    if document.document_date
                    else None
                ),
                matched_terms=tuple(sorted(frequencies)),
                occurrence_count=sum(frequencies.values()),
            )
        )
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                -item.matched_term_count,
                -item.occurrence_count,
                item.identity.sort_key,
            ),
        )
    )


def paginate_candidates(
    *,
    revision_id: str,
    revision_complete: bool,
    normalized_terms: tuple[str, ...],
    match_mode: LexicalMatchMode,
    candidates: tuple[CorpusSearchCandidate, ...],
    fingerprint: str,
    page_size: int,
    cursor: str | None,
) -> CorpusSearchPage:
    if not 1 <= page_size <= 500:
        raise ValueError("page_size must be between 1 and 500")
    offset = 0
    if cursor is not None:
        decoded = _SearchCursor.decode(cursor)
        if (
            decoded.revision_id != revision_id
            or decoded.query_fingerprint != fingerprint
            or decoded.page_size != page_size
        ):
            raise ValueError("corpus search cursor does not match this query")
        offset = decoded.offset
    if offset > len(candidates):
        raise ValueError("corpus search cursor is beyond the candidate set")
    selected = candidates[offset : offset + page_size]
    next_offset = offset + len(selected)
    next_cursor = (
        _SearchCursor(
            revision_id=revision_id,
            query_fingerprint=fingerprint,
            offset=next_offset,
            page_size=page_size,
        ).encode()
        if next_offset < len(candidates)
        else None
    )
    return CorpusSearchPage(
        revision_id=revision_id,
        normalized_terms=normalized_terms,
        match_mode=match_mode,
        total_matching_candidates=len(candidates),
        offset=offset,
        candidates=selected,
        next_cursor=next_cursor,
        revision_complete=revision_complete,
    )
