"""Deterministic byte budgeting for bounded MCP issue responses.

The transport limit in this module is a hard limit, not a best-effort target.
Known evidence sections retain small identifier/provenance projections, while
arbitrary JSON-like additions are bounded recursively and may be omitted as a
last resort. Every sequence reduction is recorded with observed/returned
counts so a bounded prefix cannot be mistaken for a complete result.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

MAX_BOUNDED_RESPONSE_BYTES = 128 * 1024

_METADATA_RESERVE_BYTES = 12 * 1024
_GENERIC_STRING_BYTES = 2 * 1024
_GENERIC_LIST_ITEMS = 64
_GENERIC_MAPPING_ITEMS = 64
_GENERIC_DEPTH = 8
_AUDIT_SAMPLE_LIMIT = 64

_TOP_LEVEL_PRIORITY = (
    "query",
    "evidence_query",
    "next_action",
    "target_resolution",
    "stage_coverage",
    "research_pagination",
    "quality",
    "provenance",
    "source_provenance",
    "evidence",
    "live_refresh",
    "live_checked_at",
    "data_mode",
    "bills",
    "speeches",
    "discussion_threads",
    "links",
    "scope_inventory",
    "timeline",
)
_IDENTIFIER_FIELDS = (
    "id",
    "bill_id",
    "bill_no",
    "bill_number",
    "external_id",
    "speech_id",
    "meeting_id",
    "document_id",
    "official_url",
    "source_url",
    "url",
    "citation",
    "official_source",
    "source_locator",
    "speaker",
    "name",
    "title",
    "date",
    "committee",
    "meeting_type",
    "stage",
    "state",
    "status",
    "role",
    "relation",
    "confidence",
    "gap_reason",
    "reason",
    "complete",
    "has_more",
    "next_minutes_offset",
    "total",
    "observed_total",
    "returned_count",
    "candidate_count",
    "checked_count",
    "matched_discussion_count",
    "failed_count",
    "pending_count",
    "text_length",
    "text_sha256",
    "text_inline_complete",
)
_CRITICAL_FIELD_FRAGMENTS = (
    "id",
    "url",
    "source",
    "provenance",
    "citation",
    "gap",
    "reason",
    "state",
    "status",
    "complete",
    "confidence",
    "count",
    "total",
    "route",
    "tool",
    "argument",
    "stage",
    "bill_no",
    "meeting_type",
)


@dataclass
class _BudgetAudit:
    truncated_sections: set[str] = field(default_factory=set)
    section_counts: dict[str, dict[str, Any]] = field(default_factory=dict)
    compacted_values: dict[str, dict[str, Any]] = field(default_factory=dict)
    omitted_top_level: set[str] = field(default_factory=set)
    truncated_section_overflow: int = 0
    section_count_overflow: int = 0
    compacted_value_overflow: int = 0

    @classmethod
    def from_previous(cls, previous: Mapping[str, Any]) -> _BudgetAudit:
        audit = cls()
        raw_sections = previous.get("truncated_sections")
        if isinstance(raw_sections, list):
            for value in raw_sections[:_AUDIT_SAMPLE_LIMIT]:
                audit.truncated_sections.add(str(value))
            audit.truncated_section_overflow += max(
                0, len(raw_sections) - _AUDIT_SAMPLE_LIMIT
            )
        raw_counts = previous.get("section_counts")
        if isinstance(raw_counts, Mapping):
            for raw_path in sorted(raw_counts, key=str)[:_AUDIT_SAMPLE_LIMIT]:
                value = raw_counts[raw_path]
                if isinstance(value, Mapping):
                    audit.section_counts[str(raw_path)] = dict(value)
            audit.section_count_overflow += max(
                0, len(raw_counts) - _AUDIT_SAMPLE_LIMIT
            )
        raw_values = previous.get("compacted_values")
        if isinstance(raw_values, Mapping):
            for raw_path in sorted(raw_values, key=str)[:_AUDIT_SAMPLE_LIMIT]:
                value = raw_values[raw_path]
                if isinstance(value, Mapping):
                    audit.compacted_values[str(raw_path)] = dict(value)
            audit.compacted_value_overflow += max(
                0, len(raw_values) - _AUDIT_SAMPLE_LIMIT
            )
        raw_omitted = previous.get("omitted_top_level")
        if isinstance(raw_omitted, list):
            audit.omitted_top_level.update(str(value) for value in raw_omitted)
        return audit

    def mark_section(self, path: str) -> None:
        if path in self.truncated_sections:
            return
        if len(self.truncated_sections) < _AUDIT_SAMPLE_LIMIT:
            self.truncated_sections.add(path)
        else:
            self.truncated_section_overflow += 1

    def mark_count(
        self,
        path: str,
        *,
        observed: int,
        returned: int,
        kind: str = "sequence",
    ) -> None:
        previous = self.section_counts.get(path)
        previous_observed = _non_negative_int(
            previous.get("observed_count") if previous is not None else None
        )
        truthful_observed = max(observed, returned, previous_observed)
        if path not in self.section_counts and len(self.section_counts) >= _AUDIT_SAMPLE_LIMIT:
            self.section_count_overflow += 1
        else:
            self.section_counts[path] = {
                "kind": kind,
                "observed_count": truthful_observed,
                "returned_count": returned,
                "truncated": returned < truthful_observed,
            }
        if returned < truthful_observed:
            self.mark_section(path)

    def observed_count(self, path: str, fallback: int) -> int:
        previous = self.section_counts.get(path)
        if previous is None:
            return fallback
        return max(fallback, _non_negative_int(previous.get("observed_count")))

    def mark_text(self, path: str, original: str, returned: str) -> None:
        if path not in self.compacted_values:
            if len(self.compacted_values) >= _AUDIT_SAMPLE_LIMIT:
                self.compacted_value_overflow += 1
            else:
                self.compacted_values[path] = {
                    "kind": "string",
                    "original_characters": len(original),
                    "original_bytes": len(original.encode("utf-8")),
                    "returned_bytes": len(returned.encode("utf-8")),
                    "sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
                    "inline_complete": False,
                }
        self.mark_section(path)


def enforce_bounded_response_budget(
    payload: dict[str, Any],
    *,
    max_bytes: int = MAX_BOUNDED_RESPONSE_BYTES,
) -> dict[str, Any]:
    """Return a deterministic JSON payload whose encoded size is at most ``max_bytes``.

    A ``max_bytes`` value below two cannot contain even an empty JSON object and
    is rejected. At usable transport sizes the function preserves routing,
    stage-gap and provenance metadata before optional descriptive content.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 2:
        raise ValueError("max_bytes must be an integer of at least 2")

    previous_value = payload.pop("response_budget", None)
    previous = previous_value if isinstance(previous_value, Mapping) else {}
    audit = _BudgetAudit.from_previous(previous)
    original_value = previous.get("original_bytes")
    original_bytes = (
        original_value
        if isinstance(original_value, int) and not isinstance(original_value, bool)
        else _size(payload)
    )
    quality_present = isinstance(payload.get("quality"), Mapping)
    quality_inputs_before = _quality_input_signature(payload)
    stage_inputs_before = _stage_input_signature(payload)

    _compact_evidence_text(payload, audit)
    _compact_stage_coverage(payload, audit)
    _bound_root(payload, audit)

    reserve = min(_METADATA_RESERVE_BYTES, max(256, max_bytes // 4))
    content_target = max(2, max_bytes - reserve)
    _reduce_known_sections(payload, audit, content_target)
    _drop_optional_top_level(payload, audit, content_target)
    if _size(payload) > content_target:
        _replace_with_emergency_projection(payload, audit)

    quality_inputs_after = _quality_input_signature(payload)
    stage_inputs_after = _stage_input_signature(payload)
    budget = _budget_payload(
        max_bytes=max_bytes,
        original_bytes=original_bytes,
        audit=audit,
        quality_present=quality_present,
        quality_inputs_changed=quality_inputs_before != quality_inputs_after,
        stage_inputs_preserved=stage_inputs_before == stage_inputs_after,
    )
    payload["response_budget"] = budget
    _settle_final_bytes(payload, budget)

    if _size(payload) > max_bytes:
        _fit_final_envelope(
            payload,
            budget,
            audit,
            max_bytes=max_bytes,
            quality_present=quality_present,
            quality_inputs_before=quality_inputs_before,
            stage_inputs_before=stage_inputs_before,
        )
    if _size(payload) > max_bytes:
        _absolute_fallback(payload, max_bytes=max_bytes, original_bytes=original_bytes)
    return payload


def _compact_evidence_text(payload: dict[str, Any], audit: _BudgetAudit) -> None:
    speeches = payload.get("speeches")
    if isinstance(speeches, list):
        for index, speech in enumerate(speeches):
            if not isinstance(speech, dict):
                continue
            if _compact_text(speech, "text", 1600):
                audit.mark_section("speeches[].text")
            for name in ("context_before", "context_after"):
                if _compact_text(speech, name, 600):
                    audit.mark_section(f"speeches[].{name}")
            _bound_record_strings(speech, f"speeches[{index}]", audit)
    threads = payload.get("discussion_threads")
    if isinstance(threads, list):
        for thread_index, thread in enumerate(threads):
            if not isinstance(thread, dict):
                continue
            turns = thread.get("turns")
            if isinstance(turns, list) and len(turns) > 5:
                observed = len(turns)
                thread["turns"] = turns[:5]
                thread["turns_observed"] = observed
                thread["turns_returned"] = 5
                thread["turns_truncated"] = True
                audit.mark_count(
                    "discussion_threads[].turns", observed=observed, returned=5
                )
                turns = thread["turns"]
            if isinstance(turns, list):
                for turn_index, turn in enumerate(turns):
                    if not isinstance(turn, dict):
                        continue
                    if _compact_text(turn, "text", 1200):
                        audit.mark_section("discussion_threads[].turns[].text")
                    _bound_record_strings(
                        turn,
                        f"discussion_threads[{thread_index}].turns[{turn_index}]",
                        audit,
                    )


def _compact_stage_coverage(payload: dict[str, Any], audit: _BudgetAudit) -> None:
    raw = payload.get("stage_coverage")
    if not isinstance(raw, Mapping):
        return
    raw_requested = raw.get("requested_stages")
    requested = (
        [str(value) for value in raw_requested]
        if isinstance(raw_requested, (list, tuple, set))
        else []
    )
    observed_requested = len(requested)
    requested = requested[:32]
    if len(requested) < observed_requested:
        audit.mark_count(
            "stage_coverage.requested_stages",
            observed=observed_requested,
            returned=len(requested),
        )
    raw_stages = raw.get("stages")
    stages = raw_stages if isinstance(raw_stages, Mapping) else {}
    stage_names = list(dict.fromkeys(requested))
    if not stage_names:
        stage_names = [str(value) for value in sorted(stages, key=str)[:16]]
    compact_stages: dict[str, Any] = {}
    for stage_name in stage_names:
        stage_value = stages.get(stage_name)
        if not isinstance(stage_value, Mapping):
            compact_stages[stage_name] = stage_value
            continue
        compact_stage: dict[str, Any] = {}
        for name in (
            "state",
            "candidate_count",
            "checked_count",
            "matched_discussion_count",
            "failed_count",
            "pending_count",
            "gap_reason",
        ):
            if name in stage_value:
                compact_stage[name] = stage_value[name]
        meetings = stage_value.get("meetings")
        if isinstance(meetings, list):
            returned = meetings[:3]
            compact_stage["meetings"] = [
                _identifier_projection(value, depth=0) for value in returned
            ]
            if len(returned) < len(meetings):
                audit.mark_count(
                    f"stage_coverage.stages.{stage_name}.meetings",
                    observed=len(meetings),
                    returned=len(returned),
                )
        compact_stages[stage_name] = compact_stage
    compact: dict[str, Any] = {
        "requested_stages": requested,
        "stages": compact_stages,
    }
    for name in ("complete", "exact_measure_check", "instruction"):
        if name in raw:
            compact[name] = raw[name]
    if len(stages) > len(compact_stages):
        audit.mark_count(
            "stage_coverage.stages",
            observed=len(stages),
            returned=len(compact_stages),
            kind="mapping",
        )
    payload["stage_coverage"] = compact


def _bound_root(payload: dict[str, Any], audit: _BudgetAudit) -> None:
    keys = list(payload)
    if len(keys) > 96:
        selected = _prioritized_keys(payload, _TOP_LEVEL_PRIORITY, 96)
        selected_set = set(selected)
        for key in keys:
            if key not in selected_set:
                audit.omitted_top_level.add(str(key))
        audit.mark_count("$", observed=len(keys), returned=len(selected), kind="mapping")
        bounded = {key: payload[key] for key in selected}
        payload.clear()
        payload.update(bounded)
    for key in list(payload):
        payload[key] = _bound_value(payload[key], str(key), audit, depth=0)


def _bound_value(value: Any, path: str, audit: _BudgetAudit, *, depth: int) -> Any:
    if isinstance(value, str):
        compact = _truncate_utf8(value, _GENERIC_STRING_BYTES)
        if compact != value:
            audit.mark_text(path, value, compact)
        return compact
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= _GENERIC_DEPTH:
        serialized = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
        compact = _truncate_utf8(serialized, _GENERIC_STRING_BYTES)
        audit.mark_text(path, serialized, compact)
        return compact
    if isinstance(value, list):
        observed = len(value)
        returned_values = value[:_GENERIC_LIST_ITEMS]
        if len(returned_values) < observed:
            audit.mark_count(path, observed=observed, returned=len(returned_values))
        return [
            _bound_value(item, f"{path}[{index}]", audit, depth=depth + 1)
            for index, item in enumerate(returned_values)
        ]
    if isinstance(value, tuple):
        return _bound_value(list(value), path, audit, depth=depth)
    if isinstance(value, Mapping):
        keys = _prioritized_keys(value, _IDENTIFIER_FIELDS, _GENERIC_MAPPING_ITEMS)
        if len(keys) < len(value):
            audit.mark_count(
                path, observed=len(value), returned=len(keys), kind="mapping"
            )
        return {
            str(key): _bound_value(
                value[key], f"{path}.{key}", audit, depth=depth + 1
            )
            for key in keys
        }
    rendered = str(value)
    compact = _truncate_utf8(rendered, _GENERIC_STRING_BYTES)
    if compact != rendered:
        audit.mark_text(path, rendered, compact)
    return compact


def _bound_record_strings(
    record: dict[str, Any], path: str, audit: _BudgetAudit
) -> None:
    for name, value in list(record.items()):
        if not isinstance(value, str):
            continue
        compact = _truncate_utf8(value, _GENERIC_STRING_BYTES)
        if compact != value:
            record[name] = compact
            audit.mark_text(f"{path}.{name}", value, compact)


def _reduce_known_sections(
    payload: dict[str, Any], audit: _BudgetAudit, target: int
) -> None:
    scope_value = payload.get("scope_inventory")
    scope = scope_value if isinstance(scope_value, dict) else {}
    inventory_paths = (
        "links",
        "speech_candidates",
        "meeting_candidates",
        "bill_candidates",
    )
    while _size(payload) > target:
        changed = False
        for name in inventory_paths:
            section = scope.get(name)
            if not isinstance(section, dict):
                continue
            items = section.get("items")
            if not isinstance(items, list) or not items:
                continue
            keep = len(items) // 2
            observed = _inventory_observed_total(section, len(items), audit, name)
            section["items"] = items[:keep]
            _mark_inventory_page(section, observed=observed)
            audit.mark_count(
                f"scope_inventory.{name}", observed=observed, returned=keep
            )
            changed = True
            if _size(payload) <= target:
                break
        if not changed:
            break

    for name, limit in (
        ("links", 50),
        ("discussion_threads", 8),
        ("speeches", 12),
        ("bills", 20),
    ):
        _trim_list(payload, name, limit, audit)

    while _size(payload) > target:
        changed = False
        for name, minimum in (
            ("discussion_threads", 1),
            ("speeches", 1),
            ("links", 0),
            ("bills", 1),
            ("timeline", 0),
        ):
            values = payload.get(name)
            if not isinstance(values, list) or len(values) <= minimum:
                continue
            observed = audit.observed_count(name, len(values))
            values.pop()
            audit.mark_count(name, observed=observed, returned=len(values))
            changed = True
            if _size(payload) <= target:
                break
        if not changed:
            break

    if _size(payload) <= target:
        return
    for name in ("discussion_threads", "speeches", "bills", "links", "timeline"):
        values = payload.get(name)
        if not isinstance(values, list):
            continue
        projected = [_identifier_projection(value, depth=0) for value in values]
        if projected != values:
            payload[name] = projected
            audit.mark_section(f"{name}[].fields")
        if _size(payload) <= target:
            return


def _drop_optional_top_level(
    payload: dict[str, Any], audit: _BudgetAudit, target: int
) -> None:
    if _size(payload) <= target:
        return
    protected = set(_TOP_LEVEL_PRIORITY)
    optional = [name for name in payload if name not in protected]
    optional.sort(key=lambda name: (-_value_size(payload[name]), str(name)))
    for name in optional:
        payload.pop(name, None)
        audit.omitted_top_level.add(str(name))
        audit.mark_section(str(name))
        if _size(payload) <= target:
            return

    for name in ("timeline", "cache_database", "graph"):
        if name not in payload:
            continue
        payload.pop(name, None)
        audit.omitted_top_level.add(name)
        audit.mark_section(name)
        if _size(payload) <= target:
            return


def _replace_with_emergency_projection(
    payload: dict[str, Any], audit: _BudgetAudit
) -> None:
    projection: dict[str, Any] = {}
    for name in _TOP_LEVEL_PRIORITY:
        if name not in payload:
            continue
        value = payload[name]
        if name in {"bills", "speeches", "discussion_threads", "links"}:
            if isinstance(value, list):
                observed = audit.observed_count(name, len(value))
                returned = value[:1]
                projection[name] = [
                    _identifier_projection(item, depth=0) for item in returned
                ]
                audit.mark_count(name, observed=observed, returned=len(returned))
            continue
        if name == "stage_coverage":
            # Already reduced by _compact_stage_coverage; its requested-stage
            # states are quality inputs and must survive emergency projection.
            projection[name] = value
        elif name == "scope_inventory":
            projection[name] = _inventory_projection(value, audit)
        elif name == "query" and isinstance(value, str):
            compact = _truncate_utf8(value, 512)
            if compact != value:
                audit.mark_text(name, value, compact)
            projection[name] = compact
        else:
            projection[name] = _identifier_projection(value, depth=0)
    for name in payload:
        if name not in projection:
            audit.omitted_top_level.add(str(name))
            audit.mark_section(str(name))
    payload.clear()
    payload.update(projection)


def _inventory_projection(value: Any, audit: _BudgetAudit) -> Any:
    if not isinstance(value, Mapping):
        return _identifier_projection(value, depth=0)
    result: dict[str, Any] = {}
    for raw_name in sorted(value, key=str):
        name = str(raw_name)
        section = value[raw_name]
        if not isinstance(section, Mapping):
            if _is_critical_field(name):
                result[name] = _identifier_projection(section, depth=0)
            continue
        page: dict[str, Any] = {}
        for field_name in (
            "complete",
            "official_source_complete",
            "total",
            "observed_total",
            "returned_count",
            "truncated",
            "selection",
            "gap_reason",
        ):
            if field_name in section:
                page[field_name] = section[field_name]
        items = section.get("items")
        if isinstance(items, list):
            path = f"scope_inventory.{name}"
            observed = _inventory_observed_total(section, len(items), audit, name)
            returned = items[:1]
            page["items"] = [
                _identifier_projection(item, depth=0) for item in returned
            ]
            _mark_inventory_page(page, observed=observed)
            audit.mark_count(path, observed=observed, returned=len(returned))
        result[name] = page
    return result


def _identifier_projection(value: Any, *, depth: int) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate_utf8(value, 768)
    if depth >= 4:
        return _truncate_utf8(str(value), 256)
    if isinstance(value, list):
        return [
            _identifier_projection(item, depth=depth + 1) for item in value[:3]
        ]
    if isinstance(value, tuple):
        return _identifier_projection(list(value), depth=depth)
    if isinstance(value, Mapping):
        selected = [name for name in _IDENTIFIER_FIELDS if name in value]
        if not selected:
            selected = [
                key
                for key in sorted(value, key=str)
                if _is_critical_field(str(key))
            ][:24]
        return {
            str(key): _identifier_projection(value[key], depth=depth + 1)
            for key in selected[:24]
        }
    return _truncate_utf8(str(value), 256)


def _fit_final_envelope(
    payload: dict[str, Any],
    budget: dict[str, Any],
    audit: _BudgetAudit,
    *,
    max_bytes: int,
    quality_present: bool,
    quality_inputs_before: str,
    stage_inputs_before: str,
) -> None:
    # Metadata samples are useful, but content identity/gap records take precedence.
    for name in ("compacted_values", "omitted_top_level", "long_text_tools"):
        if _size(payload) <= max_bytes:
            break
        if name == "compacted_values" and isinstance(budget.get(name), dict):
            values = budget[name]
            query_value = values.get("query")
            if isinstance(query_value, dict):
                budget[name] = {"query": query_value}
                if _size(payload) <= max_bytes:
                    break
        budget.pop(name, None)
    sections = budget.get("truncated_sections")
    if _size(payload) > max_bytes and isinstance(sections, list) and len(sections) > 12:
        budget["truncated_sections"] = sections[:12]
        budget["truncated_section_sampled"] = True
    counts = budget.get("section_counts")
    if _size(payload) > max_bytes and isinstance(counts, dict) and len(counts) > 12:
        selected = sorted(counts)[:12]
        budget["section_counts"] = {name: counts[name] for name in selected}
        budget["section_count_sampled"] = True
    _settle_final_bytes(payload, budget)

    removable = (
        "timeline",
        "graph",
        "live_refresh",
        "scope_inventory",
        "links",
        "discussion_threads",
        "speeches",
        "bills",
        "evidence",
        "source_provenance",
        "provenance",
        "quality",
        "evidence_query",
        "data_mode",
        "live_checked_at",
    )
    for name in removable:
        if _size(payload) <= max_bytes:
            break
        if name not in payload:
            continue
        value = payload.pop(name)
        if isinstance(value, list):
            audit.mark_count(
                name,
                observed=audit.observed_count(name, len(value)),
                returned=0,
            )
        audit.omitted_top_level.add(name)
        audit.mark_section(name)
        _refresh_contract(
            payload,
            budget,
            quality_present=quality_present,
            quality_inputs_before=quality_inputs_before,
            stage_inputs_before=stage_inputs_before,
        )
        _settle_final_bytes(payload, budget)

    if _size(payload) > max_bytes and isinstance(payload.get("query"), str):
        query = payload["query"]
        assert isinstance(query, str)
        compact = _truncate_utf8(query, 128)
        if compact != query:
            payload["query"] = compact
            audit.mark_text("query", query, compact)
    if _size(payload) > max_bytes:
        budget.pop("section_counts", None)
        budget.pop("truncated_sections", None)
        budget.pop("instruction", None)
    _refresh_contract(
        payload,
        budget,
        quality_present=quality_present,
        quality_inputs_before=quality_inputs_before,
        stage_inputs_before=stage_inputs_before,
    )
    _settle_final_bytes(payload, budget)


def _absolute_fallback(
    payload: dict[str, Any], *, max_bytes: int, original_bytes: int
) -> None:
    stage = payload.get("stage_coverage")
    pagination = payload.get("research_pagination")
    next_action = payload.get("next_action")
    minimal: dict[str, Any] = {}
    if stage is not None:
        minimal["stage_coverage"] = _identifier_projection(stage, depth=0)
    if pagination is not None:
        minimal["research_pagination"] = _identifier_projection(pagination, depth=0)
    if next_action is not None:
        minimal["next_action"] = _identifier_projection(next_action, depth=0)
    minimal_budget: dict[str, Any] = {
        "max_bytes": max_bytes,
        "original_bytes": original_bytes,
        "final_bytes": 0,
        "truncated": True,
        "payload_omitted": True,
        "quality_contract": {
            "quality_recompute_required": True,
            "stage_inputs_preserved": False,
        },
    }
    minimal["response_budget"] = minimal_budget
    payload.clear()
    payload.update(minimal)
    _settle_final_bytes(payload, minimal_budget)

    for name in ("next_action", "research_pagination", "stage_coverage"):
        if _size(payload) <= max_bytes:
            return
        payload.pop(name, None)
        _settle_final_bytes(payload, minimal_budget)
    if _size(payload) <= max_bytes:
        return

    minimal_budget.pop("quality_contract", None)
    minimal_budget.pop("original_bytes", None)
    minimal_budget.pop("payload_omitted", None)
    _settle_final_bytes(payload, minimal_budget)
    if _size(payload) <= max_bytes:
        return
    payload.clear()
    tiny_budget: dict[str, Any] = {"truncated": True, "final_bytes": 0}
    payload["response_budget"] = tiny_budget
    _settle_final_bytes(payload, tiny_budget)
    if _size(payload) > max_bytes:
        payload.clear()


def _budget_payload(
    *,
    max_bytes: int,
    original_bytes: int,
    audit: _BudgetAudit,
    quality_present: bool,
    quality_inputs_changed: bool,
    stage_inputs_preserved: bool,
) -> dict[str, Any]:
    truncated = bool(
        audit.truncated_sections
        or audit.truncated_section_overflow
        or audit.omitted_top_level
    )
    budget: dict[str, Any] = {
        "max_bytes": max_bytes,
        "original_bytes": original_bytes,
        "final_bytes": 0,
        "truncated": truncated,
        "truncated_sections": sorted(audit.truncated_sections),
        "truncated_section_count": (
            len(audit.truncated_sections) + audit.truncated_section_overflow
        ),
        "section_counts": {
            name: audit.section_counts[name] for name in sorted(audit.section_counts)
        },
        "quality_contract": {
            "quality_present": quality_present,
            "quality_inputs_changed": quality_inputs_changed,
            "quality_recompute_required": quality_present and quality_inputs_changed,
            "stage_inputs_preserved": stage_inputs_preserved,
            "instruction": (
                "quality_recompute_required가 true이면 현재 evidence 배열 기준으로 quality를 "
                "재계산해야 합니다."
            ),
        },
        "long_text_tools": ["get_speech", "get_speech_context", "get_bill_status"],
        "instruction": (
            "잘린 원문은 식별자별 전용 도구로 여세요. 후보 전체를 같은 응답에 다시 넣지 마세요."
        ),
    }
    if audit.compacted_values:
        budget["compacted_values"] = {
            name: audit.compacted_values[name]
            for name in sorted(audit.compacted_values)
        }
    if audit.omitted_top_level:
        budget["omitted_top_level"] = sorted(audit.omitted_top_level)
    if audit.section_count_overflow:
        budget["section_count_metadata_omitted"] = audit.section_count_overflow
    if audit.compacted_value_overflow:
        budget["compacted_value_metadata_omitted"] = audit.compacted_value_overflow
    return budget


def _refresh_contract(
    payload: dict[str, Any],
    budget: dict[str, Any],
    *,
    quality_present: bool,
    quality_inputs_before: str,
    stage_inputs_before: str,
) -> None:
    changed = quality_inputs_before != _quality_input_signature(payload)
    contract = budget.get("quality_contract")
    if not isinstance(contract, dict):
        contract = {}
        budget["quality_contract"] = contract
    contract.update(
        {
            "quality_present": quality_present,
            "quality_inputs_changed": changed,
            "quality_recompute_required": quality_present and changed,
            "stage_inputs_preserved": stage_inputs_before == _stage_input_signature(payload),
        }
    )


def _quality_input_signature(payload: Mapping[str, Any]) -> str:
    bills = payload.get("bills")
    speeches = payload.get("speeches")
    threads = payload.get("discussion_threads")
    speaker_counts: dict[str, int] = {}
    if isinstance(speeches, list):
        for speech in speeches:
            if not isinstance(speech, Mapping) or not speech.get("speaker"):
                continue
            speaker = str(speech["speaker"])
            speaker_counts[speaker] = speaker_counts.get(speaker, 0) + 1
    turn_count = 0
    cited_count = 0
    if isinstance(threads, list):
        for thread in threads:
            if not isinstance(thread, Mapping):
                continue
            turns = thread.get("turns")
            if not isinstance(turns, list):
                continue
            for turn in turns:
                turn_count += 1
                if isinstance(turn, Mapping) and all(
                    turn.get(field) for field in ("official_source", "source_locator")
                ):
                    cited_count += 1
    projection = {
        "bill_count": len(bills) if isinstance(bills, list) else 0,
        "speech_count": len(speeches) if isinstance(speeches, list) else 0,
        "thread_count": len(threads) if isinstance(threads, list) else 0,
        "turn_count": turn_count,
        "cited_count": cited_count,
        "speaker_counts": sorted(speaker_counts.items()),
        "stage": _quality_stage_projection(payload.get("stage_coverage")),
        "pagination": _quality_pagination_projection(
            payload.get("research_pagination")
        ),
    }
    return hashlib.sha256(_canonical_bytes(projection)).hexdigest()


def _stage_input_signature(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_bytes(_quality_stage_projection(payload.get("stage_coverage")))
    ).hexdigest()


def _quality_stage_projection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    raw_requested = value.get("requested_stages")
    requested = (
        [str(stage).strip() for stage in raw_requested if str(stage).strip()]
        if isinstance(raw_requested, (list, tuple, set))
        else []
    )
    requested = list(dict.fromkeys(requested))
    raw_stages = value.get("stages")
    stages = raw_stages if isinstance(raw_stages, Mapping) else {}
    states: list[tuple[str, str | None]] = []
    for stage in requested:
        raw_stage = stages.get(stage)
        state = (
            str(raw_stage.get("state") or "").strip()
            if isinstance(raw_stage, Mapping)
            else None
        )
        states.append((stage, state))
    return {"requested_stages": requested, "states": states}


def _quality_pagination_projection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        "has_more": bool(
            value.get("has_more")
            or value.get("hasMore")
            or value.get("next_minutes_offset") is not None
        ),
        "complete": value.get("complete", value.get("overall_complete")),
    }


def _compact_text(item: dict[str, Any], name: str, limit: int) -> bool:
    value = item.get(name)
    if not isinstance(value, str) or len(value) <= limit:
        return False
    item[f"{name}_length"] = len(value)
    item[f"{name}_sha256"] = hashlib.sha256(value.encode("utf-8")).hexdigest()
    item[f"{name}_inline_complete"] = False
    item[name] = value[:limit].rstrip() + "…"
    return True


def _trim_list(
    payload: dict[str, Any],
    name: str,
    limit: int,
    audit: _BudgetAudit,
) -> None:
    values = payload.get(name)
    if not isinstance(values, list) or len(values) <= limit:
        return
    observed = audit.observed_count(name, len(values))
    payload[name] = values[:limit]
    audit.mark_count(name, observed=observed, returned=limit)


def _mark_inventory_page(section: dict[str, Any], *, observed: int) -> None:
    items = section.get("items")
    returned = len(items) if isinstance(items, list) else 0
    section.update(
        {
            "total": observed,
            "observed_total": observed,
            "returned_count": returned,
            "complete": returned == observed,
            "truncated": returned < observed,
            "selection": "ranked_prefix",
        }
    )


def _inventory_observed_total(
    section: Mapping[str, Any],
    fallback: int,
    audit: _BudgetAudit,
    name: str,
) -> int:
    raw_total = section.get("observed_total", section.get("total", fallback))
    reported = _non_negative_int(raw_total)
    return max(fallback, reported, audit.observed_count(f"scope_inventory.{name}", 0))


def _prioritized_keys(
    value: Mapping[Any, Any], priority: tuple[str, ...], limit: int
) -> list[Any]:
    selected: list[Any] = []
    for name in priority:
        if name in value and name not in selected:
            selected.append(name)
    for key in sorted(value, key=str):
        if key not in selected:
            selected.append(key)
        if len(selected) >= limit:
            break
    return selected[:limit]


def _is_critical_field(name: str) -> bool:
    normalized = name.casefold()
    return any(fragment in normalized for fragment in _CRITICAL_FIELD_FRAGMENTS)


def _truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    if limit <= 3:
        return "" if limit == 0 else "." * limit
    return encoded[: limit - 3].decode("utf-8", errors="ignore").rstrip() + "…"


def _settle_final_bytes(payload: dict[str, Any], budget: dict[str, Any]) -> int:
    for _ in range(12):
        size = _size(payload)
        if budget.get("final_bytes") == size:
            return size
        budget["final_bytes"] = size
    return _size(payload)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _value_size(value: Any) -> int:
    return len(_canonical_bytes(value))


def _size(payload: Mapping[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


def _non_negative_int(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


__all__ = ["MAX_BOUNDED_RESPONSE_BYTES", "enforce_bounded_response_budget"]
