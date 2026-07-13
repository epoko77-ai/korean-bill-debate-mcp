"""Bounded page work and lossless reassembly for distributed API collection.

One queue delivery must never fetch an unbounded partition.  The first page
discovers the official total, subsequent pages can run independently, and this
module refuses to assemble a partition until every expected page is present and
the snapshot is internally coherent.
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from kasm.adapters.korea.client import ApiPage, ApiResult, AssemblyApiError

from .collector import MetadataPartition


@dataclass(frozen=True, slots=True)
class MetadataPageWork:
    """One bounded official API page fetch, addressed through a stored plan."""

    partition_id: str
    page: int
    expected_total: int | None = None

    def __post_init__(self) -> None:
        if not self.partition_id.strip():
            raise ValueError("partition_id is required")
        if self.page < 1:
            raise ValueError("metadata page must be positive")
        if self.page == 1 and self.expected_total is not None:
            raise ValueError("the discovery page must not assume an expected total")
        if self.page > 1 and self.expected_total is None:
            raise ValueError("follow-up pages require the first page's official total")
        if self.expected_total is not None and self.expected_total < 0:
            raise ValueError("expected_total must be non-negative")

    @property
    def work_id(self) -> str:
        return f"metadata:{self.partition_id}:page:{self.page}"


@dataclass(frozen=True, slots=True)
class MetadataPageExpansion:
    """Follow-up page work determined only after the first response."""

    partition_id: str
    official_total: int
    page_size: int
    pages: tuple[MetadataPageWork, ...]

    @property
    def total_pages(self) -> int:
        return max(1, math.ceil(self.official_total / self.page_size))


def validate_fetched_page(
    partition: MetadataPartition,
    work: MetadataPageWork,
    page: ApiPage,
) -> None:
    """Validate one independently fetched page before it becomes immutable."""

    if work.partition_id != partition.partition_id:
        raise ValueError("metadata page work belongs to another partition")
    if page.dataset != partition.dataset or page.page_size != partition.page_size:
        raise AssemblyApiError("metadata page does not match its stored partition")
    if page.page != work.page:
        raise AssemblyApiError("metadata page number does not match its work item")
    if page.total_count is None or page.total_count < 0:
        raise AssemblyApiError("metadata page has an invalid official total")
    if work.expected_total is not None and page.total_count != work.expected_total:
        raise AssemblyApiError(
            "metadata pagination total changed between distributed page fetches"
        )
    total_pages = max(1, math.ceil(page.total_count / page.page_size))
    if page.page > total_pages:
        raise AssemblyApiError("metadata page lies beyond the official total")
    expected_rows = min(
        page.page_size,
        max(0, page.total_count - ((page.page - 1) * page.page_size)),
    )
    if len(page.rows) != expected_rows:
        raise AssemblyApiError(
            f"metadata page {page.page} expected {expected_rows} rows "
            f"but received {len(page.rows)}"
        )
    _validate_source_url(page.source_url)
    _validate_sha256(page.source_hash, "metadata page source_hash")
    fingerprints = tuple(_row_fingerprint(row) for row in page.rows)
    if len(fingerprints) != len(set(fingerprints)):
        raise AssemblyApiError("metadata page contains duplicate rows")


def expand_first_page(
    partition: MetadataPartition,
    page: ApiPage,
) -> MetadataPageExpansion:
    """Validate page one and return every remaining bounded work item."""

    first = MetadataPageWork(partition.partition_id, 1)
    validate_fetched_page(partition, first, page)
    assert page.total_count is not None
    total_pages = max(1, math.ceil(page.total_count / page.page_size))
    return MetadataPageExpansion(
        partition_id=partition.partition_id,
        official_total=page.total_count,
        page_size=page.page_size,
        pages=tuple(
            MetadataPageWork(partition.partition_id, number, page.total_count)
            for number in range(2, total_pages + 1)
        ),
    )


def assemble_partition_pages(
    partition: MetadataPartition,
    pages: Iterable[ApiPage],
) -> ApiResult:
    """Reassemble a complete coherent partition or fail with explicit gaps."""

    ordered = tuple(sorted(pages, key=lambda item: item.page))
    if not ordered or ordered[0].page != 1:
        raise AssemblyApiError("metadata partition is missing discovery page 1")
    first = ordered[0]
    validate_fetched_page(
        partition,
        MetadataPageWork(partition.partition_id, 1),
        first,
    )
    assert first.total_count is not None
    expected_numbers = tuple(
        range(1, max(1, math.ceil(first.total_count / first.page_size)) + 1)
    )
    actual_numbers = tuple(page.page for page in ordered)
    if len(actual_numbers) != len(set(actual_numbers)):
        raise AssemblyApiError("metadata partition contains duplicate page artifacts")
    if actual_numbers != expected_numbers:
        missing = sorted(set(expected_numbers) - set(actual_numbers))
        extra = sorted(set(actual_numbers) - set(expected_numbers))
        raise AssemblyApiError(
            f"metadata partition pages are incomplete; missing={missing}, extra={extra}"
        )

    seen: set[str] = set()
    for page in ordered:
        work = (
            MetadataPageWork(partition.partition_id, 1)
            if page.page == 1
            else MetadataPageWork(partition.partition_id, page.page, first.total_count)
        )
        validate_fetched_page(partition, work, page)
        for row in page.rows:
            fingerprint = _row_fingerprint(row)
            if fingerprint in seen:
                raise AssemblyApiError(
                    "metadata partition contains duplicate rows across pages"
                )
            seen.add(fingerprint)

    rows = tuple(row for page in ordered for row in page.rows)
    if len(rows) != first.total_count:
        raise AssemblyApiError(
            f"metadata partition expected {first.total_count} rows but assembled {len(rows)}"
        )
    return ApiResult(
        dataset=partition.dataset,
        page_size=partition.page_size,
        total_count=first.total_count,
        rows=rows,
        pages=ordered,
    )


def page_artifact_payload(page: ApiPage) -> dict[str, Any]:
    """Return a canonical credential-free representation for durable storage."""

    _validate_source_url(page.source_url)
    return {
        "schema_version": 1,
        "dataset": page.dataset,
        "page": page.page,
        "page_size": page.page_size,
        "total_count": page.total_count,
        "rows": list(page.rows),
        "source_url": page.source_url,
        "source_hash": page.source_hash,
    }


def page_from_artifact(payload: Mapping[str, Any]) -> ApiPage:
    """Restore a page artifact while rejecting malformed mutable data."""

    if payload.get("schema_version") != 1:
        raise ValueError("unsupported metadata page artifact schema")
    rows = payload.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("metadata page artifact rows must be objects")
    total = payload.get("total_count")
    if total is not None and not isinstance(total, int):
        raise ValueError("metadata page artifact total_count must be an integer")
    return ApiPage(
        dataset=str(payload.get("dataset") or ""),
        page=int(payload.get("page") or 0),
        page_size=int(payload.get("page_size") or 0),
        total_count=total,
        rows=tuple(dict(row) for row in rows),
        source_url=str(payload.get("source_url") or ""),
        source_hash=str(payload.get("source_hash") or ""),
    )


def _validate_source_url(value: str) -> None:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "open.assembly.go.kr":
        raise ValueError("metadata provenance must use the official Assembly HTTPS host")
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    keys = [item for name, values in query.items() if name.casefold() == "key" for item in values]
    if keys and keys != ["***"]:
        raise ValueError("metadata provenance URL contains an unredacted API key")


def _validate_sha256(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a SHA-256 hex digest")


def _row_fingerprint(row: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


__all__ = [
    "MetadataPageExpansion",
    "MetadataPageWork",
    "assemble_partition_pages",
    "expand_first_page",
    "page_artifact_payload",
    "page_from_artifact",
    "validate_fetched_page",
]
