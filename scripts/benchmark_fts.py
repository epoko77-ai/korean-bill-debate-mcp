"""Measure SQLite FTS5 latency on a deterministic medium-size corpus."""

from __future__ import annotations

import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

from kasm.search.lexical import LexicalSearch
from kasm.storage.database import Database

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / "benchmarks/fts5.json"
SPEECH_COUNT = 50_000
QUERIES = ("인공지능", "재정 예산", "기후 에너지", "주거", "보건 의료")


def main() -> None:
    with Database(":memory:") as database:
        connection = database.connection
        connection.execute(
            """INSERT INTO meetings VALUES
            ('benchmark-meeting', 22, 'bench', '벤치마크위원회', NULL, '성능 측정',
             'committee', '1', '2026-01-01', 'https://example.invalid/benchmark',
             'synthetic', '2026-01-01T00:00:00+00:00')"""
        )
        vocabulary = ("인공지능", "재정 예산", "기후 에너지", "주거", "보건 의료")
        rows = [
            (
                f"speech-{number:05d}",
                number,
                f"{vocabulary[number % len(vocabulary)]} 정책에 관한 국회 심사 발언입니다 {number}",
            )
            for number in range(SPEECH_COUNT)
        ]
        connection.executemany(
            """INSERT INTO speeches
            (id, meeting_id, sequence, speaker_name, text, source_hash, parser_version)
            VALUES (?, 'benchmark-meeting', ?, '시험 발언자', ?, 'synthetic', 'benchmark')""",
            rows,
        )
        connection.commit()
        search = LexicalSearch(connection)
        for query in QUERIES:
            search.search(query, candidate_limit=50)
        timings = []
        for _ in range(10):
            for query in QUERIES:
                started = time.perf_counter()
                search.search(query, candidate_limit=50)
                timings.append((time.perf_counter() - started) * 1000)
    timings.sort()
    p95 = timings[int(len(timings) * 0.95) - 1]
    result = {
        "backend": "SQLite FTS5",
        "synthetic_speeches": SPEECH_COUNT,
        "samples": len(timings),
        "median_ms": statistics.median(timings),
        "p95_ms": p95,
        "required_p95_ms": 200.0,
        "passed": p95 < 200.0,
        "measured_at": datetime.now(UTC).isoformat(),
    }
    RESULT.parent.mkdir(exist_ok=True)
    RESULT.write_text(json.dumps(result, indent=2) + "\n", "utf-8")
    print(json.dumps(result))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
