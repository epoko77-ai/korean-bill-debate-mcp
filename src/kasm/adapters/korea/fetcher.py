"""Safe retrieval and local text extraction for official Assembly minutes PDFs."""

from __future__ import annotations

import hashlib
import subprocess
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .pdf_text import FallbackExtractor, extract_pdf_text

ALLOWED_MINUTES_HOST = "record.assembly.go.kr"


@dataclass(frozen=True, slots=True)
class FetchedMinutes:
    source_url: str
    source_hash: str
    pdf_path: Path
    text_path: Path
    text: str


class MinutesFetcher:
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

    def fetch(self, source_url: str, *, refresh: bool = False) -> FetchedMinutes:
        if urlsplit(source_url).hostname != ALLOWED_MINUTES_HOST:
            raise ValueError("minutes URL must use the official record.assembly.go.kr host")
        fingerprint = hashlib.sha256(source_url.encode()).hexdigest()[:24]
        pdf_path = self.cache_dir / "minutes" / f"{fingerprint}.pdf"
        text_path = self.cache_dir / "minutes" / f"{fingerprint}.txt"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        if refresh or not pdf_path.exists():
            request = urllib.request.Request(
                source_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; KASM/0.1)",
                    "Referer": "https://open.assembly.go.kr/",
                },
            )
            try:
                with self._opener(request, timeout=self.timeout) as response:  # type: ignore[attr-defined]
                    raw = response.read()
            except OSError as exc:
                raise RuntimeError(f"official minutes request failed: {exc}") from exc
            if not raw.startswith(b"%PDF-"):
                raise RuntimeError("official minutes response is not a PDF")
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
        return FetchedMinutes(
            source_url=source_url,
            source_hash=source_hash,
            pdf_path=pdf_path,
            text_path=text_path,
            text=text_path.read_text(encoding="utf-8"),
        )
