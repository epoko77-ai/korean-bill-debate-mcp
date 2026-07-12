"""End-to-end synchronization pipeline for verified Open Assembly records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .fetcher import MinutesFetcher
from .ingestion import IngestionResult, OpenAssemblyIngestor, _first
from .parser import parse_transcript


@dataclass(frozen=True, slots=True)
class SyncPreview:
    source_url: str
    source_hash: str
    parsed_speeches: int
    parse_failures: int
    ready_to_commit: bool


class OpenAssemblyPipeline:
    def __init__(self, connection: Any, fetcher: MinutesFetcher) -> None:
        self.fetcher = fetcher
        self.ingestor = OpenAssemblyIngestor(connection)

    @staticmethod
    def minutes_url(row: Mapping[str, Any]) -> str:
        url = _first(row, "PDF_LINK_URL", "DOWN_URL")
        if url is None:
            raise ValueError("Open Assembly row does not include an official minutes PDF URL")
        return url

    def preview(self, row: Mapping[str, Any], *, refresh: bool = False) -> SyncPreview:
        fetched = self.fetcher.fetch(self.minutes_url(row), refresh=refresh)
        parsed = parse_transcript(fetched.text, locator_prefix=fetched.source_url)
        return SyncPreview(
            source_url=fetched.source_url,
            source_hash=fetched.source_hash,
            parsed_speeches=len(parsed.speeches),
            parse_failures=len(parsed.failures),
            ready_to_commit=bool(parsed.speeches) and not parsed.failures,
        )

    def sync(self, row: Mapping[str, Any], *, refresh: bool = False) -> IngestionResult:
        fetched = self.fetcher.fetch(self.minutes_url(row), refresh=refresh)
        return self.ingestor.ingest(
            row,
            fetched.text,
            source_hash=fetched.source_hash,
            source_url=fetched.source_url,
        )


def distinct_minutes_rows(
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    """Keep the first portal row for each official PDF URL, preserving API order."""
    seen: set[str] = set()
    distinct = []
    for row in rows:
        url = OpenAssemblyPipeline.minutes_url(row)
        if url in seen:
            continue
        seen.add(url)
        distinct.append(row)
    return distinct
