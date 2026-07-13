"""Portable text extraction for official Assembly PDFs."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

FallbackExtractor = Callable[[Path], str]


def extract_pdf_text(
    pdf_path: Path,
    text_path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    fallback_extractor: FallbackExtractor | None = None,
) -> None:
    """Prefer Poppler and fall back to pure-Python extraction on serverless hosts."""
    try:
        runner(
            ["pdftotext", "-layout", str(pdf_path), str(text_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return
    except FileNotFoundError:
        pass
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"pdftotext failed: {exc.stderr.strip()}") from exc

    extractor = fallback_extractor or _extract_with_pypdf
    try:
        extracted = extractor(pdf_path)
    except Exception as exc:
        raise RuntimeError(f"Python PDF extraction failed: {exc}") from exc
    if not extracted.strip():
        raise RuntimeError("Python PDF extraction returned no text")
    text_path.write_text(extracted, encoding="utf-8")


def _extract_with_pypdf(pdf_path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - packaging protects this path
        raise RuntimeError("pypdf is required when pdftotext is unavailable") from exc

    reader = PdfReader(pdf_path)
    pages: list[str] = []
    for page in reader.pages:
        text = page.extract_text(extraction_mode="layout") or ""
        if text.strip():
            pages.append(text)
    return "\n\f\n".join(pages)
