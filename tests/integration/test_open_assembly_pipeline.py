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
        {"PDF_LINK_URL": "https://record.assembly.go.kr/a", "agenda": "1"},
        {"PDF_LINK_URL": "https://record.assembly.go.kr/a", "agenda": "2"},
        {"PDF_LINK_URL": "https://record.assembly.go.kr/b", "agenda": "3"},
    ]
    assert [row["agenda"] for row in distinct_minutes_rows(rows)] == ["1", "3"]
