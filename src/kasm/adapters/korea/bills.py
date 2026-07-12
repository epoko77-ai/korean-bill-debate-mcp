"""Normalize and ingest official Open Assembly bill/agenda records."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from kasm.core.models import Bill
from kasm.storage.repositories import BillRepository

BILL_DATASET = "nzmimeepazxkubdpn"
BILL_STATUS_DATASET = "nwbpacrgavhjryiph"
BILL_CATALOG_URL = "https://open.assembly.go.kr/portal/data/service/selectAPIServicePage.do"


def _first(row: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _date(value: str | None) -> date | None:
    if not value:
        return None
    compact = value.replace("-", "").replace(".", "")[:8]
    try:
        return datetime.strptime(compact, "%Y%m%d").date()
    except ValueError:
        return None


def bill_from_open_assembly_row(
    row: Mapping[str, Any], *, source_hash: str, retrieved_at: datetime | None = None
) -> Bill:
    bill_no = _first(row, "BILL_NO")
    bill_id = f"kna:bill:{bill_no}" if bill_no else None
    name = _first(row, "BILL_NAME", "BILL_NM")
    age = _first(row, "AGE", "AGE_NM")
    if not bill_no or not bill_id or not name or not age:
        raise ValueError("Open Assembly bill row lacks BILL_NO, BILL_ID, BILL_NAME, or AGE")
    digits = "".join(character for character in age if character.isdigit())
    if not digits:
        raise ValueError("Open Assembly bill AGE is invalid")
    official_url = _first(row, "DETAIL_LINK", "LINK_URL") or (
        f"https://likms.assembly.go.kr/bill/billDetail.do?billId={bill_id}"
    )
    return Bill(
        id=bill_id,
        bill_no=bill_no,
        name=name,
        assembly_term=int(digits),
        proposer=_first(row, "PROPOSER", "RST_PROPOSER", "PUBL_PROPOSER"),
        committee=_first(row, "COMMITTEE", "COMMITTEE_NM"),
        proposed_at=_date(_first(row, "PROPOSE_DT")),
        process_result=_first(row, "PROC_RESULT", "PROC_RESULT_CD", "LAW_PROC_RESULT_CD"),
        processed_at=_date(_first(row, "PROC_DT", "LAW_PROC_DT", "CMT_PROC_DT")),
        official_url=official_url,
        source_hash=source_hash,
        retrieved_at=retrieved_at or datetime.now(UTC),
    )


def ingest_bill_rows(
    connection: Any, rows: Iterable[Mapping[str, Any]], *, source_hash: str
) -> int:
    repository = BillRepository(connection)
    bills = [bill_from_open_assembly_row(row, source_hash=source_hash) for row in rows]
    repository.save_many(bills)
    rebuild_speech_bill_links(connection)
    return len(bills)


def rebuild_speech_bill_links(connection: Any) -> int:
    """Create explainable links only when a bill number or title is present in a speech."""
    database = getattr(connection, "connection", connection)
    bills = database.execute("SELECT id, bill_no, name FROM bills").fetchall()
    speeches = database.execute("SELECT id, text, agenda FROM speeches").fetchall()
    saved = 0
    with database:
        for bill in bills:
            bill = dict(bill)
            for speech in speeches:
                speech = dict(speech)
                haystack = f"{speech['agenda'] or ''}\n{speech['text']}"
                number_match = bill["bill_no"] in haystack
                title_match = len(bill["name"]) >= 4 and bill["name"] in haystack
                if not number_match and not title_match:
                    continue
                evidence = bill["bill_no"] if number_match else bill["name"]
                database.execute(
                    """INSERT INTO speech_bill_links
                       (speech_id, bill_id, relation_type, confidence, evidence)
                       VALUES (?, ?, 'EXPLICIT_MENTION', 1.0, ?)
                       ON CONFLICT (speech_id, bill_id, relation_type) DO UPDATE SET
                       confidence=excluded.confidence, evidence=excluded.evidence""",
                    (speech["id"], bill["id"], evidence),
                )
                saved += 1
    return saved


def rows_hash(rows: Iterable[Mapping[str, Any]]) -> str:
    payload = json.dumps(list(rows), ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()
