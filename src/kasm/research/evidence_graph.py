"""Lossless evidence graph for connected National Assembly research.

The graph records only explicit identifiers and source-backed relationships.
Nothing is top-N limited or excerpted.  Evidence whose relationship cannot be
proved is retained as a node and accompanied by both an unresolved edge and a
coverage gap instead of being silently discarded or guessed into place.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from kasm.core.models import Agenda, Bill, Meeting, Speech, SpeechRelation

from .documents import OfficialDocumentKind, ParsedOfficialDocument

_BILL_NO = re.compile(r"\d{7}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PAGE_LOCATOR_PART = re.compile(r"p\.[1-9]\d*(?::[^\s|]+)?")
_TITLE_PREFIX = re.compile(r"^\s*(?:제\s*)?\d+\s*[.)]\s*")
_OFFICIAL_HOSTS = {
    "open.assembly.go.kr",
    "record.assembly.go.kr",
    "likms.assembly.go.kr",
}

type GraphScalar = str | int | float | bool | None


class EvidenceNodeType(StrEnum):
    BILL = "bill"
    BILL_STATUS = "bill_status"
    BILL_TEXT = "bill_text"
    MEETING = "meeting"
    AGENDA = "agenda"
    MINUTES_DOCUMENT = "minutes_document"
    REVIEW_REPORT = "review_report"
    DOCUMENT_PAGE = "document_page"
    PERSON = "person"
    SPEECH = "speech"
    GOVERNMENT_RESPONSE = "government_response"
    ISSUE = "issue"


class EvidenceEdgeType(StrEnum):
    HAS_STATUS = "has_status"
    HAS_BILL_TEXT = "has_bill_text"
    HAS_AGENDA = "has_agenda"
    AGENDA_FOR_BILL = "agenda_for_bill"
    HAS_MINUTES = "has_minutes"
    EVIDENCED_BY_MINUTES = "evidenced_by_minutes"
    HAS_REVIEW_REPORT = "has_review_report"
    HAS_PAGE = "has_page"
    CONTAINS_SPEECH = "contains_speech"
    PAGE_CONTAINS_SPEECH = "page_contains_speech"
    ADDRESSES_AGENDA = "addresses_agenda"
    DISCUSSES_BILL = "discusses_bill"
    MADE_SPEECH = "made_speech"
    DERIVED_FROM_SPEECH = "derived_from_speech"
    QUESTION_TO = "question_to"
    ANSWER_TO = "answer_to"
    FOLLOW_UP_TO = "follow_up_to"
    CONTINUES = "continues"
    HAS_ISSUE = "has_issue"
    IDENTIFIES_ISSUE = "identifies_issue"
    DISCUSSES_ISSUE = "discusses_issue"


@dataclass(frozen=True, slots=True)
class EvidenceProvenance:
    """Exact official locator supporting a graph node or edge."""

    official_url: str
    source_hash: str
    locator: str

    def __post_init__(self) -> None:
        _validate_official_url(self.official_url)
        _validate_hash(self.source_hash)
        if not self.locator.strip():
            raise ValueError("evidence provenance locator is required")

    def require_page(self) -> None:
        if not _page_locator_parts(self.locator):
            raise ValueError("PDF evidence locator must identify an exact p.N page")

    def to_dict(self) -> dict[str, str]:
        return {
            "official_url": self.official_url,
            "source_hash": self.source_hash,
            "locator": self.locator,
        }


@dataclass(frozen=True, slots=True)
class EvidenceNode:
    id: str
    node_type: EvidenceNodeType
    label: str
    text: str
    provenance: EvidenceProvenance
    attributes: tuple[tuple[str, GraphScalar], ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.label.strip():
            raise ValueError("evidence node id and label are required")
        names = tuple(name for name, _value in self.attributes)
        if any(not name.strip() for name in names) or len(names) != len(set(names)):
            raise ValueError("evidence node attribute names must be non-empty and unique")
        object.__setattr__(self, "attributes", tuple(sorted(self.attributes)))
        if self.node_type in {
            EvidenceNodeType.DOCUMENT_PAGE,
            EvidenceNodeType.SPEECH,
            EvidenceNodeType.GOVERNMENT_RESPONSE,
            EvidenceNodeType.ISSUE,
        }:
            self.provenance.require_page()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.node_type.value,
            "label": self.label,
            "text": self.text,
            "text_characters": len(self.text),
            "attributes": dict(self.attributes),
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class EvidenceEdge:
    source_id: str
    target_id: str
    edge_type: EvidenceEdgeType
    provenance: EvidenceProvenance
    reason: str

    def __post_init__(self) -> None:
        if not self.source_id.strip() or not self.target_id.strip() or not self.reason.strip():
            raise ValueError("evidence edge endpoints and reason are required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "type": self.edge_type.value,
            "reason": self.reason,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class UnresolvedEdge:
    source_reference: str
    target_reference: str
    edge_type: EvidenceEdgeType
    reason: str
    provenance: EvidenceProvenance

    def __post_init__(self) -> None:
        if not self.source_reference.strip() or not self.target_reference.strip():
            raise ValueError("unresolved edge references are required")
        if not self.reason.strip():
            raise ValueError("unresolved edge reason is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_reference": self.source_reference,
            "target_reference": self.target_reference,
            "type": self.edge_type.value,
            "reason": self.reason,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class CoverageGap:
    code: str
    entity_reference: str
    detail: str

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.entity_reference.strip() or not self.detail.strip():
            raise ValueError("coverage gap code, entity, and detail are required")

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "entity_reference": self.entity_reference,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class DocumentEvidence:
    """An accepted parsed document with explicit graph attachment claims."""

    document: ParsedOfficialDocument
    bill_numbers: tuple[str, ...] = ()
    meeting_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.document.kind not in {
            OfficialDocumentKind.MINUTES,
            OfficialDocumentKind.REVIEW_REPORT,
            OfficialDocumentKind.BILL_TEXT,
        }:
            raise ValueError("evidence graph does not support this official document kind")
        _validate_bill_numbers(self.bill_numbers)
        if any(not value.strip() for value in self.meeting_ids):
            raise ValueError("document meeting ids must not be empty")


@dataclass(frozen=True, slots=True)
class SpeechEvidence:
    """An accepted speech plus its official URL and explicit relationships."""

    speech: Speech
    official_url: str
    bill_numbers: tuple[str, ...] = ()
    agenda_ids: tuple[str, ...] = ()
    issue_ids: tuple[str, ...] = ()
    government_response: bool = False

    def __post_init__(self) -> None:
        _validate_official_url(self.official_url)
        _validate_bill_numbers(self.bill_numbers)
        if any(not value.strip() for value in (*self.agenda_ids, *self.issue_ids)):
            raise ValueError("speech relationship ids must not be empty")


@dataclass(frozen=True, slots=True)
class IssueEvidence:
    """One source-backed issue and its explicit evidence references."""

    issue_id: str
    title: str
    text: str
    provenance: EvidenceProvenance
    bill_numbers: tuple[str, ...] = ()
    agenda_ids: tuple[str, ...] = ()
    document_urls: tuple[str, ...] = ()
    speech_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.issue_id.strip() or not self.title.strip() or not self.text.strip():
            raise ValueError("issue id, title, and complete text are required")
        self.provenance.require_page()
        _validate_bill_numbers(self.bill_numbers)
        for value in self.document_urls:
            _validate_official_url(value)
        if any(not value.strip() for value in (*self.agenda_ids, *self.speech_ids)):
            raise ValueError("issue relationship ids must not be empty")


@dataclass(frozen=True, slots=True)
class EvidenceGraph:
    nodes: tuple[EvidenceNode, ...]
    edges: tuple[EvidenceEdge, ...]
    unresolved_edges: tuple[UnresolvedEdge, ...]
    coverage_gaps: tuple[CoverageGap, ...]

    def __post_init__(self) -> None:
        ordered_nodes = tuple(sorted(self.nodes, key=lambda item: (item.node_type.value, item.id)))
        identifiers = tuple(node.id for node in ordered_nodes)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("evidence graph node ids must be unique")
        known = set(identifiers)
        if any(edge.source_id not in known or edge.target_id not in known for edge in self.edges):
            raise ValueError("resolved evidence edge references an unknown node")
        object.__setattr__(self, "nodes", ordered_nodes)
        object.__setattr__(self, "edges", tuple(sorted(self.edges, key=_edge_key)))
        object.__setattr__(
            self,
            "unresolved_edges",
            tuple(sorted(self.unresolved_edges, key=_unresolved_key)),
        )
        object.__setattr__(
            self,
            "coverage_gaps",
            tuple(
                sorted(
                    self.coverage_gaps,
                    key=lambda item: (item.code, item.entity_reference, item.detail),
                )
            ),
        )

    @property
    def graph_hash(self) -> str:
        encoded = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "unresolved_edges": [edge.to_dict() for edge in self.unresolved_edges],
            "coverage_gaps": [gap.to_dict() for gap in self.coverage_gaps],
            "counts": {
                "nodes": len(self.nodes),
                "edges": len(self.edges),
                "unresolved_edges": len(self.unresolved_edges),
                "coverage_gaps": len(self.coverage_gaps),
            },
        }


class EvidenceGraphBuilder:
    """Build a complete graph from accepted source-backed evidence."""

    def build(
        self,
        *,
        bills: Sequence[Bill] = (),
        meetings: Sequence[Meeting] = (),
        agendas: Sequence[Agenda] = (),
        documents: Sequence[DocumentEvidence] = (),
        speeches: Sequence[SpeechEvidence] = (),
        speech_relations: Sequence[SpeechRelation] = (),
        issues: Sequence[IssueEvidence] = (),
    ) -> EvidenceGraph:
        state = _BuildState()
        bill_nodes = self._add_bills(state, bills)
        meeting_nodes = self._add_meetings(state, meetings)
        agenda_nodes = self._add_agendas(state, agendas, bill_nodes, meeting_nodes)
        document_nodes = self._add_documents(
            state, documents, bill_nodes, meeting_nodes, agenda_nodes
        )
        qa_response_speeches = {
            relation.source_speech_id
            for relation in speech_relations
            if relation.relation_type == "ANSWER_TO"
        } | {
            relation.target_speech_id
            for relation in speech_relations
            if relation.relation_type == "QUESTION_TO"
        }
        response_speeches = {
            evidence.speech.id: (
                "qa_response"
                if evidence.speech.id in qa_response_speeches
                else "government_statement"
            )
            for evidence in speeches
            if evidence.government_response
        }
        speech_nodes = self._add_speeches(
            state,
            speeches,
            bill_nodes,
            meeting_nodes,
            agenda_nodes,
            response_speeches,
        )
        self._add_speech_relations(state, speech_relations, speech_nodes)
        self._add_issues(
            state,
            issues,
            bill_nodes,
            agenda_nodes,
            document_nodes,
            speech_nodes,
            speeches,
        )
        return state.finish()

    @staticmethod
    def _add_bills(state: _BuildState, bills: Sequence[Bill]) -> dict[str, str]:
        result: dict[str, str] = {}
        for bill in bills:
            _validate_bill_no(bill.bill_no)
            provenance = EvidenceProvenance(bill.official_url, bill.source_hash, "bill")
            node_id = f"bill:{bill.bill_no}"
            state.add_node(
                EvidenceNode(
                    node_id,
                    EvidenceNodeType.BILL,
                    bill.name,
                    bill.name,
                    provenance,
                    (
                        ("assembly_term", bill.assembly_term),
                        ("bill_no", bill.bill_no),
                        ("committee", bill.committee),
                        ("proposed_at", bill.proposed_at.isoformat() if bill.proposed_at else None),
                    ),
                )
            )
            status_id = f"{node_id}:status"
            state.add_node(
                EvidenceNode(
                    status_id,
                    EvidenceNodeType.BILL_STATUS,
                    f"{bill.bill_no} 처리상태",
                    bill.status,
                    EvidenceProvenance(bill.official_url, bill.source_hash, "bill_status"),
                    (
                        ("process_result", bill.process_result),
                        (
                            "processed_at",
                            bill.processed_at.isoformat() if bill.processed_at else None,
                        ),
                    ),
                )
            )
            state.add_edge(
                EvidenceEdge(
                    node_id,
                    status_id,
                    EvidenceEdgeType.HAS_STATUS,
                    provenance,
                    "bill status metadata",
                )
            )
            if bill.bill_no in result:
                raise ValueError(f"duplicate accepted bill number: {bill.bill_no}")
            result[bill.bill_no] = node_id
        return result

    @staticmethod
    def _add_meetings(state: _BuildState, meetings: Sequence[Meeting]) -> dict[str, str]:
        result: dict[str, str] = {}
        for meeting in meetings:
            provenance = EvidenceProvenance(meeting.source_url, meeting.source_hash, "meeting")
            node_id = f"meeting:{meeting.id}"
            state.add_node(
                EvidenceNode(
                    node_id,
                    EvidenceNodeType.MEETING,
                    meeting.title,
                    meeting.title,
                    provenance,
                    (
                        ("assembly_term", meeting.assembly_term),
                        ("committee", meeting.committee_name_ko),
                        ("date", meeting.date.isoformat()),
                        ("meeting_type", meeting.meeting_type),
                    ),
                )
            )
            if meeting.id in result:
                raise ValueError(f"duplicate accepted meeting id: {meeting.id}")
            result[meeting.id] = node_id
        return result

    @staticmethod
    def _add_agendas(
        state: _BuildState,
        agendas: Sequence[Agenda],
        bill_nodes: Mapping[str, str],
        meeting_nodes: Mapping[str, str],
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        bill_titles: dict[str, list[str]] = {}
        for bill_no, node_id in bill_nodes.items():
            node = state.nodes[node_id]
            bill_titles.setdefault(_normalized_reference(node.label), []).append(bill_no)
        for agenda in agendas:
            provenance = EvidenceProvenance(
                agenda.official_url, agenda.source_hash, f"agenda:{agenda.sequence}"
            )
            node_id = f"agenda:{agenda.id}"
            state.add_node(
                EvidenceNode(
                    node_id,
                    EvidenceNodeType.AGENDA,
                    agenda.title,
                    agenda.title,
                    provenance,
                    (("bill_no", agenda.bill_no), ("sequence", agenda.sequence)),
                )
            )
            if agenda.id in result:
                raise ValueError(f"duplicate accepted agenda id: {agenda.id}")
            result[agenda.id] = node_id
            meeting_id = meeting_nodes.get(agenda.meeting_id)
            if meeting_id:
                state.add_edge(
                    EvidenceEdge(
                        meeting_id,
                        node_id,
                        EvidenceEdgeType.HAS_AGENDA,
                        provenance,
                        "exact meeting_id",
                    )
                )
            else:
                state.unresolved(
                    node_id,
                    f"meeting:{agenda.meeting_id}",
                    EvidenceEdgeType.HAS_AGENDA,
                    "agenda meeting_id is absent from accepted meetings",
                    provenance,
                    "missing_meeting",
                )
            if agenda.bill_no is None:
                inferred = tuple(sorted(bill_titles.get(_normalized_reference(agenda.title), ())))
                target_reference = (
                    f"bill-title:{agenda.title}:candidates={','.join(inferred)}"
                    if inferred
                    else "bill:unknown"
                )
                reason = (
                    "agenda title matches accepted bill title, but title-only "
                    "inference cannot establish an authoritative bill number"
                    if inferred
                    else "agenda has no exact seven-digit bill number"
                )
                state.unresolved(
                    node_id,
                    target_reference,
                    EvidenceEdgeType.AGENDA_FOR_BILL,
                    reason,
                    provenance,
                    "agenda_bill_unresolved",
                )
            elif target := bill_nodes.get(agenda.bill_no):
                state.add_edge(
                    EvidenceEdge(
                        node_id,
                        target,
                        EvidenceEdgeType.AGENDA_FOR_BILL,
                        provenance,
                        "exact seven-digit bill number",
                    )
                )
            else:
                state.unresolved(
                    node_id,
                    f"bill:{agenda.bill_no}",
                    EvidenceEdgeType.AGENDA_FOR_BILL,
                    "agenda bill number is absent from accepted bills",
                    provenance,
                    "missing_bill",
                )
        return result

    @staticmethod
    def _add_documents(
        state: _BuildState,
        documents: Sequence[DocumentEvidence],
        bill_nodes: Mapping[str, str],
        meeting_nodes: Mapping[str, str],
        agenda_nodes: Mapping[str, str],
    ) -> dict[str, tuple[str, ...]]:
        by_url: dict[str, set[str]] = {}
        agendas_by_meeting: dict[str, list[str]] = {}
        # Agenda node ids encode the accepted Agenda id; meeting ownership is
        # read from resolved HAS_AGENDA edges to avoid a second mutable index.
        for edge in state.edges:
            if edge.edge_type is EvidenceEdgeType.HAS_AGENDA:
                agendas_by_meeting.setdefault(edge.source_id, []).append(edge.target_id)
        for evidence in documents:
            document = evidence.document
            provenance = EvidenceProvenance(document.official_url, document.source_hash, "document")
            if document.kind is OfficialDocumentKind.MINUTES:
                node_type = EvidenceNodeType.MINUTES_DOCUMENT
            elif document.kind is OfficialDocumentKind.REVIEW_REPORT:
                node_type = EvidenceNodeType.REVIEW_REPORT
            else:
                node_type = EvidenceNodeType.BILL_TEXT
            identity = hashlib.sha256(
                f"{document.official_url}\0{document.parser_version}".encode()
            ).hexdigest()[:16]
            node_id = f"document:{document.kind.value}:{document.source_hash}:{identity}"
            state.add_node(
                EvidenceNode(
                    node_id,
                    node_type,
                    f"{document.kind.value} {document.official_url}",
                    document.full_text,
                    provenance,
                    (
                        ("parser_version", document.parser_version),
                        ("text_hash", document.text_hash),
                        ("pages", len(document.segments)),
                    ),
                )
            )
            by_url.setdefault(document.official_url, set()).add(node_id)
            for segment in document.segments:
                page_provenance = EvidenceProvenance(
                    document.official_url, document.source_hash, segment.locator
                )
                page_provenance.require_page()
                page_id = f"{node_id}:page:{segment.locator}"
                state.add_node(
                    EvidenceNode(
                        page_id,
                        EvidenceNodeType.DOCUMENT_PAGE,
                        f"{document.kind.value} {segment.locator}",
                        segment.text,
                        page_provenance,
                    )
                )
                state.add_edge(
                    EvidenceEdge(
                        node_id,
                        page_id,
                        EvidenceEdgeType.HAS_PAGE,
                        page_provenance,
                        "exact parsed page locator",
                    )
                )

            if document.kind is OfficialDocumentKind.MINUTES:
                if not evidence.meeting_ids:
                    state.unresolved(
                        node_id,
                        "meeting:unknown",
                        EvidenceEdgeType.HAS_MINUTES,
                        "minutes document has no explicit meeting_id",
                        provenance,
                        "orphan_minutes_document",
                    )
                for meeting_ref in evidence.meeting_ids:
                    meeting_id = meeting_nodes.get(meeting_ref)
                    if meeting_id:
                        state.add_edge(
                            EvidenceEdge(
                                meeting_id,
                                node_id,
                                EvidenceEdgeType.HAS_MINUTES,
                                provenance,
                                "explicit meeting_id binding",
                            )
                        )
                        for agenda_id in agendas_by_meeting.get(meeting_id, ()):
                            state.add_edge(
                                EvidenceEdge(
                                    agenda_id,
                                    node_id,
                                    EvidenceEdgeType.EVIDENCED_BY_MINUTES,
                                    provenance,
                                    "agenda and minutes share exact meeting_id",
                                )
                            )
                    else:
                        state.unresolved(
                            node_id,
                            f"meeting:{meeting_ref}",
                            EvidenceEdgeType.HAS_MINUTES,
                            "document meeting_id is absent from accepted meetings",
                            provenance,
                            "missing_meeting",
                        )
                for bill_no in evidence.bill_numbers:
                    if target := bill_nodes.get(bill_no):
                        state.add_edge(
                            EvidenceEdge(
                                node_id,
                                target,
                                EvidenceEdgeType.DISCUSSES_BILL,
                                provenance,
                                "explicit exact bill number binding",
                            )
                        )
                    else:
                        state.unresolved(
                            node_id,
                            f"bill:{bill_no}",
                            EvidenceEdgeType.DISCUSSES_BILL,
                            "minutes bill number is absent from accepted bills",
                            provenance,
                            "missing_bill",
                        )
            elif document.kind is OfficialDocumentKind.REVIEW_REPORT:
                if not evidence.bill_numbers:
                    state.unresolved(
                        node_id,
                        "bill:unknown",
                        EvidenceEdgeType.HAS_REVIEW_REPORT,
                        "review report has no exact bill number binding",
                        provenance,
                        "orphan_review_report",
                    )
                for bill_no in evidence.bill_numbers:
                    if target := bill_nodes.get(bill_no):
                        state.add_edge(
                            EvidenceEdge(
                                target,
                                node_id,
                                EvidenceEdgeType.HAS_REVIEW_REPORT,
                                provenance,
                                "explicit exact bill number binding",
                            )
                        )
                    else:
                        state.unresolved(
                            node_id,
                            f"bill:{bill_no}",
                            EvidenceEdgeType.HAS_REVIEW_REPORT,
                            "review report bill number is absent from accepted bills",
                            provenance,
                            "missing_bill",
                        )
            elif document.kind is OfficialDocumentKind.BILL_TEXT:
                if not evidence.bill_numbers:
                    state.unresolved(
                        node_id,
                        "bill:unknown",
                        EvidenceEdgeType.HAS_BILL_TEXT,
                        "original bill text has no exact bill number binding",
                        provenance,
                        "orphan_bill_text",
                    )
                for bill_no in evidence.bill_numbers:
                    if target := bill_nodes.get(bill_no):
                        state.add_edge(
                            EvidenceEdge(
                                target,
                                node_id,
                                EvidenceEdgeType.HAS_BILL_TEXT,
                                provenance,
                                "verified exact billId and seven-digit bill number",
                            )
                        )
                    else:
                        state.unresolved(
                            node_id,
                            f"bill:{bill_no}",
                            EvidenceEdgeType.HAS_BILL_TEXT,
                            "original bill text bill number is absent from accepted bills",
                            provenance,
                            "missing_bill",
                        )
        return {url: tuple(sorted(ids)) for url, ids in by_url.items()}

    @staticmethod
    def _add_speeches(
        state: _BuildState,
        speeches: Sequence[SpeechEvidence],
        bill_nodes: Mapping[str, str],
        meeting_nodes: Mapping[str, str],
        agenda_nodes: Mapping[str, str],
        response_speeches: Mapping[str, str],
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        agenda_titles: dict[tuple[str, str], list[str]] = {}
        bill_titles: dict[str, list[str]] = {}
        for bill_no, bill_node_id in bill_nodes.items():
            bill_node = state.nodes[bill_node_id]
            bill_titles.setdefault(_normalized_reference(bill_node.label), []).append(bill_no)
        page_nodes: dict[tuple[str, str, str], list[str]] = {}
        for node in state.nodes.values():
            if node.node_type is EvidenceNodeType.DOCUMENT_PAGE:
                page_nodes.setdefault(
                    (
                        node.provenance.official_url,
                        node.provenance.source_hash,
                        node.provenance.locator,
                    ),
                    [],
                ).append(node.id)
        for node_id in agenda_nodes.values():
            node = state.nodes[node_id]
            meeting_sources = [
                edge.source_id
                for edge in state.edges
                if edge.edge_type is EvidenceEdgeType.HAS_AGENDA and edge.target_id == node_id
            ]
            for meeting_source in meeting_sources:
                agenda_titles.setdefault(
                    (meeting_source, _normalized_reference(node.label)), []
                ).append(node_id)
        for evidence in speeches:
            speech = evidence.speech
            if speech.source_locator is None:
                raise ValueError("accepted speech requires an exact PDF page locator")
            provenance = EvidenceProvenance(
                evidence.official_url, speech.source_hash, speech.source_locator
            )
            provenance.require_page()
            node_id = f"speech:{speech.id}"
            state.add_node(
                EvidenceNode(
                    node_id,
                    EvidenceNodeType.SPEECH,
                    f"{speech.speaker_name} 발언",
                    speech.text,
                    provenance,
                    (
                        ("agenda", speech.agenda),
                        ("organization", speech.organization),
                        ("role", speech.speaker_role),
                        ("sequence", speech.sequence),
                    ),
                )
            )
            for locator_part in _page_locator_parts(speech.source_locator):
                page_locator = locator_part.split(":", maxsplit=1)[0]
                part_provenance = EvidenceProvenance(
                    evidence.official_url,
                    speech.source_hash,
                    locator_part,
                )
                matching_pages = page_nodes.get(
                    (evidence.official_url, speech.source_hash, page_locator), []
                )
                if len(matching_pages) == 1:
                    state.add_edge(
                        EvidenceEdge(
                            matching_pages[0],
                            node_id,
                            EvidenceEdgeType.PAGE_CONTAINS_SPEECH,
                            part_provenance,
                            "speech locator resolves to one exact parsed document page",
                        )
                    )
                else:
                    reason = (
                        "speech page has no accepted parsed document"
                        if not matching_pages
                        else "speech page matches multiple accepted parser versions"
                    )
                    state.unresolved(
                        node_id,
                        f"document-page:{evidence.official_url}#{page_locator}",
                        EvidenceEdgeType.PAGE_CONTAINS_SPEECH,
                        reason,
                        part_provenance,
                        (
                            "missing_document_page"
                            if not matching_pages
                            else "ambiguous_document_page"
                        ),
                    )
            if speech.id in result:
                raise ValueError(f"duplicate accepted speech id: {speech.id}")
            result[speech.id] = node_id
            meeting_id = meeting_nodes.get(speech.meeting_id)
            if meeting_id:
                state.add_edge(
                    EvidenceEdge(
                        meeting_id,
                        node_id,
                        EvidenceEdgeType.CONTAINS_SPEECH,
                        provenance,
                        "exact speech meeting_id",
                    )
                )
            else:
                state.unresolved(
                    node_id,
                    f"meeting:{speech.meeting_id}",
                    EvidenceEdgeType.CONTAINS_SPEECH,
                    "speech meeting_id is absent from accepted meetings",
                    provenance,
                    "missing_meeting",
                )

            person_key = (
                speech.speaker_id or hashlib.sha256(speech.speaker_name.encode()).hexdigest()[:16]
            )
            person_id = f"person:{person_key}"
            state.add_node(
                EvidenceNode(
                    person_id,
                    EvidenceNodeType.PERSON,
                    speech.speaker_name,
                    speech.speaker_name,
                    provenance,
                )
            )
            state.add_edge(
                EvidenceEdge(
                    person_id,
                    node_id,
                    EvidenceEdgeType.MADE_SPEECH,
                    provenance,
                    "parsed speaker attribution",
                )
            )

            linked_agendas = list(evidence.agenda_ids)
            if not linked_agendas and speech.agenda and meeting_id:
                matches = agenda_titles.get((meeting_id, _normalized_reference(speech.agenda)), [])
                if len(matches) > 1:
                    reason = (
                        "speech agenda title matches multiple accepted agenda items; "
                        "title-only inference cannot establish an authoritative agenda id"
                    )
                    gap_code = "ambiguous_speech_agenda"
                elif matches:
                    reason = (
                        "speech agenda title matches an accepted agenda item, but "
                        "title-only inference cannot establish an authoritative agenda id"
                    )
                    gap_code = "speech_agenda_title_inferred"
                else:
                    reason = "speech agenda title has no explicit accepted agenda binding"
                    gap_code = "speech_agenda_unresolved"
                state.unresolved(
                    node_id,
                    f"agenda-title:{speech.agenda}:candidates={','.join(sorted(matches))}",
                    EvidenceEdgeType.ADDRESSES_AGENDA,
                    reason,
                    provenance,
                    gap_code,
                )
            elif not linked_agendas:
                state.unresolved(
                    node_id,
                    "agenda:unknown",
                    EvidenceEdgeType.ADDRESSES_AGENDA,
                    "speech has no explicit agenda binding",
                    provenance,
                    "speech_agenda_unresolved",
                )
            for agenda_ref in linked_agendas:
                target = agenda_nodes.get(agenda_ref)
                if target:
                    state.add_edge(
                        EvidenceEdge(
                            node_id,
                            target,
                            EvidenceEdgeType.ADDRESSES_AGENDA,
                            provenance,
                            "explicit or unique exact agenda binding",
                        )
                    )
                else:
                    state.unresolved(
                        node_id,
                        f"agenda:{agenda_ref}",
                        EvidenceEdgeType.ADDRESSES_AGENDA,
                        "speech agenda id is absent from accepted agendas",
                        provenance,
                        "missing_agenda",
                    )
            for bill_no in evidence.bill_numbers:
                if target := bill_nodes.get(bill_no):
                    state.add_edge(
                        EvidenceEdge(
                            node_id,
                            target,
                            EvidenceEdgeType.DISCUSSES_BILL,
                            provenance,
                            "explicit exact bill number binding",
                        )
                    )
                else:
                    state.unresolved(
                        node_id,
                        f"bill:{bill_no}",
                        EvidenceEdgeType.DISCUSSES_BILL,
                        "speech bill number is absent from accepted bills",
                        provenance,
                        "missing_bill",
                    )
            if not evidence.bill_numbers and speech.agenda:
                inferred_bills = tuple(
                    sorted(bill_titles.get(_normalized_reference(speech.agenda), ()))
                )
                if inferred_bills:
                    state.unresolved(
                        node_id,
                        f"bill-title:{speech.agenda}:candidates={','.join(inferred_bills)}",
                        EvidenceEdgeType.DISCUSSES_BILL,
                        "speech agenda matches accepted bill title, but title-only "
                        "inference cannot establish an authoritative bill association",
                        provenance,
                        "speech_bill_title_inferred",
                    )
            if speech.id in response_speeches:
                response_kind = response_speeches[speech.id]
                label = "정부 질의답변" if response_kind == "qa_response" else "정부 발언"
                response_id = f"government-response:{speech.id}"
                state.add_node(
                    EvidenceNode(
                        response_id,
                        EvidenceNodeType.GOVERNMENT_RESPONSE,
                        f"{speech.speaker_name} {label}",
                        speech.text,
                        provenance,
                        (
                            ("response_kind", response_kind),
                            ("speech_id", speech.id),
                        ),
                    )
                )
                state.add_edge(
                    EvidenceEdge(
                        response_id,
                        node_id,
                        EvidenceEdgeType.DERIVED_FROM_SPEECH,
                        provenance,
                        "source-backed government role with Q&A classification",
                    )
                )
        return result

    @staticmethod
    def _add_speech_relations(
        state: _BuildState,
        relations: Sequence[SpeechRelation],
        speech_nodes: Mapping[str, str],
    ) -> None:
        edge_types = {
            "QUESTION_TO": EvidenceEdgeType.QUESTION_TO,
            "ANSWER_TO": EvidenceEdgeType.ANSWER_TO,
            "FOLLOW_UP_TO": EvidenceEdgeType.FOLLOW_UP_TO,
            "CONTINUES": EvidenceEdgeType.CONTINUES,
        }
        for relation in relations:
            source = speech_nodes.get(relation.source_speech_id)
            target = speech_nodes.get(relation.target_speech_id)
            edge_type = edge_types[relation.relation_type]
            provenance_node = state.nodes.get(source or target or "")
            if provenance_node is None:
                state.add_gap(
                    "missing_speech_relation_endpoints",
                    f"{relation.source_speech_id}->{relation.target_speech_id}",
                    "speech relation has no accepted source-backed endpoint",
                )
                continue
            if source and target:
                state.add_edge(
                    EvidenceEdge(
                        source,
                        target,
                        edge_type,
                        provenance_node.provenance,
                        f"parser relation confidence={relation.confidence}",
                    )
                )
            else:
                state.unresolved(
                    source or f"speech:{relation.source_speech_id}",
                    target or f"speech:{relation.target_speech_id}",
                    edge_type,
                    "speech relation endpoint is absent from accepted speeches",
                    provenance_node.provenance,
                    "missing_speech",
                )

    @staticmethod
    def _add_issues(
        state: _BuildState,
        issues: Sequence[IssueEvidence],
        bill_nodes: Mapping[str, str],
        agenda_nodes: Mapping[str, str],
        document_nodes: Mapping[str, tuple[str, ...]],
        speech_nodes: Mapping[str, str],
        speech_evidence: Sequence[SpeechEvidence],
    ) -> None:
        issue_nodes: dict[str, str] = {}
        for issue in issues:
            node_id = f"issue:{issue.issue_id}"
            state.add_node(
                EvidenceNode(
                    node_id,
                    EvidenceNodeType.ISSUE,
                    issue.title,
                    issue.text,
                    issue.provenance,
                )
            )
            if issue.issue_id in issue_nodes:
                raise ValueError(f"duplicate accepted issue id: {issue.issue_id}")
            issue_nodes[issue.issue_id] = node_id
            for bill_no in issue.bill_numbers:
                _link_or_unresolved(
                    state,
                    bill_nodes.get(bill_no),
                    f"bill:{bill_no}",
                    node_id,
                    EvidenceEdgeType.HAS_ISSUE,
                    "explicit issue bill number",
                    issue.provenance,
                    "missing_bill",
                )
            for agenda_id in issue.agenda_ids:
                _link_or_unresolved(
                    state,
                    agenda_nodes.get(agenda_id),
                    f"agenda:{agenda_id}",
                    node_id,
                    EvidenceEdgeType.HAS_ISSUE,
                    "explicit issue agenda id",
                    issue.provenance,
                    "missing_agenda",
                )
            for url in issue.document_urls:
                candidates = document_nodes.get(url, ())
                if len(candidates) == 1:
                    state.add_edge(
                        EvidenceEdge(
                            candidates[0],
                            node_id,
                            EvidenceEdgeType.IDENTIFIES_ISSUE,
                            issue.provenance,
                            "exact official document URL",
                        )
                    )
                    matching_pages = [
                        node.id
                        for node in state.nodes.values()
                        if node.node_type is EvidenceNodeType.DOCUMENT_PAGE
                        and node.provenance == issue.provenance
                    ]
                    if len(matching_pages) == 1:
                        state.add_edge(
                            EvidenceEdge(
                                matching_pages[0],
                                node_id,
                                EvidenceEdgeType.IDENTIFIES_ISSUE,
                                issue.provenance,
                                "issue provenance resolves to one exact parsed page",
                            )
                        )
                    elif not matching_pages:
                        state.unresolved(
                            candidates[0],
                            f"document-page:{url}#{issue.provenance.locator}",
                            EvidenceEdgeType.IDENTIFIES_ISSUE,
                            "issue page is absent from accepted parsed document",
                            issue.provenance,
                            "missing_document_page",
                        )
                else:
                    reason = (
                        "issue document URL is absent"
                        if not candidates
                        else "issue document URL matches multiple parser versions"
                    )
                    state.unresolved(
                        f"document-url:{url}",
                        node_id,
                        EvidenceEdgeType.IDENTIFIES_ISSUE,
                        reason,
                        issue.provenance,
                        "missing_document" if not candidates else "ambiguous_document",
                    )
            for speech_id in issue.speech_ids:
                _link_or_unresolved(
                    state,
                    speech_nodes.get(speech_id),
                    f"speech:{speech_id}",
                    node_id,
                    EvidenceEdgeType.DISCUSSES_ISSUE,
                    "explicit issue speech id",
                    issue.provenance,
                    "missing_speech",
                )

        for evidence in speech_evidence:
            source = speech_nodes[evidence.speech.id]
            provenance = state.nodes[source].provenance
            for issue_id in evidence.issue_ids:
                target = issue_nodes.get(issue_id)
                _link_or_unresolved(
                    state,
                    source,
                    source,
                    target or f"issue:{issue_id}",
                    EvidenceEdgeType.DISCUSSES_ISSUE,
                    "explicit speech issue id",
                    provenance,
                    "missing_issue",
                    target_exists=target is not None,
                )


class _BuildState:
    def __init__(self) -> None:
        self.nodes: dict[str, EvidenceNode] = {}
        self.edges: list[EvidenceEdge] = []
        self.unresolved_edges: list[UnresolvedEdge] = []
        self.coverage_gaps: list[CoverageGap] = []
        self._edge_keys: set[tuple[str, str, str, str]] = set()
        self._unresolved_keys: set[tuple[str, str, str, str]] = set()
        self._gap_keys: set[tuple[str, str, str]] = set()

    def add_node(self, node: EvidenceNode) -> None:
        existing = self.nodes.get(node.id)
        if existing is not None and existing != node:
            if (
                existing.node_type is EvidenceNodeType.PERSON
                and node.node_type is EvidenceNodeType.PERSON
                and existing.label == node.label
                and existing.text == node.text
                and existing.attributes == node.attributes
            ):
                self.nodes[node.id] = min(
                    (existing, node), key=lambda item: _provenance_key(item.provenance)
                )
                return
            raise ValueError(f"conflicting evidence node id: {node.id}")
        self.nodes[node.id] = node

    def add_edge(self, edge: EvidenceEdge) -> None:
        key = (edge.source_id, edge.target_id, edge.edge_type.value, edge.reason)
        if key not in self._edge_keys:
            self._edge_keys.add(key)
            self.edges.append(edge)

    def unresolved(
        self,
        source: str,
        target: str,
        edge_type: EvidenceEdgeType,
        reason: str,
        provenance: EvidenceProvenance,
        gap_code: str,
    ) -> None:
        key = (source, target, edge_type.value, reason)
        if key not in self._unresolved_keys:
            self._unresolved_keys.add(key)
            self.unresolved_edges.append(
                UnresolvedEdge(source, target, edge_type, reason, provenance)
            )
        self.add_gap(gap_code, source, reason)

    def add_gap(self, code: str, entity: str, detail: str) -> None:
        key = (code, entity, detail)
        if key not in self._gap_keys:
            self._gap_keys.add(key)
            self.coverage_gaps.append(CoverageGap(code, entity, detail))

    def finish(self) -> EvidenceGraph:
        return EvidenceGraph(
            tuple(self.nodes.values()),
            tuple(self.edges),
            tuple(self.unresolved_edges),
            tuple(self.coverage_gaps),
        )


def _link_or_unresolved(
    state: _BuildState,
    source: str | None,
    source_reference: str,
    target: str,
    edge_type: EvidenceEdgeType,
    reason: str,
    provenance: EvidenceProvenance,
    gap_code: str,
    *,
    target_exists: bool = True,
) -> None:
    if source and target_exists:
        state.add_edge(EvidenceEdge(source, target, edge_type, provenance, reason))
    else:
        state.unresolved(
            source_reference,
            target,
            edge_type,
            f"{reason}: referenced accepted node is absent",
            provenance,
            gap_code,
        )


def _validate_bill_numbers(values: Iterable[str]) -> None:
    for value in values:
        _validate_bill_no(value)


def _page_locator_parts(value: str) -> tuple[str, ...]:
    parts = tuple(value.split("|"))
    if not parts or any(not _PAGE_LOCATOR_PART.fullmatch(part) for part in parts):
        return ()
    return parts


def _normalized_reference(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _TITLE_PREFIX.sub("", normalized).strip()


def _validate_bill_no(value: str) -> None:
    if not _BILL_NO.fullmatch(value):
        raise ValueError("evidence graph bill number must contain exactly seven digits")


def _validate_hash(value: str) -> None:
    if not _SHA256.fullmatch(value):
        raise ValueError("evidence graph source hash must be lowercase SHA-256 hex")


def _validate_official_url(value: str) -> None:
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(
            "evidence graph URL must use an exact official Assembly HTTPS host"
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _OFFICIAL_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ValueError("evidence graph URL must use an exact official Assembly HTTPS host")


def _edge_key(edge: EvidenceEdge) -> tuple[str, str, str, str, str]:
    return (
        edge.edge_type.value,
        edge.source_id,
        edge.target_id,
        edge.reason,
        edge.provenance.locator,
    )


def _unresolved_key(edge: UnresolvedEdge) -> tuple[str, str, str, str, str]:
    return (
        edge.edge_type.value,
        edge.source_reference,
        edge.target_reference,
        edge.reason,
        edge.provenance.locator,
    )


def _provenance_key(provenance: EvidenceProvenance) -> tuple[str, str, str]:
    return provenance.official_url, provenance.source_hash, provenance.locator


__all__ = [
    "CoverageGap",
    "DocumentEvidence",
    "EvidenceEdge",
    "EvidenceEdgeType",
    "EvidenceGraph",
    "EvidenceGraphBuilder",
    "EvidenceNode",
    "EvidenceNodeType",
    "EvidenceProvenance",
    "IssueEvidence",
    "SpeechEvidence",
    "UnresolvedEdge",
]
