"""Convert Open Assembly rows and transcripts into persistent domain records."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from kasm.core.ids import agenda_id, meeting_id, speech_id
from kasm.core.models import Agenda, Bill, Meeting, Speech
from kasm.core.relations import infer_question_answer_relations
from kasm.storage.repositories import (
    AgendaRepository,
    BillRepository,
    MeetingRepository,
    SpeechRelationRepository,
    SpeechRepository,
)

from .normalizer import normalize_text
from .parser import ParseFailure, parse_transcript
from .sources import classify_meeting

_AGENDA_BILL = re.compile(
    r"(?m)^\s*\d+\.\s*(?P<name>[^\n]{2,100}?법률안)\s*"
    r"\((?P<proposer>[^\n()]{2,60}?대표발의)\)\s*"
    r"\(의안번호\s*(?P<bill_no>\d{5,})\)"
)
_EXACT_BILL_NO = re.compile(r"(?<!\d)(\d{7})(?!\d)")


def _first(row: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _date(value: str) -> date:
    compact = value.strip().replace(".", "-").replace("/", "-")
    if len(compact) == 8 and compact.isdigit():
        compact = f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
    return date.fromisoformat(compact[:10])


def _assembly_term(value: str | None) -> int:
    match = re.search(r"\d+", value or "")
    return int(match.group()) if match else 0


def transcript_from_open_assembly_row(row: Mapping[str, Any]) -> str | None:
    """Return inline transcript text when an Open Assembly service includes it."""

    return _first(
        row,
        "TRANSCRIPT",
        "TRANSCRIPT_TEXT",
        "CONF_CONTENT",
        "MEETING_CONTENT",
        "CONTENT",
        "TEXT",
    )


def meeting_from_open_assembly_row(
    row: Mapping[str, Any],
    *,
    source_hash: str,
    source_url: str | None = None,
    retrieved_at: datetime | None = None,
) -> Meeting:
    """Map known Open Assembly field aliases without depending on another provider."""

    term = _assembly_term(_first(row, "DAE_NUM", "ERACO"))
    meeting_date = _date(_first(row, "CONF_DATE", "CONF_DT") or "")
    kind = classify_meeting(row).value
    committee_name = _first(row, "COMM_NAME", "CMIT_NM", "SB_CMIT_NM")
    number = _first(row, "DGR", "CONFER_NUM")
    identifier = _first(row, "CONF_ID") or "-".join(
        part for part in (committee_name, number) if part
    )
    if not identifier:
        identifier = hashlib.sha256(repr(sorted(row.items())).encode()).hexdigest()[:16]
    official_url = source_url or _first(row, "PDF_LINK_URL", "DOWN_URL", "CONF_LINK_URL")
    if not official_url or not official_url.startswith(("https://", "http://")):
        raise ValueError("Open Assembly row requires an official source URL")
    title = _first(row, "TITLE", "SB_CMIT_NM", "CMIT_NM") or (
        f"{committee_name or kind} {number or ''}".strip()
    )
    return Meeting(
        id=meeting_id(term, kind, meeting_date, identifier),
        assembly_term=term,
        committee_id=_first(row, "DEPT_CD", "CMIT_CD", "SB_CMIT_CD"),
        committee_name_ko=committee_name,
        committee_name_en=None,
        title=title,
        meeting_type=kind,
        meeting_number=number,
        date=meeting_date,
        source_url=official_url,
        source_hash=source_hash,
        retrieved_at=retrieved_at or datetime.now(UTC),
    )


@dataclass(frozen=True, slots=True)
class IngestionResult:
    meeting: Meeting
    speeches_saved: int
    failures: tuple[ParseFailure, ...]
    relations_saved: int = 0
    agendas_saved: int = 0


@dataclass(frozen=True, slots=True)
class PageIngestionResult:
    meetings_saved: int
    speeches_saved: int
    rows_without_transcript: int
    parse_failures: int


class OpenAssemblyIngestor:
    def __init__(self, connection: Any) -> None:
        self.connection = getattr(connection, "connection", connection)
        self.meetings = MeetingRepository(self.connection)
        self.agendas = AgendaRepository(self.connection)
        self.speeches = SpeechRepository(self.connection)
        self.relations = SpeechRelationRepository(self.connection)
        self.bills = BillRepository(self.connection)

    def ingest(
        self,
        row: Mapping[str, Any],
        transcript: str,
        *,
        source_hash: str | None = None,
        source_url: str | None = None,
        retrieved_at: datetime | None = None,
    ) -> IngestionResult:
        transcript_hash = source_hash or hashlib.sha256(transcript.encode("utf-8")).hexdigest()
        meeting = meeting_from_open_assembly_row(
            row,
            source_hash=transcript_hash,
            source_url=source_url,
            retrieved_at=retrieved_at,
        )
        parsed = parse_transcript(transcript, locator_prefix=meeting.source_url)
        agendas = agendas_from_row(row, meeting)
        speeches: list[Speech] = []
        for index, item in enumerate(parsed.speeches):
            identifier = speech_id(meeting.id, item.sequence)
            speeches.append(
                Speech(
                    id=identifier,
                    meeting_id=meeting.id,
                    sequence=item.sequence,
                    speaker_id=None,
                    speaker_name=item.speaker_name,
                    speaker_role=item.speaker_role,
                    organization=item.organization,
                    text=item.text,
                    agenda=item.agenda,
                    previous_speech_id=speech_id(meeting.id, item.sequence - 1)
                    if index > 0
                    else None,
                    next_speech_id=speech_id(meeting.id, item.sequence + 1)
                    if index + 1 < len(parsed.speeches)
                    else None,
                    source_locator=item.source_locator,
                    source_hash=transcript_hash,
                    parser_version=item.parser_version,
                )
            )
        with self.connection:
            self.meetings.save(meeting)
            self.connection.execute(
                "DELETE FROM meeting_agendas WHERE meeting_id = ?", (meeting.id,)
            )
            self.agendas.save_many(agendas)
            self.connection.execute("DELETE FROM speeches WHERE meeting_id = ?", (meeting.id,))
            self.speeches.save_many(speeches)
            relations = infer_question_answer_relations(speeches)
            for relation in relations:
                self.relations.save(relation)
            for bill in bills_from_agenda(transcript, meeting):
                self.bills.save(bill)
        # Bill-first and minutes-first refresh orders produce the same graph.
        from .bills import rebuild_speech_bill_links

        rebuild_speech_bill_links(self.connection)
        return IngestionResult(
            meeting,
            len(speeches),
            tuple(parsed.failures),
            len(relations),
            len(agendas),
        )

    def ingest_rows(
        self,
        rows: tuple[dict[str, Any], ...] | list[dict[str, Any]],
        *,
        source_hash: str,
        source_url: str,
        retrieved_at: datetime | None = None,
    ) -> PageIngestionResult:
        meetings = speeches = missing = failures = 0
        for row in rows:
            transcript = transcript_from_open_assembly_row(row)
            if transcript is None:
                missing += 1
                continue
            result = self.ingest(
                row,
                transcript,
                source_hash=source_hash,
                source_url=_first(row, "BILL_URL", "CONF_URL", "MEETING_URL", "URL") or source_url,
                retrieved_at=retrieved_at,
            )
            meetings += 1
            speeches += result.speeches_saved
            failures += len(result.failures)
        return PageIngestionResult(meetings, speeches, missing, failures)


def bills_from_agenda(transcript: str, meeting: Meeting) -> list[Bill]:
    """Bootstrap bill nodes from official minutes; the bill API later enriches status."""
    bills: dict[str, Bill] = {}
    for match in _AGENDA_BILL.finditer(transcript):
        bill_no = match.group("bill_no")
        bills[bill_no] = Bill(
            id=f"kna:bill:{bill_no}",
            bill_no=bill_no,
            name=normalize_text(match.group("name")),
            assembly_term=meeting.assembly_term,
            proposer=normalize_text(match.group("proposer")),
            committee=meeting.committee_name_ko,
            proposed_at=None,
            process_result=None,
            processed_at=None,
            official_url=meeting.source_url,
            source_hash=meeting.source_hash,
            retrieved_at=meeting.retrieved_at,
        )
    return list(bills.values())


def agendas_from_row(row: Mapping[str, Any], meeting: Meeting) -> list[Agenda]:
    """Persist the complete agenda aggregation attached to one meeting PDF."""

    raw_items = row.get("agenda_items")
    items: list[tuple[str, str | None]] = []
    if isinstance(raw_items, (list, tuple)):
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                continue
            title = _first(raw, "title", "name")
            bill_no = _first(raw, "bill_no", "BILL_NO")
            if title:
                items.append((normalize_text(title), _valid_bill_no(bill_no, title)))
    if not items:
        title = _first(
            row,
            "SUB_NAME",
            "AGENDA_NAME",
            "AGENDA_NM",
            "MTR_NM",
            "BILL_NAME",
            "BILL_NM",
        )
        if title:
            items.append(
                (
                    normalize_text(title),
                    _valid_bill_no(_first(row, "BILL_NO", "BILL_NUM"), title),
                )
            )
    return [
        Agenda(
            id=agenda_id(meeting.id, sequence, title, bill_no),
            meeting_id=meeting.id,
            sequence=sequence,
            title=title,
            bill_no=bill_no,
            official_url=meeting.source_url,
            source_hash=meeting.source_hash,
        )
        for sequence, (title, bill_no) in enumerate(items)
    ]


def _valid_bill_no(value: str | None, title: str) -> str | None:
    if value and value.isdigit() and len(value) == 7:
        return value
    match = _EXACT_BILL_NO.search(title)
    return match.group(1) if match else None
