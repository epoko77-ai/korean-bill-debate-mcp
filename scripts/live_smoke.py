#!/usr/bin/env python3
"""Run diverse questions from an empty cache against the user's Open Assembly key."""

from __future__ import annotations

import argparse
import json
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kasm.live import create_live_services


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "suite", type=Path, nargs="?", default=Path("benchmarks/live-smoke-queries.json")
    )
    parser.add_argument("--limit-cases", type=int)
    parser.add_argument("--case-id", action="append", dest="case_ids")
    parser.add_argument("--minutes-per-query", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases: list[dict[str, Any]] = json.loads(args.suite.read_text("utf-8"))
    if args.case_ids:
        requested = set(args.case_ids)
        cases = [case for case in cases if case["id"] in requested]
        missing = requested.difference(case["id"] for case in cases)
        if missing:
            parser.error(f"unknown case ids: {', '.join(sorted(missing))}")
    if args.limit_cases:
        cases = cases[: args.limit_cases]
    reports = []
    with tempfile.TemporaryDirectory(prefix="kbd-live-smoke-") as directory:
        service = create_live_services(
            data_dir=directory, max_minutes_per_request=args.minutes_per_query
        ).catalog
        assert service is not None
        for case in cases:
            started = datetime.now(UTC)
            result = service.explore_issue(case["query"], limit=10)
            speeches = result["speeches"]
            documents = sum(len(bill.get("documents", [])) for bill in result["bills"])
            citations = [speech.get("citation", {}).get("official_url") for speech in speeches]
            expected_committee = case.get("expected_committee")
            committee_hits = sum(
                speech.get("committee") == expected_committee
                or str(speech.get("committee", "")).startswith(f"{expected_committee} ")
                for speech in speeches
            )
            committee_precision = (
                committee_hits / len(speeches) if speeches and expected_committee else 1.0
            )
            failures = []
            if len(result["bills"]) < int(case.get("minimum_bills", 0)):
                failures.append("minimum_bills")
            if len(speeches) < int(case.get("minimum_speeches", 0)):
                failures.append("minimum_speeches")
            if len(result["discussion_threads"]) < int(case.get("minimum_threads", 0)):
                failures.append("minimum_threads")
            if not all(citations):
                failures.append("official_citations")
            if committee_precision < 0.8:
                failures.append("committee_precision")
            if result.get("data_mode") != "live_open_assembly_with_local_cache":
                failures.append("live_mode")
            if result.get("live_refresh", {}).get("meeting_api_calls", 0) < 1:
                failures.append("live_api_calls")
            reports.append(
                {
                    "id": case["id"],
                    "query": case["query"],
                    "passed": not failures,
                    "bills": len(result["bills"]),
                    "documents": documents,
                    "speeches": len(speeches),
                    "threads": len(result["discussion_threads"]),
                    "committee_precision": round(committee_precision, 3),
                    "committees": dict(
                        sorted(
                            Counter(
                                speech.get("committee") or "(unknown)" for speech in speeches
                            ).items()
                        )
                    ),
                    "provenance_rate": result["quality"]["provenance_rate"],
                    "elapsed_seconds": round((datetime.now(UTC) - started).total_seconds(), 2),
                    "failures": failures,
                    "live_refresh": result["live_refresh"],
                }
            )
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "cache_mode": "fresh temporary cache, shared only within this run",
        "questions": len(reports),
        "passed": sum(item["passed"] for item in reports),
        "failed": sum(not item["passed"] for item in reports),
        "results": reports,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return int(not all(report["passed"] for report in reports))


if __name__ == "__main__":
    raise SystemExit(main())
