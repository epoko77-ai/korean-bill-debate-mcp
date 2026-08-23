import sqlite3
from datetime import UTC, date, datetime

import pytest

from kasm.adapters.korea.bills import ingest_bill_rows, rebuild_speech_bill_links
from kasm.adapters.korea.ingestion import OpenAssemblyIngestor
from kasm.core.models import Bill, Meeting, Speech
from kasm.storage.database import Database
from kasm.storage.repositories import BillRepository, MeetingRepository, SpeechRepository


def _meeting() -> Meeting:
    return Meeting(
        id="meeting",
        assembly_term=22,
        committee_id="health",
        committee_name_ko="보건복지위원회",
        committee_name_en=None,
        title="법안심사소위원회",
        meeting_type="subcommittee",
        meeting_number="1",
        date=date(2026, 8, 1),
        source_url="https://record.assembly.go.kr/meeting",
        source_hash="fixture",
        retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


def _speech(sequence: int, text: str, *, agenda: str | None = None) -> Speech:
    return Speech(
        id=f"meeting:speech-{sequence:04d}",
        meeting_id="meeting",
        sequence=sequence,
        speaker_id=None,
        speaker_name="테스트위원",
        speaker_role="국회의원",
        organization=None,
        text=text,
        agenda=agenda,
        previous_speech_id=None,
        next_speech_id=None,
        source_locator=f"fixture:{sequence}",
        source_hash="fixture",
        parser_version="test",
    )


def _bill(index: int) -> Bill:
    bill_number = f"{2_200_000 + index:07d}"
    return Bill(
        id=f"bill-{index}",
        bill_no=bill_number,
        name="약사법 일부개정법률안",
        assembly_term=22,
        proposer="테스트의원",
        committee="보건복지위원회",
        proposed_at=date(2026, 1, 1),
        process_result=None,
        processed_at=None,
        official_url=f"https://likms.assembly.go.kr/bill/{bill_number}",
        source_hash="fixture",
        retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


def test_rebuild_links_only_exact_bill_numbers_and_preserves_other_relations() -> None:
    with Database(":memory:") as database:
        MeetingRepository(database).save(_meeting())
        bills = [_bill(index) for index in range(300)]
        BillRepository(database).save_many(bills)
        speeches = [
            _speech(
                1,
                "약사법 일부개정법률안의 플랫폼 도매업 제한을 논의합니다.",
                agenda="약사법 일부개정법률안",
            ),
            _speech(2, "의안번호 2200123번의 심사를 계속하겠습니다."),
            _speech(3, "이 값 122001234는 의안번호가 아닙니다."),
        ]
        SpeechRepository(database).save_many(speeches)
        database.connection.executemany(
            """INSERT INTO speech_bill_links
               (speech_id, bill_id, relation_type, confidence, evidence)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (speeches[0].id, bills[0].id, "EXPLICIT_MENTION", 1.0, bills[0].name),
                (speeches[0].id, bills[0].id, "AGENDA_MATCH", 0.95, "공식 안건"),
            ],
        )
        database.connection.commit()

        assert rebuild_speech_bill_links(database) == 1

        rows = database.connection.execute(
            """SELECT speech_id, bill_id, relation_type, confidence, evidence
               FROM speech_bill_links ORDER BY relation_type, speech_id, bill_id"""
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (speeches[0].id, bills[0].id, "AGENDA_MATCH", 0.95, "공식 안건"),
            (speeches[1].id, bills[123].id, "EXPLICIT_MENTION", 1.0, "2200123"),
        ]


@pytest.mark.parametrize("targeted", [False, True])
def test_rebuild_rolls_back_stale_link_deletion_when_insert_fails(
    targeted: bool,
) -> None:
    with Database(":memory:") as database:
        MeetingRepository(database).save(_meeting())
        bill = _bill(1)
        speech = _speech(1, "의안번호 2200001을 심사합니다.")
        BillRepository(database).save(bill)
        SpeechRepository(database).save(speech)
        database.connection.execute(
            """INSERT INTO speech_bill_links
               (speech_id, bill_id, relation_type, confidence, evidence)
               VALUES (?, ?, 'EXPLICIT_MENTION', 1.0, 'stale title match')""",
            (speech.id, bill.id),
        )
        database.connection.execute(
            """CREATE TRIGGER reject_generated_link
               BEFORE INSERT ON speech_bill_links
               WHEN NEW.relation_type = 'EXPLICIT_MENTION'
               BEGIN SELECT RAISE(ABORT, 'test failure'); END"""
        )
        database.connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="test failure"):
            if targeted:
                rebuild_speech_bill_links(database, speech_ids=[speech.id])
            else:
                rebuild_speech_bill_links(database)

        row = database.connection.execute(
            """SELECT evidence FROM speech_bill_links
               WHERE speech_id = ? AND bill_id = ? AND relation_type = 'EXPLICIT_MENTION'""",
            (speech.id, bill.id),
        ).fetchone()
        assert row is not None
        assert row["evidence"] == "stale title match"


def test_targeted_rebuild_replaces_union_of_affected_scopes_only() -> None:
    with Database(":memory:") as database:
        MeetingRepository(database).save(_meeting())
        bills = [_bill(index) for index in range(3)]
        speeches = [
            _speech(1, "의안번호 2200000을 심사합니다."),
            _speech(2, "이 발언에는 의안번호가 없습니다."),
            _speech(3, "의안번호 2200002를 심사합니다."),
        ]
        BillRepository(database).save_many(bills)
        SpeechRepository(database).save_many(speeches)
        database.connection.executemany(
            """INSERT INTO speech_bill_links
               (speech_id, bill_id, relation_type, confidence, evidence)
               VALUES (?, ?, 'EXPLICIT_MENTION', 1.0, ?)""",
            [
                (speeches[0].id, bills[2].id, "stale speech link"),
                (speeches[1].id, bills[2].id, "stale bill link"),
                (speeches[2].id, bills[1].id, "untouched link"),
            ],
        )
        database.connection.commit()

        saved = rebuild_speech_bill_links(
            database,
            speech_ids=[speeches[0].id],
            bill_ids=[bills[2].id],
        )

        assert saved == 2
        rows = database.connection.execute(
            """SELECT speech_id, bill_id, evidence FROM speech_bill_links
               WHERE relation_type = 'EXPLICIT_MENTION'
               ORDER BY speech_id, bill_id"""
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (speeches[0].id, bills[0].id, "2200000"),
            (speeches[2].id, bills[1].id, "untouched link"),
            (speeches[2].id, bills[2].id, "2200002"),
        ]


def test_bill_ingestion_backfills_only_the_ingested_bill_scope() -> None:
    with Database(":memory:") as database:
        MeetingRepository(database).save(_meeting())
        existing_bill = _bill(0)
        speeches = [
            _speech(1, "의안번호 2200999를 심사합니다."),
            _speech(2, "법률안 제목만 언급합니다."),
        ]
        BillRepository(database).save(existing_bill)
        SpeechRepository(database).save_many(speeches)
        database.connection.execute(
            """INSERT INTO speech_bill_links
               (speech_id, bill_id, relation_type, confidence, evidence)
               VALUES (?, ?, 'EXPLICIT_MENTION', 1.0, 'untouched existing link')""",
            (speeches[1].id, existing_bill.id),
        )
        database.connection.commit()

        saved = ingest_bill_rows(
            database,
            [
                {
                    "BILL_NO": "2200999",
                    "BILL_NAME": "약사법 일부개정법률안",
                    "AGE": "22",
                    "DETAIL_LINK": "https://likms.assembly.go.kr/bill/2200999",
                }
            ],
            source_hash="bill-api",
        )

        assert saved == 1
        rows = database.connection.execute(
            """SELECT speech_id, bill_id, evidence FROM speech_bill_links
               WHERE relation_type = 'EXPLICIT_MENTION'
               ORDER BY speech_id"""
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (speeches[0].id, "kna:bill:2200999", "2200999"),
            (speeches[1].id, existing_bill.id, "untouched existing link"),
        ]


def test_bill_first_and_minutes_first_create_the_same_exact_link() -> None:
    bill_row = {
        "BILL_NO": "2200777",
        "BILL_NAME": "약사법 일부개정법률안",
        "AGE": "22",
        "DETAIL_LINK": "https://likms.assembly.go.kr/bill/2200777",
    }
    meeting_row = {
        "DAE_NUM": "22",
        "CONF_DATE": "20260802",
        "CLASS_NAME": "상임위원회",
        "COMM_NAME": "보건복지위원회",
        "CONF_ID": "incremental-link-test",
        "PDF_LINK_URL": "https://record.assembly.go.kr/incremental-link-test",
    }
    transcript = """1. 약사법 일부개정법률안(테스트 의원 대표발의)(의안번호 2200777)
○테스트 위원  의안번호 2200777번을 심사하겠습니다.
"""

    results: list[list[tuple[str, str, str]]] = []
    for bill_first in (True, False):
        with Database(":memory:") as database:
            if bill_first:
                ingest_bill_rows(database, [bill_row], source_hash="bill-api")
            OpenAssemblyIngestor(database).ingest(meeting_row, transcript)
            if not bill_first:
                ingest_bill_rows(database, [bill_row], source_hash="bill-api")
            rows = database.connection.execute(
                """SELECT speech_id, bill_id, evidence FROM speech_bill_links
                   WHERE relation_type = 'EXPLICIT_MENTION'"""
            ).fetchall()
            results.append([tuple(row) for row in rows])

    assert len(results[0]) == 1
    assert results[0] == results[1]
    assert results[0][0][1:] == ("kna:bill:2200777", "2200777")
