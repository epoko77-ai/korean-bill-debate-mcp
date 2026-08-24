"""Build an answer-ready, evidence-preserving brief for bounded issue research.

The brief does not summarize policy positions.  It projects already attributed
official records into a stable contract that tells an MCP client what it must
cover, what it may infer, and which checked evidence was omitted from transport.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

_DEFAULT_STAGES = ("subcommittee", "standing_committee", "plenary")
_COMPLETE_STAGE_STATES = {
    "discussion_found",
    "record_found_no_member_debate",
    "checked_no_matching_discussion",
}
_DIRECT_EXCERPT_BYTES = 2400
_CONTEXT_EXCERPT_BYTES = 800
_SUPPLEMENTAL_EXCERPT_BYTES = 1800
_MAX_SUPPLEMENTAL_EXCERPTS = 4
_MIN_SUBSTANTIVE_EXCERPT_BYTES = 450
_MAX_EVIDENCE_PER_STAGE = 16
_ARGUMENT_MARKER = re.compile(r"(?m)(첫째|첫\s*번째|둘째|두\s*번째|셋째|세\s*번째|마지막으로)")
_DERIVED_GAP_KINDS = {
    "attributed_evidence_omitted",
    "context_evidence_omitted",
    "transport_evidence_omitted",
    "supplemental_evidence_omitted",
}


def build_answer_brief(
    payload: Mapping[str, Any],
    *,
    requested_stages: Iterable[str] = (),
) -> dict[str, Any]:
    """Return a deterministic synthesis contract without adding semantic claims."""

    coverage = _mapping(payload.get("stage_coverage"))
    coverage_stages = _mapping(coverage.get("stages"))
    requested = _ordered_stages(
        requested_stages
        or _string_sequence(coverage.get("requested_stages"))
        or _string_sequence(payload.get("requested_stages"))
    )
    if not requested:
        requested = list(_DEFAULT_STAGES)

    meeting_index, stage_by_meeting = _meeting_index(payload, coverage_stages)
    direct_by_stage = _direct_evidence(payload, requested, stage_by_meeting)
    context_by_stage = _context_evidence(
        payload,
        requested,
        stage_by_meeting,
        {
            str(item.get("speech_id") or "")
            for values in direct_by_stage.values()
            for item in values
            if item.get("speech_id")
        },
    )

    all_stage_names = list(requested)
    for stage in (*direct_by_stage, *context_by_stage):
        if stage not in all_stage_names:
            all_stage_names.append(stage)

    candidate_ids = _candidate_speech_ids(payload, stage_by_meeting)
    stages: dict[str, Any] = {}
    ledger_by_stage: dict[str, Any] = {}
    gaps: list[dict[str, Any]] = []

    for stage in all_stage_names:
        stage_coverage = _mapping(coverage_stages.get(stage))
        direct = _dedupe_evidence(direct_by_stage.get(stage, []))
        context = _dedupe_evidence(context_by_stage.get(stage, []))
        evidence = (direct + context)[:_MAX_EVIDENCE_PER_STAGE]
        all_direct_ids = _evidence_ids(direct, use="direct_claim_evidence")
        returned_direct_ids = {
            value for value in _evidence_ids(evidence, use="direct_claim_evidence") if value
        }
        all_context_ids = _evidence_ids(context, use="context_only")
        returned_context_ids = _evidence_ids(evidence, use="context_only")
        discovered_ids = candidate_ids.get(stage, set()) | all_direct_ids
        discovered_count = len(discovered_ids)
        returned_count = len(returned_direct_ids)
        omitted_count = max(0, discovered_count - returned_count)
        supplemental_selected = sum(
            _integer(item.get("supplemental_excerpt_selected_before_transport_count"), fallback=0)
            for item in evidence
            if item.get("evidence_use") == "direct_claim_evidence"
        )
        supplemental_returned = sum(
            len(_sequence_of_mappings(item.get("supplemental_excerpts")))
            for item in evidence
            if item.get("evidence_use") == "direct_claim_evidence"
        )

        meetings = _stage_meetings(stage, stage_coverage, meeting_index)
        checked_no_discussion_meeting_ids = {
            str(item.get("meeting_id") or "")
            for item in meetings
            if item.get("meeting_id")
            and str(item.get("evidence_state") or "")
            in {"checked_no_matching_discussion", "record_found_no_member_debate"}
        }
        candidate_meetings = _integer(
            stage_coverage.get("observed_candidate_count"),
            fallback=_integer(stage_coverage.get("candidate_count"), fallback=len(meetings)),
        )
        checked_meetings = min(
            candidate_meetings,
            _integer(stage_coverage.get("checked_count"), fallback=0),
        )
        returned_meeting_ids = {
            str(item.get("meeting_id") or "") for item in evidence if item.get("meeting_id")
        }
        meeting_omissions = max(0, candidate_meetings - len(returned_meeting_ids))

        state = str(stage_coverage.get("state") or "not_checked")
        stages[stage] = {
            "state": state,
            "meetings": meetings,
            "evidence": evidence,
            "participants": _stage_participants(evidence),
            "answer_instruction": _stage_instruction(state, bool(evidence)),
        }
        ledger_by_stage[stage] = {
            # These four top-level counts refer to directly attributable turns.
            "discovered_count": discovered_count,
            "checked_count": discovered_count,
            "returned_count": returned_count,
            "omitted_count": omitted_count,
            "selected_before_transport_count": returned_count,
            "selection_omitted_count": omitted_count,
            "transport_omitted_count": 0,
            "supplemental_selected_before_transport_count": supplemental_selected,
            "supplemental_returned_count": supplemental_returned,
            "supplemental_transport_omitted_count": max(
                0, supplemental_selected - supplemental_returned
            ),
            "failed_count": _integer(stage_coverage.get("failed_count"), fallback=0),
            "pending_count": _integer(stage_coverage.get("pending_count"), fallback=0),
            "context_discovered_count": len(all_context_ids),
            "context_selected_before_transport_count": len(returned_context_ids),
            "context_only_returned_count": len(returned_context_ids),
            "context_omitted_count": max(0, len(all_context_ids) - len(returned_context_ids)),
            "context_transport_omitted_count": 0,
            "meeting_counts": {
                "discovered_count": candidate_meetings,
                "checked_count": checked_meetings,
                "returned_count": len(returned_meeting_ids),
                "omitted_count": meeting_omissions,
                "selected_before_transport_count": len(returned_meeting_ids),
                "selection_omitted_count": meeting_omissions,
                "transport_omitted_count": 0,
                "failed_count": _integer(stage_coverage.get("failed_count"), fallback=0),
                "pending_count": _integer(stage_coverage.get("pending_count"), fallback=0),
                "checked_no_matching_discussion_count": len(checked_no_discussion_meeting_ids),
            },
            "omission_reasons": (
                ["answer_brief_stage_limit_or_upstream_selection"] if omitted_count else []
            )
            + (
                ["context_evidence_stage_limit"]
                if len(all_context_ids) > len(returned_context_ids)
                else []
            )
            + (
                ["checked_candidate_meeting_no_matching_discussion"]
                if checked_no_discussion_meeting_ids
                else []
            )
            + (
                ["candidate_meeting_not_represented_in_evidence"]
                if meeting_omissions > len(checked_no_discussion_meeting_ids)
                else []
            ),
        }
        if state not in _COMPLETE_STAGE_STATES:
            gaps.append(
                {
                    "kind": "stage_incomplete",
                    "stage": stage,
                    "state": state,
                    "reason": stage_coverage.get("gap_reason") or state,
                }
            )
        if omitted_count:
            gaps.append(
                {
                    "kind": "attributed_evidence_omitted",
                    "stage": stage,
                    "count": omitted_count,
                }
            )

    ledger_totals = _ledger_totals(ledger_by_stage)
    pagination = _mapping(payload.get("research_pagination"))
    unselected_meetings = _integer(
        pagination.get("unselected_candidate_count"),
        fallback=_integer(
            _mapping(payload.get("live_refresh")).get("unselected_candidate_count"),
            fallback=0,
        ),
    )
    if unselected_meetings:
        gaps.append(
            {
                "kind": "candidate_meetings_not_checked",
                "count": unselected_meetings,
                "reason": "bounded_candidate_limit",
            }
        )

    measure = _measure(payload)
    unallocated_candidates = _unallocated_candidate_count(payload, candidate_ids)
    if unallocated_candidates:
        gaps.append(
            {
                "kind": "candidate_evidence_stage_unallocated",
                "count": unallocated_candidates,
                "reason": "bounded_inventory_projection",
            }
        )
    brief = {
        "schema_version": "answer-brief-v1",
        "measure": measure,
        "scope": {
            "mode": str(payload.get("research_mode") or "bounded_live"),
            "requested_stages": requested,
            "temporal_scope": pagination.get("temporal_scope"),
            "targeted_core_complete": pagination.get("complete"),
            "candidate_inventory_complete": pagination.get("candidate_inventory_complete"),
            "unselected_candidate_meeting_count": unselected_meetings,
            "unallocated_candidate_evidence_count": unallocated_candidates,
            "not_exhaustive": True,
            "scope_label": "sufficient_targeted_search_not_full_corpus_census",
        },
        "processing": {
            "timeline": _sequence_of_mappings(payload.get("timeline")),
            "stage_order": requested,
            "vote_or_decision_must_come_from_official_record": True,
        },
        "stages": stages,
        "participant_index": [],
        "evidence_ledger": {
            "count_unit": "directly_attributed_speech_turn",
            "by_stage": ledger_by_stage,
            "totals": ledger_totals,
            "unallocated_candidate_count": unallocated_candidates,
            "instruction": (
                "발견·확인·반환·생략 수를 함께 밝히고 생략이 있으면 주요 발언 전체를 "
                "포함했다고 표현하지 마세요."
            ),
        },
        "comparison_readiness": {},
        "gaps": gaps,
        "required_answer_sections": _required_answer_sections(),
        "synthesis_contract": {
            "primary_input": "answer_brief.stages[*].evidence",
            "cover_every_direct_evidence": True,
            "cover_every_supplemental_excerpt": True,
            "supplemental_excerpts_are_continuations_of_parent_evidence": True,
            "do_not_count_supplemental_excerpts_as_additional_turns": True,
            "group_only_genuinely_redundant_arguments": True,
            "allow_stance_inference": False,
            "allow_vote_motive_inference": False,
            "separate_actor_types": [
                "legislator",
                "government",
                "committee_staff_or_expert",
                "other",
            ],
            "claim_pattern": ["claim", "reason", "response_or_rebuttal", "outcome"],
            "claim_pattern_only_when_supported": True,
            "citation_required_per_factual_claim": True,
            "require_official_citations": True,
            "official_url_and_locator_required": True,
            "context_only_evidence_cannot_support_independent_claim": True,
            "distinguish_record_absence_from_unchecked_or_failed": True,
            "distinguish_official_fact_from_analysis": True,
            "bounded_scope_disclosure_belongs_in_limitations_not_in_place_of_answer": True,
        },
    }
    return reconcile_answer_brief(brief)


def reconcile_answer_brief(brief: dict[str, Any]) -> dict[str, Any]:
    """Refresh every transport-sensitive field from the evidence still present.

    Discovery and pre-transport selection counts are immutable baselines.  The
    returned, omitted, participant, readiness, and gap fields are projections
    of the current ``stages[*].evidence`` arrays and are therefore safe to call
    repeatedly after any transport compaction.
    """

    raw_stages = brief.get("stages")
    stages = raw_stages if isinstance(raw_stages, dict) else {}
    raw_ledger = brief.get("evidence_ledger")
    ledger = raw_ledger if isinstance(raw_ledger, dict) else {}
    raw_by_stage = ledger.get("by_stage")
    by_stage = raw_by_stage if isinstance(raw_by_stage, dict) else {}

    for stage_name, raw_stage in stages.items():
        if not isinstance(raw_stage, dict):
            continue
        raw_evidence = raw_stage.get("evidence")
        evidence = (
            [item for item in raw_evidence if isinstance(item, dict)]
            if isinstance(raw_evidence, list)
            else []
        )
        raw_stage["evidence"] = evidence
        raw_stage["participants"] = _stage_participants(evidence)

        raw_counts = by_stage.get(stage_name)
        counts = raw_counts if isinstance(raw_counts, dict) else {}
        supplemental_selected_in_rows = 0
        supplemental_returned = 0
        for item in evidence:
            if item.get("evidence_use") != "direct_claim_evidence":
                continue
            raw_supplements = item.get("supplemental_excerpts")
            supplements = (
                [dict(value) for value in raw_supplements if isinstance(value, Mapping)]
                if isinstance(raw_supplements, list)
                else []
            )
            previous_supplemental_returned = _integer(
                item.get("supplemental_excerpt_returned_count"), fallback=0
            )
            selected_supplements = max(
                len(supplements),
                previous_supplemental_returned,
                _integer(
                    item.get("supplemental_excerpt_selected_before_transport_count"),
                    fallback=previous_supplemental_returned,
                ),
            )
            if selected_supplements:
                item["supplemental_excerpts"] = supplements
                item["supplemental_excerpt_selected_before_transport_count"] = selected_supplements
                item["supplemental_excerpt_returned_count"] = len(supplements)
                item["supplemental_excerpt_transport_omitted_count"] = max(
                    0, selected_supplements - len(supplements)
                )
            supplemental_selected_in_rows += selected_supplements
            supplemental_returned += len(supplements)
        direct_ids = _evidence_ids(evidence, use="direct_claim_evidence")
        context_ids = _evidence_ids(evidence, use="context_only")

        previous_returned = _integer(counts.get("returned_count"), fallback=0)
        selected_before_transport = max(
            len(direct_ids),
            previous_returned,
            _integer(
                counts.get("selected_before_transport_count"),
                fallback=previous_returned,
            ),
        )
        checked = max(
            selected_before_transport,
            _integer(counts.get("checked_count"), fallback=0),
        )
        discovered = max(
            checked,
            _integer(counts.get("discovered_count"), fallback=checked),
        )
        counts.update(
            {
                "discovered_count": discovered,
                "checked_count": checked,
                "returned_count": len(direct_ids),
                "omitted_count": max(0, discovered - len(direct_ids)),
                "selected_before_transport_count": selected_before_transport,
                "selection_omitted_count": max(0, discovered - selected_before_transport),
                "transport_omitted_count": max(0, selected_before_transport - len(direct_ids)),
            }
        )
        supplemental_selected = max(
            supplemental_selected_in_rows,
            supplemental_returned,
            _integer(
                counts.get("supplemental_selected_before_transport_count"),
                fallback=supplemental_selected_in_rows,
            ),
        )
        counts.update(
            {
                "supplemental_selected_before_transport_count": supplemental_selected,
                "supplemental_returned_count": supplemental_returned,
                "supplemental_transport_omitted_count": max(
                    0, supplemental_selected - supplemental_returned
                ),
            }
        )

        previous_context_returned = _integer(counts.get("context_only_returned_count"), fallback=0)
        context_selected = max(
            len(context_ids),
            previous_context_returned,
            _integer(
                counts.get("context_selected_before_transport_count"),
                fallback=previous_context_returned,
            ),
        )
        context_discovered = max(
            context_selected,
            _integer(counts.get("context_discovered_count"), fallback=context_selected),
        )
        counts.update(
            {
                "context_discovered_count": context_discovered,
                "context_selected_before_transport_count": context_selected,
                "context_only_returned_count": len(context_ids),
                "context_omitted_count": max(0, context_discovered - len(context_ids)),
                "context_transport_omitted_count": max(0, context_selected - len(context_ids)),
            }
        )

        raw_meeting_counts = counts.get("meeting_counts")
        meeting_counts = raw_meeting_counts if isinstance(raw_meeting_counts, dict) else {}
        returned_meetings = {
            str(item.get("meeting_id") or "") for item in evidence if item.get("meeting_id")
        }
        previous_meeting_returned = _integer(meeting_counts.get("returned_count"), fallback=0)
        meeting_selected = max(
            len(returned_meetings),
            previous_meeting_returned,
            _integer(
                meeting_counts.get("selected_before_transport_count"),
                fallback=previous_meeting_returned,
            ),
        )
        meeting_checked = max(
            meeting_selected,
            _integer(meeting_counts.get("checked_count"), fallback=0),
        )
        meeting_discovered = max(
            meeting_checked,
            _integer(
                meeting_counts.get("discovered_count"),
                fallback=meeting_checked,
            ),
        )
        meeting_counts.update(
            {
                "count_unit": "official_meeting_represented_in_returned_evidence",
                "discovered_count": meeting_discovered,
                "checked_count": meeting_checked,
                "returned_count": len(returned_meetings),
                "represented_in_returned_evidence_count": len(returned_meetings),
                "omitted_count": max(0, meeting_discovered - len(returned_meetings)),
                "selected_before_transport_count": meeting_selected,
                "selection_omitted_count": max(0, meeting_discovered - meeting_selected),
                "transport_omitted_count": max(0, meeting_selected - len(returned_meetings)),
            }
        )
        counts["meeting_counts"] = meeting_counts

        preserved_reasons = [
            str(reason)
            for reason in counts.get("omission_reasons") or []
            if str(reason)
            not in {
                "selected_evidence_limit_or_transport_budget",
                "answer_brief_stage_limit_or_upstream_selection",
                "context_evidence_stage_limit",
                "transport_budget",
                "supplemental_transport_budget",
                "candidate_meeting_not_represented_in_evidence",
                "checked_candidate_meeting_no_matching_discussion",
            }
        ]
        if counts["selection_omitted_count"]:
            preserved_reasons.append("answer_brief_stage_limit_or_upstream_selection")
        if max(
            counts["transport_omitted_count"],
            counts["context_transport_omitted_count"],
            meeting_counts["transport_omitted_count"],
        ):
            preserved_reasons.append("transport_budget")
        if counts["supplemental_transport_omitted_count"]:
            preserved_reasons.append("supplemental_transport_budget")
        if counts["context_omitted_count"] - counts["context_transport_omitted_count"] > 0:
            preserved_reasons.append("context_evidence_stage_limit")
        checked_no_discussion = _integer(
            meeting_counts.get("checked_no_matching_discussion_count"),
            fallback=0,
        )
        if checked_no_discussion:
            preserved_reasons.append("checked_candidate_meeting_no_matching_discussion")
        if meeting_counts["omitted_count"] > checked_no_discussion:
            preserved_reasons.append("candidate_meeting_not_represented_in_evidence")
        counts["omission_reasons"] = list(dict.fromkeys(preserved_reasons))
        by_stage[str(stage_name)] = counts

    ledger["by_stage"] = by_stage
    ledger["totals"] = _ledger_totals(by_stage)
    brief["evidence_ledger"] = ledger
    participant_index = _participant_index_from_stages(stages)
    brief["participant_index"] = participant_index

    raw_gaps = brief.get("gaps")
    gaps = (
        [
            dict(gap)
            for gap in raw_gaps
            if isinstance(gap, Mapping) and str(gap.get("kind") or "") not in _DERIVED_GAP_KINDS
        ]
        if isinstance(raw_gaps, list)
        else []
    )
    for stage_name, raw_counts in by_stage.items():
        stage_counts = _mapping(raw_counts)
        direct_omitted = _integer(stage_counts.get("omitted_count"), fallback=0)
        context_omitted = _integer(stage_counts.get("context_omitted_count"), fallback=0)
        direct_transport = _integer(stage_counts.get("transport_omitted_count"), fallback=0)
        context_transport = _integer(
            stage_counts.get("context_transport_omitted_count"), fallback=0
        )
        supplemental_transport = _integer(
            stage_counts.get("supplemental_transport_omitted_count"), fallback=0
        )
        meeting_transport = _integer(
            _mapping(stage_counts.get("meeting_counts")).get("transport_omitted_count"),
            fallback=0,
        )
        if direct_omitted:
            gaps.append(
                {
                    "kind": "attributed_evidence_omitted",
                    "stage": stage_name,
                    "count": direct_omitted,
                    "selection_count": _integer(
                        stage_counts.get("selection_omitted_count"), fallback=0
                    ),
                    "transport_count": direct_transport,
                }
            )
        if context_omitted:
            gaps.append(
                {
                    "kind": "context_evidence_omitted",
                    "stage": stage_name,
                    "count": context_omitted,
                    "transport_count": context_transport,
                }
            )
        if direct_transport or context_transport or meeting_transport:
            gaps.append(
                {
                    "kind": "transport_evidence_omitted",
                    "stage": stage_name,
                    "direct_count": direct_transport,
                    "context_count": context_transport,
                    "meeting_count": meeting_transport,
                }
            )
        if supplemental_transport:
            gaps.append(
                {
                    "kind": "supplemental_evidence_omitted",
                    "stage": stage_name,
                    "count": supplemental_transport,
                    "reason": "transport_budget",
                }
            )
    brief["gaps"] = gaps

    scope = _mapping(brief.get("scope"))
    requested = _ordered_stages(
        _string_sequence(scope.get("requested_stages"))
        or _string_sequence(_mapping(brief.get("processing")).get("stage_order"))
    )
    brief["comparison_readiness"] = _comparison_readiness(
        measure=_mapping(brief.get("measure")),
        requested=requested,
        stages=stages,
        ledger=by_stage,
        participant_index=participant_index,
        pagination={"complete": scope.get("targeted_core_complete")},
        unselected_meetings=_integer(scope.get("unselected_candidate_meeting_count"), fallback=0),
        unallocated_candidates=_integer(
            ledger.get("unallocated_candidate_count"),
            fallback=_integer(scope.get("unallocated_candidate_evidence_count"), fallback=0),
        ),
    )
    return brief


def _measure(payload: Mapping[str, Any]) -> dict[str, Any]:
    resolution = _mapping(payload.get("target_resolution"))
    family = _sequence_of_mappings(resolution.get("measure_family"))
    primary = str(resolution.get("primary_vehicle_bill_no") or "") or None
    source_numbers = [
        str(item.get("bill_no"))
        for item in family
        if item.get("bill_no") and str(item.get("role") or "") == "source_member_bill"
    ]
    lineage = [
        {
            "from_bill_no": number,
            "to_bill_no": primary,
            "relation": "source_to_primary_vehicle",
            "evidence_status": "retrieval_metadata_not_evidence",
        }
        for number in source_numbers
        if primary and number != primary
    ]
    bills = []
    for bill in _sequence_of_mappings(payload.get("bills")):
        bills.append(
            {
                "bill_no": bill.get("bill_no"),
                "name": bill.get("name") or bill.get("title"),
                "proposer": bill.get("proposer"),
                "proposed_at": bill.get("proposed_at"),
                "process_result": bill.get("process_result") or bill.get("status"),
                "processed_at": bill.get("processed_at"),
                "official_url": bill.get("official_url"),
                "documents_included": bill.get("documents_included"),
            }
        )
    return {
        "matched_alias": resolution.get("matched_alias"),
        "primary_vehicle_bill_no": primary,
        "source_bill_numbers": source_numbers,
        "family": family,
        "lineage": lineage,
        "official_identity_confidence": resolution.get("confidence"),
        "live_verified_bill_numbers": resolution.get("live_verified_bill_numbers") or [],
        "bills": bills,
        "identity_instruction": (
            "원안·위원회 대안·본회의 처리안을 구분하고, 공식 병합 근거가 없으면 "
            "원안이 대안에 통합됐다고 단정하지 마세요."
        ),
    }


def _meeting_index(
    payload: Mapping[str, Any], coverage_stages: Mapping[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    meetings: dict[str, dict[str, Any]] = {}
    stage_by_meeting: dict[str, str] = {}
    inventory = _mapping(_mapping(payload.get("scope_inventory")).get("meeting_candidates"))
    for item in _sequence_of_mappings(inventory.get("items")):
        meeting_id = str(item.get("meeting_id") or "")
        if not meeting_id:
            continue
        stage = _stage_name(item.get("meeting_type"), item.get("title"))
        meetings[meeting_id] = _meeting_projection(item, stage)
        if stage:
            stage_by_meeting[meeting_id] = stage
    for stage, raw in coverage_stages.items():
        for item in _sequence_of_mappings(_mapping(raw).get("meetings")):
            meeting_id = str(item.get("meeting_id") or "")
            if not meeting_id:
                continue
            stage_by_meeting[meeting_id] = str(stage)
            meetings.setdefault(meeting_id, _meeting_projection(item, str(stage)))
    for source in _sequence_of_mappings(payload.get("speeches")) + _sequence_of_mappings(
        payload.get("discussion_threads")
    ):
        meeting_id = str(source.get("meeting_id") or "")
        if not meeting_id:
            continue
        stage = stage_by_meeting.get(meeting_id) or _stage_name(
            source.get("meeting_type"), source.get("meeting")
        )
        if stage:
            stage_by_meeting[meeting_id] = stage
        meetings.setdefault(meeting_id, _meeting_projection(source, stage))
    return meetings, stage_by_meeting


def _direct_evidence(
    payload: Mapping[str, Any],
    requested: list[str],
    stage_by_meeting: Mapping[str, str],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for speech in _sequence_of_mappings(payload.get("speeches")):
        meeting_id = str(speech.get("meeting_id") or "")
        stage = stage_by_meeting.get(meeting_id) or _stage_name(
            speech.get("meeting_type"), speech.get("meeting")
        )
        if not stage:
            stage = "unassigned"
        result[stage].append(_evidence_projection(speech, stage, direct=True))
    for stage in requested:
        result.setdefault(stage, [])
    return dict(result)


def _context_evidence(
    payload: Mapping[str, Any],
    requested: list[str],
    stage_by_meeting: Mapping[str, str],
    direct_ids: set[str],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen = set(direct_ids)
    for thread in _sequence_of_mappings(payload.get("discussion_threads")):
        meeting_id = str(thread.get("meeting_id") or "")
        stage = stage_by_meeting.get(meeting_id) or _stage_name(
            thread.get("meeting_type"), thread.get("meeting")
        )
        if not stage:
            stage = "unassigned"
        for turn in _sequence_of_mappings(thread.get("turns")):
            speech_id = str(turn.get("speech_id") or "")
            if speech_id and speech_id in seen:
                continue
            if speech_id:
                seen.add(speech_id)
            merged = dict(turn)
            merged.setdefault("meeting_id", meeting_id)
            merged.setdefault("meeting", thread.get("meeting"))
            merged.setdefault("committee", thread.get("committee"))
            merged.setdefault("meeting_type", thread.get("meeting_type"))
            merged.setdefault("date", thread.get("date"))
            result[stage].append(_evidence_projection(merged, stage, direct=False))
    for stage in requested:
        result.setdefault(stage, [])
    return dict(result)


def _evidence_projection(item: Mapping[str, Any], stage: str, *, direct: bool) -> dict[str, Any]:
    speech_id = str(item.get("speech_id") or "")
    citation = _mapping(item.get("citation"))
    official_url = (
        citation.get("official_url") or item.get("official_source") or item.get("official_url")
    )
    locator = citation.get("source_locator") or item.get("source_locator")
    text = str(item.get("text") or "")
    excerpt_bytes = _DIRECT_EXCERPT_BYTES if direct else _CONTEXT_EXCERPT_BYTES
    excerpt = _clip_utf8(text, excerpt_bytes)
    evidence_id = speech_id or f"{stage}:turn:{item.get('sequence') or 0}"
    projection = {
        "evidence_id": evidence_id,
        "speech_id": speech_id or None,
        "evidence_use": "direct_claim_evidence" if direct else "context_only",
        "stage": stage,
        "speaker": item.get("speaker") or item.get("speaker_name"),
        "role": item.get("speaker_role") or item.get("role"),
        "organization": item.get("organization"),
        "meeting_id": item.get("meeting_id"),
        "meeting": item.get("meeting") or citation.get("meeting"),
        "committee": item.get("committee"),
        "meeting_type": item.get("meeting_type"),
        "date": item.get("date") or citation.get("date"),
        "sequence": item.get("sequence"),
        "agenda": item.get("agenda"),
        "excerpt_verbatim": excerpt,
        "excerpt_inline_complete": excerpt == text,
        "text_length": len(text),
        "attribution": item.get("attribution"),
        "citation": {
            "official_url": official_url,
            "source_locator": locator,
            "meeting": item.get("meeting") or citation.get("meeting"),
            "date": item.get("date") or citation.get("date"),
            "speaker": item.get("speaker") or item.get("speaker_name"),
        },
    }
    supplemental = (
        _supplemental_argument_excerpts(text, evidence_id=evidence_id)
        if direct and excerpt != text
        else []
    )
    if supplemental:
        projection.update(
            {
                "supplemental_excerpts": supplemental,
                "supplemental_excerpt_selected_before_transport_count": len(supplemental),
                "supplemental_excerpt_returned_count": len(supplemental),
                "supplemental_excerpt_transport_omitted_count": 0,
            }
        )
    return projection


def _supplemental_argument_excerpts(
    text: str,
    *,
    evidence_id: str,
) -> list[dict[str, Any]]:
    """Preserve later enumerated reasons that a prefix excerpt would hide.

    Supplemental excerpts remain part of the parent attributed turn.  They are
    deliberately created only when at least two distinct enumeration markers
    occur, avoiding semantic inference from an arbitrary long speech.
    """

    if len(text.encode("utf-8")) <= _DIRECT_EXCERPT_BYTES:
        return []
    matches = list(_ARGUMENT_MARKER.finditer(text))
    marker_kinds = {_argument_marker_kind(match.group(1)) for match in matches}
    if len(marker_kinds) < 2:
        return []

    primary_end = _utf8_prefix_character_index(text, _DIRECT_EXCERPT_BYTES)
    candidates: list[tuple[str, str, int, int]] = []
    for index, match in enumerate(matches):
        segment_start = match.start()
        segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if segment_end <= primary_end:
            continue
        if segment_start < primary_end:
            continuation_start = _sentence_start_near(text, primary_end, floor=segment_start)
            candidates.append(
                ("argument_continuation", match.group(1), continuation_start, segment_end)
            )
        else:
            candidates.append(("enumerated_argument", match.group(1), segment_start, segment_end))

    supplements: list[dict[str, Any]] = []
    for kind, marker, start, end in candidates[:_MAX_SUPPLEMENTAL_EXCERPTS]:
        source = text[start:end].strip()
        if not source:
            continue
        excerpt = _clip_utf8(source, _SUPPLEMENTAL_EXCERPT_BYTES)
        supplements.append(
            {
                "excerpt_id": f"{evidence_id}:supplement:{len(supplements) + 1}",
                "segment_kind": kind,
                "argument_marker": marker,
                "excerpt_verbatim": excerpt,
                "segment_inline_complete": excerpt == source,
                "source_start_byte": len(text[:start].encode("utf-8")),
                "source_end_byte": len(text[:end].encode("utf-8")),
            }
        )
    return supplements


def _argument_marker_kind(value: str) -> str:
    normalized = re.sub(r"\s+", "", value)
    if normalized in {"첫째", "첫번째"}:
        return "first"
    if normalized in {"둘째", "두번째"}:
        return "second"
    if normalized in {"셋째", "세번째"}:
        return "third"
    return "final"


def _utf8_prefix_character_index(value: str, maximum: int) -> int:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return len(value)
    return len(encoded[:maximum].decode("utf-8", errors="ignore"))


def _sentence_start_near(value: str, index: int, *, floor: int) -> int:
    """Return a readable continuation start with only a small prefix overlap."""

    search_start = max(floor, index - 180)
    boundary = search_start
    for match in re.finditer(r"[.!?。]\s+", value[search_start:index]):
        boundary = search_start + match.end()
    return boundary


def _candidate_speech_ids(
    payload: Mapping[str, Any], stage_by_meeting: Mapping[str, str]
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    inventory = _mapping(_mapping(payload.get("scope_inventory")).get("speech_candidates"))
    for item in _sequence_of_mappings(inventory.get("items")):
        speech_id = str(item.get("speech_id") or "")
        meeting_id = str(item.get("meeting_id") or "")
        stage = stage_by_meeting.get(meeting_id) or _stage_name(
            item.get("meeting_type"), item.get("meeting")
        )
        if speech_id and stage:
            result[stage].add(speech_id)
    return dict(result)


def _stage_meetings(
    stage: str,
    stage_coverage: Mapping[str, Any],
    meeting_index: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _sequence_of_mappings(stage_coverage.get("meetings")):
        meeting_id = str(item.get("meeting_id") or "")
        merged = dict(meeting_index.get(meeting_id) or {})
        merged.update(item)
        merged["stage"] = stage
        values.append(merged)
        seen.add(meeting_id)
    for meeting_id, indexed_item in meeting_index.items():
        if meeting_id in seen or indexed_item.get("stage") != stage:
            continue
        values.append(dict(indexed_item))
    return values


def _meeting_projection(item: Mapping[str, Any], stage: str | None) -> dict[str, Any]:
    return {
        "meeting_id": item.get("meeting_id"),
        "stage": stage,
        "date": item.get("date"),
        "title": item.get("title") or item.get("meeting"),
        "committee": item.get("committee"),
        "meeting_type": item.get("meeting_type"),
        "related_bill_numbers": item.get("related_bill_numbers") or [],
        "official_url": item.get("official_url") or item.get("official_source"),
        "full_text_loaded": item.get("full_text_loaded"),
        "speech_count": item.get("speech_count"),
        "evidence_state": item.get("evidence_state"),
    }


def _comparison_readiness(
    *,
    measure: Mapping[str, Any],
    requested: list[str],
    stages: Mapping[str, Any],
    ledger: Mapping[str, Any],
    participant_index: list[dict[str, Any]],
    pagination: Mapping[str, Any],
    unselected_meetings: int,
    unallocated_candidates: int,
) -> dict[str, Any]:
    direct = [
        item
        for stage in stages.values()
        for item in _sequence_of_mappings(_mapping(stage).get("evidence"))
        if item.get("evidence_use") == "direct_claim_evidence"
    ]
    cited = [
        item
        for item in direct
        if _mapping(item.get("citation")).get("official_url")
        and _mapping(item.get("citation")).get("source_locator")
    ]
    complete_stages = [
        stage
        for stage in requested
        if str(_mapping(stages.get(stage)).get("state") or "") in _COMPLETE_STAGE_STATES
    ]
    omitted = sum(
        _integer(_mapping(ledger.get(stage)).get("omitted_count"), fallback=0)
        for stage in requested
    )
    stages_with_evidence_or_verified_absence = [
        stage
        for stage in requested
        if _mapping(stages.get(stage)).get("evidence")
        or str(_mapping(stages.get(stage)).get("state") or "")
        in {"record_found_no_member_debate", "checked_no_matching_discussion"}
    ]
    context_count = sum(
        item.get("evidence_use") == "context_only"
        for stage in stages.values()
        for item in _sequence_of_mappings(_mapping(stage).get("evidence"))
    )
    supplemental_returned = sum(
        len(_sequence_of_mappings(item.get("supplemental_excerpts"))) for item in direct
    )
    supplemental_omitted = sum(
        _integer(
            _mapping(ledger.get(stage)).get("supplemental_transport_omitted_count"),
            fallback=0,
        )
        for stage in requested
    )
    detailed = [
        item
        for item in direct
        if item.get("date")
        and item.get("speaker")
        and item.get("excerpt_verbatim")
        and _mapping(item.get("citation")).get("source_locator")
        and (
            item.get("excerpt_inline_complete") is True
            or len(str(item.get("excerpt_verbatim") or "").encode("utf-8"))
            >= _MIN_SUBSTANTIVE_EXCERPT_BYTES
        )
    ]
    excerpt_bytes = [
        len(str(item.get("excerpt_verbatim") or "").encode("utf-8")) for item in direct
    ]
    dimensions = {
        "content": _dimension(
            bool(measure.get("primary_vehicle_bill_no")) and bool(direct),
            signals={
                "primary_vehicle_identified": bool(measure.get("primary_vehicle_bill_no")),
                "direct_evidence_count": len(direct),
            },
            gaps=[] if direct else ["no_direct_discussion_evidence"],
        ),
        "accuracy": _dimension(
            bool(direct) and len(cited) == len(direct),
            signals={
                "official_identity_confidence": measure.get("official_identity_confidence"),
                "direct_evidence_count": len(direct),
                "fully_cited_direct_evidence_count": len(cited),
                "stance_inference_allowed": False,
            },
            gaps=[] if len(cited) == len(direct) else ["missing_official_url_or_locator"],
        ),
        "completeness": _dimension(
            len(complete_stages) == len(requested)
            and omitted == 0
            and unselected_meetings == 0
            and unallocated_candidates == 0
            and pagination.get("complete") is not False,
            signals={
                "requested_stage_count": len(requested),
                "complete_stage_count": len(complete_stages),
                "omitted_direct_evidence_count": omitted,
                "unselected_candidate_meeting_count": unselected_meetings,
                "unallocated_candidate_evidence_count": unallocated_candidates,
                "bounded_core_complete": pagination.get("complete"),
            },
            gaps=([] if len(complete_stages) == len(requested) else ["stage_incomplete"])
            + (["direct_evidence_omitted"] if omitted else [])
            + (["candidate_meetings_unchecked"] if unselected_meetings else [])
            + (["candidate_evidence_unallocated"] if unallocated_candidates else []),
        ),
        "depth": _dimension(
            len(direct) >= 3 and context_count >= 1,
            signals={
                "direct_evidence_count": len(direct),
                "context_turn_count": context_count,
                "participant_count": len(participant_index),
            },
            gaps=[] if context_count else ["no_exchange_context"],
        ),
        "breadth": _dimension(
            len(stages_with_evidence_or_verified_absence) == len(requested)
            and len(participant_index) >= 2,
            signals={
                "requested_stage_count": len(requested),
                "represented_or_verified_absent_stage_count": len(
                    stages_with_evidence_or_verified_absence
                ),
                "participant_count": len(participant_index),
                "actor_role_count": len(
                    {role for item in participant_index for role in item.get("roles") or [] if role}
                ),
            },
            gaps=(
                []
                if len(stages_with_evidence_or_verified_absence) == len(requested)
                else ["requested_stage_not_represented"]
            ),
        ),
        "detail": _dimension(
            bool(direct) and len(detailed) == len(direct) and supplemental_omitted == 0,
            signals={
                "direct_evidence_count": len(direct),
                "dated_named_located_substantive_excerpt_count": len(detailed),
                "minimum_direct_excerpt_bytes": min(excerpt_bytes) if excerpt_bytes else 0,
                "supplemental_argument_excerpt_count": supplemental_returned,
                "supplemental_argument_excerpt_omitted_count": supplemental_omitted,
            },
            gaps=(
                []
                if len(detailed) == len(direct) and supplemental_omitted == 0
                else (
                    ["missing_locator_or_transport_shortened_excerpt"]
                    if len(detailed) != len(direct)
                    else []
                )
                + (["supplemental_argument_excerpt_omitted"] if supplemental_omitted else [])
            ),
        ),
    }
    return {
        "dimensions": dimensions,
        "all_dimensions_ready": all(value["status"] == "ready" for value in dimensions.values()),
        "acceptance_rule": (
            "각 축의 signals와 gaps를 비교표에 공개하고, partial을 ready로 표현하지 마세요."
        ),
    }


def _required_answer_sections() -> list[dict[str, Any]]:
    return [
        {"id": "executive_summary", "requirement": "법안의 정체와 최종 결론을 먼저 설명"},
        {
            "id": "measure_identity_and_effect",
            "requirement": "원안·대안·처리안, 의안번호와 확인된 법적 효과를 구분",
        },
        {"id": "timeline", "requirement": "날짜·단계·결정·표결을 시간순 정리"},
        {"id": "issue_map", "requirement": "쟁점별 찬성·반대·조건부 논거를 함께 배열"},
        {
            "id": "stage_by_stage_discussion",
            "requirement": "요청한 각 단계의 모든 반환 실명 발언 반영",
        },
        {"id": "argument_exchanges", "requirement": "근거가 있으면 주장-이유-반론-답변-결론 복원"},
        {"id": "government_and_expert_views", "requirement": "의원·정부·전문위원 의견을 분리"},
        {
            "id": "changes_and_outcome",
            "requirement": "최종 문안 변화와 처리 결과를 근거 범위에서 설명",
        },
        {"id": "vote", "requirement": "공식 기록에 있는 표결 수치와 토론을 구분"},
        {"id": "limitations", "requirement": "발견·확인·반환·생략 수와 미확인 범위 공개"},
        {"id": "sources", "requirement": "단계별 공식 회의록 URL과 locator 수록"},
    ]


def _dimension(ready: bool, *, signals: dict[str, Any], gaps: list[str]) -> dict[str, Any]:
    rendered_signals = [f"{name}={value}" for name, value in signals.items()]
    return {
        "status": "ready" if ready else "partial",
        "signals": rendered_signals,
        "metrics": signals,
        "gaps": gaps,
    }


def _ledger_totals(by_stage: Mapping[str, Any]) -> dict[str, int]:
    names = (
        "discovered_count",
        "checked_count",
        "returned_count",
        "omitted_count",
        "transport_omitted_count",
        "supplemental_selected_before_transport_count",
        "supplemental_returned_count",
        "supplemental_transport_omitted_count",
        "failed_count",
        "pending_count",
    )
    return {
        name: sum(_integer(_mapping(value).get(name), fallback=0) for value in by_stage.values())
        for name in names
    }


def _unallocated_candidate_count(
    payload: Mapping[str, Any], candidate_ids: Mapping[str, set[str]]
) -> int:
    inventory = _mapping(_mapping(payload.get("scope_inventory")).get("speech_candidates"))
    observed = _integer(
        inventory.get("observed_total"),
        fallback=_integer(inventory.get("total"), fallback=0),
    )
    allocated = len({speech_id for values in candidate_ids.values() for speech_id in values})
    return max(0, observed - allocated)


def _evidence_ids(evidence: Iterable[Mapping[str, Any]], *, use: str | None = None) -> set[str]:
    values: set[str] = set()
    for item in evidence:
        if use is not None and str(item.get("evidence_use") or "") != use:
            continue
        value = str(item.get("evidence_id") or item.get("speech_id") or "").strip()
        if value:
            values.add(value)
    return values


def _dedupe_evidence(evidence: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in evidence:
        evidence_id = str(item.get("evidence_id") or item.get("speech_id") or "").strip()
        if evidence_id and evidence_id in seen:
            continue
        if evidence_id:
            seen.add(evidence_id)
        values.append(item)
    return values


def _participant_index_from_stages(
    stages: Mapping[str, Any],
) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for stage_name, raw_stage in stages.items():
        stage = _mapping(raw_stage)
        for item in _sequence_of_mappings(stage.get("evidence")):
            speaker = str(item.get("speaker") or "").strip()
            if not speaker:
                continue
            record = records.setdefault(
                speaker,
                {
                    "speaker": speaker,
                    "roles": [],
                    "organizations": [],
                    "stages": [],
                    "evidence_ids": [],
                    "claim_eligible_evidence_ids": [],
                },
            )
            _append_distinct(record["roles"], item.get("role"))
            _append_distinct(record["organizations"], item.get("organization"))
            _append_distinct(record["stages"], stage_name)
            _append_distinct(record["evidence_ids"], item.get("evidence_id"))
            if item.get("evidence_use") == "direct_claim_evidence":
                _append_distinct(record["claim_eligible_evidence_ids"], item.get("evidence_id"))
    return sorted(records.values(), key=lambda item: item["speaker"])


def _stage_participants(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in evidence:
        speaker = str(item.get("speaker") or "").strip()
        if not speaker:
            continue
        value = result.setdefault(speaker, {"speaker": speaker, "roles": [], "evidence_ids": []})
        _append_distinct(value["roles"], item.get("role"))
        _append_distinct(value["evidence_ids"], item.get("evidence_id"))
    return list(result.values())


def _stage_instruction(state: str, has_evidence: bool) -> str:
    if has_evidence:
        return "반환된 직접 근거를 모두 반영하고 맥락 전용 발언을 독립 주장으로 쓰지 마세요."
    if state == "record_found_no_member_debate":
        return "확인한 해당 회의록에서 의원 토론을 찾지 못했다고만 표현하세요."
    if state in {"metadata_found_text_pending", "not_checked", "failed", "deadline_exceeded"}:
        return "미확인 또는 실패 상태를 토론 부재로 바꾸어 쓰지 마세요."
    return "state와 gap_reason을 그대로 밝혀야 합니다."


def _stage_name(meeting_type: Any, title: Any = None) -> str | None:
    value = f"{meeting_type or ''} {title or ''}".casefold()
    if any(term in value for term in ("subcommittee", "소위원회", "소위")):
        return "subcommittee"
    if any(term in value for term in ("plenary", "본회의")):
        return "plenary"
    if any(term in value for term in ("committee", "위원회", "전체회의")):
        return "standing_committee"
    return None


def _ordered_stages(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        stage = str(value).strip()
        if stage and stage not in result:
            result.append(stage)
    return result


def _append_distinct(values: list[Any], value: Any) -> None:
    if value is not None and value != "" and value not in values:
        values.append(value)


def _clip_utf8(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    ellipsis = "…"
    ellipsis_bytes = len(ellipsis.encode("utf-8"))
    if maximum < ellipsis_bytes:
        return encoded[:maximum].decode("utf-8", errors="ignore")
    return encoded[: maximum - ellipsis_bytes].decode("utf-8", errors="ignore").rstrip() + ellipsis


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence_of_mappings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _string_sequence(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value]


def _integer(value: Any, *, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return fallback


__all__ = ["build_answer_brief", "reconcile_answer_brief"]
