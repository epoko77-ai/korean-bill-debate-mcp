#!/usr/bin/env python3
"""Fail a refresh before release when its prepared evidence graph is incomplete."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--minimum-meetings", type=int, default=1)
    parser.add_argument("--minimum-speeches", type=int, default=1)
    parser.add_argument("--minimum-bills", type=int, default=1)
    args = parser.parse_args()
    connection = sqlite3.connect(args.database)
    counts = {
        table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("meetings", "speeches", "bills", "speech_relations")
    }
    minimums = {
        "meetings": args.minimum_meetings,
        "speeches": args.minimum_speeches,
        "bills": args.minimum_bills,
    }
    failures = [name for name, minimum in minimums.items() if counts[name] < minimum]
    broken_context = connection.execute(
        """SELECT count(*) FROM speeches s
           WHERE s.previous_speech_id IS NOT NULL AND NOT EXISTS
           (SELECT 1 FROM speeches p WHERE p.id = s.previous_speech_id)"""
    ).fetchone()[0]
    connection.close()
    report = {
        "passed": not failures and broken_context == 0,
        "counts": counts,
        "empty_or_small": failures,
        "broken_context_links": broken_context,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
