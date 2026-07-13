"""Fail-closed bridge between document work and the revisioned corpus.

The existing research engine discovers and parses official documents.  The
corpus indexes their full text by an explicit official identity and Assembly
term.  This module is the deliberately narrow boundary between those domains:

* a parsed document is accepted only when its kind, URL, deterministic work
  identity, Assembly term, and official identifier all agree;
* failed outcomes become scope-bound ingestion failures without persisting an
  upstream error message; and
* corpus candidates map back only through the exact corpus identity and URL.

Titles are carried as display metadata and are never used to infer identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final

from kasm.corpus.lexical import CorpusSearchCandidate
from kasm.corpus.models import (
    CorpusDocument,
    CorpusDocumentIdentity,
    CorpusEvidenceKind,
    CorpusIngestionFailure,
)

from .documents import OfficialDocumentKind, ParsedOfficialDocument
from .engine import DocumentOutcome, DocumentOutcomeStatus, DocumentWorkItem

_REASON_CODE: Final = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class CorpusBridgeGapCode(StrEnum):
    """Stable reasons why an exact bridge mapping could not be made."""

    ASSEMBLY_TERM_MISSING = "assembly_term_missing"
    ASSEMBLY_TERM_INVALID = "assembly_term_invalid"
    OFFICIAL_IDENTIFIER_MISSING = "official_identifier_missing"
    OFFICIAL_IDENTIFIER_INVALID = "official_identifier_invalid"
    WORK_IDENTITY_MISMATCH = "work_identity_mismatch"
    BILL_IDENTITY_MISSING = "bill_identity_missing"
    BILL_IDENTITY_AMBIGUOUS = "bill_identity_ambiguous"
    BILL_IDENTIFIER_MISMATCH = "bill_identifier_mismatch"
    BILL_TERM_MISMATCH = "bill_term_mismatch"
    DOCUMENT_KIND_MISMATCH = "document_kind_mismatch"
    DOCUMENT_URL_MISMATCH = "document_url_mismatch"
    CORPUS_DOCUMENT_INVALID = "corpus_document_invalid"
    OUTCOME_WORK_MISMATCH = "outcome_work_mismatch"
    OUTCOME_NOT_FAILED = "outcome_not_failed"
    FAILED_OUTCOME_HAS_RESULT = "failed_outcome_has_result"
    OUTCOME_ERROR_CODE_INVALID = "outcome_error_code_invalid"
    CANDIDATE_DUPLICATE = "candidate_duplicate"
    CANDIDATE_UNMAPPED = "candidate_unmapped"
    CANDIDATE_AMBIGUOUS = "candidate_ambiguous"
    CANDIDATE_URL_MISMATCH = "candidate_url_mismatch"


@dataclass(frozen=True, slots=True)
class ExactCorpusWorkDescriptor:
    """A work item plus identity fields proven by official inventory metadata.

    Optional values are intentional: discovery can produce work whose term or
    official identifier is absent.  Bridge functions turn that absence into a
    typed gap instead of forcing callers to invent a value.
    """

    work_item: DocumentWorkItem
    assembly_term: int | None
    official_identifier: str | None
    title: str = ""
    document_date: date | None = None
    committee: str = ""


@dataclass(frozen=True, slots=True)
class CorpusBridgeGap:
    """Public, machine-readable refusal to guess a corpus relationship."""

    code: CorpusBridgeGapCode
    work_id: str | None = None
    assembly_term: int | None = None
    evidence_kind: CorpusEvidenceKind | None = None
    official_identifier: str | None = None

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "code": self.code.value,
            "work_id": self.work_id,
            "assembly_term": self.assembly_term,
            "evidence_kind": (
                self.evidence_kind.value if self.evidence_kind else None
            ),
            "official_identifier": self.official_identifier,
        }


@dataclass(frozen=True, slots=True)
class CorpusDocumentBridgeResult:
    """Exactly one corpus document or one explicit gap."""

    document: CorpusDocument | None = None
    gap: CorpusBridgeGap | None = None

    def __post_init__(self) -> None:
        if (self.document is None) == (self.gap is None):
            raise ValueError("document bridge result requires exactly one outcome")

    @property
    def succeeded(self) -> bool:
        return self.document is not None


@dataclass(frozen=True, slots=True)
class ScopeBoundCorpusIngestionFailure:
    """Corpus failure plus the exact coverage cell it must update."""

    assembly_term: int
    evidence_kind: CorpusEvidenceKind
    failure: CorpusIngestionFailure
    retryable: bool
    work_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.assembly_term, int)
            or isinstance(self.assembly_term, bool)
            or self.assembly_term < 1
            or not self.work_id.strip()
        ):
            raise ValueError("scope-bound corpus failure has an invalid identity")
        if self.failure.failure_key != self.work_id:
            raise ValueError("corpus failure key must equal its exact work id")


@dataclass(frozen=True, slots=True)
class CorpusFailureBridgeResult:
    """Exactly one scope-bound failure or one explicit bridge gap."""

    failure: ScopeBoundCorpusIngestionFailure | None = None
    gap: CorpusBridgeGap | None = None

    def __post_init__(self) -> None:
        if (self.failure is None) == (self.gap is None):
            raise ValueError("failure bridge result requires exactly one outcome")

    @property
    def succeeded(self) -> bool:
        return self.failure is not None


@dataclass(frozen=True, slots=True)
class CorpusCandidateWorkMatch:
    """One corpus candidate bound back to one exact work descriptor."""

    candidate: CorpusSearchCandidate
    descriptor: ExactCorpusWorkDescriptor

    def __post_init__(self) -> None:
        identity, gap = _validated_descriptor_identity(self.descriptor)
        if gap is not None or identity != self.candidate.identity:
            raise ValueError("candidate work match does not share an exact identity")
        if self.descriptor.work_item.official_url != self.candidate.official_url:
            raise ValueError("candidate work match does not share an exact URL")


@dataclass(frozen=True, slots=True)
class CorpusCandidateMappingResult:
    """Exhaustive accounting for candidate-to-work mapping."""

    candidate_count: int
    matches: tuple[CorpusCandidateWorkMatch, ...]
    gaps: tuple[CorpusBridgeGap, ...]

    def __post_init__(self) -> None:
        if self.candidate_count < 0 or len(self.matches) > self.candidate_count:
            raise ValueError("candidate mapping counts are invalid")

    @property
    def matched_count(self) -> int:
        return len(self.matches)

    @property
    def unmapped_count(self) -> int:
        return self.candidate_count - self.matched_count

    @property
    def complete(self) -> bool:
        return self.matched_count == self.candidate_count and not self.gaps


def corpus_document_from_parsed(
    descriptor: ExactCorpusWorkDescriptor,
    document: ParsedOfficialDocument,
) -> CorpusDocumentBridgeResult:
    """Convert one verified parser result without inferring missing identity."""

    identity, gap = _validated_descriptor_identity(descriptor)
    if gap is not None:
        return CorpusDocumentBridgeResult(gap=gap)
    assert identity is not None
    item = descriptor.work_item
    if document.kind is not item.kind:
        return CorpusDocumentBridgeResult(
            gap=_descriptor_gap(
                descriptor,
                CorpusBridgeGapCode.DOCUMENT_KIND_MISMATCH,
                identity.evidence_kind,
            )
        )
    if document.official_url != item.official_url:
        return CorpusDocumentBridgeResult(
            gap=_descriptor_gap(
                descriptor,
                CorpusBridgeGapCode.DOCUMENT_URL_MISMATCH,
                identity.evidence_kind,
            )
        )
    try:
        corpus_document = CorpusDocument(
            identity=identity,
            official_url=document.official_url,
            source_hash=document.source_hash,
            parser_version=document.parser_version,
            text=document.full_text,
            observed_at=document.parsed_at,
            title=descriptor.title,
            document_date=descriptor.document_date,
            related_bill_numbers=item.related_bill_numbers,
            committee=descriptor.committee,
        )
    except ValueError:
        return CorpusDocumentBridgeResult(
            gap=_descriptor_gap(
                descriptor,
                CorpusBridgeGapCode.CORPUS_DOCUMENT_INVALID,
                identity.evidence_kind,
            )
        )
    return CorpusDocumentBridgeResult(document=corpus_document)


def corpus_failure_from_outcome(
    descriptor: ExactCorpusWorkDescriptor,
    outcome: DocumentOutcome,
) -> CorpusFailureBridgeResult:
    """Convert a failed worker outcome without copying its free-text message."""

    identity, gap = _validated_descriptor_identity(descriptor)
    if gap is not None:
        return CorpusFailureBridgeResult(gap=gap)
    assert identity is not None
    item = descriptor.work_item
    if outcome.work_id != item.work_id:
        return CorpusFailureBridgeResult(
            gap=_descriptor_gap(
                descriptor,
                CorpusBridgeGapCode.OUTCOME_WORK_MISMATCH,
                identity.evidence_kind,
            )
        )
    if outcome.status is DocumentOutcomeStatus.SUCCEEDED:
        return CorpusFailureBridgeResult(
            gap=_descriptor_gap(
                descriptor,
                CorpusBridgeGapCode.OUTCOME_NOT_FAILED,
                identity.evidence_kind,
            )
        )
    if outcome.result is not None:
        return CorpusFailureBridgeResult(
            gap=_descriptor_gap(
                descriptor,
                CorpusBridgeGapCode.FAILED_OUTCOME_HAS_RESULT,
                identity.evidence_kind,
            )
        )
    error_code = outcome.error_code or ""
    if not _REASON_CODE.fullmatch(error_code):
        return CorpusFailureBridgeResult(
            gap=_descriptor_gap(
                descriptor,
                CorpusBridgeGapCode.OUTCOME_ERROR_CODE_INVALID,
                identity.evidence_kind,
            )
        )
    failure = CorpusIngestionFailure(
        failure_key=item.work_id,
        reason_code=error_code,
        official_identifier=identity.official_identifier,
    )
    return CorpusFailureBridgeResult(
        failure=ScopeBoundCorpusIngestionFailure(
            assembly_term=identity.assembly_term,
            evidence_kind=identity.evidence_kind,
            failure=failure,
            retryable=outcome.status is DocumentOutcomeStatus.RETRYABLE_FAILURE,
            work_id=item.work_id,
        )
    )


def map_candidates_to_work(
    candidates: tuple[CorpusSearchCandidate, ...],
    descriptors: tuple[ExactCorpusWorkDescriptor, ...],
) -> CorpusCandidateMappingResult:
    """Map every supplied candidate by exact identity and URL, never by title."""

    by_identity: dict[str, list[ExactCorpusWorkDescriptor]] = {}
    gaps: list[CorpusBridgeGap] = []
    for descriptor in descriptors:
        identity, gap = _validated_descriptor_identity(descriptor)
        if gap is not None:
            gaps.append(gap)
            continue
        assert identity is not None
        by_identity.setdefault(identity.identity_id, []).append(descriptor)

    matches: list[CorpusCandidateWorkMatch] = []
    seen_candidates: set[str] = set()
    for candidate in candidates:
        identity_id = candidate.identity.identity_id
        if identity_id in seen_candidates:
            gaps.append(_candidate_gap(candidate, CorpusBridgeGapCode.CANDIDATE_DUPLICATE))
            continue
        seen_candidates.add(identity_id)
        options = by_identity.get(identity_id, [])
        if not options:
            gaps.append(_candidate_gap(candidate, CorpusBridgeGapCode.CANDIDATE_UNMAPPED))
            continue
        if len(options) != 1:
            gaps.append(_candidate_gap(candidate, CorpusBridgeGapCode.CANDIDATE_AMBIGUOUS))
            continue
        descriptor = options[0]
        if descriptor.work_item.official_url != candidate.official_url:
            gaps.append(
                _candidate_gap(candidate, CorpusBridgeGapCode.CANDIDATE_URL_MISMATCH)
            )
            continue
        matches.append(CorpusCandidateWorkMatch(candidate, descriptor))
    return CorpusCandidateMappingResult(
        candidate_count=len(candidates),
        matches=tuple(matches),
        gaps=tuple(gaps),
    )


def _validated_descriptor_identity(
    descriptor: ExactCorpusWorkDescriptor,
) -> tuple[CorpusDocumentIdentity | None, CorpusBridgeGap | None]:
    item = descriptor.work_item
    evidence_kind = _corpus_evidence_kind(item.kind)
    if descriptor.assembly_term is None:
        return None, _descriptor_gap(
            descriptor,
            CorpusBridgeGapCode.ASSEMBLY_TERM_MISSING,
            evidence_kind,
        )
    if (
        not isinstance(descriptor.assembly_term, int)
        or isinstance(descriptor.assembly_term, bool)
        or descriptor.assembly_term < 1
    ):
        return None, _descriptor_gap(
            descriptor,
            CorpusBridgeGapCode.ASSEMBLY_TERM_INVALID,
            evidence_kind,
        )
    if descriptor.official_identifier is None:
        return None, _descriptor_gap(
            descriptor,
            CorpusBridgeGapCode.OFFICIAL_IDENTIFIER_MISSING,
            evidence_kind,
        )
    if (
        not isinstance(descriptor.official_identifier, str)
        or not descriptor.official_identifier.strip()
    ):
        return None, _descriptor_gap(
            descriptor,
            CorpusBridgeGapCode.OFFICIAL_IDENTIFIER_INVALID,
            evidence_kind,
        )
    expected_work_id = DocumentWorkItem.create(
        item.kind,
        item.official_url,
        evidence_types=item.evidence_types,
        related_bill_numbers=item.related_bill_numbers,
    ).work_id
    if item.work_id != expected_work_id:
        return None, _descriptor_gap(
            descriptor,
            CorpusBridgeGapCode.WORK_IDENTITY_MISMATCH,
            evidence_kind,
        )
    related_bills = item.related_bill_numbers
    if item.kind in {OfficialDocumentKind.BILL_TEXT, OfficialDocumentKind.REVIEW_REPORT}:
        if not related_bills:
            return None, _descriptor_gap(
                descriptor,
                CorpusBridgeGapCode.BILL_IDENTITY_MISSING,
                evidence_kind,
            )
        if item.kind is OfficialDocumentKind.BILL_TEXT and len(related_bills) != 1:
            return None, _descriptor_gap(
                descriptor,
                CorpusBridgeGapCode.BILL_IDENTITY_AMBIGUOUS,
                evidence_kind,
            )
        if (
            item.kind is OfficialDocumentKind.BILL_TEXT
            and descriptor.official_identifier
            != f"bill:{related_bills[0]}:original"
        ):
            return None, _descriptor_gap(
                descriptor,
                CorpusBridgeGapCode.BILL_IDENTIFIER_MISMATCH,
                evidence_kind,
            )
    if any(int(number[:2]) != descriptor.assembly_term for number in related_bills):
        return None, _descriptor_gap(
            descriptor,
            CorpusBridgeGapCode.BILL_TERM_MISMATCH,
            evidence_kind,
        )
    try:
        identity = CorpusDocumentIdentity(
            assembly_term=descriptor.assembly_term,
            evidence_kind=evidence_kind,
            official_identifier=descriptor.official_identifier,
        )
    except ValueError:
        return None, _descriptor_gap(
            descriptor,
            CorpusBridgeGapCode.OFFICIAL_IDENTIFIER_INVALID,
            evidence_kind,
        )
    return identity, None


def _corpus_evidence_kind(kind: OfficialDocumentKind) -> CorpusEvidenceKind:
    return {
        OfficialDocumentKind.BILL_TEXT: CorpusEvidenceKind.BILL_ORIGINAL,
        OfficialDocumentKind.REVIEW_REPORT: CorpusEvidenceKind.REVIEW_REPORT,
        OfficialDocumentKind.MINUTES: CorpusEvidenceKind.MINUTES,
    }[kind]


def _descriptor_gap(
    descriptor: ExactCorpusWorkDescriptor,
    code: CorpusBridgeGapCode,
    evidence_kind: CorpusEvidenceKind,
) -> CorpusBridgeGap:
    return CorpusBridgeGap(
        code=code,
        work_id=descriptor.work_item.work_id,
        assembly_term=descriptor.assembly_term,
        evidence_kind=evidence_kind,
        official_identifier=descriptor.official_identifier,
    )


def _candidate_gap(
    candidate: CorpusSearchCandidate,
    code: CorpusBridgeGapCode,
) -> CorpusBridgeGap:
    return CorpusBridgeGap(
        code=code,
        assembly_term=candidate.identity.assembly_term,
        evidence_kind=candidate.identity.evidence_kind,
        official_identifier=candidate.identity.official_identifier,
    )


__all__ = [
    "CorpusBridgeGap",
    "CorpusBridgeGapCode",
    "CorpusCandidateMappingResult",
    "CorpusCandidateWorkMatch",
    "CorpusDocumentBridgeResult",
    "CorpusFailureBridgeResult",
    "ExactCorpusWorkDescriptor",
    "ScopeBoundCorpusIngestionFailure",
    "corpus_document_from_parsed",
    "corpus_failure_from_outcome",
    "map_candidates_to_work",
]
