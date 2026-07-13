from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from kasm import __version__
from kasm.adapters.korea.documents import (
    BillDocumentFetcher,
    BillDocumentIdentityError,
    BillDocumentsClient,
)
from kasm.app import LocalServices
from kasm.core.models import Bill, BillDocument
from kasm.storage.database import Database
from kasm.storage.repositories import BillDocumentRepository, BillRepository


class Response:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self.raw


def test_discovers_only_official_pdf_review_report() -> None:
    requests = []
    identity_html = b"""
    <form>
      <input type="hidden" id="billId" name="billId" value="PRC_TEST_22">
      <input type="hidden" id="billNo" name="billNo" value="2212345">
    </form>
    """
    report_html = """
    <a href="https://likms.assembly.go.kr/filegate/servlet/FileGate?bookId=review&amp;type=0"
       title="검토보고서 (HWP 파일 다운로드)">HWP</a>
    <a href="https://likms.assembly.go.kr/filegate/servlet/FileGate?bookId=review&amp;type=1"
       title="검토보고서 (PDF 파일 다운로드)">PDF</a>
    <a href="https://example.com/report.pdf"
       title="검토보고서 (PDF 파일 다운로드)">external</a>
    """.encode()

    def opener(request, **_kwargs):
        requests.append(request)
        return Response(identity_html if request.get_method() == "GET" else report_html)

    links = BillDocumentsClient(opener=opener).review_reports("PRC_TEST_22", "2212345")

    assert len(links) == 1
    assert links[0].document_type == "committee_review_report"
    assert links[0].file_format == "pdf"
    assert links[0].official_url.endswith("bookId=review&type=1")
    assert [request.get_method() for request in requests] == ["GET", "POST"]
    assert {
        request.get_header("User-agent") for request in requests
    } == {f"Mozilla/5.0 (compatible; KASM/{__version__})"}
    assert "billId=PRC_TEST_22" in requests[0].full_url
    assert b"billId=PRC_TEST_22" in requests[1].data


def test_discovers_original_bill_pdf_source_only_after_exact_identity() -> None:
    identity_html = b"""
    <form>
      <input type="hidden" id="billId" value="PRC_TEST_22">
      <input type="hidden" id="billNo" value="2212345">
    </form>
    """
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        return Response(identity_html if request.get_method() == "GET" else b"")

    links = BillDocumentsClient(opener=opener).documents(
        "PRC_TEST_22",
        "2212345",
        include_review_reports=False,
    )

    assert len(links) == 1
    original = links[0]
    assert original.document_type == "bill_text"
    assert original.title == "의안원문"
    assert original.file_format == "pdf"
    assert original.official_url.startswith(
        "https://likms.assembly.go.kr/bill/bi/bill/detail/downloadDtlZip.do?"
    )
    assert "billId=PRC_TEST_22" in original.official_url
    assert "billNo=2212345" in original.official_url
    assert "%EB%B2%95%EB%A5%A0%EC%95%88" in original.official_url
    assert [request.get_method() for request in requests] == ["GET"]


@pytest.mark.parametrize(
    "identity_html",
    (
        b'<input id="billId" value="PRC_OTHER"><input id="billNo" value="2212345">',
        b'<input id="billId" value="PRC_TEST_22"><input id="billNo" value="2299999">',
        b"<html>bill identity missing</html>",
    ),
)
def test_review_report_index_requires_exact_official_bill_identity(
    identity_html: bytes,
) -> None:
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        return Response(identity_html)

    with pytest.raises(BillDocumentIdentityError, match="did not verify"):
        BillDocumentsClient(opener=opener).review_reports("PRC_TEST_22", "2212345")

    assert len(requests) == 1
    assert requests[0].get_method() == "GET"


def test_fetches_and_extracts_only_official_bill_document_pdf(tmp_path: Path) -> None:
    def runner(command, **_kwargs):
        Path(command[-1]).write_text(
            "법체계 정합성과 집행 가능성을 검토할 필요가 있음", encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    fetcher = BillDocumentFetcher(
        tmp_path,
        opener=lambda *_args, **_kwargs: Response(b"%PDF-1.4 review"),
        runner=runner,
    )
    result = fetcher.fetch(
        "https://likms.assembly.go.kr/filegate/servlet/FileGate?bookId=review&type=1"
    )

    assert "집행 가능성" in result.text
    assert result.source_hash
    with pytest.raises(ValueError):
        fetcher.fetch("https://example.com/review.pdf")


def test_bill_document_uses_python_extraction_without_poppler(tmp_path: Path) -> None:
    def missing_runner(*_args, **_kwargs):
        raise FileNotFoundError("pdftotext")

    fetcher = BillDocumentFetcher(
        tmp_path,
        opener=lambda *_args, **_kwargs: Response(b"%PDF-1.4 review"),
        runner=missing_runner,
        fallback_extractor=lambda _path: "전문위원은 법체계 정합성을 검토했다",
    )

    result = fetcher.fetch(
        "https://likms.assembly.go.kr/filegate/servlet/FileGate?bookId=review&type=1"
    )

    assert "전문위원" in result.text


def test_review_report_text_finds_and_enriches_bill() -> None:
    now = datetime.now(UTC)
    report_text = "서두 문단\n" + ("검토 기준과 집행 자료\n" * 900) + "현장 집행 가능성\n결론 문단"
    bill = Bill(
        id="kna:bill:2212345",
        bill_no="2212345",
        name="디지털포용법안",
        assembly_term=22,
        proposer="홍길동 의원",
        committee="과학기술정보방송통신위원회",
        proposed_at=date(2026, 1, 2),
        process_result=None,
        processed_at=None,
        official_url="https://likms.assembly.go.kr/bill/billDetail.do?billId=PRC_TEST_22",
        source_hash="bill-hash",
        retrieved_at=now,
    )
    document = BillDocument(
        id="kna:bill-document:review",
        bill_id=bill.id,
        document_type="committee_review_report",
        title="전문위원 검토보고서",
        file_format="pdf",
        official_url=(
            "https://likms.assembly.go.kr/filegate/servlet/FileGate?bookId=review&type=1"
        ),
        text=report_text,
        source_hash="report-hash",
        retrieved_at=now,
    )

    with Database(":memory:") as database:
        BillRepository(database).save(bill)
        BillDocumentRepository(database).save(document)
        results = LocalServices(database).search_bills("현장 집행 가능성", limit=10)
        number_results = LocalServices(database).search_bills("2212345", limit=10)

    assert results[0]["bill_no"] == "2212345"
    assert results[0]["documents"][0]["document_type"] == "committee_review_report"
    assert results[0]["documents"][0]["text"] == report_text
    assert results[0]["documents"][0]["text_inline_complete"] is True
    assert results[0]["documents"][0]["text_length"] == len(document.text)
    assert results[0]["documents"][0]["text_sha256"] == hashlib.sha256(
        report_text.encode("utf-8")
    ).hexdigest()
    assert "text_excerpt" not in results[0]["documents"][0]
    assert results[0]["documents"][0]["citation"]["official_url"].startswith(
        "https://likms.assembly.go.kr/"
    )
    assert number_results[0]["documents"][0]["title"] == "전문위원 검토보고서"
