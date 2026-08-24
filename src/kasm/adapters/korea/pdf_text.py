"""Portable text extraction for official Assembly PDFs."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

FallbackExtractor = Callable[[Path], str]


def extract_pdf_text(
    pdf_path: Path,
    text_path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    fallback_extractor: FallbackExtractor | None = None,
    timeout: float | None = None,
) -> None:
    """Prefer Poppler and fall back to pure-Python extraction on serverless hosts."""
    try:
        runner(
            ["pdftotext", "-layout", str(pdf_path), str(text_path)],
            check=True,
            capture_output=True,
            text=True,
            **({"timeout": timeout} if timeout is not None else {}),
        )
        return
    except FileNotFoundError:
        pass
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"pdftotext failed: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        # Poppler can leave a truncated output file when it is terminated.  A
        # later request must extract again instead of treating that partial
        # artifact as a valid cache hit.
        text_path.unlink(missing_ok=True)
        raise RuntimeError("pdftotext exceeded the document extraction deadline") from exc

    if fallback_extractor is None and timeout is not None:
        _extract_with_pypdf_subprocess(pdf_path, text_path, timeout=timeout)
        return
    extractor = fallback_extractor or _extract_with_pypdf
    try:
        extracted = extractor(pdf_path)
    except Exception as exc:
        raise RuntimeError(f"Python PDF extraction failed: {exc}") from exc
    if not extracted.strip():
        raise RuntimeError("Python PDF extraction returned no text")
    text_path.write_text(extracted, encoding="utf-8")


def _extract_with_pypdf_subprocess(
    pdf_path: Path,
    text_path: Path,
    *,
    timeout: float,
) -> None:
    """Run the pure-Python fallback in a killable process with a hard timeout."""

    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "kasm.adapters.korea.pdf_text",
                "--pypdf-worker",
                str(pdf_path),
                str(text_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        text_path.unlink(missing_ok=True)
        raise RuntimeError("Python PDF extraction exceeded the document deadline") from exc
    if completed.returncode != 0:
        text_path.unlink(missing_ok=True)
        detail = " ".join(completed.stderr.split())[:300]
        raise RuntimeError(
            "Python PDF extraction failed" + (f": {detail}" if detail else "")
        )
    if not text_path.exists() or not text_path.read_text(encoding="utf-8").strip():
        text_path.unlink(missing_ok=True)
        raise RuntimeError("Python PDF extraction returned no text")


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


def _worker_main(arguments: list[str]) -> int:
    if len(arguments) != 3 or arguments[0] != "--pypdf-worker":
        return 2
    pdf_path = Path(arguments[1])
    text_path = Path(arguments[2])
    try:
        extracted = _extract_with_pypdf(pdf_path)
        if not extracted.strip():
            return 1
        text_path.write_text(extracted, encoding="utf-8")
    except Exception as exc:  # pragma: no cover - surfaced by parent process
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through parent process
    raise SystemExit(_worker_main(sys.argv[1:]))
