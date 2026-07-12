from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from kasm.adapters.korea.bills import BILL_DATASET, BILL_STATUS_DATASET
from kasm.adapters.korea.client import ApiPage
from kasm.adapters.korea.documents import BillDocumentLink, FetchedBillDocument
from kasm.adapters.korea.ingestion import OpenAssemblyIngestor
from kasm.adapters.korea.sources import DATASET_BY_SOURCE, MeetingSource
from kasm.live import LiveAssemblyServices
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
