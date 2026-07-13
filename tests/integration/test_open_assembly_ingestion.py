from datetime import UTC, datetime

from kasm.adapters.korea.ingestion import OpenAssemblyIngestor
from kasm.app import LocalServices
from kasm.storage.database import Database


def test_open_assembly_transcript_is_saved_and_searchable() -> None:
    row = {
        "DAE_NUM": "22",
        "CONF_DATE": "20250318",
        "CLASS_NAME": "상임위원회",
        "COMM_NAME": "과학기술정보방송통신위원회",
        "DEPT_CD": "ICT",
        "CONFER_NUM": "3",
        "PDF_LINK_URL": "https://record.assembly.go.kr/meeting/official-1",
        "agenda_items": [
            {"bill_no": "2200001", "title": "인공지능 기본법안"},
            {"bill_no": "2200002", "title": "인공지능 산업 진흥법안"},
        ],
    }
    transcript = """1. 인공지능 정책
○위원장 홍길동  회의를 시작합니다.
○김미래 위원  소버린 AI 생태계에 대한 지원이 필요합니다.
○과학기술정보통신부 장관 이영희  관련 예산을 검토하겠습니다.
"""
    with Database(":memory:") as database:
        result = OpenAssemblyIngestor(database).ingest(
            row, transcript, retrieved_at=datetime(2026, 7, 11, tzinfo=UTC)
        )
        assert result.speeches_saved == 3
        assert result.failures == ()
        assert result.agendas_saved == 2
        agendas = database.connection.execute(
            "SELECT bill_no, title FROM meeting_agendas ORDER BY sequence"
        ).fetchall()
        assert [tuple(row) for row in agendas] == [
            ("2200001", "인공지능 기본법안"),
            ("2200002", "인공지능 산업 진흥법안"),
        ]
        found = LocalServices(database).search("소버린 AI")
        assert found[0]["speaker"] == "김미래"
        assert found[0]["official_source"].startswith("https://record.assembly.go.kr/")


def test_unparseable_transcript_reports_failure() -> None:
    row = {
        "DAE_NUM": 22,
        "CONF_DATE": "2025-03-18",
        "CLASS_NAME": "국회본회의",
        "PDF_LINK_URL": "https://record.assembly.go.kr/meeting/official-2",
    }
    with Database(":memory:") as database:
        result = OpenAssemblyIngestor(database).ingest(row, "발언자 표식이 없는 원문")
        assert result.speeches_saved == 0
        assert result.failures[0].reason == "no speaker markers found"


def test_page_ingestion_counts_rows_without_inline_transcripts() -> None:
    base = {
        "DAE_NUM": 22,
        "CONF_DATE": "2025-03-18",
        "CLASS_NAME": "국회본회의",
        "PDF_LINK_URL": "https://record.assembly.go.kr/meeting/official-3",
    }
    rows = [
        {**base, "CONF_ID": "with-text", "CONTENT": "○의장 홍길동  개의합니다."},
        {**base, "CONF_ID": "metadata-only"},
    ]
    with Database(":memory:") as database:
        result = OpenAssemblyIngestor(database).ingest_rows(
            rows,
            source_hash="page-hash",
            source_url="https://open.assembly.go.kr/portal/openapi/service",
        )
        assert result.meetings_saved == 1
        assert result.speeches_saved == 1
        assert result.rows_without_transcript == 1
