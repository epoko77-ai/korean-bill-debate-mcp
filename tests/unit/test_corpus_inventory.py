from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from kasm.adapters.korea.bills import BILL_DATASET
from kasm.adapters.korea.client import ApiPage, ApiResult
from kasm.adapters.korea.documents import BillDocumentLink
from kasm.adapters.korea.sources import DATASET_BY_SOURCE, MeetingSource
from kasm.corpus.inventory import (
    CorpusInventoryManifest,
    OpenAssemblyCorpusInventorySource,
    finish_inventory_session,
    pin_inventory_session,
    read_inventory_manifest,
    write_inventory_manifest,
)
from kasm.corpus.models import CorpusEvidenceKind

NOW = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)
BILL_URL = (
    "https://likms.assembly.go.kr/bill/bi/bill/detail/downloadDtlZip.do?"
    "billId=PRC_TEST_22&billNo=2212345&billKindCd=법률안&dwFileGbn=B"
)
REVIEW_URL = (
    "https://likms.assembly.go.kr/filegate/servlet/FileGate?bookId=review-1&type=1"
)
PLENARY_URL = (
    "https://record.assembly.go.kr/assembly/viewer/minutes/download/pdf.do?id=50001"
)
COMMITTEE_URL = (
    "https://record.assembly.go.kr/assembly/viewer/minutes/download/pdf.do?id=50002"
)


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[tuple[str, str | int], ...]]] = []

    def fetch_all(
        self,
        dataset: str,
        *,
        page_size: int,
        parameters: dict[str, str | int],
        refresh: bool,
    ) -> ApiResult:
        del refresh
        canonical = tuple(sorted(parameters.items()))
        self.calls.append((dataset, canonical))
        rows: tuple[dict[str, Any], ...]
        if dataset == BILL_DATASET:
            rows = (
                {
                    "BILL_NO": "2212345",
                    "BILL_ID": "PRC_TEST_22",
                    "BILL_NAME": "인공지능 안전 기본법안",
                    "AGE": 22,
                    "PROPOSE_DT": "20260103",
                    "COMMITTEE_NM": "과학기술정보방송통신위원회",
                },
            )
        elif dataset == DATASET_BY_SOURCE[MeetingSource.PLENARY]:
            rows = (
                {
                    "CONF_DATE": "2026-01-12",
                    "TITLE": "제1차 본회의",
                    "PDF_LINK_URL": PLENARY_URL,
                    "SUB_NAME": "2212345 인공지능 안전 기본법안",
                },
            )
        elif dataset == DATASET_BY_SOURCE[MeetingSource.COMMITTEE]:
            rows = (
                {
                    "CONF_DATE": "2026-01-15",
                    "TITLE": "과방위 법안심사소위원회",
                    "COMM_NAME": "과학기술정보방송통신위원회",
                    "PDF_LINK_URL": COMMITTEE_URL,
                    "SUB_NAME": "2212345 인공지능 안전 기본법안",
                },
            )
        else:
            # Dedicated subcommittee inventory repeats the committee URL.  The
            # inventory must merge it rather than double-count one official PDF.
            rows = (
                {
                    "CONF_DT": "2026-01-15",
                    "TITLE": "과방위 법안심사소위원회",
                    "CMIT_NM": "과학기술정보방송통신위원회",
                    "DOWN_URL": COMMITTEE_URL,
                    "agenda_items": [
                        {"bill_no": "2212345", "title": "인공지능 안전 기본법안"}
                    ],
                },
            )
        source_hash = (hex(len(self.calls))[2:] * 64)[:64]
        page = ApiPage(
            dataset,
            1,
            page_size,
            len(rows),
            rows,
            f"https://open.assembly.go.kr/portal/openapi/{dataset}?KEY=***",
            source_hash,
        )
        return ApiResult(dataset, page_size, len(rows), rows, (page,))


class FakeBillDocuments:
    def __init__(self, *, explode: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.explode = explode

    def documents(
        self,
        bill_id: str,
        bill_no: str,
        *,
        include_bill_text: bool,
        include_review_reports: bool,
    ) -> tuple[BillDocumentLink, ...]:
        self.calls.append((bill_id, bill_no))
        if self.explode:
            raise AssertionError("completed bill-index cache was not resumed")
        assert include_bill_text and include_review_reports
        return (
            BillDocumentLink("bill_text", "의안원문", "pdf", BILL_URL),
            BillDocumentLink(
                "committee_review_report",
                "전문위원 검토보고서",
                "pdf",
                REVIEW_URL,
            ),
        )


def _collect(
    tmp_path: Path,
    bill_documents: FakeBillDocuments,
) -> CorpusInventoryManifest:
    source = OpenAssemblyCorpusInventorySource(
        FakeApi(),  # type: ignore[arg-type]
        bill_documents=bill_documents,  # type: ignore[arg-type]
        page_size=1000,
        term_bounds={22: (date(2026, 1, 1), date(2026, 1, 31))},
    )
    return source.collect(
        (22,),
        inventory_as_of=NOW,
        discovery_cache_dir=tmp_path / "inventory-cache",
    )


def test_full_term_inventory_is_exact_complete_and_credential_free(tmp_path: Path) -> None:
    documents = FakeBillDocuments()
    manifest = _collect(tmp_path, documents)

    assert documents.calls == [("PRC_TEST_22", "2212345")]
    assert manifest.complete is True
    expected = {
        entry.evidence_kind: entry.expected_count for entry in manifest.coverage
    }
    assert expected == {
        CorpusEvidenceKind.BILL_ORIGINAL: 1,
        CorpusEvidenceKind.REVIEW_REPORT: 1,
        CorpusEvidenceKind.MINUTES: 2,
    }
    assert len(manifest.items) == 4
    assert {
        item.official_identifier
        for item in manifest.items
        if item.evidence_kind is CorpusEvidenceKind.MINUTES
    } == {"minutes:50001", "minutes:50002"}
    review = next(
        item
        for item in manifest.items
        if item.evidence_kind is CorpusEvidenceKind.REVIEW_REPORT
    )
    assert review.official_identifier.startswith("review:url-sha256:")
    assert review.work_item.related_bill_numbers == ("2212345",)

    output = tmp_path / "inventory.json"
    write_inventory_manifest(output, manifest)
    encoded = output.read_text(encoding="utf-8")
    assert "KEY=***" not in encoded
    assert "ASSEMBLY_OPEN_API_KEY" not in encoded
    assert read_inventory_manifest(output) == manifest
    assert output.stat().st_mode & 0o077 == 0


def test_bill_index_inventory_resumes_one_bill_at_a_time(tmp_path: Path) -> None:
    first = _collect(tmp_path, FakeBillDocuments())
    resumed_client = FakeBillDocuments(explode=True)
    second = _collect(tmp_path, resumed_client)

    assert resumed_client.calls == []
    assert second == first


def test_inventory_session_pins_interrupted_as_of_and_rotates_after_completion(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "session"
    first = pin_inventory_session(cache, clock=lambda: NOW)
    resumed = pin_inventory_session(
        cache,
        clock=lambda: NOW.replace(hour=13),
    )
    assert resumed == first == NOW

    manifest = _collect(tmp_path / "finished", FakeBillDocuments())
    finish_inventory_session(cache, manifest)
    next_observation = pin_inventory_session(
        cache,
        clock=lambda: NOW.replace(hour=14),
    )
    assert next_observation == NOW.replace(hour=14)
