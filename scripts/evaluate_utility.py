#!/usr/bin/env python3
"""Evaluate thirty real-world questions for relevance, depth, context, and evidence quality."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any

from kasm.app import LocalServices, create_deployed_services
from kasm.storage.database import Database


def evaluate_case(services: LocalServices, case: dict[str, Any]) -> dict[str, Any]:
    result = services.explore_issue(case["query"], limit=20)
    speeches = result["speeches"]
    threads = result["discussion_threads"]
    turns = [turn for thread in threads for turn in thread["turns"]]
    expected = case["committee"]
    committee_hits = sum(item.get("committee") == expected for item in speeches)
    committee_precision = committee_hits / len(speeches) if speeches else 0.0
    provenance = sum(
        bool(turn.get("official_source") and turn.get("source_locator")) for turn in turns
    )
    provenance_rate = provenance / len(turns) if turns else 0.0
    bill_ok = bool(result["bills"]) or not case["require_bill"]
    suspect = result["quality"]["suspect_speakers"]

    relevance = min(30.0, committee_precision * 30)
    evidence = min(20.0, len(speeches) / 5 * 15 + len(threads) / 2 * 5)
    context = min(20.0, len(threads) / 2 * 10 + len(turns) / 10 * 10)
    verifiability = provenance_rate * 15
    linkage = 10.0 if bill_ok else 0.0
    cleanliness = max(0.0, 5.0 - len(suspect) * 2.5)
    score = round(relevance + evidence + context + verifiability + linkage + cleanliness, 1)
    failure_reasons: list[str] = []
    if score < 75:
        failure_reasons.append("종합 점수 75점 미만")
    if len(speeches) < 2 and not (bill_ok and len(turns) >= 5):
        failure_reasons.append("관련 발언 2건 미만")
    if not threads:
        failure_reasons.append("토론 문맥 없음")
    if committee_precision < 0.6:
        failure_reasons.append("기대 위원회 정확도 60% 미만")
    if provenance_rate < 1.0:
        failure_reasons.append("출처 누락")
    if suspect:
        failure_reasons.append("OCR 발언자명 오류 포함")
    if case["require_bill"] and not bill_ok:
        failure_reasons.append("필수 법안 연결 누락")
    passed = not failure_reasons
    return {
        "id": case["id"],
        "query": case["query"],
        "expected_committee": expected,
        "passed": passed,
        "score": score,
        "speech_matches": len(speeches),
        "discussion_threads": len(threads),
        "context_turns": len(turns),
        "committee_precision": round(committee_precision, 3),
        "bill_coverage": bool(result["bills"]),
        "bill_required": case["require_bill"],
        "provenance_rate": round(provenance_rate, 3),
        "suspect_speakers": suspect,
        "failure_reasons": failure_reasons,
        "top_speakers": result["quality"]["top_speakers"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("suite", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--vector", type=Path)
    parser.add_argument("--minimum-strict-rate", type=float, default=0.0)
    parser.add_argument("--minimum-basic-rate", type=float, default=0.0)
    args = parser.parse_args()
    suite = json.loads(args.suite.read_text(encoding="utf-8"))
    if args.vector:
        services = create_deployed_services(str(args.database), str(args.vector)).search
        database = None
    else:
        database = Database(args.database)
        services = LocalServices(database)
    results = [evaluate_case(services, case) for case in suite]  # type: ignore[arg-type]
    if database:
        database.close()
    passed = sum(case["passed"] for case in results)
    basic_usable = sum(
        case["score"] >= 70
        and (case["speech_matches"] >= 2 or case["context_turns"] >= 5)
        and case["discussion_threads"] > 0
        and case["provenance_rate"] == 1.0
        and (case["bill_coverage"] or not case["bill_required"])
        for case in results
    )
    failure_counts = Counter(reason for case in results for reason in case["failure_reasons"])
    report = {
        "generated_at": "2026-07-12",
        "source": "official Open Assembly prepared hybrid index"
        if args.vector
        else "official Open Assembly prepared lexical index",
        "summary": {
            "questions": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "pass_rate": round(passed / len(results), 3),
            "basic_usable": basic_usable,
            "basic_usable_rate": round(basic_usable / len(results), 3),
            "mean_score": round(mean(case["score"] for case in results), 1),
            "mean_speech_matches": round(mean(case["speech_matches"] for case in results), 1),
            "mean_context_turns": round(mean(case["context_turns"] for case in results), 1),
            "provenance_rate": round(mean(case["provenance_rate"] for case in results), 3),
            "failure_reasons": dict(failure_counts.most_common()),
        },
        "results": results,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    strict_rate = passed / len(results)
    basic_rate = basic_usable / len(results)
    return int(strict_rate < args.minimum_strict_rate or basic_rate < args.minimum_basic_rate)


if __name__ == "__main__":
    raise SystemExit(main())
