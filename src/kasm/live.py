"""Live-first Open Assembly research with a bounded local evidence cache."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kasm.adapters.korea.bills import (
    BILL_DATASET,
    BILL_STATUS_DATASET,
    ingest_bill_rows,
)
from kasm.adapters.korea.client import AssemblyOpenApiClient
from kasm.adapters.korea.fetcher import MinutesFetcher
from kasm.adapters.korea.ingestion import meeting_from_open_assembly_row
from kasm.adapters.korea.pipeline import OpenAssemblyPipeline, distinct_minutes_rows
from kasm.adapters.korea.sources import DATASET_BY_SOURCE, MeetingSource
from kasm.app import LocalServices, infer_bill_title_query, infer_issue_committee
from kasm.mcp.tools import ServiceContext
from kasm.search.lexical import query_terms
from kasm.storage.database import Database
from kasm.storage.repositories import MeetingRepository

_DATE_MONTH = re.compile(r"(?P<year>20\d{2})[.\-/년 ]+\s*(?P<month>1[0-2]|0?[1-9])")
_STOPWORDS = {
    "대한",
    "관련",
    "논의",
    "의견",
    "정부",
    "법안",
    "정책",
    "현재",
    "상태",
    "최근",
    "보여줘",
    "알려줘",
}


class LiveAssemblyServices:
    """Refresh official candidates for each request, then search the local evidence cache."""

    def __init__(
        self,
        database: Database,
        client: AssemblyOpenApiClient,
        fetcher: MinutesFetcher,
        *,
        assembly_term: int = 22,
        max_minutes_per_request: int = 8,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.client = client
        self.pipeline = OpenAssemblyPipeline(database, fetcher)
        self.local = LocalServices(database)
        self.assembly_term = assembly_term
        self.max_minutes_per_request = max_minutes_per_request
        self._now = now or (lambda: datetime.now(UTC))
        self.last_refresh: dict[str, Any] = {}

    def search_bills(self, query: str, **filters: Any) -> list[dict[str, Any]]:
        term = int(filters.get("assembly_term") or self.assembly_term)
        self._refresh_bills(query=query, assembly_term=term)
        results = self.local.search_bills(query, **filters)
        if not results:
            for candidate in _bill_queries(query)[1:]:
                results = self.local.search_bills(candidate, **filters)
                if results:
                    break
        return results

    def get_bill_status(self, bill_id_or_no: str) -> dict[str, Any] | None:
        bill_no = bill_id_or_no.removeprefix("kna:bill:")
        self._refresh_bill_status(bill_no)
        result = self.local.get_bill_status(bill_id_or_no)
        if result is None and bill_no != bill_id_or_no:
            result = self.local.get_bill_status(bill_no)
        return result

    def list_meetings(self, **filters: Any) -> list[dict[str, Any]]:
        months = self._months_for_query("", filters.get("date_from"), filters.get("date_to"))
        self._refresh_meetings(
            query="",
            committee=filters.get("committee"),
            months=months,
            ingest_minutes=False,
        )
        return self.local.list_meetings(**filters)

    def list_committees(
        self, assembly_term: int | None = None, query: str | None = None
    ) -> list[dict[str, Any]]:
        self._refresh_meetings(
            query=query or "",
            committee=query,
            months=self._months_for_query(query or ""),
            ingest_minutes=False,
        )
        return self.local.list_committees(assembly_term, query)

    def search(self, query: str, **filters: Any) -> list[dict[str, Any]]:
        self._hydrate_issue(query, filters)
        return self.local.search(query, **filters)

    def get(self, speech_id: str) -> dict[str, Any] | None:
        return self.local.get(speech_id)

    def context(self, speech_id: str, before: int = 2, after: int = 2) -> dict[str, Any]:
        return self.local.context(speech_id, before, after)

    def explore_issue(self, query: str, limit: int = 20) -> dict[str, Any]:
        self._hydrate_issue(query, {"limit": limit})
        result = self.local.explore_issue(query, limit)
        result["data_mode"] = "live_open_assembly_with_local_cache"
        result["live_checked_at"] = self._now().isoformat()
        result["cache_database"] = str(self.database.path)
        result["live_refresh"] = self.last_refresh
        return result

    def _hydrate_issue(self, query: str, filters: dict[str, Any]) -> None:
        term = int(filters.get("assembly_term") or self.assembly_term)
        committee = filters.get("committee") or infer_issue_committee(query)
        bills = self._refresh_bills(query=query, assembly_term=term)
        months = self._months_for_query(query, filters.get("date_from"), filters.get("date_to"))
        for bill in bills[:5]:
            committee = committee or _value(bill, "COMMITTEE", "COMMITTEE_NM")
            for field in ("PROPOSE_DT", "PROC_DT", "CMT_PROC_DT", "LAW_PROC_DT"):
                value = _value(bill, field)
                if value and len(value.replace("-", "")) >= 6:
                    compact = value.replace("-", "").replace(".", "")
                    months.add(f"{compact[:4]}-{compact[4:6]}")
        self._refresh_meetings(
            query=query,
            committee=committee,
            months=sorted(months),
            ingest_minutes=True,
        )

    def _refresh_bills(self, *, query: str, assembly_term: int) -> list[dict[str, Any]]:
        queries = _bill_queries(query)
        rows: list[dict[str, Any]] = []
        hashes: list[str] = []
        for candidate in queries[:4]:
            page = self.client.fetch_page(
                BILL_DATASET,
                page_size=100,
                parameters={"AGE": assembly_term, "BILL_NAME": candidate},
            )
            rows.extend(page.rows)
            hashes.append(page.source_hash)
        rows = _unique_rows(rows, "BILL_NO")
        if rows:
            source_hash = hashlib.sha256("".join(hashes).encode()).hexdigest()
            ingest_bill_rows(self.database, rows, source_hash=source_hash)
            for row in rows[:5]:
                bill_no = _value(row, "BILL_NO")
                if bill_no:
                    self._refresh_bill_status(bill_no)
        return rows

    def _refresh_bill_status(self, bill_no: str) -> None:
        page = self.client.fetch_page(
            BILL_STATUS_DATASET,
            page_size=100,
            parameters={"AGE": self.assembly_term, "BILL_NO": bill_no},
            refresh=True,
        )
        if page.rows:
            ingest_bill_rows(self.database, page.rows, source_hash=page.source_hash)

    def _refresh_meetings(
        self,
        *,
        query: str,
        committee: str | None,
        months: Iterable[str],
        ingest_minutes: bool,
    ) -> None:
        rows: list[dict[str, Any]] = []
        api_calls = 0
        for month in sorted(months)[-8:]:
            for source in (MeetingSource.COMMITTEE, MeetingSource.PLENARY):
                parameters: dict[str, str | int] = {
                    "DAE_NUM": self.assembly_term,
                    "CONF_DATE": month,
                }
                if committee and source is MeetingSource.COMMITTEE:
                    parameters["COMM_NAME"] = committee
                page = self.client.fetch_page(
                    DATASET_BY_SOURCE[source], page_size=100, parameters=parameters
                )
                api_calls += 1
                rows.extend(page.rows)
        subcommittee_parameters: dict[str, str | int] = {"ERACO": f"제{self.assembly_term}대"}
        if committee:
            subcommittee_parameters["CMIT_NM"] = committee
        subcommittee = self.client.fetch_page(
            DATASET_BY_SOURCE[MeetingSource.SUBCOMMITTEE],
            page_size=100,
            parameters=subcommittee_parameters,
        )
        api_calls += 1
        rows.extend(subcommittee.rows)
        candidates = distinct_minutes_rows(tuple(rows))
        candidates.sort(key=lambda row: _meeting_relevance(row, query, committee), reverse=True)
        meeting_repository = MeetingRepository(self.database)
        for row in candidates:
            try:
                source_url = OpenAssemblyPipeline.minutes_url(row)
                row_hash = hashlib.sha256(repr(sorted(row.items())).encode()).hexdigest()
                meeting_repository.save(
                    meeting_from_open_assembly_row(row, source_hash=row_hash, source_url=source_url)
                )
            except (TypeError, ValueError):
                continue
        if not ingest_minutes:
            self.last_refresh = {
                "meeting_api_calls": api_calls,
                "meeting_candidates": len(candidates),
                "minutes_ingested": 0,
                "minutes_failures": 0,
            }
            return
        ingested = 0
        failures = 0
        for row in candidates:
            if ingested >= self.max_minutes_per_request:
                break
            try:
                self.pipeline.sync(row)
            except (OSError, RuntimeError, ValueError):
                failures += 1
                continue
            ingested += 1
        self.last_refresh = {
            "meeting_api_calls": api_calls,
            "meeting_candidates": len(candidates),
            "minutes_ingested": ingested,
            "minutes_failures": failures,
            "minutes_limit": self.max_minutes_per_request,
        }

    def _months_for_query(
        self, query: str, date_from: str | None = None, date_to: str | None = None
    ) -> set[str]:
        months = {
            match.group("year") + "-" + match.group("month").zfill(2)
            for match in _DATE_MONTH.finditer(query)
        }
        for value in (date_from, date_to):
            if value and len(value) >= 7:
                months.add(value[:7])
        if not months:
            today = self._now().date()
            months.add(today.strftime("%Y-%m"))
            months.add((today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"))
        return months


def create_live_services(
    *,
    api_key: str | None = None,
    data_dir: str | Path | None = None,
    client: AssemblyOpenApiClient | None = None,
    max_minutes_per_request: int = 8,
) -> ServiceContext:
    """Create the default user-keyed live service and its private local cache."""
    root = Path(
        data_dir or os.getenv("KBD_DATA_DIR") or Path.home() / ".local/share/korean-bill-debate-mcp"
    )
    root.mkdir(parents=True, exist_ok=True)
    database = Database(root / "cache.sqlite3")
    database.initialize()
    api_client = client or AssemblyOpenApiClient(api_key, cache_dir=root / "api-cache")
    if not api_client.api_key:
        raise RuntimeError(
            "ASSEMBLY_OPEN_API_KEY is required. Issue your key at https://open.assembly.go.kr"
        )
    live = LiveAssemblyServices(
        database,
        api_client,
        MinutesFetcher(root),
        max_minutes_per_request=int(
            os.getenv("KBD_MAX_MINUTES_PER_REQUEST", str(max_minutes_per_request))
        ),
    )
    return ServiceContext(search=live, repository=live, catalog=live)


def _bill_queries(query: str) -> list[str]:
    inferred = infer_bill_title_query(query)
    terms = [term for term in query_terms(query) if term not in _STOPWORDS and len(term) >= 2]
    candidates = [
        query.strip(),
        *([inferred] if inferred else []),
        *sorted(terms, key=len, reverse=True),
    ]
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def _meeting_relevance(row: dict[str, Any], query: str, committee: str | None) -> tuple[int, str]:
    haystack = " ".join(str(value) for value in row.values()).casefold()
    score = sum(term.casefold() in haystack for term in query_terms(query))
    if committee and committee.casefold() in haystack:
        score += 5
    return score, _value(row, "CONF_DATE", "CONF_DT") or ""


def _unique_rows(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = str(row.get(key) or "")
        if value:
            unique[value] = row
    return list(unique.values())


def _value(row: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None
