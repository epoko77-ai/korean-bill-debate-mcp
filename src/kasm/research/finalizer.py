"""Build a connected, lossless research snapshot from durable engine artifacts.

The finalizer is intentionally a pure transformation.  It performs no network
I/O, never selects a top-N subset, and never creates a relationship from a
substring match.  Ambiguous relationships remain unresolved in the evidence
graph and make the corresponding coverage axis explicitly partial.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import urllib.parse
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from kasm.adapters.korea.bills import bill_from_open_assembly_row
from kasm.adapters.korea.ingestion import (
    agendas_from_row,
    meeting_from_open_assembly_row,
)
from kasm.adapters.korea.normalizer import normalize_text
from kasm.adapters.korea.pipeline import OpenAssemblyPipeline
from kasm.adapters.korea.sources import MeetingSource, classify_meeting
from kasm.core.models import Agenda, Bill, Meeting, Speech, SpeechRelation

from .contracts import CoverageLedger, EvidenceCoverage, EvidenceType
from .documents import OfficialDocumentKind, ParsedOfficialDocument, TextSegment
from .evidence_graph import (
    DocumentEvidence,
    EvidenceGraph,
    EvidenceGraphBuilder,
    EvidenceNodeType,
    SpeechEvidence,
)
from .results import EvidenceCitation, EvidenceRecord, ResearchSnapshot
from .transcript_evidence import TranscriptEvidence

if TYPE_CHECKING:
    from .engine import (
        DocumentOutcome,
        DocumentWorkItem,
        FinalizationContext,
    )

_EXACT_BILL_NO = re.compile(r"(?<!\d)(\d{7})(?!\d)")
_AGENDA_PREFIX = re.compile(r"^\s*(?:제\s*)?\d+\s*[.)]\s*")
_OFFICIAL_HOSTS = {
    "open.assembly.go.kr",
    "record.assembly.go.kr",
    "likms.assembly.go.kr",
}
_STATUS_FIELDS = {
    "PROC_RESULT",
    "PROC_RESULT_CD",
    "LAW_PROC_RESULT_CD",
    "PROC_DT",
    "LAW_PROC_DT",
    "CMT_PROC_DT",
}
_MEETING_AXES = (
    EvidenceType.AGENDAS,
    EvidenceType.SUBCOMMITTEE_MINUTES,
    EvidenceType.SPEECHES,
    EvidenceType.SPEECH_CONTEXT,
    EvidenceType.GOVERNMENT_RESPONSES,
)
_SPEECH_AXES = (
    EvidenceType.SPEECHES,
    EvidenceType.SPEECH_CONTEXT,
    EvidenceType.GOVERNMENT_RESPONSES,
)
_BILL_TOPIC_RECALL_AXES = (
    EvidenceType.BILLS,
    EvidenceType.BILL_TEXT,
    EvidenceType.BILL_STATUS,
    EvidenceType.REVIEW_REPORTS,
)


@dataclass(frozen=True, slots=True)
class FinalizationProduct:
    """The immutable public snapshot and its complete connected graph."""

    snapshot: ResearchSnapshot
    graph: EvidenceGraph


@dataclass(frozen=True, slots=True)
class _Prepared:
    bills: tuple[Bill, ...]
    bill_rows: tuple[tuple[str, Mapping[str, Any]], ...]
    status_rows: tuple[tuple[str, tuple[Mapping[str, Any], ...]], ...]
    meetings: tuple[Meeting, ...]
    agendas: tuple[Agenda, ...]
    documents: tuple[DocumentEvidence, ...]
    document_items: tuple[tuple[str, DocumentWorkItem, ParsedOfficialDocument], ...]
    speeches: tuple[SpeechEvidence, ...]
    relations: tuple[SpeechRelation, ...]
    transcripts: tuple[TranscriptEvidence, ...]
    local_gaps: tuple[tuple[tuple[EvidenceType, ...], str], ...]


class ConnectedResearchFinalizer:
    """Convert one finalization context into all evidence and honest coverage."""

    def __init__(
        self,
        *,
        build_sha: str,
        graph_builder: EvidenceGraphBuilder | None = None,
    ) -> None:
        if not build_sha.strip():
            raise ValueError("build_sha is required")
        self.build_sha = build_sha
        self.graph_builder = graph_builder or EvidenceGraphBuilder()

    def build(self, context: FinalizationContext) -> ResearchSnapshot:
        """Satisfy :class:`ResearchFinalizer` without hiding the graph API."""

        return self.finalize(context).snapshot

    def build_graph(self, context: FinalizationContext) -> EvidenceGraph:
        """Return the same deterministic graph used to construct the snapshot."""

        return self.finalize(context).graph

    def finalize(self, context: FinalizationContext) -> FinalizationProduct:
        prepared = _prepare(context)
        graph = self.graph_builder.build(
            bills=prepared.bills,
            meetings=prepared.meetings,
            agendas=prepared.agendas,
            documents=prepared.documents,
            speeches=prepared.speeches,
            speech_relations=prepared.relations,
        )
        records = _evidence_records(context, prepared, graph)
        coverage = _coverage(context, prepared, graph, records)
        snapshot = ResearchSnapshot(
            research_id=context.job.id,
            contract=context.job.contract,
            index_revision=context.job.index_revision,
            build_sha=self.build_sha,
            coverage=coverage,
            evidence=records,
        )
        return FinalizationProduct(snapshot=snapshot, graph=graph)


def _prepare(context: FinalizationContext) -> _Prepared:
    local_gaps: list[tuple[tuple[EvidenceType, ...], str]] = []
    bills, bill_rows, status_rows = _bills(context, local_gaps)
    meetings, meeting_rows = _meetings(context)
    agendas = _agendas(meetings, meeting_rows, bills, local_gaps)
    documents, document_items = _documents(context, meetings, agendas, local_gaps)
    speeches, relations, transcripts = _speeches(
        context,
        bills,
        agendas,
        local_gaps,
    )
    return _Prepared(
        bills=bills,
        bill_rows=bill_rows,
        status_rows=status_rows,
        meetings=meetings,
        agendas=agendas,
        documents=documents,
        document_items=document_items,
        speeches=speeches,
        relations=relations,
        transcripts=transcripts,
        local_gaps=tuple(dict.fromkeys(local_gaps)),
    )


def _bills(
    context: FinalizationContext,
    gaps: list[tuple[tuple[EvidenceType, ...], str]],
) -> tuple[
    tuple[Bill, ...],
    tuple[tuple[str, Mapping[str, Any]], ...],
    tuple[tuple[str, tuple[Mapping[str, Any], ...]], ...],
]:
    accepted = tuple(
        sorted(
            context.metadata.discovery.resolution.bills.accepted,
            key=lambda item: item.candidate_id,
        )
    )
    statuses: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for raw in context.metadata.status_collection.bills:
        row = dict(raw)
        number = _bill_number(row)
        if number is not None:
            statuses[number].append(row)
    for status_group in statuses.values():
        status_group.sort(key=_canonical_json)

    bills: list[Bill] = []
    merged_rows: list[tuple[str, Mapping[str, Any]]] = []
    exact_status_rows: list[tuple[str, tuple[Mapping[str, Any], ...]]] = []
    for decision in accepted:
        base = dict(decision.candidate)
        number = _required_bill_number(base)
        bill_status_rows = tuple(statuses.get(number, ()))
        merged = dict(base)
        for status_row in bill_status_rows:
            for field, value in sorted(status_row.items()):
                if value is None or value == "":
                    continue
                current = merged.get(field)
                if current is None or current == "" or field in _STATUS_FIELDS:
                    if field in _STATUS_FIELDS and current not in (None, "", value):
                        gaps.append(
                            (
                                (EvidenceType.BILL_STATUS,),
                                f"bill_status_conflict:{number}:{field}",
                            )
                        )
                    merged[field] = value
        # Open Assembly normally returns AGE, but exact/unit fixtures and some
        # legacy rows omit it.  A Korean seven-digit bill number starts with
        # its Assembly term, which is safer than assigning every historical
        # bill to the newest term in a multi-term contract.
        merged.setdefault("AGE", int(number[:2]))
        _discard_non_official_bill_links(merged)
        source_hash = _hash_payload({"metadata": base, "status": bill_status_rows})
        bill = bill_from_open_assembly_row(
            merged,
            source_hash=source_hash,
            retrieved_at=context.job.updated_at,
        )
        bills.append(bill)
        merged_rows.append((number, merged))
        exact_status_rows.append((number, bill_status_rows))
    return (
        tuple(sorted(bills, key=lambda item: item.bill_no)),
        tuple(sorted(merged_rows)),
        tuple(sorted(exact_status_rows)),
    )


def _meetings(
    context: FinalizationContext,
) -> tuple[tuple[Meeting, ...], tuple[tuple[str, Mapping[str, Any]], ...]]:
    successful_documents = {
        outcome.result.document.official_url: outcome.result.document
        for outcome in context.outcomes
        if _outcome_status(outcome) == "succeeded" and outcome.result is not None
    }
    meetings: list[Meeting] = []
    rows: list[tuple[str, Mapping[str, Any]]] = []
    for decision in sorted(
        context.metadata.discovery.resolution.meetings.accepted,
        key=lambda item: item.candidate_id,
    ):
        row = dict(decision.candidate)
        url = OpenAssemblyPipeline.minutes_url(row)
        document = successful_documents.get(url)
        source_hash = (
            document.source_hash
            if document is not None
            else _hash_payload({"meeting_metadata": row})
        )
        meeting = meeting_from_open_assembly_row(
            row,
            source_hash=source_hash,
            source_url=url,
            retrieved_at=(document.parsed_at if document is not None else context.job.updated_at),
        )
        meetings.append(meeting)
        rows.append((meeting.id, row))
    return tuple(sorted(meetings, key=lambda item: item.id)), tuple(sorted(rows))


def _agendas(
    meetings: Sequence[Meeting],
    meeting_rows: Sequence[tuple[str, Mapping[str, Any]]],
    bills: Sequence[Bill],
    gaps: list[tuple[tuple[EvidenceType, ...], str]],
) -> tuple[Agenda, ...]:
    meeting_by_id = {item.id: item for item in meetings}
    bill_titles: dict[str, list[str]] = defaultdict(list)
    for bill in bills:
        bill_titles[_normalized_title(bill.name)].append(bill.bill_no)

    result: list[Agenda] = []
    for meeting_id, row in meeting_rows:
        meeting = meeting_by_id[meeting_id]
        for agenda in agendas_from_row(row, meeting):
            result.append(agenda)
            if agenda.bill_no is None:
                matches = tuple(sorted(set(bill_titles[_normalized_title(agenda.title)])))
                label = (
                    "inferred_unresolved"
                    if len(matches) == 1
                    else "ambiguous"
                    if matches
                    else "missing"
                )
                gaps.append(
                    (
                        (EvidenceType.AGENDAS, EvidenceType.BILLS),
                        f"agenda_bill_title_{label}:{agenda.id}:candidates={','.join(matches)}",
                    )
                )
    identifiers = [item.id for item in result]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("finalized agenda identifiers must be unique")
    return tuple(sorted(result, key=lambda item: (item.meeting_id, item.sequence, item.id)))


def _documents(
    context: FinalizationContext,
    meetings: Sequence[Meeting],
    agendas: Sequence[Agenda],
    gaps: list[tuple[tuple[EvidenceType, ...], str]],
) -> tuple[
    tuple[DocumentEvidence, ...],
    tuple[tuple[str, DocumentWorkItem, ParsedOfficialDocument], ...],
]:
    items = {item.work_id: item for item in context.metadata.manifest.items}
    meetings_by_url: dict[str, list[Meeting]] = defaultdict(list)
    for meeting in meetings:
        meetings_by_url[meeting.source_url].append(meeting)
    agenda_bills: dict[str, set[str]] = defaultdict(set)
    for agenda in agendas:
        if agenda.bill_no is not None:
            agenda_bills[agenda.meeting_id].add(agenda.bill_no)

    documents: list[DocumentEvidence] = []
    item_documents: list[tuple[str, DocumentWorkItem, ParsedOfficialDocument]] = []
    seen: dict[str, ParsedOfficialDocument] = {}
    for outcome in sorted(context.outcomes, key=lambda item: item.work_id):
        if _outcome_status(outcome) != "succeeded" or outcome.result is None:
            continue
        try:
            item = items[outcome.work_id]
        except KeyError as exc:
            raise ValueError("successful document is absent from its manifest") from exc
        document = outcome.result.document
        if document.kind is not item.kind or document.official_url != item.official_url:
            raise ValueError("successful document does not match its manifest item")
        previous = seen.get(outcome.work_id)
        if previous is not None and previous != document:
            raise ValueError("one document work id produced conflicting documents")
        seen[outcome.work_id] = document
        meeting_ids: tuple[str, ...] = ()
        bill_numbers = set(item.related_bill_numbers)
        if document.kind is OfficialDocumentKind.MINUTES:
            candidates = tuple(
                sorted(meetings_by_url.get(document.official_url, ()), key=lambda value: value.id)
            )
            if len(candidates) == 1:
                meeting_ids = (candidates[0].id,)
                bill_numbers.update(agenda_bills[candidates[0].id])
            else:
                label = "missing" if not candidates else "ambiguous"
                gaps.append(
                    (
                        _SPEECH_AXES,
                        f"minutes_meeting_{label}:{outcome.work_id}",
                    )
                )
        documents.append(
            DocumentEvidence(
                document=document,
                bill_numbers=tuple(sorted(bill_numbers)),
                meeting_ids=meeting_ids,
            )
        )
        item_documents.append((outcome.work_id, item, document))
        for warning in document.warnings:
            gaps.append(
                (
                    tuple(item.evidence_types),
                    f"document_parse_warning:{outcome.work_id}:{warning}",
                )
            )
    return (
        tuple(
            sorted(
                documents,
                key=lambda item: (
                    item.document.kind.value,
                    item.document.official_url,
                    item.document.source_hash,
                    item.document.parser_version,
                ),
            )
        ),
        tuple(sorted(item_documents, key=lambda item: item[0])),
    )


def _speeches(
    context: FinalizationContext,
    bills: Sequence[Bill],
    agendas: Sequence[Agenda],
    gaps: list[tuple[tuple[EvidenceType, ...], str]],
) -> tuple[
    tuple[SpeechEvidence, ...],
    tuple[SpeechRelation, ...],
    tuple[TranscriptEvidence, ...],
]:
    agendas_by_meeting: dict[str, list[Agenda]] = defaultdict(list)
    for agenda in agendas:
        agendas_by_meeting[agenda.meeting_id].append(agenda)
    bills_by_title: dict[str, list[str]] = defaultdict(list)
    for bill in bills:
        bills_by_title[_normalized_title(bill.name)].append(bill.bill_no)

    evidence: list[SpeechEvidence] = []
    relations: dict[tuple[str, str, str], SpeechRelation] = {}
    transcripts = tuple(
        sorted(
            context.transcripts,
            key=lambda item: (item.meeting_id, item.document_url, item.document_source_hash),
        )
    )
    seen_speeches: dict[str, Speech] = {}
    for transcript in transcripts:
        meeting_agendas = agendas_by_meeting.get(transcript.meeting_id, ())
        by_title: dict[str, list[Agenda]] = defaultdict(list)
        for agenda in meeting_agendas:
            by_title[_normalized_title(agenda.title)].append(agenda)
        for speech in sorted(transcript.speeches, key=lambda item: (item.sequence, item.id)):
            previous = seen_speeches.get(speech.id)
            if previous is not None and previous != speech:
                raise ValueError("duplicate speech id has conflicting evidence")
            seen_speeches[speech.id] = speech
            bill_numbers = set(_EXACT_BILL_NO.findall(f"{speech.agenda or ''}\n{speech.text}"))
            agenda_ids: set[str] = set()
            if speech.agenda:
                normalized = _normalized_title(speech.agenda)
                agenda_matches = tuple(by_title.get(normalized, ()))
                identifier_matches = tuple(
                    item
                    for item in agenda_matches
                    if item.bill_no is not None and item.bill_no in bill_numbers
                )
                if len(identifier_matches) == 1:
                    # The exact seven-digit number in the speech and the same
                    # official agenda identifier jointly prove the binding.
                    agenda_ids.add(identifier_matches[0].id)
                elif len(identifier_matches) > 1:
                    gaps.append(
                        (
                            (EvidenceType.SPEECH_CONTEXT, EvidenceType.AGENDAS),
                            f"speech_agenda_identifier_ambiguous:{speech.id}:"
                            f"candidates={','.join(item.id for item in identifier_matches)}",
                        )
                    )
                elif agenda_matches:
                    label = "inferred_unresolved" if len(agenda_matches) == 1 else "ambiguous"
                    gaps.append(
                        (
                            (EvidenceType.SPEECH_CONTEXT, EvidenceType.AGENDAS),
                            f"speech_agenda_title_{label}:{speech.id}:"
                            f"candidates={','.join(item.id for item in agenda_matches)}",
                        )
                    )
                bill_matches = tuple(sorted(set(bills_by_title.get(normalized, ()))))
                if bill_matches and not bill_numbers:
                    label = "inferred_unresolved" if len(bill_matches) == 1 else "ambiguous"
                    gaps.append(
                        (
                            (EvidenceType.SPEECHES, EvidenceType.BILLS),
                            f"speech_bill_title_{label}:{speech.id}:"
                            f"candidates={','.join(bill_matches)}",
                        )
                    )
            # Retain exact numbers absent from the accepted bill set.  The graph
            # records an unresolved edge instead of silently deleting the claim.
            evidence.append(
                SpeechEvidence(
                    speech=speech,
                    official_url=transcript.document_url,
                    bill_numbers=tuple(sorted(bill_numbers)),
                    agenda_ids=tuple(sorted(agenda_ids)),
                    government_response=_looks_like_government_response(speech),
                )
            )
        for relation in transcript.relations:
            key = (
                relation.source_speech_id,
                relation.target_speech_id,
                relation.relation_type,
            )
            previous_relation = relations.get(key)
            if previous_relation is not None and previous_relation != relation:
                raise ValueError("duplicate speech relation has conflicting confidence")
            relations[key] = relation

    return (
        tuple(sorted(evidence, key=lambda item: item.speech.id)),
        tuple(relations[key] for key in sorted(relations)),
        transcripts,
    )


def _evidence_records(
    context: FinalizationContext,
    prepared: _Prepared,
    graph: EvidenceGraph,
) -> tuple[EvidenceRecord, ...]:
    records: list[EvidenceRecord] = []
    bill_by_no = {item.bill_no: item for item in prepared.bills}
    bill_rows = dict(prepared.bill_rows)
    statuses = dict(prepared.status_rows)
    meeting_by_id = {item.id: item for item in prepared.meetings}
    document_by_url_hash = {
        (document.official_url, document.source_hash): document
        for _work_id, _item, document in prepared.document_items
    }
    document_evidence_by_url_hash = {
        (evidence.document.official_url, evidence.document.source_hash): evidence
        for evidence in prepared.documents
    }

    for bill in prepared.bills:
        row = bill_rows[bill.bill_no]
        records.append(
            _record(
                identifier=f"evidence:bill:{bill.bill_no}",
                evidence_type=EvidenceType.BILLS,
                event_date=bill.proposed_at or context.job.contract.as_of.date(),
                rank=10,
                title=f"{bill.bill_no} {bill.name}",
                text=_canonical_json(row),
                official_url=bill.official_url,
                locator=f"bill:{bill.bill_no}:metadata",
                source_hash=bill.source_hash,
                retrieved_at=bill.retrieved_at,
                metadata=(
                    ("bill_no", bill.bill_no),
                    ("committee", bill.committee),
                    ("proposer", bill.proposer),
                ),
            )
        )
        status_rows = statuses[bill.bill_no]
        if status_rows or _contains_status(row):
            records.append(
                _record(
                    identifier=f"evidence:bill-status:{bill.bill_no}",
                    evidence_type=EvidenceType.BILL_STATUS,
                    event_date=(
                        bill.processed_at or bill.proposed_at or context.job.contract.as_of.date()
                    ),
                    rank=20,
                    title=f"{bill.bill_no} 처리상태: {bill.status}",
                    text=_canonical_json(
                        {
                            "bill_no": bill.bill_no,
                            "current_status": bill.status,
                            "official_status_rows": status_rows,
                        }
                    ),
                    official_url=bill.official_url,
                    locator=f"bill:{bill.bill_no}:status",
                    source_hash=_hash_payload(
                        {"bill_no": bill.bill_no, "status": status_rows or row}
                    ),
                    retrieved_at=bill.retrieved_at,
                    metadata=(
                        ("bill_no", bill.bill_no),
                        ("process_result", bill.process_result),
                    ),
                )
            )

    for agenda in prepared.agendas:
        meeting = meeting_by_id[agenda.meeting_id]
        records.append(
            _record(
                identifier=f"evidence:agenda:{agenda.id}",
                evidence_type=EvidenceType.AGENDAS,
                event_date=meeting.date,
                rank=30,
                title=agenda.title,
                text=agenda.title,
                official_url=agenda.official_url,
                locator=f"agenda:{agenda.sequence}",
                source_hash=agenda.source_hash,
                retrieved_at=meeting.retrieved_at,
                metadata=(
                    ("meeting_id", agenda.meeting_id),
                    ("sequence", agenda.sequence),
                    ("bill_no", agenda.bill_no),
                ),
            )
        )

    for work_id, item, document in prepared.document_items:
        document_evidence = document_evidence_by_url_hash.get(
            (document.official_url, document.source_hash)
        )
        related_bill_numbers = (
            document_evidence.bill_numbers
            if document_evidence is not None
            else item.related_bill_numbers
        )
        related_meeting_ids = (
            document_evidence.meeting_ids if document_evidence is not None else ()
        )
        event_date = _document_date(
            document,
            item.related_bill_numbers,
            prepared.meetings,
            bill_by_no,
            context.job.contract.as_of.date(),
        )
        if document.kind is OfficialDocumentKind.BILL_TEXT:
            evidence_type = EvidenceType.BILL_TEXT
        elif document.kind is OfficialDocumentKind.REVIEW_REPORT:
            evidence_type = EvidenceType.REVIEW_REPORTS
        elif EvidenceType.SUBCOMMITTEE_MINUTES in item.evidence_types:
            evidence_type = EvidenceType.SUBCOMMITTEE_MINUTES
        elif EvidenceType.SPEECH_CONTEXT in item.evidence_types:
            # A regular committee/plenary PDF page is complete discussion
            # context, but it must never be mislabeled as subcommittee minutes.
            evidence_type = EvidenceType.SPEECH_CONTEXT
        else:
            continue
        for segment_number, segment in enumerate(document.segments, start=1):
            text = segment.text if segment.text.strip() else _canonical_json({"text": ""})
            citation_url = _document_citation_url(
                document,
                item.related_bill_numbers,
                bill_by_no,
            )
            records.append(
                _record(
                    identifier=_document_page_id(work_id, document, segment),
                    evidence_type=evidence_type,
                    event_date=event_date,
                    rank=(
                        45
                        if evidence_type is EvidenceType.BILL_TEXT
                        else 50
                        if evidence_type is EvidenceType.REVIEW_REPORTS
                        else 40
                    ),
                    title=f"{document.kind.value} {segment.locator}",
                    text=text,
                    official_url=citation_url,
                    locator=segment.locator,
                    source_hash=document.source_hash,
                    retrieved_at=document.parsed_at,
                    metadata=(
                        ("work_id", work_id),
                        ("document_kind", document.kind.value),
                        ("parser_version", document.parser_version),
                        ("empty_page", not bool(segment.text.strip())),
                        (
                            "bill_no",
                            related_bill_numbers[0]
                            if len(related_bill_numbers) == 1
                            else None,
                        ),
                        ("related_bill_numbers", ",".join(related_bill_numbers)),
                        (
                            "meeting_id",
                            related_meeting_ids[0]
                            if len(related_meeting_ids) == 1
                            else None,
                        ),
                        ("related_meeting_ids", ",".join(related_meeting_ids)),
                    ),
                    ordinal=segment_number,
                )
            )

    speech_by_id = {item.speech.id: item.speech for item in prepared.speeches}
    bill_numbers_by_speech_id = {
        item.speech.id: item.bill_numbers for item in prepared.speeches
    }
    transcript_by_speech = {
        speech.id: transcript
        for transcript in prepared.transcripts
        for speech in transcript.speeches
    }
    for speech_evidence in prepared.speeches:
        speech = speech_evidence.speech
        related_bill_numbers = speech_evidence.bill_numbers
        meeting = meeting_by_id[speech.meeting_id]
        transcript = transcript_by_speech[speech.id]
        speech_document = document_by_url_hash.get(
            (transcript.document_url, transcript.document_source_hash)
        )
        retrieved_at = speech_document.parsed_at if speech_document else meeting.retrieved_at
        records.append(
            _record(
                identifier=f"evidence:speech:{speech.id}",
                evidence_type=EvidenceType.SPEECHES,
                event_date=meeting.date,
                rank=60,
                ordinal=speech.sequence,
                title=f"{speech.speaker_name} 발언",
                text=speech.text,
                official_url=speech_evidence.official_url,
                locator=speech.source_locator or "",
                source_hash=speech.source_hash,
                retrieved_at=retrieved_at,
                metadata=(
                    ("meeting_id", speech.meeting_id),
                    ("sequence", speech.sequence),
                    ("speaker", speech.speaker_name),
                    ("role", speech.speaker_role),
                    ("organization", speech.organization),
                    ("agenda", speech.agenda),
                    ("parser_version", speech.parser_version),
                    (
                        "bill_no",
                        related_bill_numbers[0]
                        if len(related_bill_numbers) == 1
                        else None,
                    ),
                    ("related_bill_numbers", ",".join(related_bill_numbers)),
                ),
            )
        )

    for relation in prepared.relations:
        source = speech_by_id.get(relation.source_speech_id)
        target = speech_by_id.get(relation.target_speech_id)
        if source is None or target is None:
            continue
        meeting = meeting_by_id[source.meeting_id]
        related_bill_numbers = tuple(
            sorted(
                {
                    bill_number
                    for speech_id in (source.id, target.id)
                    for bill_number in bill_numbers_by_speech_id.get(speech_id, ())
                }
            )
        )
        transcript = transcript_by_speech[source.id]
        context_document = document_by_url_hash.get(
            (transcript.document_url, transcript.document_source_hash)
        )
        records.append(
            _record(
                identifier=(
                    f"evidence:speech-context:{relation.relation_type}:{source.id}:{target.id}"
                ),
                evidence_type=EvidenceType.SPEECH_CONTEXT,
                event_date=meeting.date,
                rank=70,
                ordinal=min(source.sequence, target.sequence),
                title=(f"{relation.relation_type}: {source.speaker_name} → {target.speaker_name}"),
                text=_canonical_json(
                    {
                        "relation_type": relation.relation_type,
                        "source": source.to_dict(),
                        "target": target.to_dict(),
                    }
                ),
                official_url=transcript.document_url,
                locator=source.source_locator or "",
                source_hash=source.source_hash,
                retrieved_at=(
                    context_document.parsed_at if context_document else meeting.retrieved_at
                ),
                metadata=(
                    ("source_speech_id", source.id),
                    ("target_speech_id", target.id),
                    ("relation_type", relation.relation_type),
                    ("confidence", relation.confidence),
                    ("parser_version", source.parser_version),
                    ("meeting_id", source.meeting_id),
                    (
                        "bill_no",
                        related_bill_numbers[0]
                        if len(related_bill_numbers) == 1
                        else None,
                    ),
                    ("related_bill_numbers", ",".join(related_bill_numbers)),
                ),
            )
        )

    response_nodes = {
        str(dict(node.attributes).get("speech_id")): dict(node.attributes)
        for node in graph.nodes
        if node.node_type is EvidenceNodeType.GOVERNMENT_RESPONSE
    }
    for speech_id_value, response_attributes in sorted(response_nodes.items()):
        response_speech = speech_by_id.get(speech_id_value)
        if response_speech is None:
            continue
        meeting = meeting_by_id[response_speech.meeting_id]
        transcript = transcript_by_speech[response_speech.id]
        response_document = document_by_url_hash.get(
            (transcript.document_url, transcript.document_source_hash)
        )
        response_kind = str(response_attributes.get("response_kind") or "government_statement")
        related_bill_numbers = bill_numbers_by_speech_id.get(response_speech.id, ())
        title_suffix = "정부 질의답변" if response_kind == "qa_response" else "정부 발언"
        records.append(
            _record(
                identifier=f"evidence:government-response:{response_speech.id}",
                evidence_type=EvidenceType.GOVERNMENT_RESPONSES,
                event_date=meeting.date,
                rank=80,
                ordinal=response_speech.sequence,
                title=f"{response_speech.speaker_name} {title_suffix}",
                text=response_speech.text,
                official_url=transcript.document_url,
                locator=response_speech.source_locator or "",
                source_hash=response_speech.source_hash,
                retrieved_at=(
                    response_document.parsed_at if response_document else meeting.retrieved_at
                ),
                metadata=(
                    ("speech_id", response_speech.id),
                    ("speaker", response_speech.speaker_name),
                    ("role", response_speech.speaker_role),
                    ("organization", response_speech.organization),
                    ("response_kind", response_kind),
                    ("parser_version", response_speech.parser_version),
                    ("meeting_id", response_speech.meeting_id),
                    (
                        "bill_no",
                        related_bill_numbers[0]
                        if len(related_bill_numbers) == 1
                        else None,
                    ),
                    ("related_bill_numbers", ",".join(related_bill_numbers)),
                ),
            )
        )
    return tuple(records)


def _coverage(
    context: FinalizationContext,
    prepared: _Prepared,
    graph: EvidenceGraph,
    records: Sequence[EvidenceRecord],
) -> CoverageLedger:
    gap_reasons: dict[EvidenceType, list[str]] = {
        item: [] for item in context.job.contract.evidence_types
    }
    for engine_gap in context.coverage_gaps:
        for evidence_type in engine_gap.evidence_types:
            if evidence_type in gap_reasons:
                gap_reasons[evidence_type].append(engine_gap.reason)
    for evidence_types, reason in _full_text_candidate_universe_gaps(context):
        for evidence_type in evidence_types:
            if evidence_type in gap_reasons:
                gap_reasons[evidence_type].append(reason)
    for evidence_types, reason in prepared.local_gaps:
        for evidence_type in evidence_types:
            if evidence_type in gap_reasons:
                gap_reasons[evidence_type].append(reason)
    for graph_gap in graph.coverage_gaps:
        reason = f"evidence_graph:{graph_gap.code}:{graph_gap.entity_reference}:{graph_gap.detail}"
        for evidence_type in _graph_gap_axes(graph_gap.code):
            if evidence_type in gap_reasons:
                gap_reasons[evidence_type].append(reason)

    record_counts: dict[EvidenceType, int] = defaultdict(int)
    for record in records:
        record_counts[record.evidence_type] += 1

    resolution = context.metadata.discovery.resolution
    discovery_coverage = context.metadata.discovery.collection.coverage
    status_coverage = context.metadata.status_collection.coverage
    item_by_id = {item.work_id: item for item in context.metadata.manifest.items}
    outcome_by_id = {item.work_id: item for item in context.outcomes}

    document_counts: dict[EvidenceType, tuple[int, int, int, int]] = {}
    for evidence_type in (
        EvidenceType.BILL_TEXT,
        EvidenceType.SUBCOMMITTEE_MINUTES,
        EvidenceType.REVIEW_REPORTS,
    ):
        relevant = tuple(
            item for item in item_by_id.values() if evidence_type in item.evidence_types
        )
        succeeded = failed = pending = 0
        for item in relevant:
            outcome = outcome_by_id.get(item.work_id)
            status = _outcome_status(outcome) if outcome is not None else "pending"
            if status == "succeeded":
                succeeded += 1
            elif status == "failed":
                failed += 1
            else:
                pending += 1
        document_counts[evidence_type] = (
            len(relevant),
            succeeded,
            failed,
            pending,
        )

    # There must be one and only one verified original bill text per accepted
    # bill.  A failed index check or a missing source descriptor is therefore a
    # failed candidate, not a misleading zero-candidate success.
    bill_text_total = len(prepared.bills)
    text_total, text_succeeded, text_failed, text_pending = document_counts.get(
        EvidenceType.BILL_TEXT,
        (0, 0, 0, 0),
    )
    text_missing = max(0, bill_text_total - text_total)
    document_counts[EvidenceType.BILL_TEXT] = (
        bill_text_total,
        text_succeeded,
        text_failed + text_missing,
        text_pending,
    )

    parse_failures = sum(len(item.failures) for item in prepared.transcripts)
    speech_count = len(prepared.speeches)
    relation_count = len(prepared.relations)
    minutes_failed = minutes_pending = 0
    for item in item_by_id.values():
        if item.kind is not OfficialDocumentKind.MINUTES:
            continue
        outcome = outcome_by_id.get(item.work_id)
        status = _outcome_status(outcome) if outcome is not None else "pending"
        if status == "failed":
            minutes_failed += 1
        elif status != "succeeded":
            minutes_pending += 1
    meeting_rejected = discovery_coverage.meeting_rejected_rows

    agenda_count = len(prepared.agendas)
    bill_count = len(prepared.bills)
    status_matched = sum(
        bool(rows) or _contains_status(dict(prepared.bill_rows)[number])
        for number, rows in prepared.status_rows
    )
    government_count = record_counts[EvidenceType.GOVERNMENT_RESPONSES]

    counts: dict[EvidenceType, tuple[int, int, int, int]] = {
        EvidenceType.BILLS: (
            resolution.bills.total_candidates + discovery_coverage.bill_rejected_rows,
            resolution.bills.total_candidates,
            discovery_coverage.bill_rejected_rows,
            0,
        ),
        EvidenceType.BILL_STATUS: (
            bill_count + status_coverage.bill_rejected_rows,
            bill_count,
            status_coverage.bill_rejected_rows,
            0,
        ),
        EvidenceType.AGENDAS: (
            agenda_count + meeting_rejected,
            agenda_count,
            meeting_rejected,
            0,
        ),
        EvidenceType.SPEECHES: (
            speech_count + parse_failures + minutes_failed + minutes_pending + meeting_rejected,
            speech_count,
            parse_failures + minutes_failed + meeting_rejected,
            minutes_pending,
        ),
        EvidenceType.SPEECH_CONTEXT: (
            relation_count + parse_failures + minutes_failed + minutes_pending + meeting_rejected,
            relation_count,
            parse_failures + minutes_failed + meeting_rejected,
            minutes_pending,
        ),
        EvidenceType.GOVERNMENT_RESPONSES: (
            speech_count + parse_failures + minutes_failed + minutes_pending + meeting_rejected,
            speech_count,
            parse_failures + minutes_failed + meeting_rejected,
            minutes_pending,
        ),
    }
    for evidence_type, (total, checked, failed, pending) in document_counts.items():
        counts[evidence_type] = (total, checked, failed, pending)

    matched: dict[EvidenceType, int] = {
        EvidenceType.BILLS: bill_count,
        EvidenceType.BILL_STATUS: status_matched,
        EvidenceType.AGENDAS: agenda_count,
        EvidenceType.BILL_TEXT: document_counts.get(
            EvidenceType.BILL_TEXT, (0, 0, 0, 0)
        )[1],
        EvidenceType.SUBCOMMITTEE_MINUTES: document_counts.get(
            EvidenceType.SUBCOMMITTEE_MINUTES, (0, 0, 0, 0)
        )[1],
        EvidenceType.REVIEW_REPORTS: document_counts.get(EvidenceType.REVIEW_REPORTS, (0, 0, 0, 0))[
            1
        ],
        EvidenceType.SPEECHES: speech_count,
        EvidenceType.SPEECH_CONTEXT: relation_count,
        EvidenceType.GOVERNMENT_RESPONSES: government_count,
    }

    entries: list[EvidenceCoverage] = []
    for evidence_type in context.job.contract.evidence_types:
        total, checked, failed, pending = counts[evidence_type]
        reasons = tuple(dict.fromkeys(gap_reasons[evidence_type]))
        candidate_total: int | None = total
        if any(
            "source_incomplete" in reason
            or "candidate_universe_not_scanned" in reason
            for reason in reasons
        ):
            candidate_total = None
        entries.append(
            EvidenceCoverage(
                evidence_type=evidence_type,
                candidate_total=candidate_total,
                checked_count=checked,
                matched_count=min(matched[evidence_type], checked),
                failed_count=failed,
                pending_count=pending,
                gap_reasons=reasons,
            )
        )
    return CoverageLedger(context.job.contract.evidence_types, tuple(entries))


def _full_text_candidate_universe_gaps(
    context: FinalizationContext,
) -> tuple[tuple[tuple[EvidenceType, ...], str], ...]:
    """Reject false topical completeness after metadata-only relevance gates.

    Topic relevance is currently evaluated before official bill originals,
    review reports, and minutes PDFs are downloaded.  A rejected metadata row
    can therefore still contain the user's term in its official document body.
    Until a durable full-text corpus records that every rejected candidate was
    scanned at the snapshot's index revision, the corresponding candidate
    universe is unknown and must remain partial.

    Exact bill-number research is different: it binds bill documents by the
    verified official identifier and meetings by the official agenda number,
    so it does not depend on an open-ended topical metadata gate.
    """

    resolution = context.metadata.discovery.resolution
    criteria = resolution.criteria
    has_topic_terms = bool(
        criteria.statute_terms
        or criteria.issue_terms
        or criteria.related_statute_terms
        or criteria.related_issue_terms
    )
    if criteria.bill_numbers or not has_topic_terms:
        return ()

    corpus_recall = context.metadata.discovery.corpus_recall
    if corpus_recall is not None and corpus_recall.verified:
        # The immutable corpus revision exhaustively scanned every full-text
        # candidate, and the engine exact-mapped every hit before metadata
        # fan-out.  Missing current-run work is accounted separately by the
        # engine's explicit corpus_candidate_work_missing coverage gap.
        return ()

    requested = set(context.job.contract.evidence_types)
    gaps: list[tuple[tuple[EvidenceType, ...], str]] = []
    rejected_bills = resolution.bills.rejected_count
    bill_axes = tuple(item for item in _BILL_TOPIC_RECALL_AXES if item in requested)
    if rejected_bills and bill_axes:
        gaps.append(
            (
                bill_axes,
                f"bill_full_text_candidate_universe_not_scanned:{rejected_bills}",
            )
        )

    rejected_meetings = tuple(
        item for item in resolution.meetings.decisions if not item.accepted
    )
    if rejected_meetings:
        rejected_subcommittee = any(
            classify_meeting(item.candidate) is MeetingSource.SUBCOMMITTEE
            for item in rejected_meetings
        )
        meeting_axes = tuple(
            item
            for item in _MEETING_AXES
            if item in requested
            and (
                item is not EvidenceType.SUBCOMMITTEE_MINUTES
                or rejected_subcommittee
            )
        )
        if meeting_axes:
            gaps.append(
                (
                    meeting_axes,
                    "meeting_full_text_candidate_universe_not_scanned:"
                    f"{len(rejected_meetings)}",
                )
            )
    return tuple(gaps)


def _record(
    *,
    identifier: str,
    evidence_type: EvidenceType,
    event_date: date,
    rank: int,
    title: str,
    text: str,
    official_url: str,
    locator: str,
    source_hash: str,
    retrieved_at: datetime,
    metadata: tuple[tuple[str, str | int | float | bool | None], ...],
    ordinal: int = 0,
) -> EvidenceRecord:
    return EvidenceRecord(
        id=identifier,
        evidence_type=evidence_type,
        sort_key=f"{event_date.isoformat()}|{rank:02d}|{ordinal:09d}|{identifier}",
        title=title,
        text=text,
        citation=EvidenceCitation(
            official_url=official_url,
            source_locator=locator,
            source_hash=source_hash,
            retrieved_at=retrieved_at,
        ),
        metadata=metadata,
    )


def _document_page_id(
    work_id: str,
    document: ParsedOfficialDocument,
    segment: TextSegment,
) -> str:
    identity = hashlib.sha256(
        f"{work_id}\0{document.source_hash}\0{document.parser_version}\0{segment.locator}".encode()
    ).hexdigest()
    return f"evidence:document-page:{identity}"


def _document_date(
    document: ParsedOfficialDocument,
    related_bill_numbers: Sequence[str],
    meetings: Sequence[Meeting],
    bill_by_no: Mapping[str, Bill],
    fallback: date,
) -> date:
    if document.kind is OfficialDocumentKind.MINUTES:
        values = [
            meeting.date for meeting in meetings if meeting.source_url == document.official_url
        ]
        return min(values) if values else fallback
    bill_dates = [
        value
        for number in related_bill_numbers
        if (bill := bill_by_no.get(number)) is not None
        for value in (bill.processed_at or bill.proposed_at,)
        if value is not None
    ]
    return min(bill_dates) if bill_dates else fallback


def _document_citation_url(
    document: ParsedOfficialDocument,
    related_bill_numbers: Sequence[str],
    bill_by_no: Mapping[str, Bill],
) -> str:
    """Return a URL a reader can actually open for the cited source.

    Original bill PDFs are fetched through an official POST-only ZIP endpoint.
    Its query-bearing descriptor is useful for exact retrieval and caching, but
    opening it in a browser performs a GET and is not a usable citation.  Cite
    the already identity-verified official bill detail page instead; retain the
    archive descriptor on the preserved document as retrieval provenance.
    """

    if document.kind is not OfficialDocumentKind.BILL_TEXT:
        return document.official_url
    for bill_number in related_bill_numbers:
        bill = bill_by_no.get(bill_number)
        if bill is not None:
            return bill.official_url
    # Finalization coverage records the missing bill relationship as a gap.  A
    # defensive fallback keeps the immutable retrieval provenance available.
    return document.official_url


def _graph_gap_axes(code: str) -> tuple[EvidenceType, ...]:
    if code == "missing_bill":
        return (
            EvidenceType.BILLS,
            EvidenceType.BILL_TEXT,
            EvidenceType.BILL_STATUS,
            EvidenceType.AGENDAS,
            EvidenceType.REVIEW_REPORTS,
            EvidenceType.SPEECHES,
            EvidenceType.SPEECH_CONTEXT,
        )
    if "review" in code:
        return (EvidenceType.REVIEW_REPORTS, EvidenceType.BILLS)
    if "bill_text" in code:
        return (EvidenceType.BILL_TEXT, EvidenceType.BILLS)
    if "agenda" in code:
        return (EvidenceType.AGENDAS, EvidenceType.SPEECH_CONTEXT, EvidenceType.BILLS)
    if "speech" in code:
        return _SPEECH_AXES
    if "minutes" in code or "document_page" in code:
        return (
            EvidenceType.SUBCOMMITTEE_MINUTES,
            EvidenceType.SPEECHES,
            EvidenceType.SPEECH_CONTEXT,
            EvidenceType.GOVERNMENT_RESPONSES,
        )
    if "meeting" in code:
        return _MEETING_AXES
    if "bill" in code:
        return (EvidenceType.BILLS, EvidenceType.BILL_TEXT, EvidenceType.BILL_STATUS)
    return tuple(EvidenceType)


def _looks_like_government_response(speech: Speech) -> bool:
    role = normalize_text(speech.speaker_role or "")
    organization = normalize_text(speech.organization or "")
    return bool(
        any(marker in role for marker in ("장관", "차관", "정부위원", "처장", "청장", "실장"))
        or organization.endswith(("부", "처", "청"))
        or "정부" in organization
    )


def _normalized_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", normalize_text(value)).casefold()
    return _AGENDA_PREFIX.sub("", normalized).strip()


def _contains_status(row: Mapping[str, Any]) -> bool:
    return any(row.get(field) not in (None, "") for field in _STATUS_FIELDS)


def _bill_number(row: Mapping[str, Any]) -> str | None:
    value = row.get("BILL_NO", row.get("bill_no"))
    text = str(value).strip() if value is not None else ""
    return text if re.fullmatch(r"\d{7}", text) else None


def _required_bill_number(row: Mapping[str, Any]) -> str:
    value = _bill_number(row)
    if value is None:
        raise ValueError("accepted bill row lacks an exact seven-digit bill number")
    return value


def _discard_non_official_bill_links(row: dict[str, Any]) -> None:
    for field in ("DETAIL_LINK", "LINK_URL"):
        value = row.get(field)
        if value is not None and not _is_official_https(str(value)):
            row.pop(field, None)


def _is_official_https(value: str) -> bool:
    parsed = urllib.parse.urlsplit(value)
    return parsed.scheme == "https" and parsed.hostname in _OFFICIAL_HOSTS


def _outcome_status(outcome: DocumentOutcome) -> str:
    value = getattr(outcome.status, "value", outcome.status)
    return str(value)


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


__all__ = ["ConnectedResearchFinalizer", "FinalizationProduct"]
