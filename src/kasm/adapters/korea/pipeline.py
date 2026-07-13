"""End-to-end synchronization pipeline for verified Open Assembly records."""

from __future__ import annotations

import re
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
    """Merge portal agenda rows that refer to the same official minutes PDF.

    Open Assembly meeting datasets can return one row per agenda item even
    though every row points at the same minutes PDF.  The original
    implementation retained only the first row, which silently discarded the
    remaining agenda titles and bill numbers.  Keep the first row's existing
    fields for backwards compatibility and add a complete, stable agenda
    summary assembled from every matching row.
    """
    grouped: dict[str, dict[str, Any]] = {}
    agenda_by_url: dict[str, list[dict[str, str | None]]] = {}
    for row in rows:
        url = OpenAssemblyPipeline.minutes_url(row)
        if url not in grouped:
            grouped[url] = dict(row)
            agenda_by_url[url] = []
        agenda_by_url[url].extend(_agenda_items(row))

    for url, row in grouped.items():
        items = _deduplicate_agenda_items(agenda_by_url[url])
        row["agenda_items"] = items
        row["agenda_text"] = "\n".join(_agenda_item_text(item) for item in items)
    return list(grouped.values())


_AGENDA_TITLE_FIELDS = (
    "SUB_NAME",
    "AGENDA_NAME",
    "AGENDA_NM",
    "MTR_NM",
    "ITEM_NAME",
    "ITEM_NM",
    "BILL_NAME",
    "BILL_NM",
    "agenda_title",
    "agenda",
)
_AGENDA_BILL_FIELDS = (
    "BILL_NO",
    "BILL_NUM",
    "BILL_NUMBER",
    "bill_no",
)
_BILL_NUMBER = re.compile(r"(?<!\d)\d{7}(?!\d)")


def _agenda_items(row: Mapping[str, Any]) -> list[dict[str, str | None]]:
    existing = row.get("agenda_items")
    items: list[dict[str, str | None]] = []
    if isinstance(existing, (list, tuple)):
        for item in existing:
            if not isinstance(item, Mapping):
                continue
            title = _first(item, "title", "name")
            bill_no = _first(item, "bill_no", "BILL_NO")
            if title or bill_no:
                items.append({"bill_no": bill_no, "title": title})

    title = _first(row, *_AGENDA_TITLE_FIELDS)
    bill_no = _first(row, *_AGENDA_BILL_FIELDS)
    if bill_no is None and title:
        match = _BILL_NUMBER.search(title)
        bill_no = match.group() if match else None
    if title or bill_no:
        items.append({"bill_no": bill_no, "title": title})
    return items


def _deduplicate_agenda_items(
    items: list[dict[str, str | None]],
) -> list[dict[str, str | None]]:
    distinct: list[dict[str, str | None]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        bill_no = item["bill_no"]
        title = item["title"]
        normalized_title = " ".join((title or "").split()).casefold()
        key = (bill_no or "", normalized_title)
        if key in seen:
            continue
        seen.add(key)
        distinct.append({"bill_no": bill_no, "title": title})
    return distinct


def _agenda_item_text(item: Mapping[str, str | None]) -> str:
    bill_no = item.get("bill_no")
    title = item.get("title")
    if bill_no and title:
        return f"{bill_no} {title}"
    return bill_no or title or ""
