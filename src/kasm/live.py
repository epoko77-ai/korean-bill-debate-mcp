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
from kasm.core.quality import issue_quality
from kasm.mcp.tools import ServiceContext, extract_bill_numbers
from kasm.research.assembly_terms import (
    assembly_term as official_assembly_term,
)
from kasm.research.assembly_terms import (
    assembly_terms_intersecting,
)
from kasm.search.lexical import query_terms
from kasm.search.terminology import LEGAL_TERMINOLOGY
from kasm.storage.database import Database
from kasm.storage.repositories import BillDocumentRepository, MeetingRepository

_DATE_MONTH = re.compile(
    r"(?P<year>(?:19|20)\d{2})[.\-/년 ]+\s*(?P<month>1[0-2]|0?[1-9])"
)
_DATE_YEAR = re.compile(r"(?<!\d)(?P<year>(?:19|20)\d{2})(?!\d)")
_PROPOSAL_YEAR = re.compile(
    r"(?<!\d)(?P<year>(?:19|20)\d{2})\s*년(?:도)?(?:에)?\s*발의"
)
_ENGLISH_PROPOSAL_YEAR = re.compile(
    r"\b(?:proposed|introduced)\s+in\s+(?P<year>(?:19|20)\d{2})\b",
    re.IGNORECASE,
)
_NUMBERED_SCOPE_TERM = re.compile(r"^\d+(?:년|월|일|개|건|대|차)?$")
_HISTORY_TERMS = ("과거부터", "처음부터", "현재까지", "지금까지", "전체 경과", "시계열")
_STOPWORDS = {
    "내용",
    "내용을",
    "대한",
    "관련",
    "발의",
    "발의된",
    "발의한",
    "법안을",
    "논의",
    "의견",
    "정부",
    "법안",
    "상임위원회",
    "소위원회",
    "회의록",
    "입법",
    "정책",
    "정도",
    "이에",
    "중요도",
    "중요도가",
    "높은",
    "현재",
    "상태",
    "최근",
    "정리하고",
    "정리해줘",
    "보여줘",
    "알려줘",
}
_INSTRUCTION_PREFIXES = (
    "정리",
    "요약",
    "설명",
    "알려",
    "보여",
    "확인",
    "찾아",
    "발의",
    "중요",
)
_BOUNDED_PROPOSAL_DISCOVERY_LIMIT = 50


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
        term = self._selected_assembly_term(query, filters)
        include_documents = bool(filters.pop("include_documents", True))
        requested_limit = max(1, int(filters.get("limit", 10)))
        proposal_scope = _proposal_date_scope(query)
        if proposal_scope is not None:
            filters["limit"] = max(
                requested_limit,
                _BOUNDED_PROPOSAL_DISCOVERY_LIMIT,
            )
        filters["assembly_term"] = term
        self._refresh_bills(
            query=query,
            assembly_term=term,
            include_documents=False,
        )
        # Natural-language instructions are not bill titles. Query the compact,
        # topic-bearing candidates individually and merge them before applying
        # the requested top-N bound.
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in _bill_queries(query):
            found = self.local.search_bills(
                candidate,
                include_documents=False,
                **filters,
            )
            for bill in _filter_bills_by_proposal_scope(found, proposal_scope):
                identity = str(bill.get("bill_no") or bill.get("id") or "")
                if not identity or identity in seen:
                    continue
                seen.add(identity)
                results.append(bill)
        results = results[:requested_limit]
        if include_documents:
            results = self._hydrate_selected_bills(results, assembly_term=term)
        return results

    def get_bill_status(self, bill_id_or_no: str) -> dict[str, Any] | None:
        bill_no = bill_id_or_no.removeprefix("kna:bill:")
        term = _bill_assembly_term(bill_no) or self.assembly_term
        status_row = self._refresh_bill_status(bill_no, assembly_term=term)
        if status_row is None:
            status_row = self._refresh_bill_by_number(bill_no, assembly_term=term)
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
        term = self._selected_assembly_term("", filters)
        months = self._months_for_query(
            "", date_from, date_to, assembly_term=term
        )
        requested_months = _requested_months("", date_from, date_to)
        self._refresh_meetings(
            query="",
            committee=filters.get("committee"),
            months=months,
            assembly_term=term,
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
        return [
            row
            for row in self.local.list_meetings(**filters)
            if int(row.get("assembly_term") or term) == term
        ]

    def list_committees(
        self, assembly_term: int | None = None, query: str | None = None
    ) -> list[dict[str, Any]]:
        search_query = query or ""
        term = self._selected_assembly_term(
            search_query,
            {"assembly_term": assembly_term} if assembly_term is not None else {},
        )
        months = self._months_for_query(search_query, assembly_term=term)
        requested_months = _requested_months(search_query)
        self._refresh_meetings(
            query=search_query,
            committee=query,
            months=months,
            assembly_term=term,
            ingest_minutes=False,
            temporal_scope=_temporal_scope(
                mode="explicit" if requested_months else "implicit_recent_two_month_window",
                explicit=bool(requested_months),
                requested_months=requested_months,
                queried_months=months,
            ),
        )
        return self.local.list_committees(term, query)

    def search(self, query: str, **filters: Any) -> list[dict[str, Any]]:
        term = self._hydrate_issue(query, filters)
        return self.local.search(query, **{**filters, "assembly_term": term})

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
        assembly_term: int | None = None,
    ) -> dict[str, Any]:
        term = self._hydrate_issue(
            query,
            {
                "limit": limit,
                "date_from": date_from,
                "date_to": date_to,
                "minutes_offset": minutes_offset,
                "assembly_term": assembly_term,
            },
        )
        proposal_scope = _proposal_date_scope(query)
        local_limit = (
            max(limit, _BOUNDED_PROPOSAL_DISCOVERY_LIMIT)
            if proposal_scope is not None
            else limit
        )
        local_date_from = date_from
        local_date_to = date_to
        if proposal_scope is not None and date_from is None and date_to is None:
            local_date_from = proposal_scope[0].isoformat()
            local_date_to = min(
                proposal_scope[1],
                self._now().date(),
            ).isoformat()
        result = self.local.explore_issue(
            query,
            local_limit,
            date_from=local_date_from,
            date_to=local_date_to,
            assembly_term=term,
        )
        if proposal_scope is not None:
            _filter_issue_by_proposal_scope(
                result,
                proposal_scope,
                requested_limit=limit,
            )
        # A broad issue overview must not serially discover and download every
        # selected bill's review PDFs. The bill metadata and official URL remain
        # available here; get_bill_status performs lossless targeted hydration.
        result["bills"] = _bounded_bill_payloads(
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

    def _hydrate_issue(self, query: str, filters: dict[str, Any]) -> int:
        term = self._selected_assembly_term(query, filters)
        committee = filters.get("committee") or infer_issue_committee(query)
        date_from = filters.get("date_from")
        date_to = filters.get("date_to")
        bills = self._refresh_bills(
            query=query,
            assembly_term=term,
            include_documents=False,
        )
        months = self._months_for_query(
            query, date_from, date_to, assembly_term=term
        )
        initial_months = set(months)
        requested_months = _requested_months(query, date_from, date_to)
        proposal_scope = _proposal_date_scope(query)
        if proposal_scope is not None and date_from is None and date_to is None:
            # A phrase such as "2026년 발의된" scopes the bills by proposal
            # date.  Its related deliberations can only run through the
            # current day; do not describe or retain future meeting months.
            effective_end = min(proposal_scope[1], self._now().date())
            requested_months = sorted(
                _month_span(proposal_scope[0].strftime("%Y-%m"), effective_end)
            )
            months = set(requested_months)
            date_from = proposal_scope[0].isoformat()
            date_to = effective_end.isoformat()
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
                or [official_assembly_term(term).date_from.strftime("%Y-%m")]
            )
            end_month = _month_value(date_to)
            term_end = official_assembly_term(term).date_to
            end_date = min(
                _month_end_date(end_month) if end_month else self._now().date(),
                term_end,
            )
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
            assembly_term=term,
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
        return term

    def _selected_assembly_term(
        self, query: str, filters: dict[str, Any]
    ) -> int:
        return _selected_assembly_term(
            default_term=self.assembly_term,
            query=query,
            explicit_term=filters.get("assembly_term"),
            date_from=filters.get("date_from"),
            date_to=filters.get("date_to"),
            as_of=self._now().date(),
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
        rows = _filter_bills_by_proposal_scope(
            rows,
            _proposal_date_scope(query),
        )
        if rows:
            source_hash = hashlib.sha256("".join(hashes).encode()).hexdigest()
            ingest_bill_rows(self.database, rows, source_hash=source_hash)
            status_targets = rows if bill_numbers or len(rows) == 1 else []
            for row in status_targets:
                refreshed_bill_no = _value(row, "BILL_NO")
                status_row = (
                    self._refresh_bill_status(
                        refreshed_bill_no, assembly_term=assembly_term
                    )
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
        self,
        bills: list[dict[str, Any]],
        *,
        assembly_term: int | None = None,
    ) -> list[dict[str, Any]]:
        """Load status and every review report for every explicitly selected bill."""

        hydrated: list[dict[str, Any]] = []
        for bill in bills:
            bill_no = str(bill.get("bill_no") or "").strip()
            if not bill_no:
                continue
            term = _bill_assembly_term(bill_no) or assembly_term or self.assembly_term
            status_row = self._refresh_bill_status(bill_no, assembly_term=term)
            if status_row is None:
                status_row = self._refresh_bill_by_number(
                    bill_no, assembly_term=term
                )
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

    def _refresh_bill_status(
        self, bill_no: str, *, assembly_term: int | None = None
    ) -> dict[str, Any] | None:
        term = _bill_assembly_term(bill_no) or assembly_term or self.assembly_term
        page = self.client.fetch_page(
            BILL_STATUS_DATASET,
            page_size=100,
            parameters={"AGE": term, "BILL_NO": bill_no},
            refresh=True,
        )
        exact_rows = [row for row in page.rows if _value(row, "BILL_NO") == bill_no]
        if exact_rows:
            ingest_bill_rows(self.database, exact_rows, source_hash=page.source_hash)
            return exact_rows[0]
        return None

    def _refresh_bill_by_number(
        self, bill_no: str, *, assembly_term: int | None = None
    ) -> dict[str, Any] | None:
        term = _bill_assembly_term(bill_no) or assembly_term or self.assembly_term
        page = self.client.fetch_page(
            BILL_DATASET,
            page_size=10,
            parameters={"AGE": term, "BILL_NO": bill_no},
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
        assembly_term: int | None = None,
        candidate_offset: int = 0,
        temporal_scope: dict[str, Any] | None = None,
    ) -> None:
        term = assembly_term or self.assembly_term
        official_assembly_term(term)
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
        for date_query in _meeting_date_queries(
            queried_months,
            as_of=self._now().date(),
        ):
            for source in (MeetingSource.COMMITTEE, MeetingSource.PLENARY):
                parameters: dict[str, str | int] = {
                    "DAE_NUM": term,
                    "CONF_DATE": date_query,
                }
                if committee and source is MeetingSource.COMMITTEE:
                    parameters["COMM_NAME"] = committee
                fetched_rows, _source_hashes = self._fetch_complete(
                    DATASET_BY_SOURCE[source], page_size=1000, parameters=parameters
                )
                api_calls += 1
                rows.extend(fetched_rows)
        subcommittee_parameters: dict[str, str | int] = {"ERACO": f"제{term}대"}
        if committee:
            subcommittee_parameters["CMIT_NM"] = committee
        subcommittee_rows, _subcommittee_hashes = self._fetch_complete(
            DATASET_BY_SOURCE[MeetingSource.SUBCOMMITTEE],
            page_size=1000,
            parameters=subcommittee_parameters,
        )
        api_calls += 1
        rows.extend(subcommittee_rows)
        rows = _filter_meeting_rows_by_scope(rows, scope, queried_months)
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
        self,
        query: str,
        date_from: str | None = None,
        date_to: str | None = None,
        *,
        assembly_term: int | None = None,
    ) -> set[str]:
        term = official_assembly_term(assembly_term or self.assembly_term)
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
            scope_end = min(self._now().date(), term.date_to)
            months.add(scope_end.strftime("%Y-%m"))
            previous_month = scope_end.replace(day=1) - timedelta(days=1)
            if previous_month >= term.date_from:
                months.add(previous_month.strftime("%Y-%m"))
        return {
            month
            for month in months
            if _month_intersects_term(month, term.date_from, term.date_to)
        }


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
    try:
        reviewed = []
        for expansion in LEGAL_TERMINOLOGY.expand(query).expansions:
            reviewed.append(expansion.term)
            # Official bill titles legitimately mix the Korean concept and its
            # common Latin-script alias (for example, "AI 바이오헬스").
            if expansion.term == "인공지능":
                reviewed.append("AI")
    except ValueError:
        reviewed = []
    terms = [
        term
        for term in query_terms(query)
        if _is_bill_query_term(term)
    ]
    candidates = [
        *([inferred] if inferred else []),
        *reviewed,
        *terms,
    ]
    compact = list(
        dict.fromkeys(candidate.strip() for candidate in candidates if candidate.strip())
    )
    # Unknown concise titles still need one literal query. Long natural-language
    # instructions never do: sending the full sentence as BILL_NAME only adds a
    # guaranteed-empty official API round trip.
    return compact or ([query.strip()] if query.strip() else [])


def _is_bill_query_term(term: str) -> bool:
    value = term.strip()
    if len(value) < 2 or value in _STOPWORDS:
        return False
    if _NUMBERED_SCOPE_TERM.fullmatch(value):
        return False
    return not any(value.startswith(prefix) for prefix in _INSTRUCTION_PREFIXES)


def _proposal_date_scope(query: str) -> tuple[date, date] | None:
    """Return a hard proposal-date range only for explicit proposal-year grammar."""

    years = {
        int(match.group("year"))
        for pattern in (_PROPOSAL_YEAR, _ENGLISH_PROPOSAL_YEAR)
        for match in pattern.finditer(query)
    }
    # A single-year bounded filter is intentionally conservative. Multi-year
    # proposal ranges belong to the durable planner instead of being guessed.
    if len(years) != 1:
        return None
    year = next(iter(years))
    return date(year, 1, 1), date(year, 12, 31)


def _bill_proposal_date(row: dict[str, Any]) -> date | None:
    raw = _value(row, "PROPOSE_DT", "proposed_at")
    if raw is None:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 8:
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


def _filter_bills_by_proposal_scope(
    bills: Iterable[dict[str, Any]],
    scope: tuple[date, date] | None,
) -> list[dict[str, Any]]:
    values = list(bills)
    if scope is None:
        return values
    date_from, date_to = scope
    return [
        bill
        for bill in values
        if (proposed_at := _bill_proposal_date(bill)) is not None
        and date_from <= proposed_at <= date_to
    ]


def _bounded_bill_payloads(bills: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep broad overviews compact while routing report text to targeted lookup."""

    compact: list[dict[str, Any]] = []
    for value in bills:
        bill = dict(value)
        bill["documents"] = []
        bill["documents_included"] = False
        bill["documents_complete"] = False
        bill["document_coverage"] = {
            "complete": False,
            "discovered": None,
            "loaded": 0,
            "gap_reason": "targeted_get_bill_status_required",
        }
        compact.append(bill)
    return compact


def _rank_bills_by_observed_importance(
    bills: Iterable[dict[str, Any]],
    links: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rank a bounded candidate set using disclosed legislative activity signals."""

    speech_ids_by_bill: dict[str, set[str]] = {}
    for link in links:
        bill_id = str(link.get("bill_id") or "")
        speech_id = str(link.get("speech_id") or "")
        if bill_id and speech_id:
            speech_ids_by_bill.setdefault(bill_id, set()).add(speech_id)

    ranked: list[dict[str, Any]] = []
    for value in bills:
        bill = dict(value)
        relevance = bill.get("selection_relevance")
        topical_score = (
            int(relevance.get("score") or 0)
            if isinstance(relevance, dict)
            else 0
        )
        discussion_count = len(
            speech_ids_by_bill.get(str(bill.get("id") or ""), set())
        )
        processed = bool(
            bill.get("processed_at")
            or (
                str(bill.get("process_result") or "").strip()
                and str(bill.get("process_result") or "").strip() != "계류"
            )
        )
        committee_assigned = bool(str(bill.get("committee") or "").strip())
        progress_score = 12 if processed else 2 if committee_assigned else 0
        discussion_score = min(20, discussion_count * 5)
        score = topical_score + progress_score + discussion_score
        bill["importance"] = {
            "method": "bounded_observed_legislative_signals_v1",
            "score": score,
            "signals": {
                "topical_relevance_score": topical_score,
                "linked_discussion_count": discussion_count,
                "discussion_score": discussion_score,
                "processed": processed,
                "committee_assigned": committee_assigned,
                "legislative_progress_score": progress_score,
            },
            "complete_scope_known": False,
            "caveat": (
                "중요도는 현재 제한형 조회에서 관측된 주제 적합도·회의록 연결 수·"
                "입법 진행 신호를 합산한 비교 지표이며 정책적 중요성의 절대평가가 아닙니다."
            ),
        }
        ranked.append(bill)
    ranked.sort(
        key=lambda bill: (
            -int((bill.get("importance") or {}).get("score") or 0),
            -int(
                ((bill.get("importance") or {}).get("signals") or {}).get(
                    "linked_discussion_count"
                )
                or 0
            ),
            -(_bill_proposal_date(bill) or date.min).toordinal(),
            str(bill.get("bill_no") or ""),
        )
    )
    for rank, bill in enumerate(ranked, 1):
        importance = bill.get("importance")
        if isinstance(importance, dict):
            importance["rank"] = rank
    return ranked


def _filter_issue_by_proposal_scope(
    payload: dict[str, Any],
    scope: tuple[date, date],
    *,
    requested_limit: int,
) -> None:
    """Apply proposal-year semantics to selected bills and every bill inventory view."""

    scoped_bills = _filter_bills_by_proposal_scope(
        (
            bill
            for bill in payload.get("bills", [])
            if isinstance(bill, dict)
        ),
        scope,
    )
    observed_links = [
        link for link in payload.get("links", []) if isinstance(link, dict)
    ]
    ranked_bills = _rank_bills_by_observed_importance(
        scoped_bills,
        observed_links,
    )
    selected = ranked_bills[:requested_limit]
    payload["bills"] = selected
    payload["importance_selection"] = {
        "method": "bounded_observed_legislative_signals_v1",
        "requested_count": requested_limit,
        "ranked_candidate_count": len(ranked_bills),
        "signals": [
            "topical_relevance_score",
            "linked_discussion_count",
            "legislative_progress",
        ],
        "complete_scope_known": False,
        "instruction": (
            "이 순위는 제한형 후보군의 관측 신호 기준입니다. 전건 중요도 비교가 필요하면 "
            "durable 전수조사를 사용하세요."
        ),
    }

    speeches = [
        speech
        for speech in payload.get("speeches", [])
        if isinstance(speech, dict)
    ][:requested_limit]
    payload["speeches"] = speeches
    selected_speech_ids = {
        str(speech.get("speech_id") or "") for speech in speeches
    }
    threads = [
        thread
        for thread in payload.get("discussion_threads", [])
        if isinstance(thread, dict)
        and selected_speech_ids.intersection(
            str(value) for value in thread.get("matched_speech_ids", [])
        )
    ]
    payload["discussion_threads"] = threads

    raw_inventory = payload.get("scope_inventory")
    inventory = raw_inventory if isinstance(raw_inventory, dict) else {}
    raw_bill_candidates = inventory.get("bill_candidates")
    bill_candidates = (
        raw_bill_candidates if isinstance(raw_bill_candidates, dict) else {}
    )
    candidate_items = _filter_bills_by_proposal_scope(
        (
            item
            for item in bill_candidates.get("items", [])
            if isinstance(item, dict)
        ),
        scope,
    )
    selected_bill_ids = {str(bill.get("id") or "") for bill in selected}
    selected_bill_numbers = {
        str(bill.get("bill_no") or "") for bill in selected
    }
    importance_by_id = {
        str(bill.get("id") or ""): bill.get("importance")
        for bill in ranked_bills
    }
    importance_by_number = {
        str(bill.get("bill_no") or ""): bill.get("importance")
        for bill in ranked_bills
    }
    eligible_count = 0
    for item in candidate_items:
        relevance = item.get("selection_relevance")
        if not isinstance(relevance, dict):
            continue
        if relevance.get("eligible_for_synthesis") is True:
            eligible_count += 1
        relevance["selected_for_synthesis"] = (
            str(item.get("bill_id") or "") in selected_bill_ids
            or str(item.get("bill_no") or "") in selected_bill_numbers
        )
        importance = importance_by_id.get(
            str(item.get("bill_id") or "")
        ) or importance_by_number.get(str(item.get("bill_no") or ""))
        if importance is not None:
            item["importance"] = importance
    bill_candidates["items"] = candidate_items
    bill_candidates["total"] = len(candidate_items)
    inventory["bill_candidates"] = bill_candidates

    allowed_bill_ids = {
        str(item.get("bill_id") or "") for item in candidate_items
    }
    allowed_bill_numbers = {
        str(item.get("bill_no") or "") for item in candidate_items
    }

    def allowed_link(link: Any) -> bool:
        if not isinstance(link, dict):
            return False
        bill_id = str(link.get("bill_id") or "")
        bill_no = str(link.get("bill_no") or "")
        return bill_id in allowed_bill_ids or bill_no in allowed_bill_numbers

    payload["links"] = [
        link for link in payload.get("links", []) if allowed_link(link)
    ]
    raw_links = inventory.get("links")
    links_inventory = raw_links if isinstance(raw_links, dict) else {}
    link_items = [
        link for link in links_inventory.get("items", []) if allowed_link(link)
    ]
    links_inventory["items"] = link_items
    links_inventory["total"] = len(link_items)
    inventory["links"] = links_inventory

    selected_summary = inventory.get("selected_for_synthesis")
    summary = selected_summary if isinstance(selected_summary, dict) else {}
    summary.update(
        {
            "selection_limit": requested_limit,
            "bill_count": len(selected),
            "eligible_bill_count": eligible_count,
            "bill_selection_complete": len(selected) == eligible_count,
            "speech_count": len(speeches),
            "discussion_thread_count": len(threads),
        }
    )
    inventory["selected_for_synthesis"] = summary
    payload["scope_inventory"] = inventory
    payload["timeline"] = LocalServices._issue_timeline(selected, threads)
    payload["proposal_date_scope"] = {
        "basis": "proposal_date",
        "date_from": scope[0].isoformat(),
        "date_to": scope[1].isoformat(),
        "hard_filter": True,
    }
    payload["quality"] = issue_quality(payload)


def _meeting_date_queries(
    months: Iterable[str],
    *,
    as_of: date | None = None,
) -> list[str]:
    """Collapse a safe calendar span to one supported ``CONF_DATE`` query.

    A full historical year is exact.  January through the current month is
    also safe to fetch with a year query when rows are subsequently bounded by
    their actual meeting date.
    """

    by_year: dict[str, set[int]] = {}
    for value in sorted(dict.fromkeys(months)):
        match = re.fullmatch(r"((?:19|20)\d{2})-(1[0-2]|0[1-9])", value)
        if match is None:
            continue
        by_year.setdefault(match.group(1), set()).add(int(match.group(2)))
    queries: list[str] = []
    full_year = set(range(1, 13))
    for year, month_numbers in sorted(by_year.items()):
        elapsed_current_year = (
            as_of is not None
            and int(year) == as_of.year
            and month_numbers == set(range(1, as_of.month + 1))
        )
        if month_numbers == full_year or elapsed_current_year:
            queries.append(year)
        else:
            queries.extend(
                f"{year}-{month:02d}" for month in sorted(month_numbers)
            )
    return queries


def _filter_meeting_rows_by_scope(
    rows: Iterable[dict[str, Any]],
    temporal_scope: dict[str, Any],
    queried_months: Iterable[str],
) -> list[dict[str, Any]]:
    """Keep committee, plenary and subcommittee rows inside one exact window."""

    months = sorted(dict.fromkeys(queried_months))
    if not months:
        return []
    start = _date_value(str(temporal_scope.get("requested_date_from") or ""))
    end = _date_value(str(temporal_scope.get("requested_date_to") or ""))
    start = start or date.fromisoformat(f"{months[0]}-01")
    end = end or _month_end_date(months[-1])
    bounded: list[dict[str, Any]] = []
    for row in rows:
        raw = _value(row, "CONF_DATE", "CONF_DT")
        meeting_date = _meeting_date_value(raw)
        if meeting_date is not None and start <= meeting_date <= end:
            bounded.append(row)
    return bounded


def _meeting_date_value(value: str | None) -> date | None:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) < 8:
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


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
    match = re.match(
        r"^((?:19|20)\d{2})[-./년 ]+\s*(1[0-2]|0?[1-9])",
        value.strip(),
    )
    if not match:
        return None
    return f"{match.group(1)}-{int(match.group(2)):02d}"


def _requested_months(query: str, *values: str | None) -> list[str]:
    month_matches = tuple(_DATE_MONTH.finditer(query))
    months = [
        f"{match.group('year')}-{int(match.group('month')):02d}"
        for match in month_matches
    ]
    for match in _DATE_YEAR.finditer(query):
        if any(
            match.start() >= month_match.start()
            and match.end() <= month_match.end()
            for month_match in month_matches
        ):
            continue
        year = int(match.group("year"))
        months.extend(f"{year:04d}-{month:02d}" for month in range(1, 13))
    months.extend(month for value in values if (month := _month_value(value)))
    return list(dict.fromkeys(months))


def _bill_assembly_term(bill_no: str) -> int | None:
    """Infer an Assembly term from one exact seven-digit official bill number."""

    if re.fullmatch(r"\d{7}", bill_no) is None:
        return None
    term = int(bill_no[:2])
    try:
        official_assembly_term(term)
    except ValueError:
        return None
    return term


def _selected_assembly_term(
    *,
    default_term: int,
    query: str,
    explicit_term: Any = None,
    date_from: str | None = None,
    date_to: str | None = None,
    as_of: date,
) -> int:
    """Choose one term for the legacy live path without pretending to search many."""

    default = official_assembly_term(int(default_term)).number
    explicit = (
        official_assembly_term(int(explicit_term)).number
        if explicit_term is not None
        else None
    )
    bill_terms = {
        term
        for bill_no in extract_bill_numbers(query)
        if (term := _bill_assembly_term(bill_no)) is not None
    }
    if len(bill_terms) > 1:
        raise ValueError(
            "legacy live search supports one Assembly term; use start_research for multiple terms"
        )
    bill_term = next(iter(bill_terms), None)
    if explicit is not None and bill_term is not None and explicit != bill_term:
        raise ValueError("assembly_term conflicts with the exact bill number")
    if explicit is not None:
        _validate_date_scope_intersects_term(explicit, date_from, date_to)
        return explicit
    if bill_term is not None:
        return bill_term

    scoped_term = _single_assembly_term_for_dates(
        query=query,
        date_from=date_from,
        date_to=date_to,
        as_of=as_of,
    )
    return scoped_term or default


def _single_assembly_term_for_dates(
    *,
    query: str,
    date_from: str | None,
    date_to: str | None,
    as_of: date,
) -> int | None:
    months = _requested_months(query, date_from, date_to)
    if not months:
        return None
    start = date.fromisoformat(f"{min(months)}-01")
    end = _month_end_date(max(months))
    if date_from and (value := _date_value(date_from)) is not None:
        start = value
    if date_to and (value := _date_value(date_to)) is not None:
        end = value
    elif any(term in query for term in ("현재까지", "지금까지")):
        end = as_of
    terms = assembly_terms_intersecting(start, end)
    if len(terms) == 1:
        return terms[0].number
    if not terms:
        raise ValueError("the requested dates do not fall within an elected Assembly term")
    raise ValueError(
        "legacy live search supports one Assembly term; use start_research for a multi-term range"
    )


def _validate_date_scope_intersects_term(
    term: int, date_from: str | None, date_to: str | None
) -> None:
    metadata = official_assembly_term(term)
    start = _date_value(date_from) if date_from else None
    end = _date_value(date_to) if date_to else None
    if start is not None and end is not None and start > end:
        raise ValueError("date_from must be on or before date_to")
    if end is not None and end < metadata.date_from:
        raise ValueError("the requested dates do not intersect assembly_term")
    if start is not None and start > metadata.date_to:
        raise ValueError("the requested dates do not intersect assembly_term")


def _date_value(value: str) -> date | None:
    match = re.match(
        r"^\s*((?:19|20)\d{2})[-./년 ]+\s*(1[0-2]|0?[1-9])"
        r"(?:[-./월 ]+\s*(3[01]|[12]\d|0?[1-9]))?",
        value,
    )
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3) or 1))
    except ValueError:
        return None


def _month_intersects_term(month: str, date_from: date, date_to: date) -> bool:
    start = date.fromisoformat(f"{month}-01")
    end = _month_end_date(month)
    return start <= date_to and end >= date_from


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
    # Rank minutes by the same compact topic vocabulary used for bill
    # discovery. Instruction words such as "정리해줘" or generic "법안" must
    # not displace an AI-titled committee agenda.
    score = sum(term.casefold() in haystack for term in _bill_queries(query))
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
