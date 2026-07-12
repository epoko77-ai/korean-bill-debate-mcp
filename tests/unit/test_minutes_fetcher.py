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


def test_rejects_non_official_or_non_pdf_sources(tmp_path: Path) -> None:
    fetcher = MinutesFetcher(tmp_path, opener=lambda *_args, **_kwargs: Response(b"html"))
    with pytest.raises(ValueError):
        fetcher.fetch("https://example.com/minutes.pdf")
    with pytest.raises(RuntimeError, match="not a PDF"):
        fetcher.fetch("https://record.assembly.go.kr/minutes.pdf")
