from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from kasm.research.contracts import (
    CoverageLedger,
    EvidenceCoverage,
    EvidenceType,
    ResearchContract,
)
from kasm.research.overview_transport import (
    MAX_OVERVIEW_GROUPS_PER_SHARD,
    OverviewGroupShardDescriptor,
    OverviewTransportBundle,
    build_overview_transport,
    overview_catalog_page,
    overview_catalog_required_shards,
)
from kasm.research.results import EvidenceCitation, EvidenceRecord, ResearchSnapshot

NOW = datetime(2026, 7, 14, tzinfo=UTC)
LONG_SENTINEL = "NEVER_INLINE_THIS_LONG_OFFICIAL_TEXT::"


def _coverage() -> CoverageLedger:
    return CoverageLedger(
        tuple(EvidenceType),
        tuple(
            EvidenceCoverage(
                evidence_type=evidence_type,
                candidate_total=1,
                checked_count=1,
                matched_count=1,
            )
            for evidence_type in EvidenceType
        ),
    )


def _record(
    identifier: str,
    evidence_type: EvidenceType,
    sort_key: str,
    *,
    metadata: tuple[tuple[str, str], ...] = (),
    title: str | None = None,
    text: str = "complete short evidence",
    official_url: str = "https://record.assembly.go.kr/minutes/transport.pdf",
) -> EvidenceRecord:
    import hashlib

    return EvidenceRecord(
        id=identifier,
        evidence_type=evidence_type,
        sort_key=sort_key,
        title=title or identifier,
        text=text,
        citation=EvidenceCitation(
            official_url=official_url,
            source_locator="p.1",
            source_hash=hashlib.sha256(identifier.encode()).hexdigest(),
            retrieved_at=NOW,
        ),
        metadata=metadata,
    )


def _snapshot(records: tuple[EvidenceRecord, ...]) -> ResearchSnapshot:
    contract = ResearchContract(
        query="전체 국회 기록 개요",
        as_of=NOW,
        evidence_types=tuple(EvidenceType),
    )
    return ResearchSnapshot(
        research_id="research-transport",
        contract=contract,
        index_revision="transport-index-v1",
        build_sha="transport-build",
        coverage=_coverage(),
        evidence=records,
    )


def _large_records() -> tuple[EvidenceRecord, ...]:
    documents = tuple(
        _record(
            f"document-record-{number:04d}",
            EvidenceType.BILL_TEXT,
            f"2026-01-01|45|{number:09d}",
            metadata=(("work_id", f"document-{number:04d}"),),
            title=f"의안원문 문서 {number:04d}",
            text=(
                LONG_SENTINEL + "가" * 120_000
                if number == 0
                else f"complete document text {number}"
            ),
            official_url=(
                "https://likms.assembly.go.kr/filegate/servlet/"
                f"FileGate?bookId={number:04d}&type=1"
            ),
        )
        for number in range(1_005)
    )
    bills = tuple(
        _record(
            f"bill-record-{number}",
            EvidenceType.BILLS,
            f"2026-01-{number + 2:02d}|10|bill",
            metadata=(("bill_no", f"22{number:05d}"),),
            title=f"정확한 법안 {number}",
            official_url=(
                "https://likms.assembly.go.kr/bill/billDetail.do?"
                f"billId=PRC_BILL_{number}"
            ),
        )
        for number in range(3)
    )
    meetings = tuple(
        _record(
            f"meeting-record-{number}",
            EvidenceType.AGENDAS,
            f"2026-02-{number + 1:02d}|30|meeting",
            metadata=(("meeting_id", f"meeting-{number}"),),
            title=f"정확한 회의 {number}",
        )
        for number in range(2)
    )
    speeches = tuple(
        _record(
            f"evidence:speech:speech-{number}",
            EvidenceType.SPEECHES,
            f"2026-03-{number + 1:02d}|60|speech",
            title=f"발언 {number}",
        )
        for number in range(5)
    )
    orphan = _record(
        "orphan-record",
        EvidenceType.AGENDAS,
        "undated|orphan",
    )
    return (*documents, *bills, *meetings, *speeches, orphan)


def test_over_thousand_groups_are_sharded_bounded_and_never_dropped() -> None:
    bundle = build_overview_transport(_snapshot(_large_records()))
    manifest = bundle.manifest

    assert manifest.evidence_count == 1_016
    assert manifest.entity_totals.documents == 1_005
    assert manifest.entity_totals.bills == 3
    assert manifest.entity_totals.meetings == 2
    assert manifest.entity_totals.speeches == 5
    assert manifest.entity_totals.catalog_total == 1_010
    assert manifest.unassigned_evidence_count == 1
    assert manifest.undated_evidence_count == 1
    assert len(bundle.shards) == 11
    assert all(
        1 <= len(shard.groups) <= MAX_OVERVIEW_GROUPS_PER_SHARD
        for shard in bundle.shards
    )

    catalog = [
        (group.entity_type.value, group.entity_id)
        for shard in bundle.shards
        for group in shard.groups
    ]
    assert len(catalog) == len(set(catalog)) == 1_010
    assert sum(entity_type == "document" for entity_type, _entity_id in catalog) == 1_005
    assert not any(entity_type == "speech" for entity_type, _entity_id in catalog)

    returned: list[tuple[str, str]] = []
    offset = 0
    while True:
        page = bundle.page(offset=offset, page_size=37)
        assert len(page.groups) <= 37
        returned.extend(
            (group.entity_type.value, group.entity_id) for group in page.groups
        )
        if page.accounting.next_offset is None:
            assert page.accounting.complete is True
            break
        offset = page.accounting.next_offset
    assert returned == catalog


def test_manifest_and_descriptors_never_embed_long_or_member_evidence_text() -> None:
    bundle = build_overview_transport(_snapshot(_large_records()))
    manifest_payload = bundle.manifest.to_dict()
    encoded_manifest = json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True)

    assert LONG_SENTINEL not in encoded_manifest
    assert "evidence_ids" not in encoded_manifest
    assert len(encoded_manifest) < 50_000
    long_route = next(
        route for route in bundle.manifest.core if route.evidence_type is EvidenceType.BILL_TEXT
    )
    assert long_route.text_characters > 100_000
    assert long_route.text_inline_complete is False
    assert "text" not in long_route.to_dict()
    assert long_route.to_dict()["text_delivery"] == "get_evidence_document"
    short_route = next(
        route for route in bundle.manifest.core if route.evidence_type is EvidenceType.BILLS
    )
    assert short_route.text_inline_complete is True
    assert short_route.to_dict()["text"] == "complete short evidence"

    for shard in bundle.shards:
        payload = shard.to_dict()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        assert LONG_SENTINEL not in encoded
        assert "evidence_ids" not in encoded
        assert len(shard.groups) <= 100


def test_thousands_of_gap_details_are_hashed_out_of_the_small_header() -> None:
    detailed_gap_marker = "DETAIL_MUST_STAY_IN_SNAPSHOT::"
    gap_reasons = tuple(
        f"{detailed_gap_marker}{number:04d}" for number in range(1_200)
    )
    entries = tuple(
        EvidenceCoverage(
            evidence_type=evidence_type,
            candidate_total=(None if evidence_type is EvidenceType.BILL_TEXT else 1),
            checked_count=(0 if evidence_type is EvidenceType.BILL_TEXT else 1),
            matched_count=(0 if evidence_type is EvidenceType.BILL_TEXT else 1),
            gap_reasons=(
                gap_reasons if evidence_type is EvidenceType.BILL_TEXT else ()
            ),
        )
        for evidence_type in EvidenceType
    )
    snapshot = replace(
        _snapshot((_large_records()[0],)),
        coverage=CoverageLedger(tuple(EvidenceType), entries),
    )

    manifest = build_overview_transport(snapshot).manifest
    encoded = json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True)

    assert manifest.complete is False
    assert manifest.provisional_reason_count > 1_200
    bill_text = next(
        axis
        for axis in manifest.coverage_axes
        if axis.evidence_type is EvidenceType.BILL_TEXT
    )
    assert bill_text.gap_reason_count == 1_200
    assert detailed_gap_marker not in encoded
    assert len(encoded) < 30_000


def test_group_display_route_uses_only_records_already_exactly_bound_to_group() -> None:
    bill_url = "https://likms.assembly.go.kr/bill/billDetail.do?billId=PRC_EXACT"
    records = (
        _record(
            "status",
            EvidenceType.BILL_STATUS,
            "2026-06-01|20|status",
            metadata=(("bill_no", "2212345"),),
            title="처리상태",
        ),
        _record(
            "bill",
            EvidenceType.BILLS,
            "2026-05-01|10|bill",
            metadata=(("bill_no", "2212345"),),
            title="정확한 법안 표시명",
            official_url=bill_url,
        ),
        _record(
            "misleading-document",
            EvidenceType.BILL_TEXT,
            "2026-04-01|45|document",
            metadata=(("work_id", "work-only"),),
            title="2219999 전혀 다른 번호가 들어간 문서 제목",
            official_url=(
                "https://likms.assembly.go.kr/filegate/servlet/"
                "FileGate?bookId=misleading&type=1"
            ),
        ),
    )

    bundle = build_overview_transport(_snapshot(records))
    groups = {
        (group.entity_type.value, group.entity_id): group
        for shard in bundle.shards
        for group in shard.groups
    }

    bill = groups[("bill", "2212345")]
    assert bill.display_label == "정확한 법안 표시명"
    assert bill.primary_official_url == bill_url
    document = groups[("document", "work-only")]
    assert document.display_label == "2219999 전혀 다른 번호가 들어간 문서 제목"
    assert ("bill", "2219999") not in groups
    assert document.to_dict().get("evidence_ids") is None


def test_shard_identity_detects_changed_descriptor_and_build_is_deterministic() -> None:
    records = _large_records()
    forward = build_overview_transport(_snapshot(records))
    reverse = build_overview_transport(_snapshot(tuple(reversed(records))))

    assert forward.manifest.to_dict() == reverse.manifest.to_dict()
    assert [shard.to_dict() for shard in forward.shards] == [
        shard.to_dict() for shard in reverse.shards
    ]

    first = forward.shards[0]
    changed_group = replace(first.groups[0], display_label="changed")
    changed_shard = replace(first, groups=(changed_group, *first.groups[1:]))
    assert OverviewGroupShardDescriptor.from_shard(changed_shard) != forward.manifest.shards[0]
    with pytest.raises(ValueError, match="changed or misplaced"):
        OverviewTransportBundle(
            forward.manifest,
            (changed_shard, *forward.shards[1:]),
        )


def test_catalog_page_loads_and_verifies_only_overlapping_shards() -> None:
    bundle = build_overview_transport(_snapshot(_large_records()))
    required = overview_catalog_required_shards(
        bundle.manifest,
        offset=95,
        page_size=10,
    )
    selected = tuple(bundle.shards[item.number] for item in required)

    assert [item.number for item in required] == [0, 1]
    page = overview_catalog_page(
        bundle.manifest,
        selected,
        offset=95,
        page_size=10,
    )
    reference = bundle.page(offset=95, page_size=10)
    assert page.to_dict() == reference.to_dict()
    assert page.accounting.returned_count == 10

    with pytest.raises(ValueError, match="required shards are incomplete"):
        overview_catalog_page(
            bundle.manifest,
            selected[:1],
            offset=95,
            page_size=10,
        )


@pytest.mark.parametrize(
    ("inline_limit", "shard_size"),
    ((4_001, 100), (4_000, 0), (4_000, 101)),
)
def test_transport_bounds_are_enforced(inline_limit: int, shard_size: int) -> None:
    with pytest.raises(ValueError):
        build_overview_transport(
            _snapshot((_large_records()[0],)),
            inline_text_characters=inline_limit,
            shard_size=shard_size,
        )
