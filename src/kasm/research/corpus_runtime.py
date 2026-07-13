"""Revision-bound full-text recall for the durable research engine.

The corpus may widen metadata relevance only when one immutable revision
proves all of the following: complete inventory coverage, a sufficient
inventory cutoff, exact query semantics, hard date/committee scope, and an
exact identity/URL/entity bridge.  Any uncertainty produces an explicit gap
and no widening identities.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import replace

from kasm.corpus import (
    CORPUS_SCHEMA_VERSION,
    CorpusDocumentRef,
    CorpusEvidenceKind,
    CorpusRepositoryIntegrityError,
    CorpusSearchCandidate,
    CorpusStorageError,
    FullTextCorpusReader,
    IncompleteCorpusRevisionError,
    LexicalMatchMode,
)
from kasm.search.terminology import TermCategory, TermRelation

from .contracts import EvidenceType
from .corpus_bridge import ExactCorpusWorkDescriptor, map_candidates_to_work
from .documents import OfficialDocumentKind
from .engine import CorpusRecallState, CorpusRecallStatus, DocumentWorkItem
from .planner import ResearchPlan
from .relevance import RelevanceCriteria, evaluate_candidate

RECALL_ALGORITHM_VERSION = "kbd-corpus-recall-v2"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MINUTES_EVIDENCE = (
    EvidenceType.AGENDAS,
    EvidenceType.SUBCOMMITTEE_MINUTES,
    EvidenceType.SPEECHES,
    EvidenceType.SPEECH_CONTEXT,
    EvidenceType.GOVERNMENT_RESPONSES,
)


class RevisionCorpusRecallProvider:
    """Search and exact-map every relevant document in one corpus revision."""

    def __init__(
        self,
        reader: FullTextCorpusReader,
        *,
        revision_id: str,
    ) -> None:
        if not _SHA256.fullmatch(revision_id):
            raise ValueError("corpus revision_id must be a SHA-256 digest")
        self.reader = reader
        self._revision_id = revision_id
        self._binding_id = hashlib.sha256(
            (
                f"{RECALL_ALGORITHM_VERSION}\0{CORPUS_SCHEMA_VERSION}\0"
                f"{revision_id}"
            ).encode()
        ).hexdigest()

    @property
    def revision_id(self) -> str:
        return self._revision_id

    @property
    def binding_id(self) -> str:
        """Bind jobs to the corpus, schema, and exact recall algorithm."""

        return self._binding_id

    def recall(
        self,
        plan: ResearchPlan,
        criteria: RelevanceCriteria,
    ) -> CorpusRecallState:
        """Return exact identities only after exhaustive revision accounting."""

        try:
            manifest = self.reader.get_revision(self.revision_id)
        except (CorpusRepositoryIntegrityError, CorpusStorageError, ValueError):
            return self._incomplete("corpus_revision_integrity_error")
        if manifest is None:
            return self._incomplete("corpus_revision_missing")
        if not manifest.complete:
            return self._incomplete("corpus_revision_incomplete")
        if not set(plan.contract.assembly_terms).issubset(manifest.assembly_terms):
            return self._incomplete("corpus_revision_scope_missing")
        if manifest.inventory_as_of < plan.contract.as_of:
            return self._incomplete("corpus_revision_stale")

        surfaces = _recall_surfaces(criteria)
        if not surfaces:
            return self._incomplete("corpus_query_terms_missing")
        try:
            lexical_candidates = self.reader.search_all(
                self.revision_id,
                " ".join(surfaces),
                match_mode=LexicalMatchMode.ANY,
                assembly_terms=plan.contract.assembly_terms,
                evidence_kinds=tuple(CorpusEvidenceKind),
                require_complete=True,
            )
        except IncompleteCorpusRevisionError:
            return self._incomplete("corpus_revision_incomplete")
        except (
            CorpusRepositoryIntegrityError,
            CorpusStorageError,
            LookupError,
            ValueError,
        ):
            return self._incomplete("corpus_search_failed")

        reference_by_id = {
            reference.identity.identity_id: reference
            for reference in manifest.documents
        }
        topic_criteria = replace(
            criteria,
            committees=(),
            date_from=None,
            date_to=None,
        )
        selected: list[CorpusSearchCandidate] = []
        descriptors: list[ExactCorpusWorkDescriptor] = []
        scope_gaps: Counter[str] = Counter()
        for candidate in lexical_candidates:
            reference = reference_by_id.get(candidate.identity_id)
            if reference is None or reference.official_url != candidate.official_url:
                return self._incomplete(
                    "corpus_candidate_manifest_mismatch",
                    candidate_count=len(lexical_candidates),
                )
            try:
                document = self.reader.get_document(reference)
            except (CorpusRepositoryIntegrityError, CorpusStorageError, ValueError):
                return self._incomplete(
                    "corpus_candidate_document_integrity_error",
                    candidate_count=len(lexical_candidates),
                )
            topical = evaluate_candidate(
                _relevance_candidate(reference, document.text),
                topic_criteria,
            )
            if not topical.relevant:
                continue
            scope_reason = _scope_exclusion_or_gap(reference, plan, criteria)
            if scope_reason is not None:
                if scope_reason.startswith("gap:"):
                    scope_gaps[scope_reason.removeprefix("gap:")] += 1
                continue
            selected.append(candidate)
            descriptors.append(_descriptor_from_reference(reference))

        if scope_gaps:
            return CorpusRecallState(
                status=CorpusRecallStatus.INCOMPLETE,
                revision_id=self.revision_id,
                candidate_count=len(selected) + sum(scope_gaps.values()),
                gap_reasons=tuple(
                    f"corpus_candidate_{reason}:{count}"
                    for reason, count in sorted(scope_gaps.items())
                ),
            )

        candidates = tuple(selected)
        mapping = map_candidates_to_work(candidates, tuple(descriptors))
        if not mapping.complete:
            reasons = tuple(
                f"corpus_candidate_{code}:{count}"
                for code, count in sorted(
                    Counter(gap.code.value for gap in mapping.gaps).items()
                )
            ) or ("corpus_candidate_mapping_incomplete",)
            return CorpusRecallState(
                status=CorpusRecallStatus.INCOMPLETE,
                revision_id=self.revision_id,
                candidate_count=mapping.candidate_count,
                mapped_count=mapping.matched_count,
                gap_reasons=reasons,
            )

        requested_evidence = set(plan.contract.evidence_types)
        bill_numbers: set[str] = set()
        meeting_urls: set[str] = set()
        required_work_ids: set[str] = set()
        for match in mapping.matches:
            item = match.descriptor.work_item
            bill_numbers.update(item.related_bill_numbers)
            if item.kind is OfficialDocumentKind.MINUTES:
                meeting_urls.add(match.candidate.official_url)
            if requested_evidence.intersection(item.evidence_types):
                required_work_ids.add(item.work_id)
        return CorpusRecallState(
            status=CorpusRecallStatus.VERIFIED,
            revision_id=self.revision_id,
            candidate_count=mapping.candidate_count,
            mapped_count=mapping.matched_count,
            exact_bill_numbers=tuple(sorted(bill_numbers)),
            exact_meeting_urls=tuple(sorted(meeting_urls)),
            required_work_ids=tuple(sorted(required_work_ids)),
        )

    def _incomplete(
        self,
        reason: str,
        *,
        candidate_count: int = 0,
        mapped_count: int = 0,
    ) -> CorpusRecallState:
        return CorpusRecallState(
            status=CorpusRecallStatus.INCOMPLETE,
            revision_id=self.revision_id,
            candidate_count=candidate_count,
            mapped_count=mapped_count,
            gap_reasons=(reason,),
        )


def _recall_surfaces(criteria: RelevanceCriteria) -> tuple[str, ...]:
    """Use the resolver's exact and related topic universe, never scope words."""

    values: list[str] = [
        *criteria.statute_terms,
        *criteria.issue_terms,
        *criteria.related_statute_terms,
        *criteria.related_issue_terms,
    ]
    for expansion in criteria.terminology_expansions:
        if (
            expansion.category in {TermCategory.ISSUE, TermCategory.STATUTE}
            and expansion.relation in {TermRelation.EQUIVALENT, TermRelation.RELATED}
        ):
            values.extend((expansion.source_text, expansion.term))
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def _relevance_candidate(
    reference: CorpusDocumentRef,
    text: str,
) -> dict[str, object]:
    return {
        "id": reference.identity.identity_id,
        "title": reference.title,
        "text": text,
        "date": reference.document_date,
        "committee": reference.committee,
        "related_bill_numbers": reference.related_bill_numbers,
    }


def _scope_exclusion_or_gap(
    reference: CorpusDocumentRef,
    plan: ResearchPlan,
    criteria: RelevanceCriteria,
) -> str | None:
    document_date = reference.document_date
    if document_date is None:
        return "gap:document_date_missing"
    if document_date > plan.contract.as_of.date():
        return "outside:as_of"
    if criteria.committees:
        if not reference.committee.strip():
            return "gap:committee_missing"
        scoped = replace(
            criteria,
            statute_terms=(),
            issue_terms=(),
            related_statute_terms=(),
            related_issue_terms=(),
            terminology_expansions=(),
            date_from=None,
            date_to=None,
        )
        if not evaluate_candidate(_relevance_candidate(reference, ""), scoped).relevant:
            return "outside:committee"
    if reference.identity.evidence_kind is CorpusEvidenceKind.MINUTES:
        if plan.contract.date_from and document_date < plan.contract.date_from:
            return "outside:date"
        if plan.contract.date_to and document_date > plan.contract.date_to:
            return "outside:date"
    return None


def _descriptor_from_reference(
    reference: CorpusDocumentRef,
) -> ExactCorpusWorkDescriptor:
    kind, evidence_types = {
        CorpusEvidenceKind.BILL_ORIGINAL: (
            OfficialDocumentKind.BILL_TEXT,
            (EvidenceType.BILL_TEXT,),
        ),
        CorpusEvidenceKind.REVIEW_REPORT: (
            OfficialDocumentKind.REVIEW_REPORT,
            (EvidenceType.REVIEW_REPORTS,),
        ),
        CorpusEvidenceKind.MINUTES: (
            OfficialDocumentKind.MINUTES,
            _MINUTES_EVIDENCE,
        ),
    }[reference.identity.evidence_kind]
    return ExactCorpusWorkDescriptor(
        work_item=DocumentWorkItem.create(
            kind,
            reference.official_url,
            evidence_types=evidence_types,
            related_bill_numbers=reference.related_bill_numbers,
        ),
        assembly_term=reference.identity.assembly_term,
        official_identifier=reference.identity.official_identifier,
        title=reference.title,
        document_date=reference.document_date,
        committee=reference.committee,
    )


__all__ = [
    "RECALL_ALGORITHM_VERSION",
    "RevisionCorpusRecallProvider",
]
