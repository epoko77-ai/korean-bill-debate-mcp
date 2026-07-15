from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime

import pytest

from kasm.adapters.korea.bills import BILL_DATASET
from kasm.adapters.korea.sources import DATASET_BY_SOURCE, MeetingSource
from kasm.research.collector import (
    CollectionCoverage,
    MetadataCollection,
    MetadataKind,
    PartitionProvenance,
)
from kasm.research.contracts import (
    CoverageLedger,
    EvidenceCoverage,
    EvidenceType,
    ResearchContract,
)
from kasm.research.engine import (
    DiscoveryStageState,
    FamilyFilterAccounting,
    StrictFilterReport,
)
from kasm.research.overview import (
    OverviewEntityType,
    OverviewStatus,
    build_provisional_research_overview,
    build_research_overview,
)
from kasm.research.overview_transport import build_overview_transport
from kasm.research.relevance import RelevanceCriteria
from kasm.research.resolver import (
    CandidateDecision,
    CandidateSetResolution,
    MetadataResolution,
)
from kasm.research.results import EvidenceCitation, EvidenceRecord, ResearchSnapshot

NOW = datetime(2026, 7, 14, tzinfo=UTC)


def _coverage(*, provisional_type: EvidenceType | None = None) -> CoverageLedger:
    entries = []
    for evidence_type in EvidenceType:
        if evidence_type is provisional_type:
            entries.append(
                EvidenceCoverage(
                    evidence_type,
                    candidate_total=None,
                    checked_count=0,
                    matched_count=0,
                    failed_count=0,
                    gap_reasons=("candidate_universe_not_scanned",),
                )
            )
        else:
            entries.append(EvidenceCoverage(evidence_type, 1, 1, 1))
    return CoverageLedger(tuple(EvidenceType), tuple(entries))


def _record(
    identifier: str,
    evidence_type: EvidenceType,
    sort_key: str,
    *metadata: tuple[str, str | int | float | bool | None],
) -> EvidenceRecord:
    return EvidenceRecord(
        id=identifier,
        evidence_type=evidence_type,
        sort_key=sort_key,
        title=identifier,
        text=f"complete text for {identifier}",
        citation=EvidenceCitation(
            official_url="https://record.assembly.go.kr/minutes/overview.pdf",
            source_locator="p.1",
            source_hash=hashlib.sha256(identifier.encode()).hexdigest(),
            retrieved_at=NOW,
        ),
        metadata=metadata,
    )


def _records() -> tuple[EvidenceRecord, ...]:
    return (
        _record(
            "bill",
            EvidenceType.BILLS,
            "2026-01-01|10|bill",
            ("bill_no", "2212345"),
        ),
        _record(
            "status-old",
            EvidenceType.BILL_STATUS,
            "2026-01-02|20|status-old",
            ("bill_no", "2212345"),
        ),
        _record(
            "status-current",
            EvidenceType.BILL_STATUS,
            "2026-07-10|20|status-current",
            ("bill_no", "2212345"),
        ),
        _record(
            "agenda",
            EvidenceType.AGENDAS,
            "2026-02-01|30|agenda",
            ("bill_no", "2212345"),
            ("meeting_id", "meeting-1"),
        ),
        _record(
            "bill-text-page",
            EvidenceType.BILL_TEXT,
            "2026-01-03|45|bill-text-page",
            ("work_id", "document-bill-text"),
            ("related_bill_numbers", "2212345"),
        ),
        _record(
            "review-page",
            EvidenceType.REVIEW_REPORTS,
            "2026-02-04|50|review-page",
            ("work_id", "document-review"),
            ("related_bill_numbers", "2212345"),
        ),
        _record(
            "evidence:speech:speech-1",
            EvidenceType.SPEECHES,
            "2026-02-05|60|speech-1",
            ("meeting_id", "meeting-1"),
        ),
        _record(
            "government-response",
            EvidenceType.GOVERNMENT_RESPONSES,
            "2026-02-06|80|response",
            ("speech_id", "speech-1"),
        ),
        _record(
            "speech-context",
            EvidenceType.SPEECH_CONTEXT,
            "2026-02-07|70|context",
            ("source_speech_id", "speech-1"),
            ("target_speech_id", "speech-2"),
        ),
        _record("orphan", EvidenceType.AGENDAS, "undated|orphan"),
    )


def _snapshot(
    records: tuple[EvidenceRecord, ...] | None = None,
    *,
    provisional_type: EvidenceType | None = None,
) -> ResearchSnapshot:
    contract = ResearchContract(
        query="플랫폼 노동 관련 국회 기록",
        as_of=NOW,
        evidence_types=tuple(EvidenceType),
    )
    return ResearchSnapshot(
        research_id="research-overview",
        contract=contract,
        index_revision="overview-index-v1",
        build_sha="build-overview",
        coverage=_coverage(provisional_type=provisional_type),
        evidence=records or _records(),
    )


def test_complete_inventory_groups_exact_ids_and_drops_no_evidence() -> None:
    snapshot = _snapshot()
    overview = build_research_overview(snapshot, core_limit=5)

    assert overview.status is OverviewStatus.COMPLETE
    assert overview.complete is True
    assert overview.inventory.evidence_count == 10
    assert set(overview.inventory.evidence_ids) == {record.id for record in snapshot.evidence}
    assert overview.inventory.date_from == date(2026, 1, 1)
    assert overview.inventory.date_to == date(2026, 7, 10)
    assert overview.inventory.undated_evidence_ids == ("orphan",)
    assert overview.inventory.unassigned_evidence_ids == ("orphan",)

    bill = overview.inventory.bill_groups[0]
    assert bill.entity_type is OverviewEntityType.BILL
    assert bill.entity_id == "2212345"
    assert set(bill.evidence_ids) == {
        "bill",
        "status-old",
        "status-current",
        "agenda",
        "bill-text-page",
        "review-page",
    }
    meeting = overview.inventory.meeting_groups[0]
    assert set(meeting.evidence_ids) == {"agenda", "evidence:speech:speech-1"}
    assert {group.entity_id for group in overview.inventory.document_groups} == {
        "document-bill-text",
        "document-review",
    }
    speeches = {
        group.entity_id: set(group.evidence_ids)
        for group in overview.inventory.speech_groups
    }
    assert speeches == {
        "speech-1": {
            "evidence:speech:speech-1",
            "government-response",
            "speech-context",
        },
        "speech-2": {"speech-context"},
    }

    assigned = {
        evidence_id
        for groups in (
            overview.inventory.bill_groups,
            overview.inventory.meeting_groups,
            overview.inventory.document_groups,
            overview.inventory.speech_groups,
        )
        for group in groups
        for evidence_id in group.evidence_ids
    }
    assert assigned | set(overview.inventory.unassigned_evidence_ids) == set(
        overview.inventory.evidence_ids
    )


def test_document_title_and_url_never_create_a_guessed_bill_binding() -> None:
    document = EvidenceRecord(
        id="document-page-title-only",
        evidence_type=EvidenceType.BILL_TEXT,
        sort_key="2026-03-01|45|document",
        title="2219999 플랫폼 노동 법안 의안원문",
        text="제안 이유와 주요 내용 전체",
        citation=EvidenceCitation(
            official_url=(
                "https://likms.assembly.go.kr/bill/billDetail.do?"
                "billId=PRC_TITLE_ONLY"
            ),
            source_locator="p.1",
            source_hash="f" * 64,
            retrieved_at=NOW,
        ),
        metadata=(("work_id", "document-title-only"),),
    )

    overview = build_research_overview(_snapshot((document,)))

    assert overview.inventory.bill_groups == ()
    assert overview.inventory.document_groups[0].entity_id == "document-title-only"
    assert overview.inventory.document_groups[0].evidence_ids == (
        "document-page-title-only",
    )


@pytest.mark.parametrize(
    ("query", "bill_numbers"),
    (
        ("의안번호 2219564와 그 회의록을 조사해줘", ("2219564",)),
        ("보완수사권 관련 법안과 회의록을 조사해줘", ()),
    ),
    ids=("exact-bill", "broad-topic"),
)
def test_mixed_agenda_cannot_resurrect_rejected_maritime_bill_in_final_catalog(
    query: str,
    bill_numbers: tuple[str, ...],
) -> None:
    records = (
        _record(
            "evidence:bill:2219564",
            EvidenceType.BILLS,
            "2026-01-01|10|target",
            ("bill_no", "2219564"),
        ),
        _record(
            "evidence:agenda:maritime",
            EvidenceType.AGENDAS,
            "2026-02-01|30|maritime",
            ("bill_no", "2217000"),
            ("meeting_id", "mixed-meeting"),
        ),
        _record(
            "evidence:agenda:target",
            EvidenceType.AGENDAS,
            "2026-02-01|30|target",
            ("bill_no", "2219564"),
            ("meeting_id", "mixed-meeting"),
        ),
        _record(
            "evidence:document-page:mixed",
            EvidenceType.SUBCOMMITTEE_MINUTES,
            "2026-02-01|40|mixed",
            ("work_id", "minutes:mixed"),
            ("meeting_id", "mixed-meeting"),
            ("related_bill_numbers", "2217000,2219564"),
        ),
        _record(
            "evidence:speech:mixed-1",
            EvidenceType.SPEECHES,
            "2026-02-01|60|mixed-1",
            ("meeting_id", "mixed-meeting"),
            ("related_bill_numbers", "2217000,2219564"),
        ),
    )
    contract = ResearchContract(
        query=query,
        as_of=NOW,
        bill_numbers=bill_numbers,
        evidence_types=tuple(EvidenceType),
    )
    snapshot = ResearchSnapshot(
        research_id=f"research-mixed-{'exact' if bill_numbers else 'broad'}",
        contract=contract,
        index_revision="overview-index-v1",
        build_sha="build-overview",
        coverage=_coverage(),
        evidence=records,
    )

    overview = build_research_overview(snapshot, core_limit=10)
    transport = build_overview_transport(snapshot, overview)

    assert [group.entity_id for group in overview.inventory.bill_groups] == [
        "2219564"
    ]
    assert all(
        binding != (OverviewEntityType.BILL, "2217000")
        for item in overview.core
        for binding in item.entity_bindings
    )
    assert "evidence:agenda:maritime" not in {
        item.evidence_id for item in overview.core
    }
    assert "evidence:agenda:target" in {item.evidence_id for item in overview.core}
    assert {
        group.entity_id
        for shard in transport.shards
        for group in shard.groups
        if group.entity_type is OverviewEntityType.BILL
    } == {"2219564"}
    # The official mixed-meeting evidence remains lossless and auditable; only
    # its unaccepted bill mention is denied canonical catalog identity.
    mixed = next(item for item in snapshot.evidence if item.id.endswith(":mixed"))
    assert dict(mixed.metadata)["related_bill_numbers"] == "2217000,2219564"
    assert "evidence:agenda:maritime" in overview.inventory.evidence_ids


def test_bill_mention_without_verified_bill_evidence_stays_uncanonical_and_partial() -> None:
    record = _record(
        "evidence:document-page:unverified",
        EvidenceType.REVIEW_REPORTS,
        "2026-02-01|50|unverified",
        ("work_id", "review:unverified"),
        ("related_bill_numbers", "2217000"),
    )
    overview = build_research_overview(
        _snapshot((record,), provisional_type=EvidenceType.BILLS)
    )

    assert overview.status is OverviewStatus.PROVISIONAL
    assert overview.inventory.bill_groups == ()
    assert overview.inventory.document_groups[0].evidence_ids == (record.id,)
    assert "axis_incomplete:bills" in overview.provisional_reasons
    assert any(
        "candidate_universe_not_scanned" in reason
        for reason in overview.provisional_reasons
    )


def test_core_is_diversified_deterministic_and_explains_every_choice() -> None:
    forward = build_research_overview(_snapshot(), core_limit=5)
    reverse = build_research_overview(_snapshot(tuple(reversed(_records()))), core_limit=5)

    assert forward.to_dict() == reverse.to_dict()
    assert [item.evidence_type for item in forward.core] == [
        EvidenceType.BILLS,
        EvidenceType.BILL_STATUS,
        EvidenceType.BILL_TEXT,
        EvidenceType.REVIEW_REPORTS,
        EvidenceType.AGENDAS,
    ]
    assert forward.core[1].evidence_id == "status-current"
    assert all(item.reasons for item in forward.core)
    assert all("coverage:complete" in item.reasons for item in forward.core)
    assert [item.rank for item in forward.core] == [1, 2, 3, 4, 5]


def test_partial_coverage_is_explicitly_provisional() -> None:
    overview = build_research_overview(
        _snapshot(provisional_type=EvidenceType.SUBCOMMITTEE_MINUTES)
    )

    assert overview.status is OverviewStatus.PROVISIONAL
    assert overview.complete is False
    assert "axis_incomplete:subcommittee_minutes" in overview.provisional_reasons
    assert (
        "axis_gap:subcommittee_minutes:candidate_universe_not_scanned"
        in overview.provisional_reasons
    )


def _metadata_resolution() -> MetadataResolution:
    bill = CandidateDecision(
        MetadataKind.BILL,
        "bill:2212345",
        True,
        20,
        ("exact_bill_number",),
        (),
        {"BILL_NO": "2212345", "BILL_NAME": "플랫폼 종사자 보호법안"},
    )
    rejected_bill = CandidateDecision(
        MetadataKind.BILL,
        "bill:2219999",
        False,
        0,
        (),
        ("no_lexical_match",),
        {"BILL_NO": "2219999", "BILL_NAME": "무관 법안"},
    )
    meeting_url = "https://record.assembly.go.kr/minutes/meeting-1.pdf"
    meeting = CandidateDecision(
        MetadataKind.MEETING,
        f"meeting:{meeting_url}",
        True,
        15,
        ("issue_term:플랫폼 노동",),
        (),
        {"PDF_LINK_URL": meeting_url, "TITLE": "환경노동위원회 회의"},
    )
    return MetadataResolution(
        query="플랫폼 노동",
        source_hash="a" * 64,
        criteria=RelevanceCriteria(query="플랫폼 노동"),
        bills=CandidateSetResolution(
            MetadataKind.BILL,
            (bill, rejected_bill),
            (bill,),
        ),
        meetings=CandidateSetResolution(MetadataKind.MEETING, (meeting,), (meeting,)),
    )


def test_provisional_candidate_inventory_is_complete_paginatable_and_not_a_conclusion() -> None:
    overview = build_provisional_research_overview(_metadata_resolution())

    assert overview.provisional is True
    assert overview.substantive_conclusion_available is False
    assert overview.accepted_total == 2
    assert [entry.candidate_id for entry in overview.entries] == [
        "bill:2212345",
        "meeting:https://record.assembly.go.kr/minutes/meeting-1.pdf",
    ]
    assert dict(overview.entries[0].exact_identifiers) == {"bill_no": "2212345"}
    first = overview.page(offset=0, page_size=1)
    second = overview.page(offset=first.next_offset or 0, page_size=1)
    assert first.complete is False
    assert first.next_offset == 1
    assert second.complete is True
    assert {entry.candidate_id for entry in (*first.entries, *second.entries)} == {
        entry.candidate_id for entry in overview.entries
    }
    assert overview.families[0].total_candidates == 2
    assert overview.families[0].rejected_count == 1
    assert overview.families[0].rejection_reason_counts == (("no_lexical_match", 1),)


def test_discovery_state_adds_source_accounting_and_identity_mismatch_fails_closed() -> None:
    resolution = _metadata_resolution()
    partitions = (
        PartitionProvenance(
            "bills-22",
            MetadataKind.BILL,
            BILL_DATASET,
            (("AGE", 22),),
            2,
            2,
            "b" * 64,
            (),
        ),
        PartitionProvenance(
            "meetings-22",
            MetadataKind.MEETING,
            DATASET_BY_SOURCE[MeetingSource.COMMITTEE],
            (("CONF_DATE", "2026-07"), ("DAE_NUM", 22)),
            1,
            1,
            "c" * 64,
            (),
        ),
    )
    rows = MetadataCollection(
        bills=tuple(dict(item.candidate) for item in resolution.bills.decisions),
        meetings=tuple(dict(item.candidate) for item in resolution.meetings.decisions),
        partitions=partitions,
        coverage=CollectionCoverage(
            partitions_expected=2,
            partitions_complete=2,
            source_rows_expected=3,
            source_rows_fetched=3,
            bill_source_rows=2,
            bill_unique_records=2,
            bill_duplicate_rows=0,
            bill_rejected_rows=0,
            meeting_source_rows=1,
            meeting_unique_pdfs=1,
            meeting_rows_merged=0,
            meeting_rejected_rows=0,
        ),
    )
    state = DiscoveryStageState(
        collection=rows,
        filtered_collection=rows,
        filter_report=StrictFilterReport(
            FamilyFilterAccounting(2, 2),
            FamilyFilterAccounting(1, 1),
        ),
        resolution=resolution,
        status_partitions=(),
        document_bill_numbers=("2212345",),
    )

    overview = build_provisional_research_overview(state)
    assert overview.source.source_complete is True
    assert overview.source.source_rows_fetched == 3
    assert overview.source.bills_after_strict_filter == 2
    assert [item.state.value for item in overview.source.source_availability] == [
        "records_found",
        "records_found",
    ]
    assert [item["source"] for item in overview.source.to_dict()["source_availability"]] == [
        "bill_metadata",
        "committee_minutes",
    ]

    broken = CandidateDecision(
        MetadataKind.BILL,
        "bill:2212345",
        True,
        20,
        ("exact",),
        (),
        {"BILL_NAME": "번호 누락"},
    )
    invalid = MetadataResolution(
        query=resolution.query,
        source_hash=resolution.source_hash,
        criteria=resolution.criteria,
        bills=CandidateSetResolution(MetadataKind.BILL, (broken,), (broken,)),
        meetings=resolution.meetings,
    )
    with pytest.raises(ValueError, match="exact matching bill number"):
        build_provisional_research_overview(invalid)


@pytest.mark.parametrize("limit", (0, 51))
def test_core_limit_is_bounded(limit: int) -> None:
    with pytest.raises(ValueError, match="core_limit"):
        build_research_overview(_snapshot(), core_limit=limit)
