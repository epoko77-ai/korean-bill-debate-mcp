from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kasm.adapters.korea.bills import BILL_DATASET, BILL_STATUS_DATASET
from kasm.adapters.korea.client import ApiPage
from kasm.adapters.korea.documents import BillDocumentLink, FetchedBillDocument
from kasm.adapters.korea.ingestion import OpenAssemblyIngestor
from kasm.adapters.korea.sources import DATASET_BY_SOURCE, MeetingSource
from kasm.live import LiveAssemblyServices, _research_pagination
from kasm.storage.database import Database

ROOT = Path(__file__).parents[2]


class FakeClient:
    api_key = "fixture-key"

    def __init__(self, meeting_row: dict[str, object]) -> None:
        self.meeting_row = meeting_row
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
        values = parameters or {}
        self.calls.append((dataset, values))
        if dataset == BILL_DATASET:
            rows = (
                {
                    "BILL_NO": "2201000",
                    "BILL_NAME": "농업 집행 지원법률안",
                    "AGE": "22",
                    "PROPOSER": "윤준병의원 등 10인",
                    "COMMITTEE": "농림축산식품해양수산위원회",
                    "PROPOSE_DT": "20250120",
                    "DETAIL_LINK": "https://likms.assembly.go.kr/bill/billDetail.do?billId=2201000",
                },
            )
        elif dataset == BILL_STATUS_DATASET:
            rows = (
                {
                    "BILL_NO": "2201000",
                    "BILL_NAME": "농업 집행 지원법률안",
                    "AGE": "22",
                    "PROPOSER": "윤준병의원 등 10인",
                    "COMMITTEE": "농림축산식품해양수산위원회",
                    "PROPOSE_DT": "20250120",
                    "PROC_RESULT": "위원회 심사",
                    "DETAIL_LINK": "https://likms.assembly.go.kr/bill/billDetail.do?billId=2201000",
                },
            )
        elif dataset == DATASET_BY_SOURCE[MeetingSource.COMMITTEE]:
            rows = (self.meeting_row,)
        else:
            rows = ()
        return ApiPage(dataset, page, page_size, len(rows), rows, "https://official.test", dataset)


def test_empty_cache_is_hydrated_from_live_api_before_speech_search(tmp_path) -> None:
    payload = json.loads(
        (ROOT / "tests/fixtures/open_assembly/committee.json").read_text(encoding="utf-8")
    )
    meeting_row = payload["ncwgseseafwbuheph"][1]["row"][0]
    transcript = (ROOT / "tests/fixtures/parser/verified_excerpt.txt").read_text(encoding="utf-8")
    client = FakeClient(meeting_row)
    database = Database(tmp_path / "cache.sqlite3")
    database.initialize()
    service = LiveAssemblyServices(
        database,
        client,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        now=lambda: datetime(2025, 2, 10, tzinfo=UTC),
    )
    ingestor = OpenAssemblyIngestor(database)

    def sync(row: dict[str, object], *, refresh: bool = False):
        del refresh
        return ingestor.ingest(
            row, transcript, source_hash="fixture", source_url=row["PDF_LINK_URL"]
        )

    service.pipeline.sync = sync  # type: ignore[method-assign]
    results = service.search("실제 집행 과정", limit=10)

    assert results[0]["speaker"] == "문대림"
    assert results[0]["citation"]["official_url"].startswith("https://record.assembly.go.kr/")
    assert {dataset for dataset, _ in client.calls} >= {
        BILL_DATASET,
        BILL_STATUS_DATASET,
        DATASET_BY_SOURCE[MeetingSource.COMMITTEE],
    }


def test_bill_status_is_refreshed_from_official_status_api(tmp_path) -> None:
    client = FakeClient({})
    database = Database(tmp_path / "cache.sqlite3")
    database.initialize()
    service = LiveAssemblyServices(
        database,
        client,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
    )
    status = service.get_bill_status("2201000")
    assert status is not None
    assert status["status"] == "위원회 심사"
    assert any(dataset == BILL_STATUS_DATASET for dataset, _ in client.calls)


def test_exact_historical_bill_number_selects_its_own_assembly_term(tmp_path) -> None:
    class HistoricalBillClient(FakeClient):
        def fetch_page(self, dataset: str, **kwargs):
            parameters = kwargs.get("parameters") or {}
            self.calls.append((dataset, parameters))
            rows = (
                (
                    {
                        "BILL_NO": "1800001",
                        "BILL_NAME": "역사자료 확인법률안",
                        "AGE": "18",
                        "PROPOSE_DT": "20080601",
                        "PROC_RESULT": "대안반영폐기",
                    },
                )
                if dataset == BILL_DATASET
                and parameters.get("AGE") == 18
                and parameters.get("BILL_NO") == "1800001"
                else ()
            )
            return ApiPage(
                dataset,
                1,
                int(kwargs.get("page_size") or 100),
                len(rows),
                rows,
                "https://official.test",
                dataset,
            )

    client = HistoricalBillClient({})
    database = Database(tmp_path / "historical-bill.sqlite3")
    database.initialize()
    service = LiveAssemblyServices(
        database,
        client,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
    )

    status = service.get_bill_status("1800001")

    assert status is not None
    assert status["bill_no"] == "1800001"
    assert {dataset for dataset, _parameters in client.calls} >= {
        BILL_DATASET,
        BILL_STATUS_DATASET,
    }
    assert all(
        parameters["AGE"] == 18
        for dataset, parameters in client.calls
        if dataset in {BILL_DATASET, BILL_STATUS_DATASET}
    )


def test_pending_bill_status_falls_back_to_main_bill_api(tmp_path) -> None:
    class PendingClient(FakeClient):
        def fetch_page(self, dataset: str, **kwargs):
            if dataset == BILL_STATUS_DATASET:
                parameters = kwargs.get("parameters") or {}
                self.calls.append((dataset, parameters))
                return ApiPage(dataset, 1, 100, 0, (), "https://official.test", "empty")
            return super().fetch_page(dataset, **kwargs)

    client = PendingClient({})
    database = Database(tmp_path / "cache.sqlite3")
    database.initialize()
    service = LiveAssemblyServices(
        database,
        client,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
    )

    status = service.get_bill_status("2201000")

    assert status is not None
    assert status["status"] == "계류"
    assert any(
        dataset == BILL_DATASET and parameters.get("BILL_NO") == "2201000"
        for dataset, parameters in client.calls
    )


def test_natural_language_bill_number_rejects_nonmatching_api_rows(tmp_path) -> None:
    client = FakeClient({})
    database = Database(tmp_path / "cache.sqlite3")
    database.initialize()
    service = LiveAssemblyServices(
        database,
        client,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
    )

    results = service.search_bills("의안번호 2219564 보완수사권", limit=10)

    assert results == []
    assert service.local.get_bill_status("2201000") is None
    assert any(
        dataset == BILL_DATASET and parameters.get("BILL_NO") == "2219564"
        for dataset, parameters in client.calls
    )


def test_live_bill_search_attaches_on_demand_review_report(tmp_path) -> None:
    class DocumentsClient:
        def review_reports(self, bill_id: str, bill_no: str):
            assert bill_id == "2201000"
            assert bill_no == "2201000"
            return (
                BillDocumentLink(
                    "committee_review_report",
                    "전문위원 검토보고서",
                    "pdf",
                    "https://likms.assembly.go.kr/filegate/servlet/FileGate?bookId=review&type=1",
                ),
            )

    class DocumentFetcher:
        def fetch(self, source_url: str):
            return FetchedBillDocument(
                source_url,
                "report-hash",
                tmp_path / "review.pdf",
                tmp_path / "review.txt",
                "집행 가능성과 법체계 정합성을 검토할 필요가 있음",
            )

    client = FakeClient({})
    database = Database(tmp_path / "cache.sqlite3")
    database.initialize()
    service = LiveAssemblyServices(
        database,
        client,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        document_client=DocumentsClient(),  # type: ignore[arg-type]
        document_fetcher=DocumentFetcher(),  # type: ignore[arg-type]
    )

    results = service.search_bills("농업", limit=10)

    assert results[0]["documents"][0]["title"] == "전문위원 검토보고서"
    assert results[0]["documents"][0]["official_url"].startswith(
        "https://likms.assembly.go.kr/"
    )


def test_selected_bill_returns_every_review_report_with_lossless_text(tmp_path) -> None:
    links = tuple(
        BillDocumentLink(
            "committee_review_report",
            f"전문위원 검토보고서 {index}",
            "pdf",
            (
                "https://likms.assembly.go.kr/filegate/servlet/FileGate?"
                f"bookId=review-{index}&type=1"
            ),
        )
        for index in range(4)
    )

    class DocumentsClient:
        def review_reports(self, bill_id: str, bill_no: str):
            assert (bill_id, bill_no) == ("2201000", "2201000")
            return links

    class DocumentFetcher:
        def fetch(self, source_url: str):
            index = source_url.split("review-", 1)[1].split("&", 1)[0]
            text = f"검토보고서 {index}\n" + ("법체계·집행 가능성 검토\n" * 1000)
            return FetchedBillDocument(
                source_url,
                hashlib.sha256(source_url.encode()).hexdigest(),
                tmp_path / f"review-{index}.pdf",
                tmp_path / f"review-{index}.txt",
                text,
            )

    database = Database(tmp_path / "lossless.sqlite3")
    database.initialize()
    service = LiveAssemblyServices(
        database,
        FakeClient({}),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        document_client=DocumentsClient(),  # type: ignore[arg-type]
        document_fetcher=DocumentFetcher(),  # type: ignore[arg-type]
    )

    results = service.search_bills("농업", limit=10)

    documents = results[0]["documents"]
    assert len(documents) == 4
    assert results[0]["document_coverage"] == {
        "complete": True,
        "discovered": 4,
        "loaded": 4,
        "failed_official_urls": [],
        "gap_reason": None,
    }
    assert all(document["text_inline_complete"] is True for document in documents)
    assert all(document["text_length"] == len(document["text"]) for document in documents)
    assert all(len(document["text"]) > 12_000 for document in documents)
    assert all("text_excerpt" not in document for document in documents)
    assert all(
        document["text_sha256"]
        == hashlib.sha256(document["text"].encode("utf-8")).hexdigest()
        for document in documents
    )


def test_issue_exposes_complete_candidate_inventory_before_selected_detail(tmp_path) -> None:
    bill_rows = tuple(
        {
            "BILL_ID": f"PRC_TEST_{index}",
            "BILL_NO": f"22010{index:02d}",
            "BILL_NAME": f"인공지능 제도 정비법률안 {index}",
            "AGE": "22",
            "COMMITTEE": "과학기술정보방송통신위원회",
            "PROPOSE_DT": "20260102",
            "DETAIL_LINK": (
                "https://likms.assembly.go.kr/bill/billDetail.do?"
                f"billId=PRC_TEST_{index}"
            ),
        }
        for index in range(6)
    )

    class InventoryClient(FakeClient):
        def fetch_page(self, dataset: str, **kwargs):
            parameters = kwargs.get("parameters") or {}
            self.calls.append((dataset, parameters))
            if dataset == BILL_DATASET:
                requested = parameters.get("BILL_NO")
                selected = (
                    tuple(row for row in bill_rows if row["BILL_NO"] == requested)
                    if requested
                    else bill_rows
                )
            elif dataset == BILL_STATUS_DATASET:
                requested = parameters.get("BILL_NO")
                selected = tuple(
                    {**row, "PROC_RESULT": "위원회 심사"}
                    for row in bill_rows
                    if row["BILL_NO"] == requested
                )
            else:
                selected = ()
            return ApiPage(
                dataset,
                1,
                int(kwargs.get("page_size") or 100),
                len(selected),
                selected,
                "https://official.test",
                dataset,
            )

    database = Database(tmp_path / "inventory.sqlite3")
    database.initialize()
    service = LiveAssemblyServices(
        database,
        InventoryClient({}),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        document_client=None,
        document_fetcher=None,
    )
    service.pipeline.sync = lambda _row: None  # type: ignore[method-assign]

    result = service.explore_issue("인공지능 입법", limit=2)

    inventory = result["scope_inventory"]
    assert inventory["bill_candidates"]["complete"] is True
    assert inventory["bill_candidates"]["total"] == 6
    assert len(inventory["bill_candidates"]["items"]) == 6
    assert {item["bill_no"] for item in inventory["bill_candidates"]["items"]} == {
        row["BILL_NO"] for row in bill_rows
    }
    assert all(
        isinstance(item.get("selection_relevance"), dict)
        for item in inventory["bill_candidates"]["items"]
    )
    assert all(
        isinstance(bill.get("selection_relevance"), dict)
        for bill in result["bills"]
    )
    assert inventory["selected_for_synthesis"]["bill_count"] == 2
    assert inventory["selected_for_synthesis"]["eligible_bill_count"] == 6
    assert inventory["selected_for_synthesis"]["bill_selection_complete"] is False


def test_explicit_start_to_present_uses_only_requested_month_range(tmp_path) -> None:
    service = LiveAssemblyServices(
        Database(tmp_path / "range.sqlite3"),
        FakeClient({}),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 13, tzinfo=UTC),
    )

    months = service._months_for_query("2026년 1월 1일부터 현재까지 보완수사권")

    assert months == {
        "2026-01",
        "2026-02",
        "2026-03",
        "2026-04",
        "2026-05",
        "2026-06",
        "2026-07",
    }


def test_historical_year_selects_term_and_every_requested_month(tmp_path) -> None:
    service = LiveAssemblyServices(
        Database(tmp_path / "historical-year.sqlite3"),
        FakeClient({}),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 13, tzinfo=UTC),
    )
    service._refresh_bills = lambda **_kwargs: []  # type: ignore[method-assign]
    captured: dict[str, Any] = {}
    service._refresh_meetings = (  # type: ignore[method-assign]
        lambda **kwargs: captured.update(kwargs)
    )

    selected_term = service._hydrate_issue("1999년 인공지능 입법", {})

    assert selected_term == 15
    assert captured["assembly_term"] == 15
    assert captured["months"] == [f"1999-{month:02d}" for month in range(1, 13)]


def test_explicit_historical_term_uses_official_term_bounds_for_history(tmp_path) -> None:
    service = LiveAssemblyServices(
        Database(tmp_path / "historical-term.sqlite3"),
        FakeClient({}),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 13, tzinfo=UTC),
    )
    service._refresh_bills = lambda **_kwargs: []  # type: ignore[method-assign]
    captured: dict[str, Any] = {}
    service._refresh_meetings = (  # type: ignore[method-assign]
        lambda **kwargs: captured.update(kwargs)
    )

    service._hydrate_issue("과거부터 전체 경과", {"assembly_term": 18})

    months = captured["months"]
    assert captured["assembly_term"] == 18
    assert months[0] == "2008-05"
    assert months[-1] == "2012-05"
    assert "2026-07" not in months


def test_meeting_list_derives_historical_term_from_structured_dates(tmp_path) -> None:
    database = Database(tmp_path / "historical-meetings.sqlite3")
    database.initialize()
    service = LiveAssemblyServices(
        database,
        FakeClient({}),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
    )
    captured: dict[str, Any] = {}
    service._refresh_meetings = (  # type: ignore[method-assign]
        lambda **kwargs: captured.update(kwargs)
    )

    assert service.list_meetings(
        date_from="2010-01-15", date_to="2010-02-03"
    ) == []

    assert captured["assembly_term"] == 18
    assert captured["months"] == {"2010-01", "2010-02"}


def test_legacy_live_rejects_a_multi_term_date_range(tmp_path) -> None:
    service = LiveAssemblyServices(
        Database(tmp_path / "multi-term.sqlite3"),
        FakeClient({}),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="start_research"):
        service._selected_assembly_term(
            "인공지능 입법",
            {"date_from": "2010-01-01", "date_to": "2014-01-01"},
        )


def test_explicit_month_is_not_broadened_by_candidate_bill_dates(tmp_path) -> None:
    service = LiveAssemblyServices(
        Database(tmp_path / "explicit-month.sqlite3"),
        FakeClient({}),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 13, tzinfo=UTC),
    )
    service._refresh_bills = lambda **_kwargs: [  # type: ignore[method-assign]
        {"BILL_NO": "2200001", "PROPOSE_DT": "20240601", "PROC_DT": "20250115"}
    ]
    captured: dict[str, object] = {}

    def capture_meetings(**kwargs):
        captured.update(kwargs)

    service._refresh_meetings = capture_meetings  # type: ignore[method-assign]

    service._hydrate_issue("2026년 7월 검찰 보완수사권", {"limit": 5})

    assert captured["months"] == ["2026-07"]
    assert captured["temporal_scope"] == {
        "mode": "explicit",
        "explicit": True,
        "requested_date_from": None,
        "requested_date_to": None,
        "requested_months": ["2026-07"],
        "queried_months": ["2026-07"],
        "window_start_month": "2026-07",
        "window_end_month": "2026-07",
        "window_month_count": 1,
    }


def test_undated_issue_exposes_implicit_recent_window_without_overall_completion(
    tmp_path,
) -> None:
    service = LiveAssemblyServices(
        Database(tmp_path / "implicit-window.sqlite3"),
        FakeClient({}),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 13, tzinfo=UTC),
    )
    service._refresh_bills = lambda **_kwargs: []  # type: ignore[method-assign]
    captured: dict[str, Any] = {}

    def capture_meetings(**kwargs):
        captured.update(kwargs)

    service._refresh_meetings = capture_meetings  # type: ignore[method-assign]

    service._hydrate_issue("인공지능 입법", {"limit": 5})

    assert captured["months"] == ["2026-06", "2026-07"]
    assert captured["temporal_scope"] == {
        "mode": "implicit_recent_two_month_window",
        "explicit": False,
        "requested_date_from": None,
        "requested_date_to": None,
        "requested_months": [],
        "queried_months": ["2026-06", "2026-07"],
        "window_start_month": "2026-06",
        "window_end_month": "2026-07",
        "window_month_count": 2,
    }

    pagination = _research_pagination(
        {
            "has_more": False,
            "minutes_failures": 0,
            "months_queried": captured["months"],
            "temporal_scope": captured["temporal_scope"],
        }
    )
    assert pagination["window_complete"] is True
    assert pagination["overall_complete"] is False
    assert pagination["complete"] is False
    assert pagination["partial"] is True


def test_explicit_temporal_window_can_be_complete_after_full_ingestion() -> None:
    pagination = _research_pagination(
        {
            "has_more": False,
            "minutes_failures": 0,
            "months_queried": ["2026-01", "2026-02"],
            "temporal_scope": {
                "mode": "explicit",
                "explicit": True,
                "requested_months": ["2026-01", "2026-02"],
                "queried_months": ["2026-01", "2026-02"],
            },
        }
    )

    assert pagination["window_complete"] is True
    assert pagination["overall_complete"] is True
    assert pagination["complete"] is True
    assert pagination["partial"] is False


def test_structured_date_range_keeps_exact_bounds_and_queries_every_touched_month(
    tmp_path,
) -> None:
    service = LiveAssemblyServices(
        Database(tmp_path / "structured-range.sqlite3"),
        FakeClient({}),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 13, tzinfo=UTC),
    )
    service._refresh_bills = lambda **_kwargs: []  # type: ignore[method-assign]
    captured: dict[str, Any] = {}
    service._refresh_meetings = (  # type: ignore[method-assign]
        lambda **kwargs: captured.update(kwargs)
    )

    service._hydrate_issue(
        "보완수사권",
        {"date_from": "2026-01-15", "date_to": "2026-03-02"},
    )

    assert captured["months"] == ["2026-01", "2026-02", "2026-03"]
    scope = captured["temporal_scope"]
    assert scope["mode"] == "explicit"
    assert scope["explicit"] is True
    assert scope["requested_date_from"] == "2026-01-15"
    assert scope["requested_date_to"] == "2026-03-02"
    assert scope["queried_months"] == ["2026-01", "2026-02", "2026-03"]


def test_minutes_refresh_returns_a_continuation_offset(tmp_path) -> None:
    payload = json.loads(
        (ROOT / "tests/fixtures/open_assembly/committee.json").read_text(encoding="utf-8")
    )
    base_row = payload["ncwgseseafwbuheph"][1]["row"][0]
    rows = tuple(
        {
            **base_row,
            "CONF_DATE": "2026-07-01",
            "PDF_LINK_URL": f"https://record.assembly.go.kr/fake-{index}.pdf",
        }
        for index in range(5)
    )

    class ManyMeetingsClient(FakeClient):
        def fetch_page(self, dataset: str, **kwargs):
            parameters = kwargs.get("parameters") or {}
            self.calls.append((dataset, parameters))
            selected = (
                rows if dataset == DATASET_BY_SOURCE[MeetingSource.COMMITTEE] else ()
            )
            return ApiPage(
                dataset,
                1,
                100,
                len(selected),
                selected,
                "https://official.test",
                dataset,
            )

    database = Database(tmp_path / "paged.sqlite3")
    database.initialize()
    service = LiveAssemblyServices(
        database,
        ManyMeetingsClient({}),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        max_minutes_per_request=2,
    )
    attempted: list[str] = []
    service.pipeline.sync = (  # type: ignore[method-assign]
        lambda row: attempted.append(str(row["PDF_LINK_URL"]))
    )

    service._refresh_meetings(
        query="인공지능",
        committee=None,
        months=["2026-07"],
        ingest_minutes=True,
    )

    assert len(attempted) == 2
    assert service.last_refresh["has_more"] is True
    assert service.last_refresh["next_minutes_offset"] == 2


def test_minutes_failure_can_never_be_reported_as_complete(tmp_path) -> None:
    payload = json.loads(
        (ROOT / "tests/fixtures/open_assembly/committee.json").read_text(encoding="utf-8")
    )
    base_row = {
        **payload["ncwgseseafwbuheph"][1]["row"][0],
        "CONF_DATE": "2026-07-01",
    }
    database = Database(tmp_path / "failed-minutes.sqlite3")
    database.initialize()
    service = LiveAssemblyServices(
        database,
        FakeClient(base_row),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
    )

    def fail_sync(_row):
        raise RuntimeError("fixture failure")

    service.pipeline.sync = fail_sync  # type: ignore[method-assign]
    service._refresh_meetings(
        query="인공지능",
        committee=None,
        months=["2026-07"],
        ingest_minutes=True,
    )

    pagination = _research_pagination(service.last_refresh)
    assert pagination["complete"] is False
    assert pagination["partial"] is True
    assert pagination["failed_count"] == 1
    assert pagination["failed_official_urls"]


def test_one_page_fallback_fails_closed_when_official_total_is_larger(tmp_path) -> None:
    class IncompleteClient(FakeClient):
        def fetch_page(self, dataset: str, **_kwargs):
            return ApiPage(
                dataset,
                1,
                1,
                2,
                ({"BILL_NO": "2200001"},),
                "https://official.test",
                dataset,
            )

    service = LiveAssemblyServices(
        Database(tmp_path / "incomplete-page.sqlite3"),
        IncompleteClient({}),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="exhaustive fetch_all"):
        service._fetch_complete(
            BILL_DATASET,
            page_size=1,
            parameters={"AGE": 22},
        )
