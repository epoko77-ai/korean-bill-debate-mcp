"""Offline worker for preserving and parsing official Assembly PDFs.

The MCP request path only schedules this work.  A worker validates and downloads
the public official document, stores the immutable source bytes, and only then
parses every PDF page into locator-preserving text.  No Open Assembly or LLM API
credential is accepted by this module.
"""

from __future__ import annotations

import io
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from http.client import IncompleteRead
from io import BytesIO
from typing import Any, Final, Never

from kasm import __version__
from kasm.adapters.korea.documents import (
    BILL_DETAIL_HOST,
    BILL_DOCUMENT_ARCHIVE_URL,
    BillDocumentIdentityError,
    BillDocumentsClient,
)

from .documents import (
    OfficialDocumentKind,
    OfficialDocumentSource,
    OfficialDocumentStore,
    ParsedOfficialDocument,
    RawOfficialDocument,
    TextSegment,
    now_utc,
)

_MINUTES_HOST: Final = "record.assembly.go.kr"
_BILL_DOCUMENT_HOST: Final = "likms.assembly.go.kr"
_DEFAULT_MAX_BYTES: Final = 50 * 1024 * 1024
_DEFAULT_CHUNK_BYTES: Final = 64 * 1024
_BILL_ID: Final = re.compile(r"[A-Za-z0-9_]+")
_BILL_NO: Final = re.compile(r"\d{7}")
_USER_AGENT: Final = f"Mozilla/5.0 (compatible; KoreanBillDebateMCP/{__version__})"

PDFPageExtractor = Callable[[bytes], Sequence[str]]


class FailureDisposition(StrEnum):
    """Whether a queue should retry a failed document job."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"


class DocumentWorkerError(RuntimeError):
    """Base error with stable machine-readable retry semantics."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        disposition: FailureDisposition,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.disposition = disposition
        self.status_code = status_code

    @property
    def retryable(self) -> bool:
        return self.disposition is FailureDisposition.TRANSIENT

    def to_dict(self) -> dict[str, str | int | bool | None]:
        return {
            "code": self.code,
            "message": str(self),
            "disposition": self.disposition.value,
            "retryable": self.retryable,
            "status_code": self.status_code,
        }


class TransientDocumentError(DocumentWorkerError):
    """A timeout, rate limit, or server failure that can be retried."""

    def __init__(
        self, message: str, *, code: str, status_code: int | None = None
    ) -> None:
        super().__init__(
            message,
            code=code,
            disposition=FailureDisposition.TRANSIENT,
            status_code=status_code,
        )


class PermanentDocumentError(DocumentWorkerError):
    """An invalid URL, oversized payload, or unusable PDF that should not retry."""

    def __init__(
        self, message: str, *, code: str, status_code: int | None = None
    ) -> None:
        super().__init__(
            message,
            code=code,
            disposition=FailureDisposition.PERMANENT,
            status_code=status_code,
        )


@dataclass(frozen=True, slots=True)
class DocumentWorkResult:
    """Observable output and losslessness accounting for one worker job."""

    kind: OfficialDocumentKind
    official_url: str
    parser_version: str
    byte_count: int
    page_count: int
    character_count: int
    source_hash: str
    text_hash: str
    cache_hit: bool
    raw_object_key: str
    parsed_object_key: str
    document: ParsedOfficialDocument | None

    def __post_init__(self) -> None:
        if (
            self.byte_count < 1
            or self.page_count < 1
            or self.character_count < 0
            or not self.parser_version.strip()
        ):
            raise ValueError("document work accounting is invalid")
        if self.document is not None and (
            self.document.kind is not self.kind
            or self.document.official_url != self.official_url
            or self.document.source_hash != self.source_hash
            or self.document.parser_version != self.parser_version
            or self.document.text_hash != self.text_hash
            or len(self.document.segments) != self.page_count
            or len(self.document.full_text) != self.character_count
        ):
            raise ValueError("document work result does not match its parsed document")

    def to_dict(self) -> dict[str, str | int | bool]:
        return {
            "kind": self.kind.value,
            "official_url": self.official_url,
            "parser_version": self.parser_version,
            "bytes": self.byte_count,
            "pages": self.page_count,
            "characters": self.character_count,
            "source_hash": self.source_hash,
            "text_hash": self.text_hash,
            "cache_hit": self.cache_hit,
            "raw_object_key": self.raw_object_key,
            "parsed_object_key": self.parsed_object_key,
        }


class OfficialDocumentWorker:
    """Download, preserve, and losslessly parse public official PDFs."""

    def __init__(
        self,
        store: OfficialDocumentStore,
        *,
        parser_version: str,
        timeout: float = 30.0,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        chunk_bytes: int = _DEFAULT_CHUNK_BYTES,
        opener: Callable[..., Any] = urllib.request.urlopen,
        page_extractor: PDFPageExtractor | None = None,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        if not parser_version.strip():
            raise ValueError("parser_version is required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_bytes < 1 or chunk_bytes < 1:
            raise ValueError("max_bytes and chunk_bytes must be positive")
        self.store = store
        self.parser_version = parser_version
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.chunk_bytes = min(chunk_bytes, max_bytes + 1)
        self._opener = opener
        self._page_extractor = page_extractor or _extract_pdf_pages
        self._clock = clock

    def process(
        self,
        kind: OfficialDocumentKind,
        official_url: str,
        *,
        refresh: bool = False,
    ) -> DocumentWorkResult:
        """Process one document, reusing a matching immutable parse when possible."""

        _validate_kind_url(kind, official_url)
        source = None if refresh else self.store.latest_source_for_url(official_url)
        if source is not None:
            if source.kind is not kind:
                raise PermanentDocumentError(
                    "cached official document kind does not match the requested kind",
                    code="cached_kind_mismatch",
                )
            cached = self.store.get_parsed(source.source_hash, self.parser_version)
            if cached is not None:
                if cached.kind is not kind or cached.official_url != official_url:
                    raise PermanentDocumentError(
                        "cached parsed document identity does not match the job",
                        code="cached_identity_mismatch",
                    )
                return _cached_result(source, cached)
            raw = self.store.get_raw(source.source_hash)
            if raw is None:
                raise RuntimeError("cached official document source is missing")
            downloaded = False
        else:
            raw = self._download(kind, official_url)
            downloaded = True
        if downloaded:
            # A freshly downloaded PDF is preserved before cache lookup and
            # parsing. A URL-cache hit already passed this immutable store's
            # validation, so re-putting it only repeats Blob reads and hashes.
            self.store.put_raw(raw)
        cached = self.store.get_parsed(raw.source_hash, self.parser_version)
        if cached is not None:
            if cached.kind is not kind or cached.official_url != official_url:
                raise PermanentDocumentError(
                    "cached parsed document identity does not match the job",
                    code="cached_identity_mismatch",
                )
            return _result(raw, cached, cache_hit=True)

        parsed = self._parse(raw)
        self.store.put_parsed(parsed)
        return _result(raw, parsed, cache_hit=False)

    def hydrate(self, result: DocumentWorkResult) -> DocumentWorkResult:
        """Restore and verify a compact run result from the global parsed cache."""

        if result.document is not None:
            return result
        parsed = self.store.get_parsed(result.source_hash, result.parser_version)
        if parsed is None:
            raise RuntimeError("parsed official document referenced by run result is missing")
        hydrated = replace(result, document=parsed)
        if hydrated.parsed_object_key != parsed.object_key:
            raise RuntimeError("parsed official document object key does not match run result")
        return hydrated

    def _download(
        self, kind: OfficialDocumentKind, official_url: str
    ) -> RawOfficialDocument:
        if kind is OfficialDocumentKind.BILL_TEXT:
            return self._download_bill_text(official_url)
        request = urllib.request.Request(
            official_url,
            headers={
                "User-Agent": _USER_AGENT,
                "Referer": _referer(kind),
                "Accept": "application/pdf",
            },
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status = _response_status(response)
                if status >= 400:
                    _raise_http_status(status)
                final_url = _response_url(response, official_url)
                _validate_kind_url(kind, final_url)
                content_length = _content_length(response)
                if content_length is not None and content_length > self.max_bytes:
                    raise PermanentDocumentError(
                        f"official PDF exceeds configured maximum of {self.max_bytes} bytes",
                        code="document_too_large",
                    )
                content = self._read_bounded(response)
        except DocumentWorkerError:
            raise
        except urllib.error.HTTPError as exc:
            _raise_http_status(exc.code)
        except (TimeoutError, urllib.error.URLError, IncompleteRead, OSError) as exc:
            raise TransientDocumentError(
                f"official PDF request failed: {exc}", code="network_error"
            ) from exc
        if not content.startswith(b"%PDF-"):
            raise PermanentDocumentError(
                "official document response does not have PDF magic bytes",
                code="not_pdf",
            )
        return RawOfficialDocument(
            kind=kind,
            official_url=official_url,
            media_type="application/pdf",
            content=content,
            retrieved_at=self._clock(),
        )

    def _download_bill_text(self, official_url: str) -> RawOfficialDocument:
        """Fetch one exact original-bill PDF from the official document ZIP.

        The National Assembly UI exposes original bill text only through a
        POSTed ZIP.  The public source URL embeds the exact ``billId`` and
        seven-digit ``billNo``.  Before downloading, the full official detail
        page must prove that pair.  The archive is then accepted only when it
        contains exactly one safely named original-PDF member for that bill.
        """

        bill_id, bill_no = _bill_text_source_identity(official_url)
        try:
            BillDocumentsClient(
                timeout=self.timeout,
                opener=self._opener,
            ).verify_bill_identity(bill_id, bill_no)
        except BillDocumentIdentityError:
            raise PermanentDocumentError(
                "official bill detail did not verify the bill-text identifier pair",
                code="bill_identity_unverified",
            ) from None
        except (OSError, RuntimeError, TimeoutError) as exc:
            raise TransientDocumentError(
                f"official bill identity request failed: {exc}",
                code="bill_identity_unavailable",
            ) from exc

        body = urllib.parse.urlencode(
            {
                "billId": bill_id,
                "billKindCd": "법률안",
                "dwFileGbn": "B",
            }
        ).encode()
        detail_query = urllib.parse.urlencode(
            {"billId": bill_id, "ageFrom": bill_no[:2], "ageTo": bill_no[:2]}
        )
        request = urllib.request.Request(
            BILL_DOCUMENT_ARCHIVE_URL,
            data=body,
            headers={
                "User-Agent": _USER_AGENT,
                "Referer": (
                    f"https://{BILL_DETAIL_HOST}/bill/billDetail.do?{detail_query}"
                ),
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/zip",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status = _response_status(response)
                if status >= 400:
                    _raise_http_status(status)
                _validate_bill_archive_response_url(
                    _response_url(response, BILL_DOCUMENT_ARCHIVE_URL)
                )
                content_length = _content_length(response)
                if content_length is not None and content_length > self.max_bytes:
                    raise PermanentDocumentError(
                        "official bill archive exceeds configured maximum of "
                        f"{self.max_bytes} bytes",
                        code="document_too_large",
                    )
                archive = self._read_bounded(response)
        except DocumentWorkerError:
            raise
        except urllib.error.HTTPError as exc:
            _raise_http_status(exc.code)
        except (TimeoutError, urllib.error.URLError, IncompleteRead, OSError) as exc:
            raise TransientDocumentError(
                f"official bill archive request failed: {exc}",
                code="network_error",
            ) from exc

        original_pdf = _extract_exact_bill_text_pdf(
            archive,
            bill_no=bill_no,
            max_bytes=self.max_bytes,
            chunk_bytes=self.chunk_bytes,
        )
        return RawOfficialDocument(
            kind=OfficialDocumentKind.BILL_TEXT,
            official_url=official_url,
            media_type="application/pdf",
            content=original_pdf,
            retrieved_at=self._clock(),
        )

    def _read_bounded(self, response: Any) -> bytes:
        chunks: list[bytes] = []
        received = 0
        while True:
            remaining = self.max_bytes + 1 - received
            if remaining <= 0:
                raise PermanentDocumentError(
                    f"official PDF exceeds configured maximum of {self.max_bytes} bytes",
                    code="document_too_large",
                )
            raw_chunk = response.read(min(self.chunk_bytes, remaining))
            if not isinstance(raw_chunk, bytes):
                raise PermanentDocumentError(
                    "official PDF response returned non-byte content",
                    code="invalid_response_body",
                )
            if not raw_chunk:
                break
            chunks.append(raw_chunk)
            received += len(raw_chunk)
            if received > self.max_bytes:
                raise PermanentDocumentError(
                    f"official PDF exceeds configured maximum of {self.max_bytes} bytes",
                    code="document_too_large",
                )
        return b"".join(chunks)

    def _parse(self, raw: RawOfficialDocument) -> ParsedOfficialDocument:
        try:
            extracted = self._page_extractor(raw.content)
            if isinstance(extracted, (str, bytes)):
                raise PermanentDocumentError(
                    "PDF parser must return one text value per page",
                    code="parser_contract_error",
                )
            pages = tuple(extracted)
        except DocumentWorkerError:
            raise
        except Exception as exc:
            raise PermanentDocumentError(
                f"official PDF is damaged or cannot be parsed: {exc}",
                code="damaged_pdf",
            ) from exc
        if not pages:
            raise PermanentDocumentError(
                "official PDF contains no readable pages",
                code="damaged_pdf",
            )
        if any(not isinstance(text, str) for text in pages):
            raise PermanentDocumentError(
                "PDF parser returned a non-text page",
                code="parser_contract_error",
            )
        segments = tuple(
            TextSegment(locator=f"p.{number}", text=text)
            for number, text in enumerate(pages, start=1)
        )
        warnings = tuple(
            f"p.{number}: no extractable text"
            for number, text in enumerate(pages, start=1)
            if not text.strip()
        )
        return ParsedOfficialDocument(
            kind=raw.kind,
            official_url=raw.official_url,
            source_hash=raw.source_hash,
            parser_version=self.parser_version,
            parsed_at=self._clock(),
            segments=segments,
            warnings=warnings,
        )


def _result(
    raw: RawOfficialDocument,
    parsed: ParsedOfficialDocument,
    *,
    cache_hit: bool,
) -> DocumentWorkResult:
    return DocumentWorkResult(
        kind=raw.kind,
        official_url=raw.official_url,
        parser_version=parsed.parser_version,
        byte_count=len(raw.content),
        page_count=len(parsed.segments),
        character_count=len(parsed.full_text),
        source_hash=raw.source_hash,
        text_hash=parsed.text_hash,
        cache_hit=cache_hit,
        raw_object_key=raw.object_key,
        parsed_object_key=parsed.object_key,
        document=parsed,
    )


def _cached_result(
    source: OfficialDocumentSource,
    parsed: ParsedOfficialDocument,
) -> DocumentWorkResult:
    return DocumentWorkResult(
        kind=source.kind,
        official_url=source.official_url,
        parser_version=parsed.parser_version,
        byte_count=source.byte_count,
        page_count=len(parsed.segments),
        character_count=len(parsed.full_text),
        source_hash=source.source_hash,
        text_hash=parsed.text_hash,
        cache_hit=True,
        raw_object_key=source.object_key,
        parsed_object_key=parsed.object_key,
        document=parsed,
    )


def _validate_kind_url(kind: OfficialDocumentKind, official_url: str) -> None:
    parsed = urllib.parse.urlsplit(official_url)
    if kind is OfficialDocumentKind.BILL_TEXT:
        _bill_text_source_identity(official_url)
        return
    expected_host = (
        _MINUTES_HOST
        if kind is OfficialDocumentKind.MINUTES
        else _BILL_DOCUMENT_HOST
    )
    if parsed.scheme != "https" or parsed.hostname != expected_host:
        raise PermanentDocumentError(
            f"{kind.value} URL must use the official {expected_host} HTTPS host",
            code="invalid_official_host",
        )


def _referer(kind: OfficialDocumentKind) -> str:
    if kind is OfficialDocumentKind.MINUTES:
        return "https://open.assembly.go.kr/"
    return f"https://{_BILL_DOCUMENT_HOST}/bill/"


def _bill_text_source_identity(official_url: str) -> tuple[str, str]:
    """Validate and decode the exact official bill-text source descriptor."""

    parsed = urllib.parse.urlsplit(official_url)
    archive = urllib.parse.urlsplit(BILL_DOCUMENT_ARCHIVE_URL)
    if (
        parsed.scheme != "https"
        or parsed.hostname != BILL_DETAIL_HOST
        or parsed.path != archive.path
        or parsed.fragment
    ):
        raise PermanentDocumentError(
            "bill_text URL must use the official Assembly bill archive endpoint",
            code="invalid_official_host",
        )
    try:
        values = urllib.parse.parse_qs(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError:
        raise PermanentDocumentError(
            "bill_text source parameters are invalid",
            code="invalid_bill_text_source",
        ) from None
    expected = {"billId", "billNo", "billKindCd", "dwFileGbn"}
    if set(values) != expected or any(len(item) != 1 for item in values.values()):
        raise PermanentDocumentError(
            "bill_text source requires one exact identifier pair",
            code="invalid_bill_text_source",
        )
    bill_id = values["billId"][0]
    bill_no = values["billNo"][0]
    if (
        not _BILL_ID.fullmatch(bill_id)
        or not _BILL_NO.fullmatch(bill_no)
        or values["billKindCd"] != ["법률안"]
        or values["dwFileGbn"] != ["B"]
    ):
        raise PermanentDocumentError(
            "bill_text source identifiers are invalid",
            code="invalid_bill_text_source",
        )
    return bill_id, bill_no


def _validate_bill_archive_response_url(value: str) -> None:
    parsed = urllib.parse.urlsplit(value)
    archive = urllib.parse.urlsplit(BILL_DOCUMENT_ARCHIVE_URL)
    if (
        parsed.scheme != "https"
        or parsed.hostname != BILL_DETAIL_HOST
        or parsed.path != archive.path
    ):
        raise PermanentDocumentError(
            "bill archive redirected outside the official endpoint",
            code="invalid_official_host",
        )


def _extract_exact_bill_text_pdf(
    archive: bytes,
    *,
    bill_no: str,
    max_bytes: int,
    chunk_bytes: int,
) -> bytes:
    """Extract exactly one safe ``의안원문.pdf`` member without truncation."""

    try:
        container = zipfile.ZipFile(io.BytesIO(archive))
    except (OSError, zipfile.BadZipFile):
        raise PermanentDocumentError(
            "official bill document response is not a valid ZIP archive",
            code="invalid_bill_archive",
        ) from None
    with container:
        candidates: list[zipfile.ZipInfo] = []
        name_pattern = re.compile(
            rf"{re.escape(bill_no)}_(?:.+_)?의안원문\.pdf",
            re.IGNORECASE,
        )
        for info in container.infolist():
            normalized = unicodedata.normalize("NFKC", info.filename)
            if (
                info.is_dir()
                or "/" in normalized
                or "\\" in normalized
                or not name_pattern.fullmatch(normalized)
            ):
                continue
            if info.flag_bits & 0x1:
                raise PermanentDocumentError(
                    "official original bill PDF is encrypted in its archive",
                    code="encrypted_bill_text",
                )
            if info.file_size > max_bytes:
                raise PermanentDocumentError(
                    f"official original bill PDF exceeds configured maximum of {max_bytes} bytes",
                    code="document_too_large",
                )
            candidates.append(info)
        if len(candidates) != 1:
            raise PermanentDocumentError(
                "official archive did not contain exactly one original bill PDF",
                code=(
                    "bill_text_missing"
                    if not candidates
                    else "bill_text_ambiguous"
                ),
            )
        try:
            with container.open(candidates[0]) as source:
                chunks: list[bytes] = []
                received = 0
                while True:
                    remaining = max_bytes + 1 - received
                    if remaining <= 0:
                        raise PermanentDocumentError(
                            "official original bill PDF exceeds configured maximum of "
                            f"{max_bytes} bytes",
                            code="document_too_large",
                        )
                    part = source.read(min(chunk_bytes, remaining))
                    if not part:
                        break
                    chunks.append(part)
                    received += len(part)
        except DocumentWorkerError:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile):
            raise PermanentDocumentError(
                "official original bill PDF could not be read from its archive",
                code="damaged_bill_archive",
            ) from None
    pdf = b"".join(chunks)
    if not pdf.startswith(b"%PDF-"):
        raise PermanentDocumentError(
            "official original bill document does not have PDF magic bytes",
            code="not_pdf",
        )
    return pdf


def _response_status(response: Any) -> int:
    raw = getattr(response, "status", None)
    if raw is None:
        getter = getattr(response, "getcode", None)
        raw = getter() if callable(getter) else 200
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise PermanentDocumentError(
            "official PDF response has an invalid HTTP status",
            code="invalid_http_status",
        ) from exc


def _response_url(response: Any, requested_url: str) -> str:
    getter = getattr(response, "geturl", None)
    value = getter() if callable(getter) else requested_url
    return str(value or requested_url)


def _content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    raw = headers.get("Content-Length") if headers is not None else None
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise PermanentDocumentError(
            "official PDF response has an invalid Content-Length",
            code="invalid_content_length",
        ) from exc
    return value if value >= 0 else None


def _raise_http_status(status: int) -> Never:
    if status == 429 or 500 <= status <= 599:
        raise TransientDocumentError(
            f"official PDF server returned HTTP {status}",
            code=f"http_{status}",
            status_code=status,
        )
    raise PermanentDocumentError(
        f"official PDF server returned HTTP {status}",
        code=f"http_{status}",
        status_code=status,
    )


def _extract_pdf_pages(content: bytes) -> tuple[str, ...]:
    """Extract every page with pypdf; callers add stable page locators."""

    from pypdf import PdfReader

    reader = PdfReader(BytesIO(content), strict=False)
    if reader.is_encrypted and reader.decrypt("") == 0:
        raise ValueError("encrypted PDF cannot be opened without a password")
    pages: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except TypeError:  # pragma: no cover - compatibility with older pypdf
            text = page.extract_text() or ""
        pages.append(text)
    return tuple(pages)


__all__ = [
    "DocumentWorkResult",
    "DocumentWorkerError",
    "FailureDisposition",
    "OfficialDocumentWorker",
    "PDFPageExtractor",
    "PermanentDocumentError",
    "TransientDocumentError",
]
