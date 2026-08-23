"""Normalize and ingest official Open Assembly bill/agenda records."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from kasm.core.models import Bill
from kasm.storage.repositories import BillRepository

BILL_DATASET = "nzmimeepazxkubdpn"
BILL_STATUS_DATASET = "nwbpacrgavhjryiph"
BILL_CATALOG_URL = "https://open.assembly.go.kr/portal/data/service/selectAPIServicePage.do"
_BILL_NUMBER = re.compile(r"(?<!\d)\d{7}(?!\d)")
_SQL_CHUNK_SIZE = 400


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
    rebuild_speech_bill_links(connection, bill_ids=(bill.id for bill in bills))
    return len(bills)


def rebuild_speech_bill_links(
    connection: Any,
    *,
    speech_ids: Iterable[str] | None = None,
    bill_ids: Iterable[str] | None = None,
) -> int:
    """Atomically rebuild exact-number links in the requested affected scope.

    With no targets, every generated link is rebuilt for backward compatibility.
    Supplying one target limits replacement to links touching those speeches or
    bills. Supplying both rebuilds the union of both affected scopes.
    """
    database = getattr(connection, "connection", connection)
    targeted_speech_ids = _target_ids(speech_ids)
    targeted_bill_ids = _target_ids(bill_ids)
    full_rebuild = targeted_speech_ids is None and targeted_bill_ids is None
    if not full_rebuild and not targeted_speech_ids and not targeted_bill_ids:
        return 0

    with database:
        generated_links: set[tuple[str, str, str]] = set()
        if full_rebuild:
            bill_rows = database.execute("SELECT id, bill_no FROM bills").fetchall()
            speech_rows = database.execute(
                "SELECT id, text, agenda FROM speeches"
            ).fetchall()
            generated_links.update(_links_for_speeches(speech_rows, bill_rows))
        else:
            all_speech_rows: list[Any] | None = None
            if targeted_bill_ids:
                bill_rows = _rows_with_values(
                    database,
                    "SELECT id, bill_no FROM bills",
                    "id",
                    targeted_bill_ids,
                )
                if bill_rows:
                    all_speech_rows = list(
                        database.execute("SELECT id, text, agenda FROM speeches").fetchall()
                    )
                    generated_links.update(
                        _links_for_speeches(all_speech_rows, bill_rows)
                    )

            if targeted_speech_ids:
                if all_speech_rows is None:
                    speech_rows = _rows_with_values(
                        database,
                        "SELECT id, text, agenda FROM speeches",
                        "id",
                        targeted_speech_ids,
                    )
                else:
                    speech_id_set = set(targeted_speech_ids)
                    speech_rows = [
                        row for row in all_speech_rows if str(row["id"]) in speech_id_set
                    ]
                mentioned_numbers = tuple(
                    dict.fromkeys(
                        number
                        for row in speech_rows
                        for number in _numbers_in_speech(row)
                    )
                )
                bill_rows = _rows_with_values(
                    database,
                    "SELECT id, bill_no FROM bills",
                    "bill_no",
                    mentioned_numbers,
                )
                generated_links.update(_links_for_speeches(speech_rows, bill_rows))

        # EXPLICIT_MENTION is generated by this adapter. Other relation types may be
        # curated or inferred elsewhere and must survive a rebuild.
        if full_rebuild:
            database.execute(
                "DELETE FROM speech_bill_links WHERE relation_type = 'EXPLICIT_MENTION'"
            )
        else:
            if targeted_speech_ids:
                _delete_generated_links(database, "speech_id", targeted_speech_ids)
            if targeted_bill_ids:
                _delete_generated_links(database, "bill_id", targeted_bill_ids)
        database.executemany(
            """INSERT INTO speech_bill_links
               (speech_id, bill_id, relation_type, confidence, evidence)
               VALUES (?, ?, 'EXPLICIT_MENTION', 1.0, ?)""",
            sorted(generated_links),
        )
    return len(generated_links)


def _target_ids(values: Iterable[str] | None) -> tuple[str, ...] | None:
    if values is None:
        return None
    return tuple(dict.fromkeys(str(value) for value in values))


def _rows_with_values(
    database: Any,
    select_sql: str,
    column: str,
    values: tuple[str, ...],
) -> list[Any]:
    rows: list[Any] = []
    for offset in range(0, len(values), _SQL_CHUNK_SIZE):
        chunk = values[offset : offset + _SQL_CHUNK_SIZE]
        placeholders = ",".join("?" for _ in chunk)
        rows.extend(
            database.execute(
                f"{select_sql} WHERE {column} IN ({placeholders})", chunk
            ).fetchall()
        )
    return rows


def _numbers_in_speech(row: Any) -> tuple[str, ...]:
    haystack = f"{row['agenda'] or ''}\n{row['text']}"
    return tuple(dict.fromkeys(_BILL_NUMBER.findall(haystack)))


def _links_for_speeches(
    speech_rows: Iterable[Any], bill_rows: Iterable[Any]
) -> set[tuple[str, str, str]]:
    bill_ids_by_number = {
        str(row["bill_no"]): str(row["id"])
        for row in bill_rows
        if _BILL_NUMBER.fullmatch(str(row["bill_no"]))
    }
    links: set[tuple[str, str, str]] = set()
    for row in speech_rows:
        for bill_number in _numbers_in_speech(row):
            bill_id = bill_ids_by_number.get(bill_number)
            if bill_id is not None:
                links.add((str(row["id"]), bill_id, bill_number))
    return links


def _delete_generated_links(
    database: Any, column: str, target_ids: tuple[str, ...]
) -> None:
    for offset in range(0, len(target_ids), _SQL_CHUNK_SIZE):
        chunk = target_ids[offset : offset + _SQL_CHUNK_SIZE]
        placeholders = ",".join("?" for _ in chunk)
        database.execute(
            f"""DELETE FROM speech_bill_links
                WHERE relation_type = 'EXPLICIT_MENTION'
                  AND {column} IN ({placeholders})""",
            chunk,
        )


def rows_hash(rows: Iterable[Mapping[str, Any]]) -> str:
    payload = json.dumps(list(rows), ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()
