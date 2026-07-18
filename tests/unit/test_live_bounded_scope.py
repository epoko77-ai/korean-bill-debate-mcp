from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from kasm.adapters.korea.bills import BILL_DATASET, BILL_STATUS_DATASET
from kasm.adapters.korea.client import ApiPage
from kasm.adapters.korea.sources import DATASET_BY_SOURCE, MeetingSource
from kasm.live import (
    LiveAssemblyServices,
    _bill_queries,
    _filter_bills_by_proposal_scope,
    _filter_meeting_rows_by_scope,
    _meeting_date_queries,
    _proposal_date_scope,
)
from kasm.storage.database import Database

QUERY = (
    "2026년 발의된 인공지능 관련 법안 중 중요도가 높은 법안을 5개 정도 "
    "정리하고, 이에 대한 소위원회, 상임위원회 논의 내용을 정리해줘."
)


class RecordingClient:
    api_key = "fixture-key"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str | int]]] = []

    def fetch_page(
        self,
        dataset: str,
        *,
        page: int = 1,
        page_size: int = 100,
        parameters: dict[str, str | int] | None = None,
        refresh: bool = False,
    ) -> ApiPage:
        del refresh
        values = dict(parameters or {})
        self.calls.append((dataset, values))
        if dataset == BILL_DATASET:
            rows = (
                {
                    "BILL_ID": "PRC_2025",
                    "BILL_NO": "2210001",
                    "BILL_NAME": "인공지능 과거 법안",
                    "AGE": "22",
                    "PROPOSE_DT": "2025-11-01",
                },
                {
                    "BILL_ID": "PRC_2026",
                    "BILL_NO": "2210002",
                    "BILL_NAME": "AI 산업 진흥법안",
                    "AGE": "22",
                    "PROPOSE_DT": "2026-01-08",
                },
            )
        elif dataset == BILL_STATUS_DATASET:
            rows = (
                {
                    "BILL_ID": "PRC_2026",
                    "BILL_NO": "2210002",
                    "BILL_NAME": "AI 산업 진흥법안",
                    "AGE": "22",
                    "PROPOSE_DT": "2026-01-08",
                    "PROC_RESULT": "위원회 심사",
                },
            )
        else:
            rows = ()
        return ApiPage(
            dataset,
            page,
            page_size,
            len(rows),
            rows,
            "https://open.assembly.go.kr/portal/openapi/fixture",
            dataset,
        )


def test_exact_question_uses_only_three_topic_bill_queries() -> None:
    assert _bill_queries(QUERY) == ["인공지능", "AI", "인공지능 기본법"]


def test_exact_question_has_hard_proposal_scope_and_one_year_meeting_query() -> None:
    assert _proposal_date_scope(QUERY) == (
        date(2026, 1, 1),
        date(2026, 12, 31),
    )
    assert _meeting_date_queries(
        [f"2026-{month:02d}" for month in range(1, 13)]
    ) == ["2026"]
    assert _meeting_date_queries(["2026-01", "2026-02", "2026-03"]) == [
        "2026-01",
        "2026-02",
        "2026-03",
    ]
    assert _meeting_date_queries(
        [f"2026-{month:02d}" for month in range(1, 8)],
        as_of=date(2026, 7, 18),
    ) == ["2026"]


def test_meeting_rows_are_hard_filtered_to_effective_scope() -> None:
    rows = [
        {"CONF_DATE": "2025-12-31"},
        {"CONF_DT": "2026-01-01"},
        {"CONF_DATE": "2026-07-18"},
        {"CONF_DT": "2026-07-19"},
        {"CONF_DATE": "2026-12-01"},
        {"TITLE": "missing date"},
    ]

    assert _filter_meeting_rows_by_scope(
        rows,
        {
            "requested_date_from": "2026-01-01",
            "requested_date_to": "2026-07-18",
        },
        [f"2026-{month:02d}" for month in range(1, 8)],
    ) == rows[1:3]


def test_proposal_scope_rejects_2025_and_missing_proposal_dates() -> None:
    bills = [
        {"bill_no": "2210001", "proposed_at": "2025-11-01"},
        {"bill_no": "2210002", "proposed_at": "2026-01-08"},
        {"bill_no": "2210003", "RGS_PROC_DT": "2026-03-09"},
    ]

    assert [
        bill["bill_no"]
        for bill in _filter_bills_by_proposal_scope(
            bills,
            (date(2026, 1, 1), date(2026, 12, 31)),
        )
    ] == ["2210002"]


def test_live_metadata_calls_are_bounded_to_three_bill_and_three_meeting_queries(
    tmp_path,
) -> None:
    database = Database(tmp_path / "metadata-calls.sqlite3")
    database.initialize()
    client = RecordingClient()
    service = LiveAssemblyServices(
        database,
        client=client,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 18, tzinfo=UTC),
    )

    bills = service._refresh_bills(
        query=QUERY,
        assembly_term=22,
        include_documents=False,
    )
    bill_calls = [
        parameters
        for dataset, parameters in client.calls
        if dataset == BILL_DATASET
    ]

    assert [bill["BILL_NO"] for bill in bills] == ["2210002"]
    assert [call["BILL_NAME"] for call in bill_calls] == [
        "인공지능",
        "AI",
        "인공지능 기본법",
    ]

    client.calls.clear()
    service._refresh_meetings(
        query=QUERY,
        committee=None,
        months=[f"2026-{month:02d}" for month in range(1, 13)],
        assembly_term=22,
        ingest_minutes=False,
    )
    meeting_calls = [
        (dataset, parameters)
        for dataset, parameters in client.calls
        if dataset
        in {
            DATASET_BY_SOURCE[MeetingSource.COMMITTEE],
            DATASET_BY_SOURCE[MeetingSource.PLENARY],
            DATASET_BY_SOURCE[MeetingSource.SUBCOMMITTEE],
        }
    ]

    assert len(meeting_calls) == 3
    assert {
        parameters.get("CONF_DATE")
        for _dataset, parameters in meeting_calls
        if "CONF_DATE" in parameters
    } == {"2026"}


def test_proposal_year_meeting_scope_stops_at_today(tmp_path) -> None:
    service = LiveAssemblyServices(
        Database(tmp_path / "proposal-meeting-scope.sqlite3"),
        client=object(),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 18, tzinfo=UTC),
    )
    service._refresh_bills = lambda **_kwargs: []  # type: ignore[method-assign]
    captured: dict[str, Any] = {}
    service._refresh_meetings = (  # type: ignore[method-assign]
        lambda **kwargs: captured.update(kwargs)
    )

    service._hydrate_issue(QUERY, {"limit": 5})

    elapsed_months = [f"2026-{month:02d}" for month in range(1, 8)]
    assert captured["months"] == elapsed_months
    assert captured["temporal_scope"] == {
        "mode": "explicit",
        "explicit": True,
        "requested_date_from": "2026-01-01",
        "requested_date_to": "2026-07-18",
        "requested_months": elapsed_months,
        "queried_months": elapsed_months,
        "window_start_month": "2026-01",
        "window_end_month": "2026-07",
        "window_month_count": 7,
    }


def test_bounded_issue_filters_cache_by_year_ranks_five_and_skips_bill_pdfs(
    tmp_path,
) -> None:
    database = Database(tmp_path / "bounded.sqlite3")
    database.initialize()
    service = LiveAssemblyServices(
        database,
        client=object(),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 18, tzinfo=UTC),
    )
    service._hydrate_issue = lambda _query, _filters: 22  # type: ignore[method-assign]
    service._merge_selected_bill_inventory = lambda _bills: None  # type: ignore[method-assign]
    service._merge_cached_bill_inventory = lambda _items: None  # type: ignore[method-assign]
    service._hydrate_selected_bills = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bounded overview eagerly hydrated bill PDFs")
        )
    )
    service.last_refresh = {
        "has_more": False,
        "minutes_failures": 0,
        "months_queried": [f"2026-{month:02d}" for month in range(1, 8)],
        "temporal_scope": {
            "mode": "explicit",
            "explicit": True,
            "requested_months": [f"2026-{month:02d}" for month in range(1, 8)],
            "queried_months": [f"2026-{month:02d}" for month in range(1, 8)],
        },
    }
    calls: dict[str, Any] = {}

    def local_explore(
        query: str,
        limit: int,
        *,
        date_from: str | None,
        date_to: str | None,
        assembly_term: int,
    ) -> dict[str, Any]:
        calls.update(
            query=query,
            limit=limit,
            date_from=date_from,
            date_to=date_to,
            assembly_term=assembly_term,
        )
        bills = [
            {
                "id": f"bill-{number}",
                "bill_no": f"22{number:05d}",
                "name": f"인공지능 법안 {number}",
                "proposed_at": (
                    "2025-12-31" if number == 1 else f"2026-0{min(number, 6)}-01"
                ),
                "processed_at": "2026-06-01" if number == 2 else None,
                "committee": "과학기술정보방송통신위원회",
                "official_url": (
                    "https://likms.assembly.go.kr/bill/billDetail.do?"
                    f"billId=PRC_{number}"
                ),
                "documents": [{"text": "must not be returned"}],
                "selection_relevance": {"score": 30},
            }
            for number in range(1, 8)
        ]
        return {
            "query": query,
            "bills": bills,
            "speeches": [],
            "discussion_threads": [],
            "timeline": [],
            "links": [
                {"bill_id": "bill-2", "speech_id": "speech-1"},
                {"bill_id": "bill-2", "speech_id": "speech-2"},
            ],
            "scope_inventory": {
                "bill_candidates": {"items": bills},
                "selected_for_synthesis": {},
            },
        }

    service.local.explore_issue = local_explore  # type: ignore[method-assign]

    result = service.explore_issue(QUERY, limit=5)

    assert calls == {
        "query": QUERY,
        "limit": 50,
        "date_from": "2026-01-01",
        "date_to": "2026-07-18",
        "assembly_term": 22,
    }
    assert len(result["bills"]) == 5
    assert all(str(bill["proposed_at"]).startswith("2026-") for bill in result["bills"])
    assert result["bills"][0]["bill_no"] == "2200002"
    assert result["bills"][0]["importance"]["rank"] == 1
    assert all(
        bill["official_url"].startswith("https://likms.assembly.go.kr/")
        for bill in result["bills"]
    )
    assert all(bill["documents"] == [] for bill in result["bills"])
    assert all(
        bill["document_coverage"]["gap_reason"]
        == "targeted_get_bill_status_required"
        for bill in result["bills"]
    )
    assert result["proposal_date_scope"]["basis"] == "proposal_date"
    assert result["importance_selection"]["requested_count"] == 5
