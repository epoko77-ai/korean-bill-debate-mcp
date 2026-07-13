from dataclasses import dataclass
from pathlib import Path

from kasm.adapters.korea.fetcher import FetchedMinutes
from kasm.adapters.korea.pipeline import OpenAssemblyPipeline, distinct_minutes_rows
from kasm.storage.database import Database
from kasm.storage.repositories import SpeechRepository


@dataclass
class Fetcher:
    root: Path

    def fetch(self, url: str, *, refresh: bool = False) -> FetchedMinutes:
        del refresh
        return FetchedMinutes(
            source_url=url,
            source_hash="verified-pdf-hash",
            pdf_path=self.root / "fixture.pdf",
            text_path=self.root / "fixture.txt",
            text=(
                "○김미래 위원  추가 예산을 확보할 계획입니까?\n"
                "○농림축산식품부차관 박범수  필요한 만큼 확보하겠습니다."
            ),
        )


def test_preview_then_transactional_sync(tmp_path: Path) -> None:
    row = {
        "CONFER_NUM": 52695,
        "TITLE": "제22대 제421회 제2차 국회본회의",
        "CLASS_NAME": "국회본회의",
        "DAE_NUM": 22,
        "CONF_DATE": "2025-01-23",
        "PDF_LINK_URL": "https://record.assembly.go.kr/minutes.pdf?id=52695",
        "CONF_ID": "054706",
    }
    with Database(":memory:") as database:
        pipeline = OpenAssemblyPipeline(database, Fetcher(tmp_path))  # type: ignore[arg-type]
        preview = pipeline.preview(row)
        assert preview.ready_to_commit
        assert preview.parsed_speeches == 2
        result = pipeline.sync(row)
        assert result.speeches_saved == 2
        assert result.relations_saved == 2
        assert len(SpeechRepository(database).for_meeting(result.meeting.id)) == 2


def test_distinct_minutes_rows_deduplicates_agenda_rows() -> None:
    rows = [
        {
            "PDF_LINK_URL": "https://record.assembly.go.kr/a",
            "CONF_ID": "meeting-a",
            "SUB_NAME": "인공지능 기본법안",
            "BILL_NO": "2200001",
        },
        {
            "PDF_LINK_URL": "https://record.assembly.go.kr/a",
            "CONF_ID": "meeting-a",
            "SUB_NAME": "인공지능 산업 진흥법안",
            "BILL_NO": "2200002",
        },
        {
            "PDF_LINK_URL": "https://record.assembly.go.kr/a",
            "CONF_ID": "meeting-a",
            "SUB_NAME": "인공지능 기본법안",
            "BILL_NO": "2200001",
        },
        {
            "PDF_LINK_URL": "https://record.assembly.go.kr/b",
            "CONF_ID": "meeting-b",
            "SUB_NAME": "데이터 기본법안 (의안번호 2200003)",
        },
    ]
    result = distinct_minutes_rows(rows)

    assert [row["CONF_ID"] for row in result] == ["meeting-a", "meeting-b"]
    assert result[0]["SUB_NAME"] == "인공지능 기본법안"
    assert result[0]["BILL_NO"] == "2200001"
    assert result[0]["agenda_items"] == [
        {"bill_no": "2200001", "title": "인공지능 기본법안"},
        {"bill_no": "2200002", "title": "인공지능 산업 진흥법안"},
    ]
    assert result[0]["agenda_text"] == (
        "2200001 인공지능 기본법안\n2200002 인공지능 산업 진흥법안"
    )
    assert result[1]["agenda_items"] == [
        {"bill_no": "2200003", "title": "데이터 기본법안 (의안번호 2200003)"}
    ]

    # The aggregation is non-mutating so raw API rows remain reusable.
    assert "agenda_items" not in rows[0]
