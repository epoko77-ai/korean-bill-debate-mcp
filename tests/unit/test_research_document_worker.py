from __future__ import annotations

import hashlib
import io
import urllib.error
import urllib.parse
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kasm import __version__
from kasm.research.document_worker import (
    FailureDisposition,
    OfficialDocumentWorker,
    PermanentDocumentError,
    TransientDocumentError,
)
from kasm.research.documents import (
    FilesystemOfficialDocumentStore,
    OfficialDocumentKind,
)

NOW = datetime(2026, 7, 13, 12, tzinfo=UTC)
MINUTES_URL = "https://record.assembly.go.kr/assembly/minutes.pdf?id=1"
REVIEW_URL = "https://likms.assembly.go.kr/filegate/review.pdf?id=1"


def bill_text_url(bill_id: str = "PRC_TEST_22", bill_no: str = "2212345") -> str:
    query = urllib.parse.urlencode(
        {
            "billId": bill_id,
            "billNo": bill_no,
            "billKindCd": "법률안",
            "dwFileGbn": "B",
        }
    )
    return (
        "https://likms.assembly.go.kr/bill/bi/bill/detail/"
        f"downloadDtlZip.do?{query}"
    )


def bill_archive(*entries: tuple[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


class Response:
    def __init__(
        self,
        content: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        final_url: str | None = None,
    ) -> None:
        self.content = content
        self.status = status
        self.headers = headers or {}
        self.final_url = final_url
        self.position = 0
        self.read_sizes: list[int] = []

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if self.position >= len(self.content):
            return b""
        end = len(self.content) if size < 0 else self.position + size
        chunk = self.content[self.position : end]
        self.position += len(chunk)
        return chunk

    def geturl(self) -> str | None:
        return self.final_url


def worker(
    tmp_path: Path,
    *,
    opener: Any,
    page_extractor: Any = None,
    max_bytes: int = 1_000_000,
    timeout: float = 7.5,
) -> OfficialDocumentWorker:
    return OfficialDocumentWorker(
        FilesystemOfficialDocumentStore(tmp_path),
        parser_version="pypdf-layout-v1",
        opener=opener,
        page_extractor=page_extractor,
        max_bytes=max_bytes,
        timeout=timeout,
        chunk_bytes=17,
        clock=lambda: NOW,
    )


def test_preserves_raw_before_parsing_and_keeps_200k_text_untruncated(
    tmp_path: Path,
) -> None:
    content = b"%PDF-1.7\n" + b"x" * 128
    response = Response(content)
    requests: list[tuple[Any, float]] = []
    store = FilesystemOfficialDocumentStore(tmp_path)

    def opener(request: Any, *, timeout: float) -> Response:
        requests.append((request, timeout))
        return response

    def extractor(raw: bytes) -> tuple[str, ...]:
        # Parsing is not allowed to begin until immutable raw bytes exist.
        import hashlib

        assert store.get_raw(hashlib.sha256(raw).hexdigest()) is not None
        return ("가" * 200_000, "둘째 페이지")

    document_worker = OfficialDocumentWorker(
        store,
        parser_version="fixture-v1",
        opener=opener,
        page_extractor=extractor,
        max_bytes=1_000,
        chunk_bytes=17,
        timeout=7.5,
        clock=lambda: NOW,
    )

    result = document_worker.process(OfficialDocumentKind.MINUTES, MINUTES_URL)

    assert result.byte_count == len(content)
    assert result.page_count == 2
    assert result.character_count == 200_000 + len("\n\n둘째 페이지")
    assert result.document.full_text == "가" * 200_000 + "\n\n둘째 페이지"
    assert [segment.locator for segment in result.document.segments] == ["p.1", "p.2"]
    assert result.source_hash
    assert result.text_hash == result.document.text_hash
    assert result.cache_hit is False
    assert result.to_dict()["characters"] == result.character_count
    assert all(size <= 17 for size in response.read_sizes if size >= 0)
    assert requests[0][1] == 7.5
    headers = dict(requests[0][0].header_items())
    assert headers["User-agent"] == (
        f"Mozilla/5.0 (compatible; KoreanBillDebateMCP/{__version__})"
    )
    assert not any(
        "key" in name.casefold() or "authorization" in name.casefold()
        for name in headers
    )


def test_same_source_hash_and_parser_version_is_an_idempotent_cache_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"%PDF-1.4 cached"
    opener_calls = 0
    parser_calls = 0

    def opener(*_args: Any, **_kwargs: Any) -> Response:
        nonlocal opener_calls
        opener_calls += 1
        return Response(content)

    def extractor(_raw: bytes) -> tuple[str, ...]:
        nonlocal parser_calls
        parser_calls += 1
        return ("전체 원문",)

    document_worker = worker(tmp_path, opener=opener, page_extractor=extractor)
    put_raw_calls = 0
    get_raw_calls = 0
    original_put_raw = document_worker.store.put_raw
    original_get_raw = document_worker.store.get_raw

    def counted_put_raw(raw: Any) -> Any:
        nonlocal put_raw_calls
        put_raw_calls += 1
        return original_put_raw(raw)

    def counted_get_raw(source_hash: str) -> Any:
        nonlocal get_raw_calls
        get_raw_calls += 1
        return original_get_raw(source_hash)

    monkeypatch.setattr(document_worker.store, "put_raw", counted_put_raw)
    monkeypatch.setattr(document_worker.store, "get_raw", counted_get_raw)

    first = document_worker.process(OfficialDocumentKind.MINUTES, MINUTES_URL)
    first_get_raw_calls = get_raw_calls
    second = document_worker.process(OfficialDocumentKind.MINUTES, MINUTES_URL)
    refreshed = document_worker.process(
        OfficialDocumentKind.MINUTES, MINUTES_URL, refresh=True
    )

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert refreshed.cache_hit is True
    assert first.source_hash == second.source_hash == refreshed.source_hash
    assert first.text_hash == second.text_hash == refreshed.text_hash
    assert opener_calls == 2
    assert parser_calls == 1
    # A warm parsed cache verifies the source through metadata/stat only. It
    # never transfers the preserved PDF body again.
    assert get_raw_calls == first_get_raw_calls
    assert document_worker.hydrate(replace(second, document=None)) == second
    # The URL-cache hit is already immutable and validated. Only the first and
    # explicit refresh downloads need a raw-store write.
    assert put_raw_calls == 2


def test_kind_specific_official_hosts_are_enforced_before_network(tmp_path: Path) -> None:
    calls = 0

    def opener(*_args: Any, **_kwargs: Any) -> Response:
        nonlocal calls
        calls += 1
        return Response(b"%PDF-1.4")

    document_worker = worker(tmp_path, opener=opener, page_extractor=lambda _raw: ("x",))

    for kind, url in (
        (OfficialDocumentKind.MINUTES, REVIEW_URL),
        (OfficialDocumentKind.REVIEW_REPORT, MINUTES_URL),
        (OfficialDocumentKind.MINUTES, "https://example.com/minutes.pdf"),
    ):
        with pytest.raises(PermanentDocumentError) as raised:
            document_worker.process(kind, url)
        assert raised.value.code == "invalid_official_host"
        assert raised.value.disposition is FailureDisposition.PERMANENT
        assert raised.value.retryable is False
    assert calls == 0

    review = document_worker.process(OfficialDocumentKind.REVIEW_REPORT, REVIEW_URL)
    assert review.kind is OfficialDocumentKind.REVIEW_REPORT
    assert calls == 1


def test_bill_text_reverifies_identity_extracts_only_original_and_keeps_full_text(
    tmp_path: Path,
) -> None:
    original = b"%PDF-1.7 original bill text"
    review = b"%PDF-1.7 committee review"
    archive = bill_archive(
        ("2212345_의사국 의안과_의안원문.pdf", original),
        ("2212345_법제사법위원회_검토보고서.pdf", review),
    )
    identity = b"""
    <input id="billId" value="PRC_TEST_22">
    <input id="billNo" value="2212345">
    """
    requests: list[Any] = []

    def opener(request: Any, **_kwargs: Any) -> Response:
        requests.append(request)
        if request.get_method() == "GET":
            return Response(identity)
        return Response(
            archive,
            headers={"Content-Type": "application/zip", "Content-Length": str(len(archive))},
        )

    text = "의안 원문 " + "가" * 200_000
    document_worker = worker(
        tmp_path,
        opener=opener,
        page_extractor=lambda raw: (text,) if raw == original else (),
        max_bytes=1_000_000,
    )

    result = document_worker.process(OfficialDocumentKind.BILL_TEXT, bill_text_url())

    assert [request.get_method() for request in requests] == ["GET", "POST"]
    assert requests[1].full_url.endswith("/bill/bi/bill/detail/downloadDtlZip.do")
    assert requests[1].get_header("User-agent") == (
        f"Mozilla/5.0 (compatible; KoreanBillDebateMCP/{__version__})"
    )
    assert b"billId=PRC_TEST_22" in requests[1].data
    assert result.kind is OfficialDocumentKind.BILL_TEXT
    assert result.source_hash == hashlib.sha256(original).hexdigest()
    assert result.document.full_text == text
    assert result.character_count == len(text)


def test_bill_text_identity_mismatch_never_downloads_or_attaches_archive(
    tmp_path: Path,
) -> None:
    requests: list[Any] = []

    def opener(request: Any, **_kwargs: Any) -> Response:
        requests.append(request)
        return Response(
            b'<input id="billId" value="PRC_OTHER"><input id="billNo" value="2212345">'
        )

    with pytest.raises(PermanentDocumentError) as raised:
        worker(tmp_path, opener=opener).process(
            OfficialDocumentKind.BILL_TEXT,
            bill_text_url(),
        )

    assert raised.value.code == "bill_identity_unverified"
    assert [request.get_method() for request in requests] == ["GET"]


def test_bill_text_archive_never_chooses_arbitrarily_between_originals(
    tmp_path: Path,
) -> None:
    identity = b"""
    <input id="billId" value="PRC_TEST_22">
    <input id="billNo" value="2212345">
    """
    archive = bill_archive(
        ("2212345_의안원문.pdf", b"%PDF-one"),
        ("2212345_수정_의안원문.pdf", b"%PDF-two"),
    )

    def opener(request: Any, **_kwargs: Any) -> Response:
        return Response(identity if request.get_method() == "GET" else archive)

    with pytest.raises(PermanentDocumentError) as raised:
        worker(tmp_path, opener=opener).process(
            OfficialDocumentKind.BILL_TEXT,
            bill_text_url(),
        )

    assert raised.value.code == "bill_text_ambiguous"


@pytest.mark.parametrize("status", (429, 500, 503))
def test_rate_limits_and_server_errors_are_transient(
    tmp_path: Path, status: int
) -> None:
    def opener(*_args: Any, **_kwargs: Any) -> Response:
        raise urllib.error.HTTPError(MINUTES_URL, status, "failure", {}, None)

    with pytest.raises(TransientDocumentError) as raised:
        worker(tmp_path, opener=opener).process(OfficialDocumentKind.MINUTES, MINUTES_URL)

    assert raised.value.code == f"http_{status}"
    assert raised.value.status_code == status
    assert raised.value.retryable is True


def test_non_retryable_http_and_stream_size_limit_are_explicit(tmp_path: Path) -> None:
    not_found = worker(tmp_path / "not-found", opener=lambda *_a, **_k: Response(b"", status=404))
    with pytest.raises(PermanentDocumentError) as raised:
        not_found.process(OfficialDocumentKind.MINUTES, MINUTES_URL)
    assert raised.value.code == "http_404"
    assert raised.value.retryable is False

    oversized = worker(
        tmp_path / "large",
        opener=lambda *_a, **_k: Response(b"%PDF-" + b"x" * 100),
        max_bytes=20,
    )
    with pytest.raises(PermanentDocumentError) as raised:
        oversized.process(OfficialDocumentKind.MINUTES, MINUTES_URL)
    assert raised.value.code == "document_too_large"
    assert "20 bytes" in str(raised.value)


def test_non_pdf_is_rejected_without_polluting_raw_store(tmp_path: Path) -> None:
    store = FilesystemOfficialDocumentStore(tmp_path)
    document_worker = OfficialDocumentWorker(
        store,
        parser_version="v1",
        opener=lambda *_a, **_k: Response(b"<html>temporary error</html>"),
        clock=lambda: NOW,
    )

    with pytest.raises(PermanentDocumentError) as raised:
        document_worker.process(OfficialDocumentKind.MINUTES, MINUTES_URL)

    assert raised.value.code == "not_pdf"
    assert store.latest_raw_for_url(MINUTES_URL) is None


def test_damaged_pdf_is_preserved_before_permanent_parse_failure(tmp_path: Path) -> None:
    content = b"%PDF-1.7\nthis is not a complete PDF"
    store = FilesystemOfficialDocumentStore(tmp_path)
    document_worker = OfficialDocumentWorker(
        store,
        parser_version="pypdf-v1",
        opener=lambda *_a, **_k: Response(content),
        clock=lambda: NOW,
    )

    with pytest.raises(PermanentDocumentError) as raised:
        document_worker.process(OfficialDocumentKind.MINUTES, MINUTES_URL)

    assert raised.value.code == "damaged_pdf"
    assert raised.value.retryable is False
    raw = store.latest_raw_for_url(MINUTES_URL)
    assert raw is not None
    assert raw.content == content


def test_redirect_to_non_official_host_is_permanent(tmp_path: Path) -> None:
    document_worker = worker(
        tmp_path,
        opener=lambda *_a, **_k: Response(
            b"%PDF-1.4", final_url="https://example.com/stolen.pdf"
        ),
        page_extractor=lambda _raw: ("x",),
    )

    with pytest.raises(PermanentDocumentError) as raised:
        document_worker.process(OfficialDocumentKind.MINUTES, MINUTES_URL)

    assert raised.value.code == "invalid_official_host"


def test_network_timeout_is_transient_and_exposes_queue_metadata(tmp_path: Path) -> None:
    def opener(*_args: Any, **_kwargs: Any) -> Response:
        raise TimeoutError("timed out")

    with pytest.raises(TransientDocumentError) as raised:
        worker(tmp_path, opener=opener).process(OfficialDocumentKind.MINUTES, MINUTES_URL)

    assert raised.value.to_dict() == {
        "code": "network_error",
        "message": "official PDF request failed: timed out",
        "disposition": "transient",
        "retryable": True,
        "status_code": None,
    }


def test_content_length_limit_fails_before_body_read(tmp_path: Path) -> None:
    response = Response(
        b"%PDF-1.4 content",
        headers={"Content-Length": "1001"},
    )
    document_worker = worker(tmp_path, opener=lambda *_a, **_k: response, max_bytes=1000)

    with pytest.raises(PermanentDocumentError) as raised:
        document_worker.process(OfficialDocumentKind.MINUTES, MINUTES_URL)

    assert raised.value.code == "document_too_large"
    assert response.read_sizes == []
