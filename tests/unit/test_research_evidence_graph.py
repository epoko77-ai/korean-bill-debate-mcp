from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from kasm.core.models import Agenda, Bill, Meeting, Speech, SpeechRelation
from kasm.research.documents import (
    OfficialDocumentKind,
    ParsedOfficialDocument,
    TextSegment,
)
from kasm.research.evidence_graph import (
    DocumentEvidence,
    EvidenceEdgeType,
    EvidenceGraphBuilder,
    EvidenceNodeType,
    EvidenceProvenance,
    IssueEvidence,
    SpeechEvidence,
)

NOW = datetime(2026, 7, 13, 12, tzinfo=UTC)
BILL_URL = "https://likms.assembly.go.kr/bill/2219564"
MINUTES_URL = "https://record.assembly.go.kr/minutes/one.pdf"
REVIEW_URL = "https://likms.assembly.go.kr/filegate/review.pdf"
BILL_TEXT_URL = (
    "https://likms.assembly.go.kr/bill/bi/bill/detail/downloadDtlZip.do?"
    "billId=PRC_TEST&billNo=2219564"
)
BILL_HASH = "a" * 64
MINUTES_HASH = "b" * 64
REVIEW_HASH = "c" * 64
BILL_TEXT_HASH = "d" * 64


def bill(number: str = "2219564") -> Bill:
    return Bill(
        id=f"kna:bill:{number}",
        bill_no=number,
        name="형사소송법 일부개정법률안",
        assembly_term=22,
        proposer="김의원",
        committee="법제사법위원회",
        proposed_at=date(2026, 6, 26),
        process_result="위원회 심사",
        processed_at=date(2026, 7, 8),
        official_url=BILL_URL,
        source_hash=BILL_HASH,
        retrieved_at=NOW,
    )


def meeting() -> Meeting:
    return Meeting(
        id="meeting-1",
        assembly_term=22,
        committee_id="legislation",
        committee_name_ko="법제사법위원회",
        committee_name_en=None,
        title="법제사법위원회 법안심사소위원회",
        meeting_type="subcommittee",
        meeting_number="1",
        date=date(2026, 7, 8),
        source_url=MINUTES_URL,
        source_hash=MINUTES_HASH,
        retrieved_at=NOW,
    )


def agenda(identifier: str = "agenda-1", sequence: int = 1) -> Agenda:
    return Agenda(
        id=identifier,
        meeting_id="meeting-1",
        sequence=sequence,
        title="형사소송법 일부개정법률안",
        bill_no="2219564",
        official_url=MINUTES_URL,
        source_hash=MINUTES_HASH,
    )


def speech(
    identifier: str,
    sequence: int,
    name: str,
    role: str,
    text: str,
    locator: str,
) -> Speech:
    return Speech(
        id=identifier,
        meeting_id="meeting-1",
        sequence=sequence,
        speaker_id=f"person-{name}",
        speaker_name=name,
        speaker_role=role,
        organization="법무부" if role == "장관" else None,
        text=text,
        agenda="형사소송법 일부개정법률안",
        previous_speech_id=None,
        next_speech_id=None,
        source_locator=locator,
        source_hash=MINUTES_HASH,
        parser_version="korea-rules-v2",
    )


def parsed_document(
    kind: OfficialDocumentKind,
    url: str,
    source_hash: str,
    *pages: str,
) -> ParsedOfficialDocument:
    return ParsedOfficialDocument(
        kind=kind,
        official_url=url,
        source_hash=source_hash,
        parser_version="pypdf-layout-v1",
        parsed_at=NOW,
        segments=tuple(
            TextSegment(locator=f"p.{number}", text=text)
            for number, text in enumerate(pages, start=1)
        ),
    )


def test_builds_connected_lossless_graph_with_status_pages_speeches_and_issue() -> None:
    long_review = "전문위원 검토의견 " + "가" * 120_000
    minutes = parsed_document(
        OfficialDocumentKind.MINUTES,
        MINUTES_URL,
        MINUTES_HASH,
        "의사일정과 질의",
        "정부 답변",
    )
    review = parsed_document(
        OfficialDocumentKind.REVIEW_REPORT,
        REVIEW_URL,
        REVIEW_HASH,
        long_review,
    )
    question = speech("q1", 1, "김의원", "위원", "보완수사권이 필요합니까?", "p.1:10-40")
    answer = speech("a1", 2, "이장관", "장관", "정부는 필요하다고 봅니다.", "p.2:1-30")
    issue = IssueEvidence(
        issue_id="supplementary-investigation",
        title="보완수사권 범위",
        text="전문위원은 권한 범위와 통제 장치를 쟁점으로 지적했다.",
        provenance=EvidenceProvenance(REVIEW_URL, REVIEW_HASH, "p.1"),
        bill_numbers=("2219564",),
        agenda_ids=("agenda-1",),
        document_urls=(REVIEW_URL,),
        speech_ids=("q1", "a1"),
    )
    inputs = {
        "bills": (bill(),),
        "meetings": (meeting(),),
        "agendas": (agenda(),),
        "documents": (
            DocumentEvidence(
                minutes,
                bill_numbers=("2219564",),
                meeting_ids=("meeting-1",),
            ),
            DocumentEvidence(review, bill_numbers=("2219564",)),
        ),
        "speeches": (
            SpeechEvidence(
                question,
                MINUTES_URL,
                bill_numbers=("2219564",),
                agenda_ids=("agenda-1",),
                issue_ids=(issue.issue_id,),
            ),
            SpeechEvidence(
                answer,
                MINUTES_URL,
                bill_numbers=("2219564",),
                agenda_ids=("agenda-1",),
                issue_ids=(issue.issue_id,),
                government_response=True,
            ),
        ),
        "speech_relations": (
            SpeechRelation("q1", "a1", "QUESTION_TO", 0.9),
            SpeechRelation("a1", "q1", "ANSWER_TO", 0.9),
        ),
        "issues": (issue,),
    }

    graph = EvidenceGraphBuilder().build(**inputs)
    reverse = EvidenceGraphBuilder().build(
        **{
            key: tuple(reversed(value)) if isinstance(value, tuple) else value
            for key, value in inputs.items()
        }
    )

    assert graph.to_dict() == reverse.to_dict()
    assert graph.graph_hash == reverse.graph_hash
    assert graph.unresolved_edges == ()
    assert graph.coverage_gaps == ()
    node_types = {node.node_type for node in graph.nodes}
    assert {
        EvidenceNodeType.BILL,
        EvidenceNodeType.BILL_STATUS,
        EvidenceNodeType.AGENDA,
        EvidenceNodeType.MEETING,
        EvidenceNodeType.MINUTES_DOCUMENT,
        EvidenceNodeType.REVIEW_REPORT,
        EvidenceNodeType.DOCUMENT_PAGE,
        EvidenceNodeType.PERSON,
        EvidenceNodeType.SPEECH,
        EvidenceNodeType.GOVERNMENT_RESPONSE,
        EvidenceNodeType.ISSUE,
    } <= node_types
    review_node = next(
        node for node in graph.nodes if node.node_type is EvidenceNodeType.REVIEW_REPORT
    )
    review_page = next(
        node
        for node in graph.nodes
        if node.node_type is EvidenceNodeType.DOCUMENT_PAGE
        and node.provenance.official_url == REVIEW_URL
    )
    assert review_node.text == long_review
    assert review_page.text == long_review
    assert len(review_page.text) > 100_000
    assert graph.to_dict()["nodes"][graph.nodes.index(review_page)]["text"] == long_review
    edge_types = {edge.edge_type for edge in graph.edges}
    assert {
        EvidenceEdgeType.HAS_STATUS,
        EvidenceEdgeType.HAS_AGENDA,
        EvidenceEdgeType.AGENDA_FOR_BILL,
        EvidenceEdgeType.HAS_MINUTES,
        EvidenceEdgeType.EVIDENCED_BY_MINUTES,
        EvidenceEdgeType.HAS_REVIEW_REPORT,
        EvidenceEdgeType.HAS_PAGE,
        EvidenceEdgeType.CONTAINS_SPEECH,
        EvidenceEdgeType.PAGE_CONTAINS_SPEECH,
        EvidenceEdgeType.MADE_SPEECH,
        EvidenceEdgeType.DERIVED_FROM_SPEECH,
        EvidenceEdgeType.QUESTION_TO,
        EvidenceEdgeType.ANSWER_TO,
        EvidenceEdgeType.HAS_ISSUE,
        EvidenceEdgeType.IDENTIFIES_ISSUE,
        EvidenceEdgeType.DISCUSSES_ISSUE,
    } <= edge_types


def test_orphans_and_ambiguous_links_are_preserved_as_gaps() -> None:
    orphan_review = parsed_document(
        OfficialDocumentKind.REVIEW_REPORT,
        REVIEW_URL,
        REVIEW_HASH,
        "연결할 의안번호가 없는 검토보고서",
    )
    orphan_minutes = parsed_document(
        OfficialDocumentKind.MINUTES,
        MINUTES_URL,
        MINUTES_HASH,
        "연결할 회의가 없는 회의록",
    )
    orphan_agenda = Agenda(
        id="orphan-agenda",
        meeting_id="missing-meeting",
        sequence=1,
        title="의안번호가 불명확한 안건",
        bill_no=None,
        official_url=MINUTES_URL,
        source_hash=MINUTES_HASH,
    )
    orphan_speech = Speech(
        id="orphan-speech",
        meeting_id="missing-meeting",
        sequence=1,
        speaker_id=None,
        speaker_name="익명위원",
        speaker_role="위원",
        organization=None,
        text="어느 안건인지 확인이 더 필요합니다.",
        agenda=None,
        previous_speech_id=None,
        next_speech_id=None,
        source_locator="p.1",
        source_hash=MINUTES_HASH,
        parser_version="v1",
    )

    graph = EvidenceGraphBuilder().build(
        agendas=(orphan_agenda,),
        documents=(
            DocumentEvidence(orphan_review),
            DocumentEvidence(orphan_minutes, meeting_ids=("missing-meeting",)),
        ),
        speeches=(SpeechEvidence(orphan_speech, MINUTES_URL),),
    )

    assert any(node.node_type is EvidenceNodeType.REVIEW_REPORT for node in graph.nodes)
    assert any(node.node_type is EvidenceNodeType.MINUTES_DOCUMENT for node in graph.nodes)
    assert any(node.node_type is EvidenceNodeType.SPEECH for node in graph.nodes)
    assert len(graph.unresolved_edges) >= 6
    gap_codes = {gap.code for gap in graph.coverage_gaps}
    assert {
        "agenda_bill_unresolved",
        "missing_meeting",
        "orphan_review_report",
        "speech_agenda_unresolved",
    } <= gap_codes


def test_all_accepted_agendas_are_preserved_without_top_n_limit() -> None:
    agendas = tuple(agenda(f"agenda-{number:03d}", number) for number in range(125))

    graph = EvidenceGraphBuilder().build(
        bills=(bill(),),
        meetings=(meeting(),),
        agendas=tuple(reversed(agendas)),
    )

    agenda_nodes = [node for node in graph.nodes if node.node_type is EvidenceNodeType.AGENDA]
    assert len(agenda_nodes) == 125
    assert {node.id for node in agenda_nodes} == {
        f"agenda:agenda-{number:03d}" for number in range(125)
    }
    assert graph.unresolved_edges == ()


def test_all_related_documents_and_speeches_are_preserved_without_top_n_limit() -> None:
    documents = tuple(
        DocumentEvidence(
            parsed_document(
                OfficialDocumentKind.REVIEW_REPORT,
                f"https://likms.assembly.go.kr/filegate/review.pdf?id={number}",
                f"{number + 1:064x}",
                f"검토보고서 전체 원문 {number}",
            ),
            bill_numbers=("2219564",),
        )
        for number in range(105)
    )
    speeches = tuple(
        SpeechEvidence(
            speech(
                f"speech-{number:03d}",
                number,
                "김의원",
                "위원",
                f"전체 발언 {number}",
                "p.1",
            ),
            MINUTES_URL,
            bill_numbers=("2219564",),
            agenda_ids=("agenda-1",),
        )
        for number in range(105)
    )

    graph = EvidenceGraphBuilder().build(
        bills=(bill(),),
        meetings=(meeting(),),
        agendas=(agenda(),),
        documents=tuple(
            reversed(
                (
                    *documents,
                    DocumentEvidence(
                        parsed_document(
                            OfficialDocumentKind.MINUTES,
                            MINUTES_URL,
                            MINUTES_HASH,
                            "발언이 수록된 회의록",
                        ),
                        meeting_ids=("meeting-1",),
                    ),
                )
            )
        ),
        speeches=tuple(reversed(speeches)),
    )

    assert sum(node.node_type is EvidenceNodeType.REVIEW_REPORT for node in graph.nodes) == 105
    assert sum(node.node_type is EvidenceNodeType.SPEECH for node in graph.nodes) == 105
    assert sum(edge.edge_type is EvidenceEdgeType.HAS_REVIEW_REPORT for edge in graph.edges) == 105
    assert sum(edge.edge_type is EvidenceEdgeType.DISCUSSES_BILL for edge in graph.edges) == 105
    assert graph.unresolved_edges == ()


def test_ambiguous_exact_agenda_title_is_not_guessed() -> None:
    duplicate_title_agendas = (
        agenda("agenda-a", 1),
        agenda("agenda-b", 2),
    )
    spoken = speech("s1", 1, "김의원", "위원", "두 안건을 함께 봐야 합니다.", "p.1")

    graph = EvidenceGraphBuilder().build(
        bills=(bill(),),
        meetings=(meeting(),),
        agendas=duplicate_title_agendas,
        speeches=(SpeechEvidence(spoken, MINUTES_URL),),
    )

    unresolved = next(
        edge
        for edge in graph.unresolved_edges
        if edge.edge_type is EvidenceEdgeType.ADDRESSES_AGENDA
    )
    assert "multiple" in unresolved.reason
    assert any(gap.code == "ambiguous_speech_agenda" for gap in graph.coverage_gaps)
    assert any(node.id == "speech:s1" for node in graph.nodes)


def test_title_only_matches_remain_inferred_and_never_create_authoritative_edges() -> None:
    unbound_agenda = Agenda(
        id="agenda-title-only",
        meeting_id="meeting-1",
        sequence=1,
        title="형사소송법 일부개정법률안",
        bill_no=None,
        official_url=MINUTES_URL,
        source_hash=MINUTES_HASH,
    )
    spoken = speech(
        "title-only-speech",
        1,
        "김의원",
        "위원",
        "법안 제목만 언급한 발언입니다.",
        "p.1:0-20",
    )

    graph = EvidenceGraphBuilder().build(
        bills=(bill(),),
        meetings=(meeting(),),
        agendas=(unbound_agenda,),
        speeches=(SpeechEvidence(spoken, MINUTES_URL),),
    )

    forbidden = {
        EvidenceEdgeType.AGENDA_FOR_BILL,
        EvidenceEdgeType.ADDRESSES_AGENDA,
        EvidenceEdgeType.DISCUSSES_BILL,
    }
    assert not any(edge.edge_type in forbidden for edge in graph.edges)
    inferred = [edge for edge in graph.unresolved_edges if edge.edge_type in forbidden]
    assert {edge.edge_type for edge in inferred} == forbidden
    assert all("title" in edge.reason for edge in inferred)


def test_multi_page_speech_is_linked_to_every_exact_source_page() -> None:
    minutes = parsed_document(
        OfficialDocumentKind.MINUTES,
        MINUTES_URL,
        MINUTES_HASH,
        "첫 페이지 발언",
        "둘째 페이지에 이어진 발언",
    )
    spoken = speech(
        "cross-page",
        1,
        "김의원",
        "위원",
        "첫 페이지 발언\n둘째 페이지에 이어진 발언",
        "p.1:0-8|p.2:0-14",
    )

    graph = EvidenceGraphBuilder().build(
        bills=(bill(),),
        meetings=(meeting(),),
        agendas=(agenda(),),
        documents=(DocumentEvidence(minutes, meeting_ids=("meeting-1",)),),
        speeches=(
            SpeechEvidence(
                spoken,
                MINUTES_URL,
                bill_numbers=("2219564",),
                agenda_ids=("agenda-1",),
            ),
        ),
    )

    page_edges = [
        edge
        for edge in graph.edges
        if edge.edge_type is EvidenceEdgeType.PAGE_CONTAINS_SPEECH
        and edge.target_id == "speech:cross-page"
    ]
    assert len(page_edges) == 2
    assert {edge.provenance.locator for edge in page_edges} == {
        "p.1:0-8",
        "p.2:0-14",
    }


def test_government_statement_and_qa_response_are_classified_separately() -> None:
    question = speech("question", 1, "김의원", "위원", "입장은 무엇입니까?", "p.1")
    answer = speech("answer", 2, "이장관", "장관", "답변드리겠습니다.", "p.2")
    statement = speech("statement", 3, "박장관", "장관", "정부 입장을 밝힙니다.", "p.3")
    private_answer = speech(
        "private-answer",
        4,
        "최전문위원",
        "전문위원",
        "검토 의견입니다.",
        "p.4",
    )

    graph = EvidenceGraphBuilder().build(
        bills=(bill(),),
        meetings=(meeting(),),
        agendas=(agenda(),),
        speeches=(
            SpeechEvidence(answer, MINUTES_URL, government_response=True),
            SpeechEvidence(private_answer, MINUTES_URL),
            SpeechEvidence(question, MINUTES_URL),
            SpeechEvidence(statement, MINUTES_URL, government_response=True),
        ),
        speech_relations=(
            SpeechRelation("question", "answer", "QUESTION_TO", 0.9),
            SpeechRelation("answer", "question", "ANSWER_TO", 0.9),
            SpeechRelation("question", "private-answer", "QUESTION_TO", 0.8),
            SpeechRelation("private-answer", "question", "ANSWER_TO", 0.8),
        ),
    )

    responses = {
        str(dict(node.attributes)["speech_id"]): str(dict(node.attributes)["response_kind"])
        for node in graph.nodes
        if node.node_type is EvidenceNodeType.GOVERNMENT_RESPONSE
    }
    assert responses == {
        "answer": "qa_response",
        "statement": "government_statement",
    }


def test_exact_bill_url_hash_and_page_locator_are_enforced() -> None:
    with pytest.raises(ValueError, match="official Assembly"):
        EvidenceProvenance("https://example.com/a.pdf", "a" * 64, "p.1")
    with pytest.raises(ValueError, match="SHA-256"):
        EvidenceProvenance(MINUTES_URL, "short", "p.1")
    with pytest.raises(ValueError, match="exact p.N"):
        EvidenceProvenance(MINUTES_URL, MINUTES_HASH, "page one").require_page()
    with pytest.raises(ValueError, match="seven digits"):
        DocumentEvidence(
            parsed_document(
                OfficialDocumentKind.REVIEW_REPORT,
                REVIEW_URL,
                REVIEW_HASH,
                "text",
            ),
            bill_numbers=("221",),
        )
    with pytest.raises(ValueError, match="seven digits"):
        EvidenceGraphBuilder().build(bills=(bill("221"),))


def test_original_bill_text_is_lossless_and_attached_only_by_exact_bill_number() -> None:
    full_text = "의안 원문 " + "가" * 150_000
    document = parsed_document(
        OfficialDocumentKind.BILL_TEXT,
        BILL_TEXT_URL,
        BILL_TEXT_HASH,
        full_text,
    )

    graph = EvidenceGraphBuilder().build(
        bills=(bill(),),
        documents=(DocumentEvidence(document, bill_numbers=("2219564",)),),
    )

    node = next(
        item for item in graph.nodes if item.node_type is EvidenceNodeType.BILL_TEXT
    )
    page = next(
        item
        for item in graph.nodes
        if item.node_type is EvidenceNodeType.DOCUMENT_PAGE
    )
    edge = next(
        item
        for item in graph.edges
        if item.edge_type is EvidenceEdgeType.HAS_BILL_TEXT
    )
    assert node.text == full_text
    assert page.text == full_text
    assert len(page.text) > 100_000
    assert edge.source_id == "bill:2219564"
    assert edge.target_id == node.id
    assert graph.coverage_gaps == ()


def test_repeated_speaker_is_one_deterministic_person_with_every_speech() -> None:
    first = speech("same-1", 1, "김의원", "위원", "첫 발언", "p.2")
    second = speech("same-2", 2, "김의원", "위원", "둘째 발언", "p.1")
    evidences = (
        SpeechEvidence(first, MINUTES_URL, agenda_ids=("agenda-1",)),
        SpeechEvidence(second, MINUTES_URL, agenda_ids=("agenda-1",)),
    )

    forward = EvidenceGraphBuilder().build(
        bills=(bill(),),
        meetings=(meeting(),),
        agendas=(agenda(),),
        speeches=evidences,
    )
    reverse = EvidenceGraphBuilder().build(
        bills=(bill(),),
        meetings=(meeting(),),
        agendas=(agenda(),),
        speeches=tuple(reversed(evidences)),
    )

    assert forward.to_dict() == reverse.to_dict()
    people = [node for node in forward.nodes if node.node_type is EvidenceNodeType.PERSON]
    assert len(people) == 1
    assert people[0].provenance.locator == "p.1"
    made_speech = [edge for edge in forward.edges if edge.edge_type is EvidenceEdgeType.MADE_SPEECH]
    assert len(made_speech) == 2
