from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from kasm.adapters.korea.ingestion import meeting_from_open_assembly_row
from kasm.core.ids import speech_id
from kasm.core.models import Speech, SpeechRelation
from kasm.research.collector import CollectionCoverage, MetadataCollection
from kasm.research.contracts import EvidenceType
from kasm.research.document_worker import DocumentWorkResult
from kasm.research.documents import (
    OfficialDocumentKind,
    ParsedOfficialDocument,
    TextSegment,
)
from kasm.research.engine import (
    BillDocumentDiscovery,
    DiscoveryStageState,
    DocumentOutcome,
    DocumentOutcomeStatus,
    DocumentWorkItem,
    DocumentWorkManifest,
    FamilyFilterAccounting,
    FinalizationContext,
    GatewayPlanState,
    MetadataStageState,
    StrictFilterReport,
)
from kasm.research.engine import (
    CoverageGap as EngineCoverageGap,
)
from kasm.research.evidence_graph import EvidenceNodeType
from kasm.research.finalizer import ConnectedResearchFinalizer
from kasm.research.jobs import JobStatus, ResearchJob
from kasm.research.partitioning import ResearchPartitionPlanner
from kasm.research.planner import ResearchPlan, plan_research
from kasm.research.resolver import (
    CandidateSetResolution,
    MetadataResolution,
    resolve_metadata_candidates,
)
from kasm.research.transcript_evidence import TranscriptEvidence

NOW = datetime(2026, 7, 13, 12, tzinfo=UTC)
MINUTES_URL = "https://record.assembly.go.kr/minutes/finalizer.pdf"
REVIEW_URL = "https://likms.assembly.go.kr/filegate/review.pdf"
BILL_TEXT_URL = (
    "https://likms.assembly.go.kr/bill/bi/bill/detail/downloadDtlZip.do?"
    "billId=PRC_2219564&billNo=2219564"
)
MINUTES_HASH = "a" * 64


def _bill_row(number: str, *, title: str = "형사소송법 일부개정법률안") -> dict[str, Any]:
    return {
        "BILL_NO": number,
        "BILL_NAME": title,
        "AGE": 22,
        "PROPOSER": "김의원",
        "COMMITTEE": "법제사법위원회",
        "PROPOSE_DT": "2026-06-01",
        "summary": "보완수사권과 보완수사요구권을 정비한다.",
        "DETAIL_LINK": (f"https://likms.assembly.go.kr/bill/billDetail.do?billId={number}"),
    }


def _meeting_row(
    agenda_items: list[dict[str, str | None]],
) -> dict[str, Any]:
    return {
        "DAE_NUM": 22,
        "CONF_ID": "finalizer-meeting",
        "CONF_DATE": "2026-07-02",
        "COMM_NAME": "법제사법위원회 법안심사소위원회",
        "TITLE": "법제사법위원회 법안심사소위원회",
        "PDF_LINK_URL": MINUTES_URL,
        "agenda_items": agenda_items,
        "agenda_text": "\n".join(
            f"{item.get('bill_no') or ''} {item['title']}" for item in agenda_items
        ),
    }


def _collection(
    *,
    bills: tuple[dict[str, Any], ...] = (),
    meetings: tuple[dict[str, Any], ...] = (),
    bill_rejected: int = 0,
    meeting_rejected: int = 0,
) -> MetadataCollection:
    fetched = len(bills) + len(meetings) + bill_rejected + meeting_rejected
    return MetadataCollection(
        bills=bills,
        meetings=meetings,
        partitions=(),
        coverage=CollectionCoverage(
            partitions_expected=0,
            partitions_complete=0,
            source_rows_expected=fetched,
            source_rows_fetched=fetched,
            bill_source_rows=len(bills) + bill_rejected,
            bill_unique_records=len(bills),
            bill_duplicate_rows=0,
            bill_rejected_rows=bill_rejected,
            meeting_source_rows=len(meetings) + meeting_rejected,
            meeting_unique_pdfs=len(meetings),
            meeting_rows_merged=0,
            meeting_rejected_rows=meeting_rejected,
        ),
    )


def _document(
    kind: OfficialDocumentKind,
    url: str,
    source_hash: str,
    text: str,
) -> ParsedOfficialDocument:
    return ParsedOfficialDocument(
        kind=kind,
        official_url=url,
        source_hash=source_hash,
        parser_version="test-parser-v1",
        parsed_at=NOW,
        segments=(TextSegment("p.1", text),),
    )


def _outcome(item: DocumentWorkItem, document: ParsedOfficialDocument) -> DocumentOutcome:
    result = DocumentWorkResult(
        kind=document.kind,
        official_url=document.official_url,
        parser_version=document.parser_version,
        byte_count=max(1, len(document.full_text.encode())),
        page_count=len(document.segments),
        character_count=len(document.full_text),
        source_hash=document.source_hash,
        text_hash=document.text_hash,
        cache_hit=False,
        raw_object_key=f"official/raw/{document.source_hash}",
        parsed_object_key=document.object_key,
        document=document,
    )
    return DocumentOutcome(
        work_id=item.work_id,
        status=DocumentOutcomeStatus.SUCCEEDED,
        result=result,
    )


def _job(plan: ResearchPlan, identifier: str = "research-finalizer") -> ResearchJob:
    return ResearchJob(
        id=identifier,
        contract=plan.contract,
        query_fingerprint=plan.contract.fingerprint("index-finalizer-v1"),
        index_revision="index-finalizer-v1",
        status=JobStatus.RUNNING,
        stage="finalizing",
        progress=0.95,
        created_at=NOW,
        updated_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )


def _context(
    *,
    plan: ResearchPlan,
    discovery_collection: MetadataCollection,
    status_collection: MetadataCollection,
    resolution: MetadataResolution,
    manifest: DocumentWorkManifest,
    outcomes: tuple[DocumentOutcome, ...],
    transcripts: tuple[TranscriptEvidence, ...] = (),
    gaps: tuple[EngineCoverageGap, ...] = (),
    identifier: str = "research-finalizer",
) -> FinalizationContext:
    job = _job(plan, identifier)
    discovery = DiscoveryStageState(
        collection=discovery_collection,
        filtered_collection=discovery_collection,
        filter_report=StrictFilterReport(
            bills=FamilyFilterAccounting(
                len(discovery_collection.bills),
                len(discovery_collection.bills),
            ),
            meetings=FamilyFilterAccounting(
                len(discovery_collection.meetings),
                len(discovery_collection.meetings),
            ),
        ),
        resolution=resolution,
        status_partitions=(),
        document_bill_numbers=tuple(
            decision.candidate_id.removeprefix("bill:") for decision in resolution.bills.accepted
        ),
    )
    metadata = MetadataStageState(
        discovery=discovery,
        status_collection=status_collection,
        manifest=manifest,
        coverage_gaps=gaps,
    )
    partition_plan = ResearchPartitionPlanner().plan(plan)
    return FinalizationContext(
        job=job,
        gateway=GatewayPlanState(
            job=job,
            research_plan=plan,
            partition_plan=partition_plan,
            discovery_partitions=partition_plan.metadata_partitions,
        ),
        metadata=metadata,
        outcomes=outcomes,
        transcripts=transcripts,
        coverage_gaps=gaps,
    )


def _large_context() -> FinalizationContext:
    plan = plan_research(
        "2219564 보완수사권 법안과 소위원회 질의답변",
        as_of=NOW,
    )
    agenda_items: list[dict[str, str | None]] = [
        {
            "bill_no": "2219564",
            "title": (
                "형사소송법 일부개정법률안" if number == 0 else f"보완수사권 부대 안건 {number:03d}"
            ),
        }
        for number in range(125)
    ]
    meeting_row = _meeting_row(agenda_items)
    discovery_collection = _collection(
        bills=(_bill_row("2219564"),),
        meetings=(meeting_row,),
    )
    resolution = resolve_metadata_candidates(plan, discovery_collection)
    assert resolution.bills.accepted_count == 1
    assert resolution.meetings.accepted_count == 1
    status_collection = _collection(
        bills=(
            {
                "BILL_NO": "2219564",
                "PROC_RESULT": "위원회 심사",
                "PROC_DT": "2026-07-02",
            },
        )
    )

    minute_item = DocumentWorkItem.create(
        OfficialDocumentKind.MINUTES,
        MINUTES_URL,
        evidence_types=(
            EvidenceType.AGENDAS,
            EvidenceType.SUBCOMMITTEE_MINUTES,
            EvidenceType.SPEECHES,
            EvidenceType.SPEECH_CONTEXT,
            EvidenceType.GOVERNMENT_RESPONSES,
        ),
    )
    minute_document = _document(
        OfficialDocumentKind.MINUTES,
        MINUTES_URL,
        MINUTES_HASH,
        "공식 소위원회 회의록 전체",
    )
    review_items: list[DocumentWorkItem] = []
    review_documents: list[ParsedOfficialDocument] = []
    for number in range(104):
        url = f"{REVIEW_URL}?document={number:03d}"
        item = DocumentWorkItem.create(
            OfficialDocumentKind.REVIEW_REPORT,
            url,
            evidence_types=(EvidenceType.REVIEW_REPORTS,),
            related_bill_numbers=("2219564",),
        )
        review_items.append(item)
        review_documents.append(
            _document(
                OfficialDocumentKind.REVIEW_REPORT,
                url,
                f"{number + 1:064x}",
                (
                    "전문위원 검토보고서 " + "가" * 120_000
                    if number == 0
                    else f"전문위원 검토보고서 전체 원문 {number}"
                ),
            )
        )
    bill_text_item = DocumentWorkItem.create(
        OfficialDocumentKind.BILL_TEXT,
        BILL_TEXT_URL,
        evidence_types=(EvidenceType.BILL_TEXT,),
        related_bill_numbers=("2219564",),
    )
    bill_text_document = _document(
        OfficialDocumentKind.BILL_TEXT,
        BILL_TEXT_URL,
        "f" * 64,
        "의안 원문 " + "나" * 140_000,
    )
    discovery = BillDocumentDiscovery(
        "2219564",
        (bill_text_item, *review_items),
    )
    manifest = DocumentWorkManifest.create(
        (minute_item, bill_text_item, *review_items),
        (discovery,),
    )
    outcomes = (
        _outcome(minute_item, minute_document),
        _outcome(bill_text_item, bill_text_document),
        *(
            _outcome(item, document)
            for item, document in zip(
                review_items,
                review_documents,
                strict=True,
            )
        ),
    )

    meeting = meeting_from_open_assembly_row(
        meeting_row,
        source_hash=MINUTES_HASH,
        source_url=MINUTES_URL,
        retrieved_at=NOW,
    )
    speeches = tuple(
        Speech(
            id=speech_id(meeting.id, number + 1),
            meeting_id=meeting.id,
            sequence=number + 1,
            speaker_id=f"person-{number:03d}",
            speaker_name=("박장관" if number % 2 else "김위원"),
            speaker_role=("장관" if number % 2 else "위원"),
            organization=("법무부" if number % 2 else None),
            text=f"의안번호 2219564 잘리지 않은 전체 발언 {number:03d}",
            agenda="1. 형사소송법 일부개정법률안",
            previous_speech_id=(speech_id(meeting.id, number) if number > 0 else None),
            next_speech_id=(speech_id(meeting.id, number + 2) if number < 104 else None),
            source_locator=f"p.1:{number}-{number + 1}",
            source_hash=MINUTES_HASH,
            parser_version="test-parser-v1",
        )
        for number in range(105)
    )
    relations = (
        SpeechRelation(speeches[0].id, speeches[1].id, "QUESTION_TO", 1.0),
        SpeechRelation(speeches[1].id, speeches[0].id, "ANSWER_TO", 1.0),
    )
    transcript = TranscriptEvidence(
        meeting_id=meeting.id,
        document_url=MINUTES_URL,
        document_source_hash=MINUTES_HASH,
        speeches=speeches,
        relations=relations,
        failures=(),
        page_count=1,
        source_characters=len(minute_document.full_text),
        speech_characters=sum(len(item.text) for item in speeches),
    )
    return _context(
        plan=plan,
        discovery_collection=discovery_collection,
        status_collection=status_collection,
        resolution=resolution,
        manifest=manifest,
        outcomes=tuple(outcomes),
        transcripts=(transcript,),
    )


def _context_with_document_only_topic_candidates(
    *, exact_bill_scope: bool,
) -> FinalizationContext:
    """Add candidates whose topic is visible only after downloading the PDF."""

    base = _large_context()
    plan = (
        base.gateway.research_plan
        if exact_bill_scope
        else plan_research(
            "보완수사권 법안과 전문위원 검토보고서, 소위원회 회의록",
            as_of=NOW,
        )
    )
    visible_bill = dict(base.metadata.discovery.collection.bills[0])
    visible_meeting = dict(base.metadata.discovery.collection.meetings[0])

    hidden_bill = _bill_row(
        "2219000",
        title="정보통신망 이용촉진 및 정보보호 등에 관한 법률 일부개정법률안",
    )
    hidden_bill["summary"] = "인용 조문과 용어를 정비한다."
    # These fields model text that is unavailable to metadata relevance.  The
    # production resolver deliberately does not read them before PDF hydration.
    hidden_bill["_official_bill_pdf_text"] = "보완수사권을 신설하는 의안 원문"
    hidden_bill["_official_review_pdf_text"] = "전문위원은 보완수사권 범위를 지적했다."

    hidden_meeting = _meeting_row(
        [{"bill_no": None, "title": "정보통신망법 일부개정법률안"}]
    )
    hidden_meeting.update(
        {
            "CONF_ID": "document-only-topic-meeting",
            "PDF_LINK_URL": "https://record.assembly.go.kr/minutes/document-only.pdf",
            "_official_minutes_pdf_text": "정부는 보완수사권 쟁점에 답변했다.",
        }
    )
    discovery_collection = _collection(
        bills=(visible_bill, hidden_bill),
        meetings=(visible_meeting, hidden_meeting),
    )
    resolution = resolve_metadata_candidates(plan, discovery_collection)
    assert resolution.bills.accepted_count == 1
    assert resolution.bills.rejected_count == 1
    assert resolution.meetings.accepted_count == 1
    assert resolution.meetings.rejected_count == 1
    return _context(
        plan=plan,
        discovery_collection=discovery_collection,
        status_collection=base.metadata.status_collection,
        resolution=resolution,
        manifest=base.metadata.manifest,
        outcomes=base.outcomes,
        transcripts=base.transcripts,
        identifier=(
            "research-exact-document-scope"
            if exact_bill_scope
            else "research-topical-document-scope"
        ),
    )


def test_preserves_every_document_speech_and_long_bill_text_without_truncation() -> None:
    product = ConnectedResearchFinalizer(build_sha="build-finalizer").finalize(_large_context())

    records = product.snapshot.evidence
    assert sum(item.evidence_type is EvidenceType.AGENDAS for item in records) == 125
    assert (
        sum(
            item.evidence_type in {EvidenceType.SUBCOMMITTEE_MINUTES, EvidenceType.REVIEW_REPORTS}
            for item in records
        )
        == 105
    )
    bill_text = next(
        item for item in records if item.evidence_type is EvidenceType.BILL_TEXT
    )
    assert bill_text.text == "의안 원문 " + "나" * 140_000
    assert bill_text.citation.official_url == (
        "https://likms.assembly.go.kr/bill/billDetail.do?billId=2219564"
    )
    assert "downloadDtlZip.do" not in bill_text.citation.official_url
    assert dict(bill_text.metadata)["bill_no"] == "2219564"
    assert dict(bill_text.metadata)["related_bill_numbers"] == "2219564"
    assert sum(item.evidence_type is EvidenceType.SPEECHES for item in records) == 105
    long_page = next(
        item
        for item in records
        if item.evidence_type is EvidenceType.REVIEW_REPORTS and len(item.text) > 100_000
    )
    assert long_page.text == "전문위원 검토보고서 " + "가" * 120_000
    assert dict(long_page.metadata)["bill_no"] == "2219564"
    assert dict(long_page.metadata)["related_bill_numbers"] == "2219564"
    assert product.snapshot.coverage.complete is True
    assert product.graph.coverage_gaps == ()
    assert (
        sum(node.node_type is EvidenceNodeType.DOCUMENT_PAGE for node in product.graph.nodes) == 106
    )
    assert sum(node.node_type is EvidenceNodeType.BILL_TEXT for node in product.graph.nodes) == 1
    assert sum(node.node_type is EvidenceNodeType.SPEECH for node in product.graph.nodes) == 105


def test_topical_metadata_gate_cannot_claim_unscanned_pdf_candidates_are_complete() -> None:
    context = _context_with_document_only_topic_candidates(exact_bill_scope=False)

    product = ConnectedResearchFinalizer(build_sha="build-finalizer").finalize(context)
    by_type = {item.evidence_type: item for item in product.snapshot.coverage.entries}
    bill_reason = "bill_full_text_candidate_universe_not_scanned:1"
    meeting_reason = "meeting_full_text_candidate_universe_not_scanned:1"

    assert product.snapshot.evidence
    assert product.graph.coverage_gaps == ()
    assert product.snapshot.coverage.complete is False
    for evidence_type in (
        EvidenceType.BILLS,
        EvidenceType.BILL_TEXT,
        EvidenceType.BILL_STATUS,
        EvidenceType.REVIEW_REPORTS,
    ):
        assert bill_reason in by_type[evidence_type].gap_reasons
        assert by_type[evidence_type].candidate_total is None
        assert by_type[evidence_type].complete is False
    for evidence_type in (
        EvidenceType.AGENDAS,
        EvidenceType.SUBCOMMITTEE_MINUTES,
        EvidenceType.SPEECHES,
        EvidenceType.SPEECH_CONTEXT,
        EvidenceType.GOVERNMENT_RESPONSES,
    ):
        assert meeting_reason in by_type[evidence_type].gap_reasons
        assert by_type[evidence_type].candidate_total is None
        assert by_type[evidence_type].complete is False


def test_exact_bill_scope_stays_complete_despite_unrelated_metadata_candidates() -> None:
    context = _context_with_document_only_topic_candidates(exact_bill_scope=True)

    product = ConnectedResearchFinalizer(build_sha="build-finalizer").finalize(context)

    assert product.snapshot.coverage.complete is True
    assert all(
        "candidate_universe_not_scanned" not in reason
        for entry in product.snapshot.coverage.entries
        for reason in entry.gap_reasons
    )


def test_regular_minutes_pages_are_context_not_mislabeled_as_subcommittee() -> None:
    context = _large_context()
    manifest = context.metadata.manifest
    regular_items = tuple(
        replace(
            item,
            evidence_types=tuple(
                value
                for value in item.evidence_types
                if value is not EvidenceType.SUBCOMMITTEE_MINUTES
            ),
        )
        if item.kind is OfficialDocumentKind.MINUTES
        else item
        for item in manifest.items
    )
    regular_manifest = DocumentWorkManifest.create(
        regular_items,
        manifest.bill_discoveries,
        manifest.gaps,
    )
    regular_context = replace(
        context,
        metadata=replace(context.metadata, manifest=regular_manifest),
    )

    records = (
        ConnectedResearchFinalizer(build_sha="build-finalizer")
        .finalize(regular_context)
        .snapshot.evidence
    )
    minutes_page = next(
        item
        for item in records
        if item.citation.official_url == MINUTES_URL
        and item.id.startswith("evidence:document-page:")
    )
    assert minutes_page.evidence_type is EvidenceType.SPEECH_CONTEXT
    assert not any(
        item.evidence_type is EvidenceType.SUBCOMMITTEE_MINUTES
        and item.citation.official_url == MINUTES_URL
        for item in records
    )


def test_reversed_container_inputs_produce_identical_snapshot_and_graph() -> None:
    context = _large_context()
    resolution = context.metadata.discovery.resolution
    reversed_resolution = replace(
        resolution,
        bills=_reverse_resolution(resolution.bills),
        meetings=_reverse_resolution(resolution.meetings),
    )
    reversed_discovery = replace(
        context.metadata.discovery,
        collection=replace(
            context.metadata.discovery.collection,
            bills=tuple(reversed(context.metadata.discovery.collection.bills)),
            meetings=tuple(reversed(context.metadata.discovery.collection.meetings)),
        ),
        resolution=reversed_resolution,
    )
    manifest = context.metadata.manifest
    reversed_manifest = DocumentWorkManifest(
        items=tuple(reversed(manifest.items)),
        bill_discoveries=tuple(reversed(manifest.bill_discoveries)),
        gaps=tuple(reversed(manifest.gaps)),
    )
    reversed_transcripts = tuple(
        replace(
            transcript,
            speeches=tuple(reversed(transcript.speeches)),
            relations=tuple(reversed(transcript.relations)),
        )
        for transcript in reversed(context.transcripts)
    )
    reversed_context = replace(
        context,
        metadata=replace(
            context.metadata,
            discovery=reversed_discovery,
            status_collection=replace(
                context.metadata.status_collection,
                bills=tuple(reversed(context.metadata.status_collection.bills)),
            ),
            manifest=reversed_manifest,
        ),
        outcomes=tuple(reversed(context.outcomes)),
        transcripts=reversed_transcripts,
    )

    finalizer = ConnectedResearchFinalizer(build_sha="build-finalizer")
    original = finalizer.finalize(context)
    reversed_product = finalizer.finalize(reversed_context)

    assert original.snapshot == reversed_product.snapshot
    assert original.graph.to_dict() == reversed_product.graph.to_dict()
    assert original.graph.graph_hash == reversed_product.graph.graph_hash


def _reverse_resolution(value: CandidateSetResolution) -> CandidateSetResolution:
    return replace(
        value,
        decisions=tuple(reversed(value.decisions)),
        accepted=tuple(reversed(value.accepted)),
    )


def test_missing_document_and_ambiguous_title_are_partial_and_unresolved() -> None:
    plan = plan_research("보완수사권 형사소송법", as_of=NOW)
    title = "형사소송법 일부개정법률안"
    meeting_row = _meeting_row([{"bill_no": None, "title": title}])
    discovery_collection = _collection(
        bills=(
            _bill_row("2219564", title=title),
            _bill_row("2219565", title=title),
        ),
        meetings=(meeting_row,),
    )
    resolution = resolve_metadata_candidates(plan, discovery_collection)
    assert resolution.bills.accepted_count == 2
    status_collection = _collection(
        bills=(
            {"BILL_NO": "2219564", "PROC_RESULT": "위원회 심사"},
            {"BILL_NO": "2219565", "PROC_RESULT": "위원회 심사"},
        )
    )
    minute_item = DocumentWorkItem.create(
        OfficialDocumentKind.MINUTES,
        MINUTES_URL,
        evidence_types=(
            EvidenceType.AGENDAS,
            EvidenceType.SUBCOMMITTEE_MINUTES,
            EvidenceType.SPEECHES,
            EvidenceType.SPEECH_CONTEXT,
            EvidenceType.GOVERNMENT_RESPONSES,
        ),
    )
    orphan_review_item = DocumentWorkItem.create(
        OfficialDocumentKind.REVIEW_REPORT,
        REVIEW_URL,
        evidence_types=(EvidenceType.REVIEW_REPORTS,),
    )
    manifest = DocumentWorkManifest.create(
        (minute_item, orphan_review_item),
        (
            BillDocumentDiscovery("2219564", (orphan_review_item,)),
            BillDocumentDiscovery("2219565"),
        ),
    )
    failed_minutes = DocumentOutcome(
        minute_item.work_id,
        DocumentOutcomeStatus.FAILED,
        error_code="damaged_pdf",
        error_message="official PDF could not be parsed",
    )
    review = _document(
        OfficialDocumentKind.REVIEW_REPORT,
        REVIEW_URL,
        "b" * 64,
        "연결 의안번호가 없는 전문위원 검토보고서 전체",
    )
    engine_reason = f"document_failed:{minute_item.work_id}:damaged_pdf"
    context = _context(
        plan=plan,
        discovery_collection=discovery_collection,
        status_collection=status_collection,
        resolution=resolution,
        manifest=manifest,
        outcomes=(failed_minutes, _outcome(orphan_review_item, review)),
        gaps=(
            EngineCoverageGap(
                (
                    EvidenceType.SUBCOMMITTEE_MINUTES,
                    EvidenceType.SPEECHES,
                    EvidenceType.SPEECH_CONTEXT,
                    EvidenceType.GOVERNMENT_RESPONSES,
                ),
                engine_reason,
            ),
        ),
        identifier="research-partial",
    )

    product = ConnectedResearchFinalizer(build_sha="build-finalizer").finalize(context)
    by_type = {item.evidence_type: item for item in product.snapshot.coverage.entries}

    assert product.snapshot.coverage.complete is False
    assert engine_reason in by_type[EvidenceType.SUBCOMMITTEE_MINUTES].gap_reasons
    assert any(
        "agenda_bill_title_ambiguous" in reason
        for reason in by_type[EvidenceType.AGENDAS].gap_reasons
    )
    assert any(
        "orphan_review_report" in reason
        for reason in by_type[EvidenceType.REVIEW_REPORTS].gap_reasons
    )
    assert product.graph.unresolved_edges
    assert {gap.code for gap in product.graph.coverage_gaps} >= {
        "agenda_bill_unresolved",
        "orphan_review_report",
    }


def test_unique_title_match_does_not_fill_missing_official_bill_number() -> None:
    plan = plan_research("형사소송법 법안과 회의 발언", as_of=NOW)
    title = "형사소송법 일부개정법률안"
    meeting_row = _meeting_row([{"bill_no": None, "title": title}])
    discovery_collection = _collection(
        bills=(_bill_row("2219564", title=title),),
        meetings=(meeting_row,),
    )
    resolution = resolve_metadata_candidates(plan, discovery_collection)
    assert resolution.bills.accepted_count == 1
    assert resolution.meetings.accepted_count == 1
    status_collection = _collection(bills=({"BILL_NO": "2219564", "PROC_RESULT": "위원회 심사"},))
    minute_item = DocumentWorkItem.create(
        OfficialDocumentKind.MINUTES,
        MINUTES_URL,
        evidence_types=(
            EvidenceType.AGENDAS,
            EvidenceType.SUBCOMMITTEE_MINUTES,
            EvidenceType.SPEECHES,
            EvidenceType.SPEECH_CONTEXT,
            EvidenceType.GOVERNMENT_RESPONSES,
        ),
    )
    minute_document = _document(
        OfficialDocumentKind.MINUTES,
        MINUTES_URL,
        MINUTES_HASH,
        "공식 회의록 원문",
    )
    manifest = DocumentWorkManifest.create((minute_item,), ())
    meeting = meeting_from_open_assembly_row(
        meeting_row,
        source_hash=MINUTES_HASH,
        source_url=MINUTES_URL,
        retrieved_at=NOW,
    )
    spoken = Speech(
        id=speech_id(meeting.id, 1),
        meeting_id=meeting.id,
        sequence=1,
        speaker_id=None,
        speaker_name="김위원",
        speaker_role="위원",
        organization=None,
        text="제목만으로는 의안번호를 확정할 수 없습니다.",
        agenda=title,
        previous_speech_id=None,
        next_speech_id=None,
        source_locator="p.1:0-10",
        source_hash=MINUTES_HASH,
        parser_version="test-parser-v1",
    )
    transcript = TranscriptEvidence(
        meeting_id=meeting.id,
        document_url=MINUTES_URL,
        document_source_hash=MINUTES_HASH,
        speeches=(spoken,),
        relations=(),
        failures=(),
        page_count=1,
        source_characters=len(minute_document.full_text),
        speech_characters=len(spoken.text),
    )
    context = _context(
        plan=plan,
        discovery_collection=discovery_collection,
        status_collection=status_collection,
        resolution=resolution,
        manifest=manifest,
        outcomes=(_outcome(minute_item, minute_document),),
        transcripts=(transcript,),
        identifier="research-title-only",
    )

    product = ConnectedResearchFinalizer(build_sha="build-finalizer").finalize(context)

    agenda_node = next(
        node for node in product.graph.nodes if node.node_type is EvidenceNodeType.AGENDA
    )
    assert dict(agenda_node.attributes)["bill_no"] is None
    assert not any(
        edge.edge_type.value
        in {
            "agenda_for_bill",
            "addresses_agenda",
            "discusses_bill",
        }
        and edge.source_id in {agenda_node.id, f"speech:{spoken.id}"}
        for edge in product.graph.edges
    )
    assert {gap.code for gap in product.graph.coverage_gaps} >= {
        "agenda_bill_unresolved",
        "speech_agenda_title_inferred",
        "speech_bill_title_inferred",
    }


def test_empty_but_fully_checked_scope_can_be_complete() -> None:
    plan = plan_research("인공지능 입법", as_of=NOW)
    empty = _collection()
    resolution = resolve_metadata_candidates(plan, empty)
    context = _context(
        plan=plan,
        discovery_collection=empty,
        status_collection=empty,
        resolution=resolution,
        manifest=DocumentWorkManifest.create((), ()),
        outcomes=(),
        identifier="research-empty",
    )

    product = ConnectedResearchFinalizer(build_sha="build-finalizer").finalize(context)

    assert product.snapshot.evidence == ()
    assert product.graph.nodes == ()
    assert product.snapshot.coverage.complete is True
    assert all(
        entry.candidate_total == entry.checked_count == entry.matched_count == 0
        for entry in product.snapshot.coverage.entries
    )
