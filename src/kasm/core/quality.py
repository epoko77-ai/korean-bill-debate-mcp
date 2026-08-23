"""Deterministic quality signals for evidence-rich legislative research results."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

_COMPLETE_STAGE_STATES = frozenset(
    {
        "discussion_found",
        "record_found_no_member_debate",
        "checked_no_matching_discussion",
    }
)
_PENDING_STAGE_STATE = "metadata_found_text_pending"
_NOT_CHECKED_STAGE_STATE = "not_checked"
_FAILED_STAGE_STATE = "failed"


def issue_quality(payload: dict[str, Any]) -> dict[str, Any]:
    speeches = payload.get("speeches", [])
    bills = payload.get("bills", [])
    threads = payload.get("discussion_threads", [])
    turns = [turn for thread in threads for turn in thread.get("turns", [])]
    provenance_fields = ("official_source", "source_locator")
    cited = sum(all(turn.get(field) for field in provenance_fields) for turn in turns)
    provenance_rate = cited / len(turns) if turns else 0.0
    speakers = Counter(item.get("speaker") for item in speeches if item.get("speaker"))
    role_words = ("위원", "의원", "장관", "위원장", "처장", "국장")
    suspect_speakers = sorted(
        speaker
        for speaker in speakers
        if len(speaker) > 5
        or any(role in speaker for role in role_words)
        or re.fullmatch(r"[가-힣·]{2,5}", speaker) is None
    )
    warnings: list[str] = []
    if not bills:
        warnings.append("관련 법안·의안이 준비된 인덱스에서 확인되지 않았습니다.")
    if len(speeches) < 3:
        warnings.append("관련 발언 근거가 3건 미만입니다.")
    if not threads:
        warnings.append("앞뒤 발언을 복원한 토론 스레드가 없습니다.")
    if provenance_rate < 1.0:
        warnings.append("일부 토론 발언에 공식 출처 또는 원문 위치가 없습니다.")
    if suspect_speakers:
        warnings.append("OCR로 인해 발언자명이 의심되는 결과가 있습니다.")
    coverage = _coverage_completeness(payload)
    missing_stages = coverage["missing_stages"]
    pending_stages = coverage["pending_stages"]
    not_checked_stages = coverage["not_checked_stages"]
    failed_stages = coverage["failed_stages"]
    unknown_stages = coverage["unknown_stages"]
    if missing_stages:
        warnings.append(
            "요청한 논의 단계가 결과에서 누락되었습니다: "
            + ", ".join(missing_stages)
            + "."
        )
    if pending_stages:
        warnings.append(
            "요청한 논의 단계의 회의록 본문 확인이 아직 끝나지 않았습니다: "
            + ", ".join(pending_stages)
            + "."
        )
    if not_checked_stages:
        warnings.append(
            "요청한 논의 단계가 아직 확인되지 않았습니다: "
            + ", ".join(not_checked_stages)
            + "."
        )
    if failed_stages:
        warnings.append(
            "요청한 논의 단계의 원문 확인에 실패했습니다: "
            + ", ".join(failed_stages)
            + "."
        )
    if unknown_stages:
        warnings.append(
            "요청한 논의 단계의 상태를 판정할 수 없습니다: "
            + ", ".join(unknown_stages)
            + "."
        )
    if coverage["research_pagination_complete"] is False:
        if coverage["research_has_more"]:
            warnings.append(
                "회의록 페이지가 더 남아 있어 요청 범위 조사가 완료되지 않았습니다."
            )
        else:
            warnings.append("요청 범위의 회의록 조사가 완료되지 않았습니다.")
    score = 100
    score -= 25 if not bills else 0
    score -= 20 if len(speeches) < 3 else 0
    score -= 25 if not threads else 0
    score -= round((1.0 - provenance_rate) * 30)
    score -= min(15, len(suspect_speakers) * 3)
    score -= min(30, len(coverage["incomplete_stages"]) * 10)
    score -= 10 if coverage["research_pagination_complete"] is False else 0
    return {
        "score": max(0, score),
        "evidence_sufficient": (
            len(speeches) >= 3
            and bool(threads)
            and provenance_rate == 1.0
            and coverage["complete"]
        ),
        "bill_coverage": bool(bills),
        "speech_matches": len(speeches),
        "discussion_threads": len(threads),
        "context_turns": len(turns),
        "distinct_speakers": len(speakers),
        "top_speakers": dict(speakers.most_common(10)),
        "suspect_speakers": suspect_speakers,
        "provenance_rate": provenance_rate,
        "coverage_completeness": coverage,
        "warnings": warnings,
    }


def _coverage_completeness(payload: dict[str, Any]) -> dict[str, Any]:
    raw_coverage = payload.get("stage_coverage")
    stage_coverage = raw_coverage if isinstance(raw_coverage, dict) else None
    raw_requested = (
        stage_coverage.get("requested_stages") if stage_coverage is not None else None
    )
    requested_stages = (
        list(
            dict.fromkeys(
                str(stage).strip()
                for stage in raw_requested
                if str(stage).strip()
            )
        )
        if isinstance(raw_requested, (list, tuple, set))
        else []
    )
    raw_stages = stage_coverage.get("stages") if stage_coverage is not None else None
    stages = raw_stages if isinstance(raw_stages, dict) else {}

    stage_states: dict[str, str] = {}
    complete_stages: list[str] = []
    missing_stages: list[str] = []
    pending_stages: list[str] = []
    not_checked_stages: list[str] = []
    failed_stages: list[str] = []
    unknown_stages: list[str] = []
    for stage in requested_stages:
        raw_stage = stages.get(stage)
        if not isinstance(raw_stage, dict):
            stage_states[stage] = "missing"
            missing_stages.append(stage)
            continue
        state = str(raw_stage.get("state") or "").strip()
        stage_states[stage] = state or "missing"
        if state in _COMPLETE_STAGE_STATES:
            complete_stages.append(stage)
        elif state == _PENDING_STAGE_STATE:
            pending_stages.append(stage)
        elif state == _NOT_CHECKED_STAGE_STATE:
            not_checked_stages.append(stage)
        elif state == _FAILED_STAGE_STATE:
            failed_stages.append(stage)
        elif not state:
            missing_stages.append(stage)
        else:
            unknown_stages.append(stage)

    incomplete_stages = [
        stage for stage in requested_stages if stage not in complete_stages
    ]
    raw_pagination = payload.get("research_pagination")
    pagination = raw_pagination if isinstance(raw_pagination, dict) else None
    pagination_provided = pagination is not None
    pagination_has_more = False
    pagination_complete: bool | None = None
    if pagination is not None:
        pagination_has_more = bool(
            pagination.get("has_more")
            or pagination.get("hasMore")
            or pagination.get("next_minutes_offset") is not None
        )
        completion_marker = pagination.get(
            "complete", pagination.get("overall_complete")
        )
        pagination_complete = completion_marker is True and not pagination_has_more

    stages_complete = not incomplete_stages
    complete = stages_complete and pagination_complete is not False
    return {
        "complete": complete,
        "stage_coverage_provided": stage_coverage is not None,
        "stages_complete": stages_complete,
        "requested_stages": requested_stages,
        "complete_stages": complete_stages,
        "incomplete_stages": incomplete_stages,
        "missing_stages": missing_stages,
        "pending_stages": pending_stages,
        "not_checked_stages": not_checked_stages,
        "failed_stages": failed_stages,
        "unknown_stages": unknown_stages,
        "stage_states": stage_states,
        "research_pagination_provided": pagination_provided,
        "research_pagination_complete": pagination_complete,
        "research_has_more": pagination_has_more,
    }
