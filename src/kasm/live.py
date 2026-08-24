"""Live-first Open Assembly research with a bounded local evidence cache."""

from __future__ import annotations

import hashlib
import os
import re
import time
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
from kasm.adapters.korea.parser import PARSER_VERSION
from kasm.adapters.korea.pipeline import OpenAssemblyPipeline, distinct_minutes_rows
from kasm.adapters.korea.sources import DATASET_BY_SOURCE, MeetingSource, classify_meeting
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
from kasm.research.request_scope import requested_stages
from kasm.search.lexical import query_terms
from kasm.search.measure_aliases import MeasureAliasHint, resolve_measure_alias
from kasm.search.terminology import LEGAL_TERMINOLOGY
from kasm.storage.database import Database
from kasm.storage.repositories import BillDocumentRepository, MeetingRepository

_DATE_MONTH = re.compile(r"(?P<year>(?:19|20)\d{2})[.\-/년 ]+\s*(?P<month>1[0-2]|0?[1-9])")
_DATE_YEAR = re.compile(r"(?<!\d)(?P<year>(?:19|20)\d{2})(?!\d)")
_PROPOSAL_YEAR = re.compile(r"(?<!\d)(?P<year>(?:19|20)\d{2})\s*년(?:도)?(?:에)?\s*발의")
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
_TARGETED_MINUTES_LIMIT = 6
_TARGETED_SPEECH_LIMIT = 36
_EXACT_BILL_NUMBER = re.compile(r"(?<!\d)(\d{7})(?!\d)")
_AGENDA_ITEM_TITLE_NUMBER = re.compile(r"^\s*(\d+)\.")
_AGENDA_ITEM_REFERENCE = re.compile(r"의사일정\s*제\s*(\d+)\s*항")
_AGENDA_ITEM_RANGE = re.compile(
    r"의사일정\s*제\s*(\d+)\s*항\s*부터\s*"
    r"(?:의사일정\s*)?제\s*(\d+)\s*항\s*까지"
)
_OUTCOME_END = re.compile(r"가결되었음을\s*선포합니다[.。]?")
_NEXT_BILL_PARAGRAPH = re.compile(
    r"(?m)^\s*(?:다음은\s*)?[가-힣·][가-힣·\s]{1,90}?"
    r"(?:법률|법)\s*(?:일부개정)?법률안"
)
_LIVE_BILL_INVENTORY_LIMIT = 50
_LIVE_MEETING_INVENTORY_LIMIT = 50
_TARGETED_DEADLINE_SECONDS = 120.0
_STRUCTURAL_SPEAKER_LABELS = frozenset(
    {"소위", "소위원회", "의안", "안건", "보고", "심사경과", "심사경과보고"}
)


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
        targeted_deadline_seconds: float = _TARGETED_DEADLINE_SECONDS,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
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
        self.targeted_deadline_seconds = max(1.0, float(targeted_deadline_seconds))
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
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
            date_from=filters.get("date_from"),
            date_to=filters.get("date_to"),
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
            result["document_coverage"] = self._bill_document_coverage(bill_no, result)
        return result

    def list_meetings(self, **filters: Any) -> list[dict[str, Any]]:
        date_from = filters.get("date_from")
        date_to = filters.get("date_to")
        term = self._selected_assembly_term("", filters)
        months = self._months_for_query("", date_from, date_to, assembly_term=term)
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
        committee: str | None = None,
    ) -> dict[str, Any]:
        measure_hint = resolve_measure_alias(query)
        targeted_deadline_at = (
            self._monotonic() + self.targeted_deadline_seconds if measure_hint is not None else None
        )
        term = self._hydrate_issue(
            query,
            {
                "limit": limit,
                "date_from": date_from,
                "date_to": date_to,
                "minutes_offset": minutes_offset,
                "assembly_term": assembly_term,
                "committee": committee,
                "targeted_deadline_at": targeted_deadline_at,
            },
        )
        if measure_hint is not None and measure_hint.assembly_term != term:
            measure_hint = None
        proposal_scope = _proposal_date_scope(query)
        local_limit = (
            max(limit, _BOUNDED_PROPOSAL_DISCOVERY_LIMIT) if proposal_scope is not None else limit
        )
        local_date_from = date_from
        local_date_to = date_to
        if proposal_scope is not None and date_from is None and date_to is None:
            local_date_from = proposal_scope[0].isoformat()
            local_date_to = min(
                proposal_scope[1],
                self._now().date(),
            ).isoformat()
        evidence_query = measure_hint.evidence_query if measure_hint is not None else query
        local_options: dict[str, Any] = {
            "date_from": local_date_from,
            "date_to": local_date_to,
            "assembly_term": term,
        }
        # The exact-measure refresh already applies the committee to committee
        # metadata. Do not apply it to local evidence search or plenary turns vanish.
        local_committee = committee
        if local_committee is not None:
            local_options["committee"] = local_committee
        result = self.local.explore_issue(
            evidence_query,
            local_limit,
            **local_options,
        )
        result["query"] = query
        if measure_hint is not None:
            result["evidence_query"] = evidence_query
            _filter_issue_to_measure_family(
                result,
                measure_hint.bill_numbers,
                {str(item.get("meeting_id") or "") for item in self._latest_meeting_inventory},
            )
            self._replace_with_attributed_measure_evidence(
                result,
                measure_hint,
                requested_stage_names=requested_stages(query),
                limit=limit,
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
            "bill_candidates": _bounded_inventory_page(
                self._latest_bill_inventory,
                limit=_LIVE_BILL_INVENTORY_LIMIT,
            ),
            "meeting_candidates": _bounded_inventory_page(
                self._latest_meeting_inventory,
                limit=_LIVE_MEETING_INVENTORY_LIMIT,
            ),
            "speech_candidates": cached_inventory.get("speech_candidates")
            or {"complete": True, "total": 0, "items": []},
            "links": cached_inventory.get("links") or {"complete": True, "total": 0, "items": []},
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
        if measure_hint is not None:
            resolution = measure_hint.public_payload()
            verified_bill_numbers = sorted(
                {
                    str(item.get("bill_no") or "")
                    for item in self._latest_bill_inventory
                    if item.get("bill_no")
                    and item.get("verification_state") == "official_api_matched"
                }
            )
            verified_meeting_bill_numbers = sorted(
                {
                    str(number)
                    for item in self._latest_meeting_inventory
                    for number in item.get("related_bill_numbers", [])
                    if number
                }
            )
            primary = measure_hint.primary_vehicle_bill_no
            bill_verified = primary in verified_bill_numbers
            agenda_verified = primary in verified_meeting_bill_numbers
            if bill_verified and agenda_verified:
                confidence = "official_bill_and_agenda_identifiers_matched"
            elif bill_verified:
                confidence = "official_bill_matched_vehicle_agenda_pending"
            elif agenda_verified:
                confidence = "official_agenda_matched_bill_metadata_pending"
            else:
                confidence = "retrieval_hint_pending_live_verification"
            resolution.update(
                {
                    "live_verified_bill_numbers": verified_bill_numbers,
                    "meeting_verified_bill_numbers": verified_meeting_bill_numbers,
                    "confidence": confidence,
                }
            )
            result["target_resolution"] = resolution
        result["stage_coverage"] = _issue_stage_coverage(
            query,
            result,
            self._latest_meeting_inventory,
            self.last_refresh,
        )
        result["quality"] = issue_quality(result)
        return result

    def _replace_with_attributed_measure_evidence(
        self,
        payload: dict[str, Any],
        hint: MeasureAliasHint,
        *,
        requested_stage_names: tuple[str, ...],
        limit: int,
    ) -> None:
        """Replace broad lexical hits with bill-attributed, stage-balanced turns."""

        meeting_type_by_id = {
            str(item.get("meeting_id") or ""): str(item.get("meeting_type") or "")
            for item in self._latest_meeting_inventory
            if item.get("meeting_id")
        }
        meeting_inventory_by_id = {
            str(item.get("meeting_id") or ""): item
            for item in self._latest_meeting_inventory
            if item.get("meeting_id")
        }
        target_agenda_numbers_by_meeting = {
            meeting_id: _target_agenda_numbers(item, exact_numbers=set(hint.bill_numbers))
            for meeting_id, item in meeting_inventory_by_id.items()
        }
        meeting_ids = tuple(meeting_type_by_id)
        if not meeting_ids:
            payload["speeches"] = []
            payload["discussion_threads"] = []
            payload["links"] = []
            return
        placeholders = ",".join("?" for _ in meeting_ids)
        rows = self.database.connection.execute(
            f"""SELECT s.*, m.title AS meeting, m.committee_name_ko AS committee,
                       m.date, m.meeting_type, m.source_url AS official_source
                FROM speeches s JOIN meetings m ON m.id = s.meeting_id
                WHERE s.meeting_id IN ({placeholders})
                ORDER BY m.date, s.sequence, s.id""",
            meeting_ids,
        ).fetchall()
        bill_rows = self.database.connection.execute(
            f"""SELECT id, bill_no FROM bills
                WHERE bill_no IN ({",".join("?" for _ in hint.bill_numbers)})""",
            hint.bill_numbers,
        ).fetchall()
        bill_number_by_id = {str(row["id"]): str(row["bill_no"]) for row in bill_rows}
        linked_numbers_by_speech: dict[str, set[str]] = {}
        if bill_number_by_id:
            bill_ids = tuple(bill_number_by_id)
            link_rows = self.database.connection.execute(
                f"""SELECT speech_id, bill_id FROM speech_bill_links
                    WHERE bill_id IN ({",".join("?" for _ in bill_ids)})""",
                bill_ids,
            ).fetchall()
            for link in link_rows:
                linked_numbers_by_speech.setdefault(str(link["speech_id"]), set()).add(
                    bill_number_by_id[str(link["bill_id"])]
                )

        attributed: list[dict[str, Any]] = []
        exact_numbers = set(hint.bill_numbers)
        informative_anchors = tuple(term for term in hint.evidence_terms if term not in {"약사법"})
        query_tokens = query_terms(payload.get("query") or hint.evidence_query)
        anchored_agenda_numbers: dict[tuple[str, str], set[str]] = {}
        for raw in rows:
            row = dict(raw)
            agenda = str(row.get("agenda") or "").strip()
            if not agenda or agenda.startswith("복수 의사일정"):
                continue
            observed_all = set(
                _EXACT_BILL_NUMBER.findall(f"{agenda}\n{str(row.get('text') or '')}")
            )
            observed = observed_all.intersection(exact_numbers)
            if observed and not observed_all.difference(exact_numbers):
                anchored_agenda_numbers.setdefault(
                    (str(row.get("meeting_id") or ""), agenda), set()
                ).update(observed)
        discussion_segment_rows = _measure_discussion_segment_rows(
            rows,
            exact_numbers=exact_numbers,
            linked_numbers_by_speech=linked_numbers_by_speech,
            hint=hint,
            target_agenda_numbers_by_meeting=target_agenda_numbers_by_meeting,
        )
        for raw in rows:
            row = dict(raw)
            speech_id = str(row.get("id") or "")
            meeting_id = str(row.get("meeting_id") or "")
            agenda = str(row.get("agenda") or "")
            text = str(row.get("text") or "")
            speaker_name = str(row.get("speaker_name") or "").strip()
            if _is_structural_speaker_label(speaker_name):
                continue
            haystack = f"{agenda}\n{text}"
            observed_numbers = set(_EXACT_BILL_NUMBER.findall(haystack))
            linked_numbers = linked_numbers_by_speech.get(speech_id, set())
            matched_anchors = [
                anchor
                for anchor in informative_anchors
                if _normalized_phrase_present(anchor, haystack)
            ]
            exact_observed = observed_numbers.intersection(exact_numbers)
            if exact_observed and not observed_numbers.difference(exact_numbers):
                attribution_state = "exact_bill_number_in_turn_or_agenda"
                attributed_numbers = sorted(exact_observed)
                base_score = 100
            elif observed_numbers:
                # An explicit different agenda identifier overrides fuzzy topic
                # similarity inside a multi-agenda meeting.
                continue
            elif linked_numbers:
                attribution_state = "exact_speech_bill_link"
                attributed_numbers = sorted(linked_numbers)
                base_score = 90
            elif (meeting_id, agenda.strip()) in anchored_agenda_numbers:
                attribution_state = "exact_agenda_segment_context"
                attributed_numbers = sorted(anchored_agenda_numbers[(meeting_id, agenda.strip())])
                base_score = 84
            elif speech_id in discussion_segment_rows:
                segment_kind = discussion_segment_rows[speech_id]
                if segment_kind == "short_context":
                    continue
                attribution_state = "exact_measure_discussion_segment"
                attributed_numbers = sorted(
                    set(
                        meeting_inventory_by_id.get(meeting_id, {}).get("related_bill_numbers", [])
                    ).intersection(exact_numbers)
                )
                if not attributed_numbers:
                    attributed_numbers = sorted(exact_numbers)
                base_score = {
                    "outcome": 94,
                    "anchor": 86,
                    "government_response": 82,
                    "bridge": 76,
                }.get(segment_kind, 76)
            else:
                continue
            item = dict(row)
            item["speech_id"] = item.pop("id")
            item["speaker"] = item.pop("speaker_name")
            scoping_segment_kind = discussion_segment_rows.get(speech_id)
            if scoping_segment_kind is not None:
                scoped_text = _scope_target_measure_turn_text(
                    text,
                    target_agenda_numbers=target_agenda_numbers_by_meeting.get(meeting_id, set()),
                    hint=hint,
                    segment_kind=scoping_segment_kind,
                )
                if scoped_text != text:
                    item["source_text_length"] = len(text)
                    item["text"] = scoped_text
                    item["text_scoped_to_target_agenda"] = True
            item["matched_terms"] = [
                term for term in query_tokens if term.casefold() in text.casefold()
            ]
            is_legislator = _is_legislator_role(str(item.get("speaker_role") or ""))
            item["attribution"] = {
                "state": attribution_state,
                "bill_numbers": attributed_numbers,
                "matched_measure_anchors": matched_anchors,
                "is_legislator": is_legislator,
            }
            if speech_id in discussion_segment_rows:
                item["attribution"]["segment_kind"] = discussion_segment_rows[speech_id]
            item["attribution_score"] = (
                base_score
                + len(matched_anchors) * 10
                + len(item["matched_terms"])
                + (3 if is_legislator else 0)
            )
            item["citation"] = {
                "official_url": item.get("official_source"),
                "source_locator": item.get("source_locator"),
                "meeting": item.get("meeting"),
                "date": item.get("date"),
                "speaker": item.get("speaker"),
            }
            attributed.append(item)

        attributed.sort(
            key=lambda item: (
                -int(item.get("attribution_score") or 0),
                str(item.get("date") or ""),
                int(item.get("sequence") or 0),
                str(item.get("speech_id") or ""),
            )
        )
        selected = _stage_balanced_speeches(
            attributed,
            meeting_type_by_id,
            requested_stage_names,
            limit=max(1, limit, min(_TARGETED_SPEECH_LIMIT, len(attributed))),
        )
        stage_order = {stage: index for index, stage in enumerate(requested_stage_names)}
        selected.sort(
            key=lambda item: (
                stage_order.get(
                    _stage_for_meeting_type(
                        meeting_type_by_id.get(str(item.get("meeting_id") or ""), "")
                    )
                    or "",
                    len(stage_order),
                ),
                str(item.get("date") or ""),
                int(item.get("sequence") or 0),
                str(item.get("speech_id") or ""),
            )
        )
        # Generic one-turn context has no agenda-boundary metadata. The
        # discussion threads below preserve only same-agenda context instead.
        payload["speeches"] = selected
        threads = self.local._discussion_threads(attributed)
        payload["discussion_threads"] = _bound_measure_threads(
            threads,
            selected,
            segment_context_ids={
                speech_id
                for speech_id, kind in discussion_segment_rows.items()
                if kind == "short_context"
            },
        )

        selected_speech_ids = {str(item.get("speech_id") or "") for item in selected}
        selected_bill_ids = set(bill_number_by_id)
        links: list[dict[str, Any]] = []
        if selected_bill_ids and selected_speech_ids:
            bill_ids = tuple(sorted(selected_bill_ids))
            speech_ids = tuple(sorted(selected_speech_ids))
            links = [
                dict(row)
                for row in self.database.connection.execute(
                    f"""SELECT * FROM speech_bill_links
                        WHERE bill_id IN ({",".join("?" for _ in bill_ids)})
                          AND speech_id IN ({",".join("?" for _ in speech_ids)})
                        ORDER BY confidence DESC, bill_id, speech_id, relation_type""",
                    (*bill_ids, *speech_ids),
                ).fetchall()
            ]
        payload["links"] = links[:_LIVE_MEETING_INVENTORY_LIMIT]
        raw_inventory = payload.get("scope_inventory")
        inventory = raw_inventory if isinstance(raw_inventory, dict) else {}
        inventory["speech_candidates"] = _bounded_inventory_page(
            [self.local._speech_inventory_item(item) for item in attributed],
            limit=_LIVE_MEETING_INVENTORY_LIMIT,
        )
        inventory["links"] = _bounded_inventory_page(
            links,
            limit=_LIVE_MEETING_INVENTORY_LIMIT,
        )
        payload["scope_inventory"] = inventory
        payload["timeline"] = self.local._issue_timeline(
            payload.get("bills", []), payload["discussion_threads"]
        )

    def _hydrate_issue(self, query: str, filters: dict[str, Any]) -> int:
        term = self._selected_assembly_term(query, filters)
        measure_hint = resolve_measure_alias(query)
        if measure_hint is not None and measure_hint.assembly_term != term:
            measure_hint = None
        committee = (
            filters.get("committee")
            or (measure_hint.committee if measure_hint is not None else None)
            or infer_issue_committee(query)
        )
        date_from = filters.get("date_from")
        date_to = filters.get("date_to")
        bills = self._refresh_bills(
            query=query,
            assembly_term=term,
            include_documents=False,
            deadline_at=filters.get("targeted_deadline_at"),
            date_from=date_from,
            date_to=date_to,
        )
        months = self._months_for_query(query, date_from, date_to, assembly_term=term)
        requested_months = _requested_months(query, date_from, date_to)
        if measure_hint is not None and not requested_months:
            # Search exact bill numbers from their proposal month through the
            # known processing milestones.  This stays bounded without silently
            # dropping an earlier exact-number committee record.
            months = {
                *measure_hint.milestone_months,
                *(
                    identity.proposed_at[:7]
                    for identity in measure_hint.identities
                    if re.fullmatch(r"(?:19|20)\d{2}-\d{2}-\d{2}", identity.proposed_at)
                ),
            }
        initial_months = set(months)
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
            value for bill in bills if (value := _value(bill, "COMMITTEE", "COMMITTEE_NM"))
        }
        if committee is None and len(bill_committees) == 1:
            committee = next(iter(bill_committees))
        if not explicit_temporal_scope and measure_hint is None:
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
        elif measure_hint is not None:
            scope_mode = "targeted_measure_milestones"
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
            measure_hint=measure_hint,
            requested_stage_names=requested_stages(query),
            deadline_at=filters.get("targeted_deadline_at"),
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

    def _selected_assembly_term(self, query: str, filters: dict[str, Any]) -> int:
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
        deadline_at: float | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        queries = _bill_queries(query)
        measure_hint = resolve_measure_alias(query)
        if measure_hint is not None and measure_hint.assembly_term != assembly_term:
            measure_hint = None
        requested_bill_numbers = extract_bill_numbers(query)
        bill_numbers = requested_bill_numbers or (
            list(measure_hint.bill_numbers) if measure_hint is not None else []
        )
        rows: list[dict[str, Any]] = []
        hashes: list[str] = []
        recovered_status_by_number: dict[str, dict[str, Any]] = {}
        for bill_no in bill_numbers:
            if self._deadline_expired(deadline_at):
                break
            fetched_rows, source_hashes = self._fetch_complete(
                BILL_DATASET,
                page_size=10,
                parameters={"AGE": assembly_term, "BILL_NO": bill_no},
            )
            exact_rows = [row for row in fetched_rows if _value(row, "BILL_NO") == bill_no]
            rows.extend(exact_rows)
            hashes.extend(source_hashes)
            if exact_rows:
                continue
            # Committee alternatives can appear in the official status feed even
            # when the main proposal endpoint returns no row. Recover that exact
            # vehicle instead of silently leaving only a nickname-registry hint.
            if self._deadline_expired(deadline_at):
                break
            status_rows, status_hashes = self._fetch_complete(
                BILL_STATUS_DATASET,
                page_size=10,
                parameters={"AGE": assembly_term, "BILL_NO": bill_no},
            )
            exact_status_rows = [row for row in status_rows if _value(row, "BILL_NO") == bill_no]
            if exact_status_rows:
                recovered = exact_status_rows[0]
                rows.append(recovered)
                recovered_status_by_number[bill_no] = recovered
            hashes.extend(status_hashes)
        if not bill_numbers:
            for candidate in queries:
                if self._deadline_expired(deadline_at):
                    break
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
        rows = _filter_bills_by_temporal_scope(
            rows,
            date_from=date_from,
            date_to=date_to,
        )
        if rows:
            source_hash = hashlib.sha256("".join(hashes).encode()).hexdigest()
            ingest_bill_rows(self.database, rows, source_hash=source_hash)
            status_targets = rows if bill_numbers or len(rows) == 1 else []
            for row in status_targets:
                refreshed_bill_no = _value(row, "BILL_NO")
                status_row = (
                    recovered_status_by_number.get(refreshed_bill_no)
                    or self._refresh_bill_status(
                        refreshed_bill_no,
                        assembly_term=assembly_term,
                        deadline_at=deadline_at,
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
        if measure_hint is not None:
            observed = {str(item.get("bill_no") or "") for item in self._latest_bill_inventory}
            for identity in measure_hint.identities:
                if identity.bill_no in observed:
                    continue
                self._latest_bill_inventory.append(
                    {
                        "bill_no": identity.bill_no,
                        "bill_id": None,
                        "name": identity.name,
                        "committee": measure_hint.committee,
                        "proposed_at": identity.proposed_at,
                        "process_result": None,
                        "official_url": identity.official_url,
                        "role": identity.role,
                        "verification_state": "registry_hint_official_metadata_not_returned",
                    }
                )
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
                status_row = self._refresh_bill_by_number(bill_no, assembly_term=term)
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
            refreshed["document_coverage"] = self._bill_document_coverage(bill_no, refreshed)
            hydrated.append(refreshed)
        return hydrated

    def _bill_document_coverage(self, bill_no: str, bill: dict[str, Any]) -> dict[str, Any]:
        known = self._document_refresh.get(bill_no)
        if known is not None:
            return known
        document_count = len([item for item in bill.get("documents", []) if isinstance(item, dict)])
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
                document for document in bill.get("documents", []) if isinstance(document, dict)
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
        self,
        bill_no: str,
        *,
        assembly_term: int | None = None,
        deadline_at: float | None = None,
    ) -> dict[str, Any] | None:
        if self._deadline_expired(deadline_at):
            return None
        term = _bill_assembly_term(bill_no) or assembly_term or self.assembly_term
        page = self.client.fetch_page(
            BILL_STATUS_DATASET,
            page_size=100,
            parameters={"AGE": term, "BILL_NO": bill_no},
        )
        exact_rows = [row for row in page.rows if _value(row, "BILL_NO") == bill_no]
        if exact_rows:
            ingest_bill_rows(self.database, exact_rows, source_hash=page.source_hash)
            return exact_rows[0]
        return None

    def _deadline_expired(self, deadline_at: float | None) -> bool:
        return deadline_at is not None and self._monotonic() >= deadline_at

    def _refresh_bill_by_number(
        self, bill_no: str, *, assembly_term: int | None = None
    ) -> dict[str, Any] | None:
        term = _bill_assembly_term(bill_no) or assembly_term or self.assembly_term
        page = self.client.fetch_page(
            BILL_DATASET,
            page_size=10,
            parameters={"AGE": term, "BILL_NO": bill_no},
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
            document_id = (
                "kna:bill-document:" + hashlib.sha256(link.official_url.encode()).hexdigest()[:24]
            )
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
        measure_hint: MeasureAliasHint | None = None,
        requested_stage_names: Iterable[str] = (),
        deadline_at: float | None = None,
    ) -> None:
        term = assembly_term or self.assembly_term
        official_assembly_term(term)
        requested_stage_names = tuple(requested_stage_names)
        rows: list[dict[str, Any]] = []
        api_calls = 0
        deadline_exceeded = self._deadline_expired(deadline_at)
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
        exact_numbers = measure_hint.bill_numbers if measure_hint is not None else ()
        effective_queried_months = queried_months
        if measure_hint is not None and queried_months:
            effective_queried_months = sorted(
                _month_span(queried_months[0], _month_end_date(queried_months[-1]))
            )
            scope.update(_temporal_window(effective_queried_months))
            scope.update(
                {
                    "mode": "targeted_measure_exact_bill_years",
                    "query_granularity": "calendar_year_with_exact_bill_number",
                    "query_marker_months": queried_months,
                }
            )
        if measure_hint is not None:
            query_specs = _targeted_measure_meeting_queries(
                measure_hint,
                queried_months,
                requested_stage_names,
            )
            scope["exact_years_queried"] = sorted(
                {date_query[:4] for _source, date_query, _bill_no in query_specs}
            )
            scope["exact_query_count"] = len(query_specs)
            for source, date_query, bill_no in query_specs:
                if self._deadline_expired(deadline_at):
                    deadline_exceeded = True
                    break
                parameters: dict[str, str | int] = {
                    "DAE_NUM": term,
                    "CONF_DATE": date_query,
                    "SUB_NAME": bill_no,
                }
                # SUB_NAME is an exact bill-number constraint.  Adding the
                # originating committee here would hide later referrals to a
                # different committee and make the processing path incomplete.
                fetched_rows, _source_hashes = self._fetch_complete(
                    DATASET_BY_SOURCE[source], page_size=1000, parameters=parameters
                )
                api_calls += 1
                rows.extend(
                    row for row in fetched_rows if _row_mentions_exact_bill(row, (bill_no,))
                )
                deadline_exceeded = deadline_exceeded or self._deadline_expired(deadline_at)
        else:
            for date_query in _meeting_date_queries(
                queried_months,
                as_of=self._now().date(),
            ):
                for source in (MeetingSource.COMMITTEE, MeetingSource.PLENARY):
                    if self._deadline_expired(deadline_at):
                        deadline_exceeded = True
                        break
                    parameters = {
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
                if deadline_exceeded:
                    break
            subcommittee_parameters: dict[str, str | int] = {"ERACO": f"제{term}대"}
            if committee:
                subcommittee_parameters["CMIT_NM"] = committee
            if not self._deadline_expired(deadline_at):
                subcommittee_rows, _subcommittee_hashes = self._fetch_complete(
                    DATASET_BY_SOURCE[MeetingSource.SUBCOMMITTEE],
                    page_size=1000,
                    parameters=subcommittee_parameters,
                )
                api_calls += 1
                rows.extend(subcommittee_rows)
            else:
                deadline_exceeded = True
        rows = _filter_meeting_rows_by_scope(rows, scope, effective_queried_months)
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
        self._latest_meeting_inventory = _meeting_inventory(self.database, candidates)
        if not ingest_minutes:
            self.last_refresh = {
                "meeting_api_calls": api_calls,
                "meeting_candidates": len(candidates),
                "targeted_measure": measure_hint is not None,
                "exact_bill_numbers": list(exact_numbers),
                "minutes_ingested": 0,
                "minutes_failures": 0,
                "deadline_seconds": (
                    self.targeted_deadline_seconds if measure_hint is not None else None
                ),
                "deadline_exceeded": deadline_exceeded,
                "months_queried": effective_queried_months,
                "query_marker_months": queried_months,
                "temporal_scope": scope,
                "candidate_offset": 0,
                "next_minutes_offset": None,
                "has_more": False,
            }
            return
        ingested = 0
        cache_reused = 0
        failures = 0
        attempted = 0
        effective_minutes_limit = self.max_minutes_per_request
        if measure_hint is not None:
            stage_count = len(tuple(dict.fromkeys(requested_stage_names))) or 3
            targeted_limit = min(
                _TARGETED_MINUTES_LIMIT,
                max(self.max_minutes_per_request, stage_count),
            )
            effective_minutes_limit = targeted_limit
            ordered_candidates = _stage_balanced_meeting_rows(
                candidates,
                requested_stage_names,
                limit=len(candidates),
            )
            window = ordered_candidates[candidate_offset : candidate_offset + targeted_limit]
        else:
            window = candidates[candidate_offset : candidate_offset + self.max_minutes_per_request]
        bounded_core_urls = [
            value for row in window if (value := _optional_minutes_url(row)) is not None
        ]
        cached_current_urls = {
            str(item.get("official_url") or "")
            for item in self._latest_meeting_inventory
            if item.get("current_parser_complete") is True
        }
        for row in window:
            if self._deadline_expired(deadline_at):
                deadline_exceeded = True
                break
            attempted += 1
            try:
                official_url = OpenAssemblyPipeline.minutes_url(row)
            except ValueError:
                official_url = ""
            if official_url and official_url in cached_current_urls:
                cache_reused += 1
                self._minutes_failed_urls.discard(official_url)
                continue
            try:
                self.pipeline.sync(row)
            except (OSError, RuntimeError, ValueError):
                failures += 1
                if official_url:
                    self._minutes_failed_urls.add(official_url)
                deadline_exceeded = deadline_exceeded or self._deadline_expired(deadline_at)
                continue
            if official_url:
                self._minutes_failed_urls.discard(official_url)
            ingested += 1
            deadline_exceeded = deadline_exceeded or self._deadline_expired(deadline_at)
            if deadline_exceeded:
                break
        self._latest_meeting_inventory = _meeting_inventory(self.database, candidates)
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
            "targeted_measure": measure_hint is not None,
            "exact_bill_numbers": list(exact_numbers),
            "bounded_core_candidates": len(window),
            "bounded_core_attempted": attempted,
            "bounded_core_official_urls": bounded_core_urls,
            "attempted_candidate_count": min(len(candidates), next_offset),
            "checked_candidate_count": sum(
                item.get("full_text_loaded") is True for item in self._latest_meeting_inventory
            ),
            "unselected_candidate_count": max(0, len(candidates) - next_offset),
            "deadline_seconds": (
                self.targeted_deadline_seconds if measure_hint is not None else None
            ),
            "deadline_exceeded": deadline_exceeded,
            "deadline_skipped_count": max(0, len(window) - attempted),
            "minutes_ingested": ingested,
            "minutes_cache_reused": cache_reused,
            "minutes_failures": len(failed_urls),
            "minutes_failures_in_window": failures,
            "failed_official_urls": failed_urls,
            "minutes_limit": effective_minutes_limit,
            "months_queried": effective_queried_months,
            "query_marker_months": queried_months,
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
            month for month in months if _month_intersects_term(month, term.date_from, term.date_to)
        }


def create_live_services(
    *,
    api_key: str | None = None,
    data_dir: str | Path | None = None,
    client: AssemblyOpenApiClient | None = None,
    max_minutes_per_request: int = 20,
    source_timeout: float = 20.0,
) -> ServiceContext:
    """Create the default user-keyed live service and its private local cache."""
    root = Path(
        data_dir or os.getenv("KBD_DATA_DIR") or Path.home() / ".local/share/korean-bill-debate-mcp"
    )
    root.mkdir(parents=True, exist_ok=True)
    database = Database(root / "cache.sqlite3")
    database.initialize()
    api_client = client or AssemblyOpenApiClient(
        api_key,
        cache_dir=root / "api-cache",
        timeout=min(source_timeout, 15.0),
    )
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
    # A high-signal named statute is already the narrowest official BILL_NAME
    # key. Related-concept expansion here only multiplies API calls and admits
    # unrelated bills before exact identity resolution.
    if inferred:
        return [inferred]
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
    terms = [term for term in query_terms(query) if _is_bill_query_term(term)]
    candidates = [
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


def _filter_bills_by_temporal_scope(
    bills: Iterable[dict[str, Any]],
    *,
    date_from: str | None,
    date_to: str | None,
) -> list[dict[str, Any]]:
    """Keep bills with at least one official lifecycle date in the request window."""

    lower = _date_value(date_from or "")
    upper = _date_value(date_to or "")
    values = list(bills)
    if lower is None and upper is None:
        return values
    date_fields = (
        "PROPOSE_DT",
        "PROC_DT",
        "CMT_PROC_DT",
        "COMMITTEE_PROC_DT",
        "LAW_PROC_DT",
        "RGS_PROC_DT",
        "ANNOUNCE_DT",
    )
    bounded: list[dict[str, Any]] = []
    for bill in values:
        dates = [
            parsed
            for field in date_fields
            if (parsed := _date_value(_value(bill, field) or "")) is not None
        ]
        if any(
            (lower is None or observed >= lower) and (upper is None or observed <= upper)
            for observed in dates
        ):
            bounded.append(bill)
    return bounded


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
        topical_score = int(relevance.get("score") or 0) if isinstance(relevance, dict) else 0
        discussion_count = len(speech_ids_by_bill.get(str(bill.get("id") or ""), set()))
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
                ((bill.get("importance") or {}).get("signals") or {}).get("linked_discussion_count")
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
        (bill for bill in payload.get("bills", []) if isinstance(bill, dict)),
        scope,
    )
    observed_links = [link for link in payload.get("links", []) if isinstance(link, dict)]
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

    speeches = [speech for speech in payload.get("speeches", []) if isinstance(speech, dict)][
        :requested_limit
    ]
    payload["speeches"] = speeches
    selected_speech_ids = {str(speech.get("speech_id") or "") for speech in speeches}
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
    bill_candidates = raw_bill_candidates if isinstance(raw_bill_candidates, dict) else {}
    candidate_items = _filter_bills_by_proposal_scope(
        (item for item in bill_candidates.get("items", []) if isinstance(item, dict)),
        scope,
    )
    selected_bill_ids = {str(bill.get("id") or "") for bill in selected}
    selected_bill_numbers = {str(bill.get("bill_no") or "") for bill in selected}
    importance_by_id = {str(bill.get("id") or ""): bill.get("importance") for bill in ranked_bills}
    importance_by_number = {
        str(bill.get("bill_no") or ""): bill.get("importance") for bill in ranked_bills
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

    allowed_bill_ids = {str(item.get("bill_id") or "") for item in candidate_items}
    allowed_bill_numbers = {str(item.get("bill_no") or "") for item in candidate_items}

    def allowed_link(link: Any) -> bool:
        if not isinstance(link, dict):
            return False
        bill_id = str(link.get("bill_id") or "")
        bill_no = str(link.get("bill_no") or "")
        return bill_id in allowed_bill_ids or bill_no in allowed_bill_numbers

    payload["links"] = [link for link in payload.get("links", []) if allowed_link(link)]
    raw_links = inventory.get("links")
    links_inventory = raw_links if isinstance(raw_links, dict) else {}
    link_items = [link for link in links_inventory.get("items", []) if allowed_link(link)]
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
            queries.extend(f"{year}-{month:02d}" for month in sorted(month_numbers))
    return queries


def _targeted_measure_meeting_queries(
    hint: MeasureAliasHint,
    months: Iterable[str],
    stage_names: Iterable[str],
) -> list[tuple[MeetingSource, str, str]]:
    """Build the smallest exact agenda query plan for a resolved measure family."""

    years = sorted(
        {value[:4] for value in months if re.fullmatch(r"(?:19|20)\d{2}-(?:1[0-2]|0[1-9])", value)}
    )
    if not years:
        return []
    requested = set(stage_names)
    if not requested:
        requested = {"subcommittee", "standing_committee", "plenary"}
    source_identity = next(
        (identity for identity in hint.identities if identity.role == "source_member_bill"),
        hint.identities[0],
    )
    primary_identity = hint.identity(hint.primary_vehicle_bill_no)
    source_year = source_identity.proposed_at[:4]
    primary_year = primary_identity.proposed_at[:4] if primary_identity else years[0]
    source_years = [year for year in years if source_year <= year <= primary_year] or [years[0]]
    milestone_years = sorted(
        {value[:4] for value in hint.milestone_months if value[:4] in years}
    )
    plenary_year = milestone_years[-1] if milestone_years else years[-1]
    result: list[tuple[MeetingSource, str, str]] = []
    if requested.intersection({"subcommittee", "standing_committee"}):
        # Deliberation is indexed under a member bill while the committee
        # alternative usually appears only after that deliberation. Querying
        # both numbers doubles calls and commonly adds an empty result.
        for year in source_years:
            result.append((MeetingSource.COMMITTEE, year, source_identity.bill_no))
    if "plenary" in requested:
        result.append(
            (
                MeetingSource.PLENARY,
                plenary_year,
                hint.primary_vehicle_bill_no,
            )
        )
    return list(dict.fromkeys(result))


def _row_mentions_exact_bill(row: dict[str, Any], bill_numbers: Iterable[str]) -> bool:
    expected = set(bill_numbers)
    if not expected:
        return False

    def values(value: Any) -> Iterable[str]:
        if isinstance(value, dict):
            for nested in value.values():
                yield from values(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                yield from values(nested)
        elif value is not None:
            yield str(value)

    observed = {match.group(1) for raw in values(row) for match in _EXACT_BILL_NUMBER.finditer(raw)}
    return bool(expected.intersection(observed))


def _stage_balanced_meeting_rows(
    rows: list[dict[str, Any]],
    stage_names: Iterable[str],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Select at most one core minute per requested stage before filling spare slots."""

    if limit <= 0:
        return []
    requested = list(dict.fromkeys(stage_names)) or [
        "subcommittee",
        "standing_committee",
        "plenary",
    ]
    meeting_type_by_stage = {
        "subcommittee": MeetingSource.SUBCOMMITTEE,
        "standing_committee": MeetingSource.COMMITTEE,
        "plenary": MeetingSource.PLENARY,
    }
    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    for stage in requested:
        source = meeting_type_by_stage.get(stage)
        if source is None:
            continue
        for row in rows:
            if id(row) in selected_ids or classify_meeting(row) is not source:
                continue
            selected.append(row)
            selected_ids.add(id(row))
            break
        if len(selected) >= limit:
            return selected
    for row in rows:
        if id(row) in selected_ids:
            continue
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def _normalized_phrase_present(phrase: str, text: str) -> bool:
    return re.sub(r"\s+", "", phrase).casefold() in re.sub(r"\s+", "", text).casefold()


def _is_structural_speaker_label(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    return compact in _STRUCTURAL_SPEAKER_LABELS or compact in {
        "위원장",
        "소위원장",
        "의장",
        "부의장",
    }


def _stage_for_meeting_type(meeting_type: str) -> str | None:
    return {
        MeetingSource.SUBCOMMITTEE.value: "subcommittee",
        MeetingSource.COMMITTEE.value: "standing_committee",
        MeetingSource.PLENARY.value: "plenary",
    }.get(meeting_type)


def _target_agenda_numbers(meeting: dict[str, Any], *, exact_numbers: set[str]) -> set[int]:
    """Return official agenda ordinals tied to the exact target bill family."""

    result: set[int] = set()
    raw_items = meeting.get("agenda_items")
    if not isinstance(raw_items, list):
        return result
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        bill_no = str(raw.get("bill_no") or "")
        if bill_no not in exact_numbers:
            continue
        match = _AGENDA_ITEM_TITLE_NUMBER.match(str(raw.get("title") or ""))
        if match is not None:
            result.add(int(match.group(1)))
    return result


def _referenced_agenda_numbers(text: str) -> set[int]:
    """Expand explicit ``의사일정 제N항`` references, including small ranges."""

    numbers = {int(value) for value in _AGENDA_ITEM_REFERENCE.findall(text)}
    for start_value, end_value in _AGENDA_ITEM_RANGE.findall(text):
        start, end = int(start_value), int(end_value)
        if start <= end and end - start <= 100:
            numbers.update(range(start, end + 1))
    return numbers


def _agenda_reference_spans(text: str) -> list[tuple[int, int, set[int]]]:
    """Return non-overlapping agenda references with expanded range members."""

    spans: list[tuple[int, int, set[int]]] = []
    range_bounds: list[tuple[int, int]] = []
    for match in _AGENDA_ITEM_RANGE.finditer(text):
        start_value, end_value = int(match.group(1)), int(match.group(2))
        numbers = {start_value, end_value}
        if start_value <= end_value and end_value - start_value <= 100:
            numbers = set(range(start_value, end_value + 1))
        spans.append((match.start(), match.end(), numbers))
        range_bounds.append((match.start(), match.end()))
    for match in _AGENDA_ITEM_REFERENCE.finditer(text):
        if any(start <= match.start() < end for start, end in range_bounds):
            continue
        spans.append((match.start(), match.end(), {int(match.group(1))}))
    return sorted(spans, key=lambda value: (value[0], value[1]))


def _phrase_pattern(value: str) -> re.Pattern[str]:
    compact = re.sub(r"\s+", "", value)
    return re.compile(r"\s*".join(re.escape(character) for character in compact))


def _agenda_references_form_named_range(
    text: str,
    current: tuple[int, int, set[int]],
    following: tuple[int, int, set[int]],
    *,
    following_boundary: int,
) -> bool:
    """Recognize ``제N항 <제명>부터 제M항 <제명>까지`` groupings.

    ``_AGENDA_ITEM_RANGE`` handles the compact form where the two references
    are adjacent.  Plenary minutes commonly insert each bill title between
    ``부터`` and ``까지``; treating the second reference as a new agenda block
    would leave only the first title in the cited excerpt.
    """

    _current_start, current_end, _current_numbers = current
    following_start, following_end, _following_numbers = following
    between = text[current_end:following_start]
    after_following = text[following_end:following_boundary]
    return bool(re.search(r"부터\s*$", between) and re.search(r"까지", after_following))


def _scope_target_measure_turn_text(
    text: str,
    *,
    target_agenda_numbers: set[int],
    hint: MeasureAliasHint,
    segment_kind: str,
) -> str:
    """Keep the verbatim target block inside a chair's multi-item speech."""

    if not text.strip():
        return text
    references = _agenda_reference_spans(text)
    target_index = next(
        (
            index
            for index, (_start, _end, numbers) in enumerate(references)
            if target_agenda_numbers.intersection(numbers)
        ),
        None,
    )
    start: int | None = None
    reference_end = 0
    if target_index is not None:
        start, reference_end, _numbers = references[target_index]
        if target_index > 0:
            previous_start, _previous_end, _previous_numbers = references[target_index - 1]
            preceding = re.sub(r"\s+", "", text[previous_start:start]).casefold()
            target_titles = {
                re.sub(r"\s+", "", identity.name).casefold()
                for identity in hint.identities
                if identity.name
            }
            if any(title in preceding for title in target_titles):
                start = previous_start
    else:
        alternative_patterns = [
            _phrase_pattern(identity.name)
            for identity in hint.identities
            if identity.name and identity.role == "committee_alternative_primary_vehicle"
        ]
        title_match = next(
            (
                match
                for pattern in alternative_patterns
                if (match := pattern.search(text)) is not None
            ),
            None,
        )
        if title_match is None:
            return text
        start = title_match.start()
        reference_end = title_match.end()

    end = len(text)
    if segment_kind == "outcome":
        outcome = _OUTCOME_END.search(text, reference_end)
        if outcome is not None:
            end = outcome.end()
    elif target_index is not None and target_index + 1 < len(references):
        boundary_index = target_index + 1
        while boundary_index < len(references):
            following_boundary = (
                references[boundary_index + 1][0]
                if boundary_index + 1 < len(references)
                else len(text)
            )
            if not _agenda_references_form_named_range(
                text,
                references[boundary_index - 1],
                references[boundary_index],
                following_boundary=following_boundary,
            ):
                break
            boundary_index += 1
        if boundary_index < len(references):
            end = references[boundary_index][0]
    else:
        next_bill = _NEXT_BILL_PARAGRAPH.search(text, reference_end)
        if next_bill is not None:
            end = next_bill.start()
    scoped = text[start:end].strip()
    return scoped or text


def _measure_discussion_segment_rows(
    rows: Iterable[Any],
    *,
    exact_numbers: set[str],
    linked_numbers_by_speech: dict[str, set[str]],
    hint: MeasureAliasHint,
    target_agenda_numbers_by_meeting: dict[str, set[int]] | None = None,
) -> dict[str, str]:
    """Find compact same-discussion runs without treating a whole PDF as one agenda.

    Exact Open Assembly agenda queries can return only the matching agenda row even
    though the PDF contains dozens of other items.  Meeting-level attribution is
    therefore unsafe.  Strong measure anchors establish short runs; substantive
    turns between nearby anchors are retained as bridge evidence.
    """

    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in rows:
        row = dict(raw)
        grouped.setdefault(str(row.get("meeting_id") or ""), []).append(row)

    alternative_titles = {
        re.sub(r"\s+", "", identity.name).casefold()
        for identity in hint.identities
        if identity.name and identity.role == "committee_alternative_primary_vehicle"
    }
    result: dict[str, str] = {}
    target_agenda_numbers_by_meeting = target_agenda_numbers_by_meeting or {}
    for meeting_id, meeting_rows in grouped.items():
        ordered = sorted(
            meeting_rows,
            key=lambda item: (
                int(item.get("sequence") or 0),
                str(item.get("id") or ""),
            ),
        )
        target_agenda_numbers = target_agenda_numbers_by_meeting.get(meeting_id, set())
        anchor_kinds: dict[int, str] = {}
        for position, row in enumerate(ordered):
            speech_id = str(row.get("id") or "")
            text = str(row.get("text") or "")
            agenda = str(row.get("agenda") or "")
            compact = re.sub(r"\s+", "", f"{agenda}\n{text}").casefold()
            text_compact = re.sub(r"\s+", "", text).casefold()
            observed_numbers = set(_EXACT_BILL_NUMBER.findall(compact))
            exact_identifier = bool(observed_numbers.intersection(exact_numbers))
            exact_link = bool(
                linked_numbers_by_speech.get(speech_id, set()).intersection(exact_numbers)
            )
            omnibus = (
                bool(observed_numbers.difference(exact_numbers))
                or compact.count("일부개정법률안") > 3
            )
            alternative_title_anchor = any(
                title and title in text_compact for title in alternative_titles
            )
            platform_anchor = any(
                term in compact for term in ("플랫폼", "비대면", "약국중개", "원격의료산업협의회")
            )
            wholesale_anchor = any(term in compact for term in ("의약품도매", "도매상", "도매업"))
            rebate_anchor = "리베이트" in compact
            alias_anchor = "닥터나우" in compact
            concept_count = sum((platform_anchor, wholesale_anchor, rebate_anchor))
            referenced_agenda_numbers = _referenced_agenda_numbers(f"{agenda}\n{text}")
            target_agenda_reference = bool(
                target_agenda_numbers.intersection(referenced_agenda_numbers)
            )
            procedural = any(
                term in text_compact
                for term in ("상정", "심사", "토론", "의결", "투표", "채택", "가결")
            )
            outcome = any(
                term in text_compact for term in ("투표결과", "가결되었음을선포", "대안으로채택")
            )
            safe_specific_anchor = (
                alias_anchor or concept_count >= 2
            ) and not observed_numbers.difference(exact_numbers)
            target_procedure_anchor = target_agenda_reference and procedural
            alternative_outcome_anchor = alternative_title_anchor and outcome
            if not (
                (exact_identifier and not omnibus)
                or (exact_link and not omnibus)
                or safe_specific_anchor
                or target_procedure_anchor
                or alternative_outcome_anchor
            ):
                continue
            anchor_kinds[position] = (
                "outcome"
                if outcome and (target_agenda_reference or alternative_title_anchor)
                else "anchor"
            )
        anchor_positions = sorted(anchor_kinds)
        if not anchor_positions:
            continue

        clusters: list[list[int]] = []
        for position in anchor_positions:
            sequence = int(ordered[position].get("sequence") or 0)
            if not clusters:
                clusters.append([position])
                continue
            previous_sequence = int(ordered[clusters[-1][-1]].get("sequence") or 0)
            if sequence - previous_sequence <= 8:
                clusters[-1].append(position)
            else:
                clusters.append([position])

        anchor_set = set(anchor_positions)
        for cluster in clusters:
            first, last = cluster[0], cluster[-1]
            end = last
            if last + 1 < len(ordered):
                next_role = str(ordered[last + 1].get("speaker_role") or "")
                if any(
                    role in next_role
                    for role in ("장관", "차관", "정부위원", "처장", "청장", "국장")
                ):
                    end += 1
            for position in range(first, end + 1):
                row = ordered[position]
                speech_id = str(row.get("id") or "")
                speaker = str(row.get("speaker_name") or "").strip()
                text = str(row.get("text") or "").strip()
                if not speech_id or _is_structural_speaker_label(speaker):
                    continue
                if position in anchor_set:
                    result[speech_id] = anchor_kinds[position]
                elif position == end and end > last:
                    result[speech_id] = (
                        "government_response"
                        if len(re.sub(r"\s+", "", text)) >= 24
                        else "short_context"
                    )
                elif len(re.sub(r"\s+", "", text)) < 24:
                    result[speech_id] = "short_context"
                else:
                    result[speech_id] = "bridge"
    return result


def _stage_balanced_speeches(
    speeches: list[dict[str, Any]],
    meeting_type_by_id: dict[str, str],
    stage_names: Iterable[str],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Reserve comparable evidence space for every requested legislative stage."""

    if limit <= 0:
        return []
    requested = list(dict.fromkeys(stage_names)) or [
        "subcommittee",
        "standing_committee",
        "plenary",
    ]
    type_by_stage = {
        "subcommittee": MeetingSource.SUBCOMMITTEE.value,
        "standing_committee": MeetingSource.COMMITTEE.value,
        "plenary": MeetingSource.PLENARY.value,
    }
    quota = max(1, limit // max(1, len(requested)))
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for stage in requested:
        meeting_type = type_by_stage.get(stage)
        if meeting_type is None:
            continue
        candidates = [
            speech
            for speech in speeches
            if meeting_type_by_id.get(str(speech.get("meeting_id") or "")) == meeting_type
        ]
        diverse: list[dict[str, Any]] = []
        seen_speakers: set[str] = set()
        for speech in candidates:
            speaker = str(speech.get("speaker") or "").strip()
            if not speaker or speaker in seen_speakers:
                continue
            diverse.append(speech)
            seen_speakers.add(speaker)
            if len(diverse) >= quota:
                break
        if len(diverse) < quota:
            diverse_ids = {str(item.get("speech_id") or "") for item in diverse}
            diverse.extend(
                speech
                for speech in candidates
                if str(speech.get("speech_id") or "") not in diverse_ids
            )
        for speech in diverse[:quota]:
            speech_id = str(speech.get("speech_id") or "")
            if speech_id in selected_ids:
                continue
            selected.append(speech)
            selected_ids.add(speech_id)
            if len(selected) >= limit:
                return selected
    for speech in speeches:
        speech_id = str(speech.get("speech_id") or "")
        if speech_id in selected_ids:
            continue
        selected.append(speech)
        selected_ids.add(speech_id)
        if len(selected) >= limit:
            break
    return selected


def _bound_measure_threads(
    threads: list[dict[str, Any]],
    attributed_speeches: list[dict[str, Any]],
    *,
    segment_context_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Keep direct turns plus context proven to sit inside a target segment."""

    attributed_by_id = {
        str(speech.get("speech_id") or ""): speech for speech in attributed_speeches
    }
    segment_context_ids = segment_context_ids or set()
    bounded: list[dict[str, Any]] = []
    for raw_thread in threads:
        thread = dict(raw_thread)
        matched_ids = [
            str(value)
            for value in thread.get("matched_speech_ids") or []
            if str(value) in attributed_by_id
        ]
        if not matched_ids:
            continue
        turns: list[dict[str, Any]] = []
        for raw_turn in thread.get("turns") or []:
            if not isinstance(raw_turn, dict):
                continue
            turn = dict(raw_turn)
            speech_id = str(turn.get("speech_id") or "")
            directly_attributed = attributed_by_id.get(speech_id)
            if directly_attributed is None and speech_id not in segment_context_ids:
                continue
            turn["attribution"] = (
                directly_attributed.get("attribution")
                if directly_attributed is not None
                else {
                    "state": "exact_measure_discussion_segment_context",
                    "segment_kind": "short_context",
                    "evidence_use": "context_only",
                    "instruction": (
                        "확인된 동일 의제 구간의 짧은 문답입니다. 앞뒤 직접 근거와 함께만 "
                        "사용하고 독립적인 법안 입장으로 쓰지 마세요."
                    ),
                }
            )
            turns.append(turn)
        if not turns:
            continue
        thread["matched_speech_ids"] = matched_ids
        thread["turns"] = turns
        thread["participants"] = list(
            dict.fromkeys(str(turn.get("speaker") or "") for turn in turns)
        )
        bounded.append(thread)
    return bounded


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
    months = [f"{match.group('year')}-{int(match.group('month')):02d}" for match in month_matches]
    for match in _DATE_YEAR.finditer(query):
        if any(
            match.start() >= month_match.start() and match.end() <= month_match.end()
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
        official_assembly_term(int(explicit_term)).number if explicit_term is not None else None
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
    compact = re.fullmatch(r"\s*((?:19|20)\d{6})\s*", value)
    if compact is not None:
        try:
            return date(
                int(compact.group(1)[:4]),
                int(compact.group(1)[4:6]),
                int(compact.group(1)[6:8]),
            )
        except ValueError:
            return None
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
    deadline_exceeded = refresh.get("deadline_exceeded") is True
    targeted_measure = refresh.get("targeted_measure") is True
    unselected_candidates = int(refresh.get("unselected_candidate_count") or 0)
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
            temporal_scope.get("queried_months") or refresh.get("months_queried") or ()
        )
    )
    window_complete = not has_more and failures == 0 and not deadline_exceeded
    # A resolved single-measure summary is complete for its bounded core when
    # every requested stage selection was checked. It is never relabelled as an
    # exhaustive candidate inventory; unselected exact-agenda rows stay visible.
    overall_complete = window_complete and (
        temporal_scope.get("explicit") is True or targeted_measure
    )
    return {
        "complete": overall_complete,
        "overall_complete": overall_complete,
        "window_complete": window_complete,
        "partial": not overall_complete,
        "window_partial": not window_complete,
        "completion_scope": ("bounded_targeted_core" if targeted_measure else "temporal_window"),
        "candidate_inventory_complete": unselected_candidates == 0,
        "unselected_candidate_count": unselected_candidates,
        "deadline_exceeded": deadline_exceeded,
        "temporal_scope": temporal_scope,
        "next_minutes_offset": next_offset,
        "failed_count": failures,
        "failed_official_urls": refresh.get("failed_official_urls") or [],
        "instruction": (
            "Call the same tool again with minutes_offset=" + str(next_offset)
            if has_more
            else (
                "The bounded request deadline was reached; disclose unchecked stages."
                if deadline_exceeded
                else (
                    "The meeting window is partial; disclose every failed official URL."
                    if failures
                    else (
                        "The exact measure's bounded stage core has been checked; do not "
                        "describe it as an exhaustive meeting inventory."
                        if targeted_measure and overall_complete
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
                )
            )
        ),
    }


def _optional_minutes_url(row: dict[str, Any]) -> str | None:
    try:
        return OpenAssemblyPipeline.minutes_url(row)
    except ValueError:
        return None


def _bounded_inventory_page(items: list[dict[str, Any]], *, limit: int) -> dict[str, Any]:
    total = len(items)
    returned = items[:limit]
    return {
        "complete": total <= limit,
        "total": total,
        "observed_total": total,
        "returned_count": len(returned),
        "truncated": total > limit,
        "selection": "ranked_prefix",
        "items": returned,
    }


def _bill_inventory_entry(row: dict[str, Any]) -> dict[str, Any]:
    bill_no = _value(row, "BILL_NO")
    bill_id = _value(row, "BILL_ID")
    official_url = _value(row, "DETAIL_LINK", "LINK_URL")
    if not official_url and bill_id:
        official_url = "https://likms.assembly.go.kr/bill/billDetail.do?" + urllib.parse.urlencode(
            {"billId": bill_id}
        )
    return {
        "bill_no": bill_no,
        "bill_id": bill_id,
        "name": _value(row, "BILL_NAME", "BILL_NM"),
        "committee": _value(row, "COMMITTEE", "COMMITTEE_NM"),
        "proposed_at": _value(row, "PROPOSE_DT"),
        "process_result": _value(row, "PROC_RESULT", "LAW_PROC_RESULT_CD"),
        "official_url": official_url,
        "verification_state": "official_api_matched",
    }


def _meeting_inventory(database: Database, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    speech_stats: dict[str, dict[str, Any]] = {}
    for row in database.connection.execute(
        """SELECT meeting_id, parser_version, count(*) AS speech_count
           FROM speeches GROUP BY meeting_id, parser_version"""
    ).fetchall():
        meeting_id = str(row["meeting_id"])
        stats = speech_stats.setdefault(meeting_id, {"speech_count": 0, "parser_versions": set()})
        stats["speech_count"] += int(row["speech_count"])
        if row["parser_version"]:
            stats["parser_versions"].add(str(row["parser_version"]))
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
        if not related_bill_numbers:
            related_bill_numbers = list(dict.fromkeys(_EXACT_BILL_NUMBER.findall(repr(row))))
        stats = speech_stats.get(meeting.id, {"speech_count": 0, "parser_versions": set()})
        speech_count = int(stats["speech_count"])
        parser_versions = sorted(stats["parser_versions"])
        items.append(
            {
                "meeting_id": meeting.id,
                "date": meeting.date.isoformat(),
                "title": meeting.title,
                "committee": meeting.committee_name_ko,
                "meeting_type": meeting.meeting_type,
                "related_bill_numbers": related_bill_numbers,
                "agenda_items": agendas,
                "agenda_item_count": len(agendas),
                "official_url": meeting.source_url,
                "full_text_loaded": (speech_count > 0 and parser_versions == [PARSER_VERSION]),
                "cached_speech_rows_present": speech_count > 0,
                "speech_count": speech_count,
                "parser_versions": parser_versions,
                "current_parser_complete": (
                    speech_count > 0 and parser_versions == [PARSER_VERSION]
                ),
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


def _filter_issue_to_measure_family(
    payload: dict[str, Any],
    bill_numbers: Iterable[str],
    meeting_ids: set[str],
) -> None:
    """Remove cache spillover after an exact nickname measure has been resolved."""

    numbers = set(bill_numbers)
    bills = [
        bill
        for bill in payload.get("bills", [])
        if isinstance(bill, dict) and str(bill.get("bill_no") or "") in numbers
    ]
    payload["bills"] = bills
    allowed_bill_ids = {str(bill.get("id") or f"kna:bill:{bill.get('bill_no')}") for bill in bills}
    allowed_bill_ids.update(f"kna:bill:{number}" for number in numbers)
    speeches = [
        speech
        for speech in payload.get("speeches", [])
        if isinstance(speech, dict) and str(speech.get("meeting_id") or "") in meeting_ids
    ]
    payload["speeches"] = speeches
    speech_ids = {str(speech.get("speech_id") or "") for speech in speeches}
    payload["discussion_threads"] = [
        thread
        for thread in payload.get("discussion_threads", [])
        if isinstance(thread, dict) and str(thread.get("meeting_id") or "") in meeting_ids
    ]
    payload["links"] = [
        link
        for link in payload.get("links", [])
        if isinstance(link, dict)
        and str(link.get("bill_id") or "") in allowed_bill_ids
        and str(link.get("speech_id") or "") in speech_ids
    ]
    payload["timeline"] = [
        event
        for event in payload.get("timeline", [])
        if isinstance(event, dict)
        and (
            str(event.get("bill_no") or "") in numbers
            or str(event.get("meeting_id") or "") in meeting_ids
        )
    ]
    raw_inventory = payload.get("scope_inventory")
    inventory = raw_inventory if isinstance(raw_inventory, dict) else {}
    raw_bills = inventory.get("bill_candidates")
    bill_inventory = raw_bills if isinstance(raw_bills, dict) else {}
    bill_items = [
        item
        for item in bill_inventory.get("items", [])
        if isinstance(item, dict) and str(item.get("bill_no") or "") in numbers
    ]
    bill_inventory.update({"items": bill_items, "total": len(bill_items)})
    raw_speeches = inventory.get("speech_candidates")
    speech_inventory = raw_speeches if isinstance(raw_speeches, dict) else {}
    speech_items = [
        item
        for item in speech_inventory.get("items", [])
        if isinstance(item, dict) and str(item.get("meeting_id") or "") in meeting_ids
    ]
    speech_inventory.update({"items": speech_items, "total": len(speech_items)})
    raw_links = inventory.get("links")
    link_inventory = raw_links if isinstance(raw_links, dict) else {}
    link_items = [
        item
        for item in link_inventory.get("items", [])
        if isinstance(item, dict)
        and str(item.get("bill_id") or "") in allowed_bill_ids
        and str(item.get("speech_id") or "") in speech_ids
    ]
    link_inventory.update({"items": link_items, "total": len(link_items)})
    inventory.update(
        {
            "bill_candidates": bill_inventory,
            "speech_candidates": speech_inventory,
            "links": link_inventory,
        }
    )
    payload["scope_inventory"] = inventory


def _is_legislator_role(role: str) -> bool:
    normalized = role.strip()
    if not normalized or any(
        excluded in normalized
        for excluded in ("전문위원", "국무위원", "정부위원", "장관", "차관", "처장", "국장")
    ):
        return False
    return normalized == "의원" or normalized.endswith("위원") or normalized.endswith("위원장")


def _issue_stage_coverage(
    query: str,
    payload: dict[str, Any],
    meeting_inventory: list[dict[str, Any]],
    refresh: dict[str, Any],
) -> dict[str, Any]:
    requested = requested_stages(query)
    type_by_stage = {
        "subcommittee": MeetingSource.SUBCOMMITTEE.value,
        "standing_committee": MeetingSource.COMMITTEE.value,
        "plenary": MeetingSource.PLENARY.value,
    }
    speech_meeting_ids = {
        str(speech.get("meeting_id") or "")
        for speech in payload.get("speeches", [])
        if isinstance(speech, dict)
    }
    member_speech_meeting_ids = {
        str(speech.get("meeting_id") or "")
        for speech in payload.get("speeches", [])
        if isinstance(speech, dict)
        and (
            (
                isinstance(speech.get("attribution"), dict)
                and speech["attribution"].get("is_legislator") is True
            )
            or _is_legislator_role(str(speech.get("speaker_role") or ""))
        )
    }
    failed_urls = set(refresh.get("failed_official_urls") or [])
    exact_check_performed = bool(refresh.get("targeted_measure"))
    deadline_exceeded = refresh.get("deadline_exceeded") is True
    stages: dict[str, dict[str, Any]] = {}
    complete_states = {
        "discussion_found",
        "record_found_no_member_debate",
        "checked_no_matching_discussion",
    }
    for stage in requested:
        meeting_type = type_by_stage[stage]
        all_candidates = [
            item
            for item in meeting_inventory
            if str(item.get("meeting_type") or "") == meeting_type
        ]
        # Exact-measure candidates omitted by the bounded PDF window still
        # belong to completeness accounting.  Excluding them here previously
        # let three checked PDFs masquerade as a complete candidate search.
        candidates = all_candidates
        checked = [item for item in candidates if item.get("full_text_loaded") is True]
        matched = [
            item for item in candidates if str(item.get("meeting_id") or "") in speech_meeting_ids
        ]
        member_matched = [
            item
            for item in candidates
            if str(item.get("meeting_id") or "") in member_speech_meeting_ids
        ]
        failures = [
            item for item in candidates if str(item.get("official_url") or "") in failed_urls
        ]
        if failures:
            state = "failed"
        elif deadline_exceeded and (not candidates or len(checked) < len(candidates)):
            state = "deadline_exceeded"
        elif candidates and len(checked) < len(candidates):
            state = "metadata_found_text_pending"
        elif member_matched:
            state = "discussion_found"
        elif matched:
            state = "record_found_no_member_debate"
        elif candidates:
            state = (
                "record_found_no_member_debate"
                if stage == "plenary"
                else "checked_no_matching_discussion"
            )
        elif exact_check_performed:
            state = "checked_no_matching_discussion"
        else:
            state = "not_checked"
        meeting_records: list[dict[str, Any]] = []
        for item in candidates[:12]:
            meeting_id = str(item.get("meeting_id") or "")
            official_url = str(item.get("official_url") or "")
            if official_url in failed_urls:
                evidence_state = "failed"
            elif item.get("full_text_loaded") is not True:
                evidence_state = "metadata_found_text_pending"
            elif meeting_id in member_speech_meeting_ids:
                evidence_state = "discussion_found"
            elif meeting_id in speech_meeting_ids:
                evidence_state = "record_found_no_member_debate"
            else:
                evidence_state = "checked_no_matching_discussion"
            meeting_records.append(
                {
                    "meeting_id": item.get("meeting_id"),
                    "date": item.get("date"),
                    "title": item.get("title"),
                    "official_url": item.get("official_url"),
                    "full_text_loaded": item.get("full_text_loaded"),
                    "evidence_state": evidence_state,
                }
            )
        stages[stage] = {
            "state": state,
            "candidate_count": len(candidates),
            "observed_candidate_count": len(all_candidates),
            "unselected_candidate_count": max(0, len(all_candidates) - len(candidates)),
            "checked_count": len(checked),
            "matched_speech_count": len(matched),
            "matched_discussion_count": len(member_matched),
            "failed_count": len(failures),
            "pending_count": max(0, len(candidates) - len(checked) - len(failures)),
            "meetings": meeting_records,
            "gap_reason": (
                None if state in {"discussion_found", "record_found_no_member_debate"} else state
            ),
        }
    return {
        "requested_stages": list(requested),
        "complete": all(
            str(stages[stage].get("state") or "") in complete_states for stage in requested
        ),
        "stages": stages,
        "exact_measure_check": exact_check_performed,
        "instruction": (
            "discussion_found가 아닌 단계는 토론이 있었다고 추정하지 말고 state와 gap_reason을 "
            "그대로 밝혀야 합니다."
        ),
    }


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
