"""Deterministic quality signals for evidence-rich legislative research results."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any


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
    score = 100
    score -= 25 if not bills else 0
    score -= 20 if len(speeches) < 3 else 0
    score -= 25 if not threads else 0
    score -= round((1.0 - provenance_rate) * 30)
    score -= min(15, len(suspect_speakers) * 3)
    return {
        "score": max(0, score),
        "evidence_sufficient": len(speeches) >= 3 and bool(threads) and provenance_rate == 1.0,
        "bill_coverage": bool(bills),
        "speech_matches": len(speeches),
        "discussion_threads": len(threads),
        "context_turns": len(turns),
        "distinct_speakers": len(speakers),
        "top_speakers": dict(speakers.most_common(10)),
        "suspect_speakers": suspect_speakers,
        "provenance_rate": provenance_rate,
        "warnings": warnings,
    }
