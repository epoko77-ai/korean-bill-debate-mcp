"""Collect distinct official minutes excerpts for manual parser review.

This operator-only utility writes a candidate JSON file. It never writes the API key or raw
API URL, and it deduplicates agenda-level rows by minutes PDF URL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from kasm.adapters.korea.client import AssemblyOpenApiClient
from kasm.adapters.korea.fetcher import MinutesFetcher
from kasm.adapters.korea.parser import parse_transcript
from kasm.adapters.korea.pipeline import OpenAssemblyPipeline
from kasm.adapters.korea.sources import DATASET_BY_SOURCE, MeetingSource

MONTHS = tuple(
    f"{year}-{month:02d}"
    for year, months in ((2024, range(6, 13)), (2025, range(1, 7)))
    for month in months
)
RANGE = re.compile(r":(?P<start>\d+)-(?P<end>\d+)$")


def compact_excerpt(source: str, locator: str, *, maximum: int = 180) -> str:
    match = RANGE.search(locator)
    if match is None:
        raise ValueError("parser locator does not end in an offset range")
    start, end = int(match["start"]), int(match["end"])
    excerpt = source[start : min(end, start + maximum)].strip()
    return excerpt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=Path(".kasm-cache/review-set"))
    parser.add_argument("--documents", type=int, default=20)
    args = parser.parse_args()
    client = AssemblyOpenApiClient(cache_dir=args.cache_dir / "api")
    fetcher = MinutesFetcher(args.cache_dir)
    seen: set[str] = set()
    documents = []
    for month in MONTHS:
        page = client.fetch_page(
            DATASET_BY_SOURCE[MeetingSource.COMMITTEE],
            page_size=100,
            parameters={"DAE_NUM": 22, "CONF_DATE": month},
        )
        for row in page.rows:
            url = OpenAssemblyPipeline.minutes_url(row)
            if url in seen:
                continue
            seen.add(url)
            fetched = fetcher.fetch(url)
            result = parse_transcript(fetched.text, locator_prefix=url)
            if not result.speeches:
                continue
            first = result.speeches[0]
            excerpt = compact_excerpt(fetched.text, first.source_locator or "")
            reviewed = parse_transcript(excerpt, locator_prefix=url)
            if not reviewed.speeches:
                continue
            boundary = reviewed.speeches[0]
            documents.append(
                {
                    "source_url": url,
                    "source_sha256": fetched.source_hash,
                    "excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
                    "excerpt": excerpt,
                    "expected": {
                        "speaker_name": boundary.speaker_name,
                        "speaker_role": boundary.speaker_role,
                    },
                    "document_parsed_speeches": len(result.speeches),
                    "document_parse_failures": len(result.failures),
                }
            )
            if len(documents) >= args.documents:
                break
        if len(documents) >= args.documents:
            break
    if len(documents) < args.documents:
        raise RuntimeError(f"found only {len(documents)} distinct parseable documents")
    payload = {
        "description": "Minimal reviewed quotations from distinct official Assembly minutes PDFs.",
        "attribution": "대한민국 국회 회의록 (record.assembly.go.kr)",
        "generated_at": datetime.now(UTC).isoformat(),
        "documents": documents,
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"wrote {len(documents)} distinct documents to {args.output}")


if __name__ == "__main__":
    main()
