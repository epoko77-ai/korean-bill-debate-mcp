"""Live-first Open Assembly research with a bounded local evidence cache."""

from __future__ import annotations

import hashlib
import os
import re
import urllib.parse
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from kasm.adapters.korea.bills import (
    BILL_DATASET,
    BILL_STATUS_DATASET,
    ingest_bill_rows,
)
from kasm.adapters.korea.client import AssemblyOpenApiClient
from kasm.adapters.korea.documents import BillDocumentFetcher, BillDocumentsClient
from kasm.adapters.korea.fetcher import MinutesFetcher
from kasm.adapters.korea.ingestion import meeting_from_open_assembly_row
from kasm.adapters.korea.pipeline import OpenAssemblyPipeline, distinct_minutes_rows
from kasm.adapters.korea.sources import DATASET_BY_SOURCE, MeetingSource
from kasm.app import LocalServices, infer_bill_title_query, infer_issue_committee
from kasm.core.models import BillDocument
from kasm.mcp.tools import ServiceContext, extract_bill_numbers
from kasm.search.lexical import query_terms
from kasm.storage.database import Database
from kasm.storage.repositories import BillDocumentRepository, MeetingRepository

_DATE_MONTH = re.compile(r"(?P<year>20\d{2})[.\-/년 ]+\s*(?P<month>1[0-2]|0?[1-9])")
_HISTORY_TERMS = ("과거부터", "처음부터", "현재까지", "지금까지", "전체 경과", "시계열")
_ASSEMBLY_START_MONTH = {22: "2024-05"}
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
        document_client: BillDocumentsClient | None = None,
        document_fetcher: BillDocumentFetcher | None = None,
        assembly_term: int = 22,
        max_minutes_per_request: int = 20,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.client = client
        self.pipeline = OpenAssemblyPipeline(database, fetcher)
        self.document_client = document_client
        self.document_fetcher = document_fetcher
        self.bill_documents = BillDocumentRepository(database)
        self._document_checks: set[str] = set()
        self._document_refresh: dict[str, dict[str, Any]] = {}
        self._minutes_failed_urls: set[str] = set()
        self.local = LocalServices(database)
        self.assembly_term = assembly_term
        self.max_minutes_per_request = max_minutes_per_request
        self._now = now or (lambda: datetime.now(UTC))
        self.last_refresh: dict[str, Any] = {}
        self._latest_bill_inventory: list[dict[str, Any]] = []
        self._latest_meeting_inventory: list[dict[str, Any]] = []

    def search_bills(self, query: str, **filters: Any) -> list[dict[str, Any]]:
        term = int(filters.get("assembly_term") or self.assembly_term)
        include_documents = bool(filters.pop("include_documents", True))
        self._refresh_bills(
            query=query,
            assembly_term=term,
            include_documents=False,
        )
        # Candidate search stays compact.  Full report text is loaded only for
        # explicitly selected bills below, where delivery is lossless.
        results = self.local.search_bills(
            query,
            include_documents=False,
            **filters,
        )
        if not results:
            for candidate in _bill_queries(query)[1:]:
                results = self.local.search_bills(
                    candidate,
                    include_documents=False,
                    **filters,
                )
                if results:
                    break
        if include_documents:
            results = self._hydrate_selected_bills(results)
        return results

    def get_bill_status(self, bill_id_or_no: str) -> dict[str, Any] | None:
        bill_no = bill_id_or_no.removeprefix("kna:bill:")
        status_row = self._refresh_bill_status(bill_no)
        if status_row is None:
            status_row = self._refresh_bill_by_number(bill_no)
        result = self.local.get_bill_status(bill_id_or_no)
        if result is None and bill_no != bill_id_or_no:
            result = self.local.get_bill_status(bill_no)
        if result is not None:
            self._refresh_bill_documents({**result, **(status_row or {})})
            result = self.local.get_bill_status(bill_no) or result
            _attach_lossless_bill_documents(self.database, result)
            result["document_coverage"] = self._bill_document_coverage(
                bill_no, result
            )
        return result

    def list_meetings(self, **filters: Any) -> list[dict[str, Any]]:
        date_from = filters.get("date_from")
        date_to = filters.get("date_to")
        months = self._months_for_query("", date_from, date_to)
        requested_months = _requested_months("", date_from, date_to)
        self._refresh_meetings(
            query="",
            committee=filters.get("committee"),
            months=months,
            ingest_minutes=False,
            temporal_scope=_temporal_scope(
                mode="explicit" if requested_months else "implicit_recent_two_month_window",
                explicit=bool(requested_months),
                requested_months=requested_months,
                queried_months=months,
                date_from=date_from,
                date_to=date_to,
            ),
        )
        return self.local.list_meetings(**filters)

    def list_committees(
        self, assembly_term: int | None = None, query: str | None = None
    ) -> list[dict[str, Any]]:
        search_query = query or ""
        months = self._months_for_query(search_query)
        requested_months = _requested_months(search_query)
        self._refresh_meetings(
            query=search_query,
            committee=query,
            months=months,
            ingest_minutes=False,
            temporal_scope=_temporal_scope(
                mode="explicit" if requested_months else "implicit_recent_two_month_window",
                explicit=bool(requested_months),
                requested_months=requested_months,
                queried_months=months,
            ),
        )
        return self.local.list_committees(assembly_term, query)

    def search(self, query: str, **filters: Any) -> list[dict[str, Any]]:
        self._hydrate_issue(query, filters)
        return self.local.search(query, **filters)

    def get(self, speech_id: str) -> dict[str, Any] | None:
        return self.local.get(speech_id)

    def context(self, speech_id: str, before: int = 2, after: int = 2) -> dict[str, Any]:
        return self.local.context(speech_id, before, after)

    def explore_issue(
        self,
        query: str,
        limit: int = 20,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        minutes_offset: int = 0,
    ) -> dict[str, Any]:
        self._hydrate_issue(
            query,
            {
                "limit": limit,
                "date_from": date_from,
                "date_to": date_to,
                "minutes_offset": minutes_offset,
            },
        )
        result = self.local.explore_issue(
            query,
            limit,
            date_from=date_from,
            date_to=date_to,
        )
        result["bills"] = self._hydrate_selected_bills(
            [bill for bill in result.get("bills", []) if isinstance(bill, dict)]
        )
        self._merge_selected_bill_inventory(result["bills"])
        result["data_mode"] = "live_open_assembly_with_local_cache"
        result["live_checked_at"] = self._now().isoformat()
        result["cache_database"] = str(self.database.path)
        result["live_refresh"] = self.last_refresh
        pagination = _research_pagination(self.last_refresh)
        result["research_pagination"] = pagination
        local_scope = result.get("scope_inventory")
        cached_inventory = local_scope if isinstance(local_scope, dict) else {}
        self._merge_cached_bill_inventory(cached_inventory.get("bill_candidates"))
        raw_selected = cached_inventory.get("selected_for_synthesis")
        cached_selected = raw_selected if isinstance(raw_selected, dict) else {}
        result["scope_inventory"] = {
            "cache_scope": cached_inventory.get("cache_scope")
            or {
                "complete": True,
                "official_source_complete": False,
                "note": "현재 요청에서 내려받은 로컬 캐시 범위입니다.",
            },
            "bill_candidates": {
                "complete": True,
                "total": len(self._latest_bill_inventory),
                "items": self._latest_bill_inventory,
            },
            "meeting_candidates": {
                "complete": True,
                "total": len(self._latest_meeting_inventory),
                "items": self._latest_meeting_inventory,
            },
            "speech_candidates": cached_inventory.get("speech_candidates")
            or {"complete": True, "total": 0, "items": []},
            "links": cached_inventory.get("links")
            or {"complete": True, "total": 0, "items": []},
            "selected_for_synthesis": {
                **cached_selected,
                "bill_count": len(result["bills"]),
                "speech_count": len(result.get("speeches", [])),
                "discussion_thread_count": len(result.get("discussion_threads", [])),
                "minutes_full_text_complete": pagination["complete"],
                "minutes_window_full_text_complete": pagination["window_complete"],
                "overall_scope_complete": pagination["overall_complete"],
                "temporal_scope": pagination["temporal_scope"],
                "note": (
                    "bill_candidates와 meeting_candidates는 이번 공식 API 조회에서 확인한 "
                    "전체 후보 지도입니다. speech_candidates와 links는 현재까지 내려받은 "
                    "회의록 캐시의 전건 지도입니다. selected_for_synthesis는 핵심 원문이며 "
                    "어느 지도 전체와도 같은 뜻이 아닙니다."
                ),
            },
        }
        return result

    def _hydrate_issue(self, query: str, filters: dict[str, Any]) -> None:
        term = int(filters.get("assembly_term") or self.assembly_term)
        committee = filters.get("committee") or infer_issue_committee(query)
        date_from = filters.get("date_from")
        date_to = filters.get("date_to")
        bills = self._refresh_bills(
            query=query,
            assembly_term=term,
            include_documents=False,
        )
        months = self._months_for_query(query, date_from, date_to)
        initial_months = set(months)
        requested_months = _requested_months(query, date_from, date_to)
        explicit_temporal_scope = bool(requested_months)
        bill_committees = {
            value
            for bill in bills
            if (value := _value(bill, "COMMITTEE", "COMMITTEE_NM"))
        }
        if committee is None and len(bill_committees) == 1:
            committee = next(iter(bill_committees))
        if not explicit_temporal_scope:
            for bill in bills:
                for field in ("PROPOSE_DT", "PROC_DT", "CMT_PROC_DT", "LAW_PROC_DT"):
                    value = _value(bill, field)
                    if value and len(value.replace("-", "")) >= 6:
                        compact = value.replace("-", "").replace(".", "")
                        months.add(f"{compact[:4]}-{compact[4:6]}")
        bill_number_history = bool(extract_bill_numbers(query))
        natural_language_history = any(value in query for value in _HISTORY_TERMS)
        if bill_number_history or natural_language_history:
            proposal_months = [
                compact[:4] + "-" + compact[4:6]
                for bill in bills
                if (compact := re.sub(r"\D", "", _value(bill, "PROPOSE_DT") or ""))
                and len(compact) >= 6
            ]
            history_start_months = _requested_months(query, date_from)
            start_month = min(
                history_start_months
                or proposal_months
                or [_ASSEMBLY_START_MONTH.get(self.assembly_term, f"{self._now().year}-01")]
            )
            end_month = _month_value(date_to)
            end_date = _month_end_date(end_month) if end_month else self._now().date()
            months.update(_month_span(start_month, end_date))
        if explicit_temporal_scope:
            scope_mode = "explicit"
        elif bill_number_history or natural_language_history:
            scope_mode = "derived_history"
        elif months != initial_months:
            scope_mode = "derived_bill_dates"
        else:
            scope_mode = "implicit_recent_two_month_window"
        self._refresh_meetings(
            query=query,
            committee=committee,
            months=sorted(months),
            ingest_minutes=True,
            candidate_offset=max(0, int(filters.get("minutes_offset") or 0)),
            temporal_scope=_temporal_scope(
                mode=scope_mode,
                explicit=explicit_temporal_scope,
                requested_months=requested_months,
                queried_months=months,
                date_from=date_from,
                date_to=date_to,
            ),
        )

    def _refresh_bills(
        self,
        *,
        query: str,
        assembly_term: int,
        include_documents: bool = True,
    ) -> list[dict[str, Any]]:
        queries = _bill_queries(query)
        bill_numbers = extract_bill_numbers(query)
        rows: list[dict[str, Any]] = []
        hashes: list[str] = []
        for bill_no in bill_numbers:
            fetched_rows, source_hashes = self._fetch_complete(
                BILL_DATASET,
                page_size=10,
                parameters={"AGE": assembly_term, "BILL_NO": bill_no},
                refresh=True,
            )
            rows.extend(row for row in fetched_rows if _value(row, "BILL_NO") == bill_no)
            hashes.extend(source_hashes)
        if not bill_numbers:
            for candidate in queries:
                fetched_rows, source_hashes = self._fetch_complete(
                    BILL_DATASET,
                    page_size=1000,
                    parameters={"AGE": assembly_term, "BILL_NAME": candidate},
                )
                rows.extend(fetched_rows)
                hashes.extend(source_hashes)
        rows = _unique_rows(rows, "BILL_NO")
        if rows:
            source_hash = hashlib.sha256("".join(hashes).encode()).hexdigest()
            ingest_bill_rows(self.database, rows, source_hash=source_hash)
            status_targets = rows if bill_numbers or len(rows) == 1 else []
            for row in status_targets:
                refreshed_bill_no = _value(row, "BILL_NO")
                status_row = (
                    self._refresh_bill_status(refreshed_bill_no)
                    if refreshed_bill_no
                    else None
                )
                if status_row is not None:
                    row.update(status_row)
            if include_documents:
                for row in rows:
                    self._refresh_bill_documents(row)
        self._latest_bill_inventory = [_bill_inventory_entry(row) for row in rows]
        return rows

    def _fetch_complete(
        self,
        dataset: str,
        *,
        page_size: int,
        parameters: dict[str, str | int],
        refresh: bool = False,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Return every official API page, with a one-page fallback for simple test clients."""

        fetch_all = getattr(self.client, "fetch_all", None)
        if callable(fetch_all):
            result = fetch_all(
                dataset,
                page_size=page_size,
                parameters=parameters,
                refresh=refresh,
            )
            return list(result.rows), list(result.source_hashes)
        page = self.client.fetch_page(
            dataset,
            page_size=page_size,
            parameters=parameters,
            refresh=refresh,
        )
        if len(page.rows) != page.total_count:
            raise RuntimeError(
                "official API client lacks exhaustive fetch_all support for a multi-page result"
            )
        return list(page.rows), [page.source_hash]

    def _hydrate_selected_bills(
        self, bills: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Load status and every review report for every explicitly selected bill."""

        hydrated: list[dict[str, Any]] = []
        for bill in bills:
            bill_no = str(bill.get("bill_no") or "").strip()
            if not bill_no:
                continue
            status_row = self._refresh_bill_status(bill_no)
            if status_row is None:
                status_row = self._refresh_bill_by_number(bill_no)
            self._refresh_bill_documents({**bill, **(status_row or {})})
            refreshed = self.local.get_bill_status(bill_no)
            if refreshed is None:
                continue
            for key in (
                "linked_by",
                "link_confidence",
                "link_evidence",
                "selection_relevance",
            ):
                if key in bill:
                    refreshed[key] = bill[key]
            _attach_lossless_bill_documents(self.database, refreshed)
            refreshed["document_coverage"] = self._bill_document_coverage(
                bill_no, refreshed
            )
            hydrated.append(refreshed)
        return hydrated

    def _bill_document_coverage(
        self, bill_no: str, bill: dict[str, Any]
    ) -> dict[str, Any]:
        known = self._document_refresh.get(bill_no)
        if known is not None:
            return known
        document_count = len(
            [item for item in bill.get("documents", []) if isinstance(item, dict)]
        )
        return {
            "complete": False,
            "discovered": document_count,
            "loaded": document_count,
            "gap_reason": (
                "review_report_client_unconfigured"
                if self.document_client is None or self.document_fetcher is None
                else "review_report_discovery_not_run"
            ),
        }

    def _merge_selected_bill_inventory(self, bills: list[dict[str, Any]]) -> None:
        by_number = {
            str(item.get("bill_no") or ""): item
            for item in self._latest_bill_inventory
            if item.get("bill_no")
        }
        for bill in bills:
            bill_no = str(bill.get("bill_no") or "").strip()
            if not bill_no:
                continue
            item = by_number.get(bill_no)
            if item is None:
                item = {
                    "bill_no": bill_no,
                    "bill_id": str(bill.get("id") or "").removeprefix("kna:bill:"),
                    "name": bill.get("name"),
                    "committee": bill.get("committee"),
                    "proposed_at": bill.get("proposed_at"),
                    "process_result": bill.get("process_result"),
                    "official_url": bill.get("official_url"),
                }
                self._latest_bill_inventory.append(item)
                by_number[bill_no] = item
            documents = [
                document
                for document in bill.get("documents", [])
                if isinstance(document, dict)
            ]
            item.update(
                {
                    "selected_for_synthesis": True,
                    "process_result": bill.get("process_result"),
                    "status": bill.get("status"),
                    "review_report_count": len(documents),
                    "document_coverage": bill.get("document_coverage"),
                    "selection_relevance": bill.get("selection_relevance"),
                    "review_reports": [
                        {
                            "document_id": document.get("document_id"),
                            "title": document.get("title"),
                            "official_url": document.get("official_url"),
                            "text_length": document.get("text_length"),
                            "text_sha256": document.get("text_sha256"),
                            "text_inline_complete": document.get("text_inline_complete"),
                        }
                        for document in documents
                    ],
                }
            )

    def _merge_cached_bill_inventory(self, value: Any) -> None:
        """Attach deterministic relevance without discarding the official raw map."""

        if not isinstance(value, dict):
            return
        raw_items = value.get("items")
        cached_items = raw_items if isinstance(raw_items, list) else []
        by_number = {
            str(item.get("bill_no") or ""): item
            for item in self._latest_bill_inventory
            if item.get("bill_no")
        }
        by_id = {
            str(item.get("bill_id") or ""): item
            for item in self._latest_bill_inventory
            if item.get("bill_id")
        }
        for cached in cached_items:
            if not isinstance(cached, dict):
                continue
            bill_no = str(cached.get("bill_no") or "").strip()
            bill_id = str(cached.get("bill_id") or "").removeprefix("kna:bill:")
            item = by_number.get(bill_no) or by_id.get(bill_id)
            if item is None:
                item = dict(cached)
                if bill_id:
                    item["bill_id"] = bill_id
                self._latest_bill_inventory.append(item)
                if bill_no:
                    by_number[bill_no] = item
                if bill_id:
                    by_id[bill_id] = item
            for key in (
                "selection_relevance",
                "linked_by",
                "link_confidence",
                "link_evidence",
                "document_count",
            ):
                if key in cached:
                    item[key] = cached[key]

    def _refresh_bill_status(self, bill_no: str) -> dict[str, Any] | None:
        page = self.client.fetch_page(
            BILL_STATUS_DATASET,
            page_size=100,
            parameters={"AGE": self.assembly_term, "BILL_NO": bill_no},
            refresh=True,
        )
        exact_rows = [row for row in page.rows if _value(row, "BILL_NO") == bill_no]
        if exact_rows:
            ingest_bill_rows(self.database, exact_rows, source_hash=page.source_hash)
            return exact_rows[0]
        return None

    def _refresh_bill_by_number(self, bill_no: str) -> dict[str, Any] | None:
        page = self.client.fetch_page(
            BILL_DATASET,
            page_size=10,
            parameters={"AGE": self.assembly_term, "BILL_NO": bill_no},
            refresh=True,
        )
        exact_rows = [row for row in page.rows if _value(row, "BILL_NO") == bill_no]
        if exact_rows:
            ingest_bill_rows(self.database, exact_rows, source_hash=page.source_hash)
            return exact_rows[0]
        return None

    def _refresh_bill_documents(self, row: dict[str, Any]) -> None:
        if self.document_client is None or self.document_fetcher is None:
            return
        bill_no = _value(row, "BILL_NO", "bill_no")
        external_bill_id = _bill_external_id(row)
        if not bill_no:
            return
        if not external_bill_id:
            self._document_refresh[bill_no] = {
                "complete": False,
                "discovered": 0,
                "loaded": 0,
                "gap_reason": "official_bill_identity_unavailable",
            }
            return
        if bill_no in self._document_checks:
            return
        self._document_checks.add(bill_no)
        try:
            links = self.document_client.review_reports(external_bill_id, bill_no)
        except RuntimeError:
            self._document_refresh[bill_no] = {
                "complete": False,
                "discovered": 0,
                "loaded": 0,
                "gap_reason": "official_review_report_discovery_failed",
            }
            return
        loaded = 0
        failed_urls: list[str] = []
        for link in links:
            try:
                fetched = self.document_fetcher.fetch(link.official_url)
            except RuntimeError:
                failed_urls.append(link.official_url)
                continue
            if not fetched.text.strip():
                failed_urls.append(link.official_url)
                continue
            document_id = "kna:bill-document:" + hashlib.sha256(
                link.official_url.encode()
            ).hexdigest()[:24]
            self.bill_documents.save(
                BillDocument(
                    id=document_id,
                    bill_id=f"kna:bill:{bill_no}",
                    document_type=link.document_type,
                    title=link.title,
                    file_format=link.file_format,
                    official_url=link.official_url,
                    text=fetched.text,
                    source_hash=fetched.source_hash,
                    retrieved_at=self._now(),
                )
            )
            loaded += 1
        self._document_refresh[bill_no] = {
            "complete": loaded == len(links),
            "discovered": len(links),
            "loaded": loaded,
            "failed_official_urls": failed_urls,
            "gap_reason": None if loaded == len(links) else "review_report_fetch_failed",
        }

    def _refresh_meetings(
        self,
        *,
        query: str,
        committee: str | None,
        months: Iterable[str],
        ingest_minutes: bool,
        candidate_offset: int = 0,
        temporal_scope: dict[str, Any] | None = None,
    ) -> None:
        rows: list[dict[str, Any]] = []
        api_calls = 0
        queried_months = sorted(months)
        scope = dict(
            temporal_scope
            or _temporal_scope(
                mode="unspecified",
                explicit=False,
                requested_months=(),
                queried_months=queried_months,
            )
        )
        scope.update(_temporal_window(queried_months))
        for month in queried_months:
            for source in (MeetingSource.COMMITTEE, MeetingSource.PLENARY):
                parameters: dict[str, str | int] = {
                    "DAE_NUM": self.assembly_term,
                    "CONF_DATE": month,
                }
                if committee and source is MeetingSource.COMMITTEE:
                    parameters["COMM_NAME"] = committee
                fetched_rows, _source_hashes = self._fetch_complete(
                    DATASET_BY_SOURCE[source], page_size=1000, parameters=parameters
                )
                api_calls += 1
                rows.extend(fetched_rows)
        subcommittee_parameters: dict[str, str | int] = {"ERACO": f"제{self.assembly_term}대"}
        if committee:
            subcommittee_parameters["CMIT_NM"] = committee
        subcommittee_rows, _subcommittee_hashes = self._fetch_complete(
            DATASET_BY_SOURCE[MeetingSource.SUBCOMMITTEE],
            page_size=1000,
            parameters=subcommittee_parameters,
        )
        api_calls += 1
        rows.extend(subcommittee_rows)
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
        self._latest_meeting_inventory = _meeting_inventory(
            self.database, candidates
        )
        if not ingest_minutes:
            self.last_refresh = {
                "meeting_api_calls": api_calls,
                "meeting_candidates": len(candidates),
                "minutes_ingested": 0,
                "minutes_failures": 0,
                "months_queried": queried_months,
                "temporal_scope": scope,
                "candidate_offset": 0,
                "next_minutes_offset": None,
                "has_more": False,
            }
            return
        ingested = 0
        failures = 0
        attempted = 0
        window = candidates[candidate_offset : candidate_offset + self.max_minutes_per_request]
        for row in window:
            attempted += 1
            try:
                official_url = OpenAssemblyPipeline.minutes_url(row)
            except ValueError:
                official_url = ""
            try:
                self.pipeline.sync(row)
            except (OSError, RuntimeError, ValueError):
                failures += 1
                if official_url:
                    self._minutes_failed_urls.add(official_url)
                continue
            if official_url:
                self._minutes_failed_urls.discard(official_url)
            ingested += 1
        self._latest_meeting_inventory = _meeting_inventory(
            self.database, candidates
        )
        next_offset = candidate_offset + attempted
        has_more = next_offset < len(candidates)
        candidate_urls = {
            candidate_url
            for row in candidates
            if (candidate_url := _optional_minutes_url(row)) is not None
        }
        failed_urls = sorted(self._minutes_failed_urls.intersection(candidate_urls))
        self.last_refresh = {
            "meeting_api_calls": api_calls,
            "meeting_candidates": len(candidates),
            "minutes_ingested": ingested,
            "minutes_failures": len(failed_urls),
            "minutes_failures_in_window": failures,
            "failed_official_urls": failed_urls,
            "minutes_limit": self.max_minutes_per_request,
            "months_queried": queried_months,
            "temporal_scope": scope,
            "candidate_offset": candidate_offset,
            "next_minutes_offset": next_offset if has_more else None,
            "has_more": has_more,
        }

    def _months_for_query(
        self, query: str, date_from: str | None = None, date_to: str | None = None
    ) -> set[str]:
        months = set(_requested_months(query, date_from, date_to))
        start_month = _month_value(date_from)
        end_month = _month_value(date_to)
        query_months = _requested_months(query)
        if not start_month and query_months:
            start_month = min(query_months)
        if not end_month and any(term in query for term in _HISTORY_TERMS):
            end_month = self._now().date().strftime("%Y-%m")
        if start_month and end_month:
            months.update(_month_span(start_month, _month_end_date(end_month)))
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
    max_minutes_per_request: int = 20,
    source_timeout: float = 60.0,
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
        MinutesFetcher(root, timeout=source_timeout),
        document_client=BillDocumentsClient(timeout=min(source_timeout, 30.0)),
        document_fetcher=BillDocumentFetcher(root, timeout=source_timeout),
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


def _month_span(start_month: str, end_date: date) -> set[str]:
    try:
        year, month = (int(part) for part in start_month.split("-", 1))
        cursor = date(year, month, 1)
    except (TypeError, ValueError):
        return set()
    end = end_date.replace(day=1)
    months: set[str] = set()
    while cursor <= end:
        months.add(cursor.strftime("%Y-%m"))
        cursor = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
    return months


def _month_value(value: str | None) -> str | None:
    if not value:
        return None
    match = re.match(r"^(20\d{2})[-./년 ]+\s*(1[0-2]|0?[1-9])", value.strip())
    if not match:
        return None
    return f"{match.group(1)}-{int(match.group(2)):02d}"


def _requested_months(query: str, *values: str | None) -> list[str]:
    months = [
        f"{match.group('year')}-{int(match.group('month')):02d}"
        for match in _DATE_MONTH.finditer(query)
    ]
    months.extend(month for value in values if (month := _month_value(value)))
    return list(dict.fromkeys(months))


def _temporal_window(months: Iterable[str]) -> dict[str, Any]:
    values = sorted(dict.fromkeys(months))
    return {
        "queried_months": values,
        "window_start_month": values[0] if values else None,
        "window_end_month": values[-1] if values else None,
        "window_month_count": len(values),
    }


def _temporal_scope(
    *,
    mode: str,
    explicit: bool,
    requested_months: Iterable[str],
    queried_months: Iterable[str],
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "explicit": explicit,
        "requested_date_from": date_from,
        "requested_date_to": date_to,
        "requested_months": sorted(dict.fromkeys(requested_months)),
        **_temporal_window(queried_months),
    }


def _month_end_date(month: str) -> date:
    year, number = (int(part) for part in month.split("-", 1))
    if number == 12:
        return date(year, 12, 31)
    return date(year, number + 1, 1) - timedelta(days=1)


def _research_pagination(refresh: dict[str, Any]) -> dict[str, Any]:
    has_more = bool(refresh.get("has_more"))
    failures = int(refresh.get("minutes_failures") or 0)
    next_offset = refresh.get("next_minutes_offset")
    raw_scope = refresh.get("temporal_scope")
    temporal_scope = (
        dict(raw_scope)
        if isinstance(raw_scope, dict)
        else _temporal_scope(
            mode="unspecified",
            explicit=False,
            requested_months=(),
            queried_months=refresh.get("months_queried") or (),
        )
    )
    temporal_scope.update(
        {
            "mode": str(temporal_scope.get("mode") or "unspecified"),
            "explicit": temporal_scope.get("explicit") is True,
            "requested_date_from": temporal_scope.get("requested_date_from"),
            "requested_date_to": temporal_scope.get("requested_date_to"),
            "requested_months": temporal_scope.get("requested_months") or [],
        }
    )
    temporal_scope.update(
        _temporal_window(
            temporal_scope.get("queried_months")
            or refresh.get("months_queried")
            or ()
        )
    )
    window_complete = not has_more and failures == 0
    overall_complete = window_complete and temporal_scope.get("explicit") is True
    return {
        "complete": overall_complete,
        "overall_complete": overall_complete,
        "window_complete": window_complete,
        "partial": not overall_complete,
        "window_partial": not window_complete,
        "temporal_scope": temporal_scope,
        "next_minutes_offset": next_offset,
        "failed_count": failures,
        "failed_official_urls": refresh.get("failed_official_urls") or [],
        "instruction": (
            "Call the same tool again with minutes_offset=" + str(next_offset)
            if has_more
            else (
                "The meeting window is partial; disclose every failed official URL."
                if failures
                else (
                    "The explicit temporal scope has been checked."
                    if overall_complete
                    else (
                        "The configured meeting window has been checked, but its temporal "
                        "scope was implicit or derived; do not claim the overall natural-"
                        "language scope is complete."
                    )
                )
            )
        ),
    }


def _optional_minutes_url(row: dict[str, Any]) -> str | None:
    try:
        return OpenAssemblyPipeline.minutes_url(row)
    except ValueError:
        return None


def _bill_inventory_entry(row: dict[str, Any]) -> dict[str, Any]:
    bill_no = _value(row, "BILL_NO")
    bill_id = _value(row, "BILL_ID")
    official_url = _value(row, "DETAIL_LINK", "LINK_URL")
    if not official_url and bill_id:
        official_url = (
            "https://likms.assembly.go.kr/bill/billDetail.do?"
            + urllib.parse.urlencode({"billId": bill_id})
        )
    return {
        "bill_no": bill_no,
        "bill_id": bill_id,
        "name": _value(row, "BILL_NAME", "BILL_NM"),
        "committee": _value(row, "COMMITTEE", "COMMITTEE_NM"),
        "proposed_at": _value(row, "PROPOSE_DT"),
        "process_result": _value(row, "PROC_RESULT", "LAW_PROC_RESULT_CD"),
        "official_url": official_url,
    }


def _meeting_inventory(
    database: Database, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    speech_counts = {
        str(row["meeting_id"]): int(row["speech_count"])
        for row in database.connection.execute(
            "SELECT meeting_id, count(*) AS speech_count FROM speeches GROUP BY meeting_id"
        ).fetchall()
    }
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            official_url = OpenAssemblyPipeline.minutes_url(row)
            source_hash = hashlib.sha256(repr(sorted(row.items())).encode()).hexdigest()
            meeting = meeting_from_open_assembly_row(
                row,
                source_hash=source_hash,
                source_url=official_url,
            )
        except (TypeError, ValueError):
            continue
        raw_agendas = row.get("agenda_items")
        agendas = raw_agendas if isinstance(raw_agendas, list) else []
        related_bill_numbers = list(
            dict.fromkeys(
                str(agenda.get("bill_no"))
                for agenda in agendas
                if isinstance(agenda, dict) and agenda.get("bill_no")
            )
        )
        speech_count = speech_counts.get(meeting.id, 0)
        items.append(
            {
                "meeting_id": meeting.id,
                "date": meeting.date.isoformat(),
                "title": meeting.title,
                "committee": meeting.committee_name_ko,
                "meeting_type": meeting.meeting_type,
                "related_bill_numbers": related_bill_numbers,
                "official_url": meeting.source_url,
                "full_text_loaded": speech_count > 0,
                "speech_count": speech_count,
            }
        )
    return items


def _attach_lossless_bill_documents(database: Database, bill: dict[str, Any]) -> None:
    """Replace legacy excerpts with the exact stored report text and integrity metadata."""

    bill_id = str(bill.get("id") or "").strip()
    if not bill_id:
        return
    rows = database.connection.execute(
        """SELECT id, bill_id, document_type, title, file_format, official_url, text,
                  source_hash, retrieved_at
           FROM bill_documents WHERE bill_id = ? ORDER BY title, official_url""",
        (bill_id,),
    ).fetchall()
    documents: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        text = str(row.pop("text"))
        row["document_id"] = row.pop("id")
        row.pop("bill_id", None)
        row["text"] = text
        row["text_length"] = len(text)
        row["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        row["text_inline_complete"] = True
        row["citation"] = {
            "official_url": row["official_url"],
            "source_locator": "전문위원 검토보고서 PDF 전체 본문",
        }
        documents.append(row)
    bill["documents"] = documents


def _bill_external_id(row: dict[str, Any]) -> str | None:
    direct = _value(row, "BILL_ID")
    if direct:
        return direct
    detail_url = _value(row, "DETAIL_LINK", "LINK_URL", "official_url", "source_url")
    if not detail_url:
        return None
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(detail_url).query)
    return values.get("billId", [None])[0]


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
