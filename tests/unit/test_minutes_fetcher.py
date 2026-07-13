from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kasm.adapters.korea.fetcher import MinutesFetcher


class Response:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self.raw


def test_fetches_only_official_pdf_and_extracts_text(tmp_path: Path) -> None:
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        return Response(b"%PDF-1.4 fixture")

    def runner(command, **_kwargs):
        Path(command[-1]).write_text("○위원장 홍길동  개의합니다.", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    fetcher = MinutesFetcher(tmp_path, opener=opener, runner=runner)
    result = fetcher.fetch(
        "https://record.assembly.go.kr/assembly/viewer/minutes/download/pdf.do?id=1"
    )
    assert result.text.startswith("○위원장")
    assert result.source_hash
    assert requests[0].headers["Referer"] == "https://open.assembly.go.kr/"


def test_falls_back_to_python_extraction_when_poppler_is_missing(tmp_path: Path) -> None:
    def missing_runner(*_args, **_kwargs):
        raise FileNotFoundError("pdftotext")

    fetcher = MinutesFetcher(
        tmp_path,
        opener=lambda *_args, **_kwargs: Response(b"%PDF-1.4 minutes"),
        runner=missing_runner,
        fallback_extractor=lambda _path: "위원의 질문과 정부의 답변",
    )

    result = fetcher.fetch("https://record.assembly.go.kr/minutes.pdf")

    assert "정부의 답변" in result.text


def test_rejects_non_official_or_non_pdf_sources(tmp_path: Path) -> None:
    fetcher = MinutesFetcher(tmp_path, opener=lambda *_args, **_kwargs: Response(b"html"))
    with pytest.raises(ValueError):
        fetcher.fetch("https://example.com/minutes.pdf")
    with pytest.raises(RuntimeError, match="not a PDF"):
        fetcher.fetch("https://record.assembly.go.kr/minutes.pdf")
