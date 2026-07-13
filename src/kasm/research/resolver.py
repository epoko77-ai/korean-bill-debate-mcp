"""Resolve every collected bill and meeting candidate against a research plan.

This stage is deliberately exhaustive.  It applies the deterministic relevance
contract to every normalized metadata record, preserves both positive and
negative decisions, and never imposes a top-N limit.  Paging belongs to the
result layer after the complete candidate set has been resolved.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .collector import MetadataCollection, MetadataKind
from .planner import ResearchPlan
from .relevance import (
    DEFAULT_MINIMUM_SCORE,
    RelevanceCriteria,
    RelevanceResult,
    evaluate_candidate,
    rank_candidates,
)


class ExactBillNotFoundError(ValueError):
    """Raised when an explicitly requested bill is absent from full metadata."""

    def __init__(self, missing_bill_numbers: Sequence[str]) -> None:
        self.missing_bill_numbers = tuple(sorted(set(missing_bill_numbers)))
        numbers = ", ".join(self.missing_bill_numbers)
        super().__init__(
            f"exact requested bill number is absent from collected metadata: {numbers}"
        )


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    """Auditable resolution of one complete metadata candidate."""

    kind: MetadataKind
    candidate_id: str
    accepted: bool
    score: int
    match_reasons: tuple[str, ...]
    rejection_reasons: tuple[str, ...]
    candidate: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "candidate_id": self.candidate_id,
            "accepted": self.accepted,
            "score": self.score,
            "match_reasons": list(self.match_reasons),
            "rejection_reasons": list(self.rejection_reasons),
            "candidate": dict(self.candidate),
        }


@dataclass(frozen=True, slots=True)
class CandidateSetResolution:
    """Complete accounting for one bill or meeting candidate family."""

    kind: MetadataKind
    decisions: tuple[CandidateDecision, ...]
    accepted: tuple[CandidateDecision, ...]

    def __post_init__(self) -> None:
        identifiers = tuple(item.candidate_id for item in self.decisions)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(f"{self.kind.value} candidate ids must be unique")
        if any(item.kind is not self.kind for item in (*self.decisions, *self.accepted)):
            raise ValueError("candidate resolution contains the wrong metadata kind")
        accepted_ids = tuple(item.candidate_id for item in self.accepted)
        decision_by_id = {item.candidate_id: item for item in self.decisions}
        if len(accepted_ids) != len(set(accepted_ids)) or any(
            identifier not in decision_by_id for identifier in accepted_ids
        ):
            raise ValueError("accepted candidates must be unique resolved candidates")
        if any(not item.accepted for item in self.accepted):
            raise ValueError("accepted candidates must carry an accepted decision")
        if set(accepted_ids) != {
            item.candidate_id for item in self.decisions if item.accepted
        }:
            raise ValueError("accepted candidates must include every accepted decision")

    @property
    def total_candidates(self) -> int:
        return len(self.decisions)

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)

    @property
    def rejected_count(self) -> int:
        return self.total_candidates - self.accepted_count

    @property
    def rejection_reason_counts(self) -> tuple[tuple[str, int], ...]:
        counts = Counter(
            reason
            for decision in self.decisions
            if not decision.accepted
            for reason in decision.rejection_reasons
        )
        return tuple(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "total_candidates": self.total_candidates,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "rejection_reason_counts": dict(self.rejection_reason_counts),
            # Ranking is complete, not paged or truncated.  The explicit IDs
            # make this observable without duplicating every candidate payload.
            "accepted_candidate_ids": [item.candidate_id for item in self.accepted],
            "decisions": [item.to_dict() for item in self.decisions],
        }


@dataclass(frozen=True, slots=True)
class MetadataResolution:
    """Complete deterministic resolution of one metadata snapshot."""

    query: str
    source_hash: str
    criteria: RelevanceCriteria
    bills: CandidateSetResolution
    meetings: CandidateSetResolution

    def __post_init__(self) -> None:
        if self.bills.kind is not MetadataKind.BILL:
            raise ValueError("bills resolution must contain bill candidates")
        if self.meetings.kind is not MetadataKind.MEETING:
            raise ValueError("meetings resolution must contain meeting candidates")

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "source_hash": self.source_hash,
            "criteria": {
                "bill_numbers": list(self.criteria.bill_numbers),
                "statute_terms": list(self.criteria.statute_terms),
                "issue_terms": list(self.criteria.issue_terms),
                "related_statute_terms": list(self.criteria.related_statute_terms),
                "related_issue_terms": list(self.criteria.related_issue_terms),
                "committees": list(self.criteria.committees),
                "date_from": (
                    self.criteria.date_from.isoformat() if self.criteria.date_from else None
                ),
                "date_to": (
                    self.criteria.date_to.isoformat() if self.criteria.date_to else None
                ),
                "minimum_score": self.criteria.minimum_score,
                "terminology_version": self.criteria.terminology_version,
                "expansion_reasons": list(self.criteria.expansion_reasons),
            },
            "bills": self.bills.to_dict(),
            "meetings": self.meetings.to_dict(),
        }


class MetadataCandidateResolver:
    """Apply one plan to all metadata candidates without arbitrary truncation."""

    def __init__(self, *, minimum_score: int = DEFAULT_MINIMUM_SCORE) -> None:
        if minimum_score < 1:
            raise ValueError("minimum_score must be positive")
        self.minimum_score = minimum_score

    def resolve(
        self, plan: ResearchPlan, collection: MetadataCollection
    ) -> MetadataResolution:
        criteria = RelevanceCriteria.from_query(
            plan.contract.query,
            bill_numbers=plan.contract.bill_numbers,
            committees=plan.contract.committees,
            date_from=plan.contract.date_from,
            date_to=plan.contract.date_to,
            minimum_score=self.minimum_score,
        )
        self._require_exact_bills(criteria.bill_numbers, collection.bills)
        bills = _resolve_set(MetadataKind.BILL, collection.bills, criteria)
        meetings = _resolve_set(MetadataKind.MEETING, collection.meetings, criteria)
        return MetadataResolution(
            query=plan.contract.query,
            source_hash=collection.source_hash,
            criteria=criteria,
            bills=bills,
            meetings=meetings,
        )

    @staticmethod
    def _require_exact_bills(
        requested: Sequence[str], bills: Sequence[Mapping[str, Any]]
    ) -> None:
        available = {
            str(value).strip()
            for bill in bills
            if (value := bill.get("BILL_NO", bill.get("bill_no"))) is not None
        }
        missing = tuple(number for number in requested if number not in available)
        if missing:
            raise ExactBillNotFoundError(missing)


def accept_exact_corpus_candidates(
    resolution: MetadataResolution,
    *,
    bill_candidate_ids: Sequence[str] = (),
    meeting_candidate_ids: Sequence[str] = (),
) -> MetadataResolution:
    """Promote only exact, already-collected identities proven by corpus mapping.

    The caller must first map every lexical hit through ``corpus_bridge`` and
    verify that each resulting identity exists in this complete metadata
    resolution. Titles and fuzzy text never enter this function.
    """

    bills = _accept_exact_ids(
        resolution.bills,
        tuple(bill_candidate_ids),
        prefix="bill:",
    )
    meetings = _accept_exact_ids(
        resolution.meetings,
        tuple(meeting_candidate_ids),
        prefix="meeting:",
    )
    return MetadataResolution(
        query=resolution.query,
        source_hash=resolution.source_hash,
        criteria=resolution.criteria,
        bills=bills,
        meetings=meetings,
    )


def _accept_exact_ids(
    resolution: CandidateSetResolution,
    candidate_ids: tuple[str, ...],
    *,
    prefix: str,
) -> CandidateSetResolution:
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("exact corpus candidate ids must be unique")
    if any(not candidate_id.startswith(prefix) for candidate_id in candidate_ids):
        raise ValueError("exact corpus candidate id has the wrong metadata kind")
    by_id = {item.candidate_id: item for item in resolution.decisions}
    missing = tuple(candidate_id for candidate_id in candidate_ids if candidate_id not in by_id)
    if missing:
        raise ValueError("exact corpus candidate is absent from metadata resolution")
    exact = set(candidate_ids)
    updated: dict[str, CandidateDecision] = {}
    for decision in resolution.decisions:
        if decision.candidate_id not in exact:
            updated[decision.candidate_id] = decision
            continue
        updated[decision.candidate_id] = CandidateDecision(
            kind=decision.kind,
            candidate_id=decision.candidate_id,
            accepted=True,
            score=decision.score,
            match_reasons=tuple(
                dict.fromkeys((*decision.match_reasons, "corpus_exact_identity"))
            ),
            rejection_reasons=(),
            candidate=decision.candidate,
        )
    accepted_order = [item.candidate_id for item in resolution.accepted]
    accepted_order.extend(
        candidate_id
        for candidate_id in sorted(exact)
        if candidate_id not in accepted_order
    )
    decisions = tuple(updated[item.candidate_id] for item in resolution.decisions)
    accepted = tuple(updated[candidate_id] for candidate_id in accepted_order)
    return CandidateSetResolution(resolution.kind, decisions, accepted)


def resolve_metadata_candidates(
    plan: ResearchPlan,
    collection: MetadataCollection,
    *,
    minimum_score: int = DEFAULT_MINIMUM_SCORE,
) -> MetadataResolution:
    """Resolve a plan and metadata snapshot through the exhaustive default resolver."""

    return MetadataCandidateResolver(minimum_score=minimum_score).resolve(plan, collection)


def _resolve_set(
    kind: MetadataKind,
    candidates: Sequence[Mapping[str, Any]],
    criteria: RelevanceCriteria,
) -> CandidateSetResolution:
    prepared = tuple(_prepare_candidate(kind, candidate) for candidate in candidates)
    identifiers = tuple(candidate_id for candidate_id, _original, _scored in prepared)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{kind.value} candidate ids must be unique")
    original_by_id = {
        candidate_id: original for candidate_id, original, _scored in prepared
    }
    scored_candidates = tuple(scored for _candidate_id, _original, scored in prepared)

    ranked = rank_candidates(scored_candidates, criteria)
    decision_by_id = {
        result.candidate_id: _decision(kind, original_by_id[result.candidate_id], result)
        for result in ranked
    }
    for candidate_id, original, scored in prepared:
        if candidate_id in decision_by_id:
            continue
        result = evaluate_candidate(scored, criteria)
        decision_by_id[candidate_id] = _decision(kind, original, result)

    decisions = tuple(decision_by_id[key] for key in sorted(decision_by_id))
    accepted = tuple(decision_by_id[result.candidate_id] for result in ranked)
    return CandidateSetResolution(kind=kind, decisions=decisions, accepted=accepted)


def _prepare_candidate(
    kind: MetadataKind, candidate: Mapping[str, Any]
) -> tuple[str, Mapping[str, Any], dict[str, Any]]:
    original = dict(candidate)
    scored = dict(candidate)
    if kind is MetadataKind.BILL:
        raw_number = candidate.get("BILL_NO", candidate.get("bill_no"))
        bill_no = str(raw_number).strip() if raw_number is not None else ""
        if len(bill_no) != 7 or not bill_no.isdigit():
            raise ValueError("resolved bill candidates require an exact seven-digit bill number")
        candidate_id = f"bill:{bill_no}"
    else:
        raw_url = candidate.get("PDF_LINK_URL", candidate.get("DOWN_URL"))
        source_url = str(raw_url).strip() if raw_url is not None else ""
        if not source_url:
            raise ValueError("resolved meeting candidates require an official PDF URL")
        candidate_id = f"meeting:{source_url}"
        # Official meeting datasets use several date labels.  Relevance keeps
        # one canonical field contract, so adapt the metadata without dropping
        # or rewriting the original candidate returned in the decision.
        if "date" not in scored:
            for field in ("CONF_DATE", "MEETING_DATE", "MTG_DATE"):
                value = candidate.get(field)
                if value is not None and str(value).strip():
                    scored["date"] = value
                    break
    scored["id"] = candidate_id
    return candidate_id, original, scored


def _decision(
    kind: MetadataKind,
    original: Mapping[str, Any],
    result: RelevanceResult,
) -> CandidateDecision:
    return CandidateDecision(
        kind=kind,
        candidate_id=result.candidate_id,
        accepted=result.relevant,
        score=result.score,
        match_reasons=result.match_reasons,
        rejection_reasons=result.rejection_reasons,
        candidate=original,
    )


__all__ = [
    "CandidateDecision",
    "CandidateSetResolution",
    "ExactBillNotFoundError",
    "MetadataCandidateResolver",
    "MetadataResolution",
    "accept_exact_corpus_candidates",
    "resolve_metadata_candidates",
]
