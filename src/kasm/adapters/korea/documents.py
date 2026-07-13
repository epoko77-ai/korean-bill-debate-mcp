"""Discover and fetch official bill review documents from the Assembly bill system."""

from __future__ import annotations

import hashlib
import re
import subprocess
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from kasm import __version__

from .pdf_text import FallbackExtractor, extract_pdf_text

BILL_DETAIL_HOST = "likms.assembly.go.kr"
BILL_DETAIL_URL = f"https://{BILL_DETAIL_HOST}/bill/billDetail.do"
BILL_INFO_URL = f"https://{BILL_DETAIL_HOST}/bill/bi/bill/detail/billInfo.do"
BILL_DOCUMENT_ARCHIVE_URL = (
    f"https://{BILL_DETAIL_HOST}/bill/bi/bill/detail/downloadDtlZip.do"
)
_BILL_ID = re.compile(r"[A-Za-z0-9_]+")
_BILL_NO = re.compile(r"\d{7}")
_USER_AGENT = f"Mozilla/5.0 (compatible; KASM/{__version__})"


class BillDocumentIdentityError(RuntimeError):
    """The official detail response did not prove the requested bill identity."""


@dataclass(frozen=True, slots=True)
class BillDocumentLink:
    document_type: str
    title: str
    file_format: str
    official_url: str


@dataclass(frozen=True, slots=True)
class FetchedBillDocument:
    source_url: str
    source_hash: str
    pdf_path: Path
    text_path: Path
    text: str


class _ReviewReportParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[BillDocumentLink] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        values = {key.casefold(): value or "" for key, value in attrs}
        title = values.get("title", "")
        href = values.get("href", "")
        if "검토보고서" not in title or "PDF" not in title.upper() or not href:
            return
        official_url = urljoin(BILL_INFO_URL, href.replace("&amp;", "&"))
        parsed = urlsplit(official_url)
        if parsed.scheme != "https" or parsed.hostname != BILL_DETAIL_HOST:
            return
        self.links.append(
            BillDocumentLink(
                document_type="committee_review_report",
                title="전문위원 검토보고서",
                file_format="pdf",
                official_url=official_url,
            )
        )


class _BillIdentityParser(HTMLParser):
    """Read the authoritative hidden identifiers from a bill detail page."""

    def __init__(self) -> None:
        super().__init__()
        self.bill_ids: list[str] = []
        self.bill_numbers: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "input":
            return
        values = {key.casefold(): value or "" for key, value in attrs}
        field = (values.get("id") or values.get("name") or "").casefold()
        value = values.get("value", "").strip()
        if field == "billid" and value:
            self.bill_ids.append(value)
        elif field == "billno" and value:
            self.bill_numbers.append(value)


class BillDocumentsClient:
    """Read the official bill detail fragment that lists committee review documents."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        opener: Callable[..., object] = urllib.request.urlopen,
    ) -> None:
        self.timeout = timeout
        self._opener = opener

    def review_reports(self, bill_id: str, bill_no: str) -> tuple[BillDocumentLink, ...]:
        """Return every exact official review-report PDF for one verified bill."""

        self.verify_bill_identity(bill_id, bill_no)
        return self._review_reports(bill_id, bill_no)

    def documents(
        self,
        bill_id: str,
        bill_no: str,
        *,
        include_bill_text: bool = True,
        include_review_reports: bool = True,
    ) -> tuple[BillDocumentLink, ...]:
        """Return the original bill text plus every review report for one bill.

        The official detail page does not expose a stable direct link for the
        original PDF.  Its public UI POSTs the verified ``billId`` to the
        official bulk-document endpoint.  The returned source URL therefore
        carries the exact identifier pair; the document worker re-verifies the
        pair and accepts exactly one ``{billNo}_..._의안원문.pdf`` archive member.
        """

        self.verify_bill_identity(bill_id, bill_no)
        source_query = urllib.parse.urlencode(
            {
                "billId": bill_id,
                "billNo": bill_no,
                "billKindCd": "법률안",
                "dwFileGbn": "B",
            }
        )
        items: list[BillDocumentLink] = []
        if include_bill_text:
            items.append(
                BillDocumentLink(
                    document_type="bill_text",
                    title="의안원문",
                    file_format="pdf",
                    official_url=f"{BILL_DOCUMENT_ARCHIVE_URL}?{source_query}",
                )
            )
        if include_review_reports:
            items.extend(self._review_reports(bill_id, bill_no))
        return tuple(items)

    def _review_reports(
        self, bill_id: str, bill_no: str
    ) -> tuple[BillDocumentLink, ...]:
        if not _BILL_ID.fullmatch(bill_id):
            raise ValueError("bill_id must contain only letters, numbers, and underscores")
        if not _BILL_NO.fullmatch(bill_no):
            raise ValueError("bill_no must contain exactly seven digits")
        body = urllib.parse.urlencode(
            {"billId": bill_id, "billNo": bill_no, "billKindCd": "법률안"}
        ).encode()
        request = urllib.request.Request(
            BILL_INFO_URL,
            data=body,
            headers={
                "User-Agent": _USER_AGENT,
                "Referer": f"https://{BILL_DETAIL_HOST}/bill/billDetail.do?billId={bill_id}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:  # type: ignore[attr-defined]
                raw = response.read()
        except OSError as exc:
            raise RuntimeError(f"official bill documents request failed: {exc}") from exc
        parser = _ReviewReportParser()
        parser.feed(raw.decode("utf-8", errors="replace"))
        return tuple(dict.fromkeys(parser.links))

    def verify_bill_identity(self, bill_id: str, bill_no: str) -> None:
        """Fail closed before accepting links from the billId-only index.

        The review-report endpoint ignores ``billNo`` and selects documents by
        ``billId`` alone.  The full official detail page, however, publishes
        both values together as hidden form fields.  Require that exact pair
        before the fragment response can be associated with a bill.
        """

        if not _BILL_ID.fullmatch(bill_id):
            raise ValueError("bill_id must contain only letters, numbers, and underscores")
        if not _BILL_NO.fullmatch(bill_no):
            raise ValueError("bill_no must contain exactly seven digits")
        assembly_term = bill_no[:2]
        query = urllib.parse.urlencode(
            {"billId": bill_id, "ageFrom": assembly_term, "ageTo": assembly_term}
        )
        request = urllib.request.Request(
            f"{BILL_DETAIL_URL}?{query}",
            headers={"User-Agent": _USER_AGENT},
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:  # type: ignore[attr-defined]
                raw = response.read()
        except OSError as exc:
            raise RuntimeError(f"official bill identity request failed: {exc}") from exc

        parser = _BillIdentityParser()
        parser.feed(raw.decode("utf-8", errors="replace"))
        if set(parser.bill_ids) != {bill_id} or set(parser.bill_numbers) != {bill_no}:
            raise BillDocumentIdentityError(
                "official bill detail did not verify the requested bill identity"
            )


class BillDocumentFetcher:
    """Download and extract text only from official Assembly bill-document PDFs."""

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        timeout: float = 60.0,
        opener: Callable[..., object] = urllib.request.urlopen,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        fallback_extractor: FallbackExtractor | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout
        self._opener = opener
        self._runner = runner
        self._fallback_extractor = fallback_extractor

    def fetch(self, source_url: str, *, refresh: bool = False) -> FetchedBillDocument:
        parsed = urlsplit(source_url)
        if parsed.scheme != "https" or parsed.hostname != BILL_DETAIL_HOST:
            raise ValueError("bill document URL must use the official likms.assembly.go.kr host")
        fingerprint = hashlib.sha256(source_url.encode()).hexdigest()[:24]
        pdf_path = self.cache_dir / "bill-documents" / f"{fingerprint}.pdf"
        text_path = self.cache_dir / "bill-documents" / f"{fingerprint}.txt"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        if refresh or not pdf_path.exists():
            request = urllib.request.Request(
                source_url,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Referer": f"https://{BILL_DETAIL_HOST}/bill/",
                },
            )
            try:
                with self._opener(request, timeout=self.timeout) as response:  # type: ignore[attr-defined]
                    raw = response.read()
            except OSError as exc:
                raise RuntimeError(f"official bill document request failed: {exc}") from exc
            if not raw.startswith(b"%PDF-"):
                raise RuntimeError("official bill document response is not a PDF")
            pdf_path.write_bytes(raw)
        raw = pdf_path.read_bytes()
        source_hash = hashlib.sha256(raw).hexdigest()
        if refresh or not text_path.exists():
            extract_pdf_text(
                pdf_path,
                text_path,
                runner=self._runner,
                fallback_extractor=self._fallback_extractor,
            )
        return FetchedBillDocument(
            source_url=source_url,
            source_hash=source_hash,
            pdf_path=pdf_path,
            text_path=text_path,
            text=text_path.read_text(encoding="utf-8"),
        )
