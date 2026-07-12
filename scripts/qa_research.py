#!/usr/bin/env python3
"""Run evidence-depth QA against a prepared KASM database."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from kasm.app import LocalServices
from kasm.storage.database import Database

DEFAULT_QUERIES = (
    "검찰 보완수사권 폐지",
    "인공지능 정책에 대한 의원 의견",
    "범죄 피해자 보호와 수사기관 통제",
)


def evaluate(database: Path, queries: tuple[str, ...]) -> dict[str, Any]:
    connection = Database(database)
    services = LocalServices(connection)
    cases = []
    for query in queries:
        result = services.explore_issue(query, limit=20)
        quality = result["quality"]
        agenda_values = {item.get("agenda") for item in result["speeches"] if item.get("agenda")}
        passed = (
            quality["evidence_sufficient"]
            and quality["bill_coverage"]
            and not quality["suspect_speakers"]
        )
        cases.append(
            {
                "query": query,
                "passed": passed,
                "quality": quality,
                "agenda_labels": sorted(agenda_values),
                "single_agenda_labels": [
                    agenda for agenda in sorted(agenda_values) if "복수 의사일정" not in agenda
                ],
                "thread_preview": [
                    {
                        "meeting": thread["meeting"],
                        "date": thread["date"],
                        "participants": thread["participants"],
                        "turns": len(thread["turns"]),
                    }
                    for thread in result["discussion_threads"][:5]
                ],
            }
        )
    connection.close()
    return {"passed": all(case["passed"] for case in cases), "cases": cases}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("queries", nargs="*")
    args = parser.parse_args()
    report = evaluate(args.database, tuple(args.queries) or DEFAULT_QUERIES)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
