import hashlib
import json
from datetime import UTC, datetime

import pytest

from kasm.research.contracts import (
    DEFAULT_EVIDENCE_TYPES,
    CoverageLedger,
    EvidenceCoverage,
    EvidenceType,
    ResearchContract,
)
from kasm.research.results import (
    PUBLIC_INLINE_TEXT_CHARACTERS,
    EvidenceCitation,
    EvidenceRecord,
    ResearchSnapshot,
    build_snapshot_index,
    snapshot_payload,
)


def _contract() -> ResearchContract:
    return ResearchContract(
        query="최근 AI 입법을 시계열로 알려줘",
        as_of=datetime(2026, 7, 13, tzinfo=UTC),
    )


def _coverage(*, complete: bool = True) -> CoverageLedger:
    return CoverageLedger(
        requested=DEFAULT_EVIDENCE_TYPES,
        entries=tuple(
            EvidenceCoverage(
                evidence_type=item,
                candidate_total=1,
                checked_count=1 if complete else 0,
                matched_count=1 if complete else 0,
                pending_count=0 if complete else 1,
            )
            for item in DEFAULT_EVIDENCE_TYPES
        ),
    )


def _record(number: int, *, text: str | None = None) -> EvidenceRecord:
    return EvidenceRecord(
        id=f"evidence-{number:03d}",
        evidence_type=EvidenceType.SPEECHES,
        sort_key=f"2026-01-{number % 28 + 1:02d}:{number:03d}",
        title=f"발언 {number}",
        text=text or f"전문 {number}",
        citation=EvidenceCitation(
            official_url=f"https://record.assembly.go.kr/minutes/{number}",
            source_locator=f"p.{number}",
            source_hash=f"{number:064x}",
            retrieved_at=datetime(2026, 7, 13, tzinfo=UTC),
        ),
        metadata=(("sequence", number),),
    )


def _snapshot(records: tuple[EvidenceRecord, ...]) -> ResearchSnapshot:
    return ResearchSnapshot(
        research_id="research_test",
        contract=_contract(),
        index_revision="index-2026-07-13",
        build_sha="abcdef1",
        coverage=_coverage(),
        evidence=records,
    )


def test_pages_return_all_237_records_without_gaps_or_reordering() -> None:
    snapshot = _snapshot(tuple(_record(number) for number in range(237)))
    cursor = None
    returned: list[str] = []

    while True:
        page = snapshot.page(cursor=cursor, page_size=37)
        returned.extend(item.id for item in page.evidence)
        cursor = page.page.next_cursor
        if cursor is None:
            assert page.page.complete is True
            break

    expected = [item.id for item in snapshot.evidence]
    assert returned == expected
    assert len(returned) == len(set(returned)) == 237


def test_evidence_text_is_not_silently_truncated() -> None:
    full_text = "가" * 120_000
    page = _snapshot((_record(1, text=full_text),)).page(page_size=1).to_dict()

    assert page["evidence"][0]["text"] == full_text
    assert page["evidence"][0]["text_characters"] == 120_000
    assert page["page"]["complete"] is True


def test_transport_index_omits_long_text_and_requires_lossless_document_read() -> None:
    full_text = "가🙂" * 60_000
    page = _snapshot((_record(1, text=full_text),)).page(page_size=1).to_index_dict()
    item = page["evidence"][0]

    assert "text" not in item
    assert item["text_inline_complete"] is False
    assert item["text_delivery"] == "get_evidence_document"
    assert item["text_characters"] == len(full_text)
    assert item["text_hash"] == hashlib.sha256(full_text.encode()).hexdigest()
    assert page["full_text_required_ids"] == [item["id"]]
    assert page["full_text_required_count"] == 1


def test_transport_index_inlines_only_complete_short_evidence() -> None:
    page = _snapshot((_record(1, text="짧은 전체 원문"),)).page(page_size=1).to_index_dict()

    assert page["evidence"][0]["text"] == "짧은 전체 원문"
    assert page["evidence"][0]["text_inline_complete"] is True
    assert page["full_text_required_ids"] == []


def test_web_safe_twenty_item_page_has_a_bounded_inline_text_ceiling() -> None:
    records = tuple(
        _record(number, text="가" * PUBLIC_INLINE_TEXT_CHARACTERS)
        for number in range(25)
    )
    snapshot = _snapshot(records)

    first = snapshot.page(page_size=20).to_index_dict()
    inline_characters = sum(
        len(str(item.get("text") or "")) for item in first["evidence"]
    )

    assert len(first["evidence"]) == 20
    assert inline_characters == 20 * PUBLIC_INLINE_TEXT_CHARACTERS == 80_000
    assert first["page"]["complete"] is False
    assert first["page"]["next_cursor"]

    second = snapshot.page(
        cursor=first["page"]["next_cursor"],
        page_size=20,
    ).to_index_dict()
    assert len(second["evidence"]) == 5
    assert second["page"]["matched_total"] == 25
    assert second["page"]["complete"] is True


def test_transport_index_size_is_bounded_for_100_large_records() -> None:
    records = tuple(_record(number, text=(f"비밀원문-{number}-" * 12_000)) for number in range(100))

    page = _snapshot(records).page(page_size=100).to_index_dict()
    encoded = json.dumps(page, ensure_ascii=False).encode()

    assert len(page["evidence"]) == 100
    assert page["full_text_required_count"] == 100
    assert all("text" not in item for item in page["evidence"])
    assert len(encoded) < 200_000


def test_snapshot_index_shards_never_copy_long_source_text() -> None:
    secret_text = "의안원문-절대-인덱스에-복사하지-않음" * 20_000
    records = tuple(_record(number, text=secret_text) for number in range(237))

    manifest, shards, lookup_buckets, text_shards = build_snapshot_index(
        _snapshot(records)
    )
    encoded = json.dumps(
        {
            "manifest": repr(manifest),
            "shards": repr(shards),
            "lookup": repr(lookup_buckets),
        },
        ensure_ascii=False,
    )

    assert manifest.evidence_total == 237
    assert manifest.full_text_required_total == 237
    assert manifest.first_full_text_id == shards[0].entries[0].id
    assert tuple(len(shard.entries) for shard in shards) == (100, 100, 37)
    assert all(entry.inline_text is None for shard in shards for entry in shard.entries)
    assert sum(len(bucket.entries) for bucket in lookup_buckets) == 237
    assert sum(len(shard.records) for shard in text_shards) == 237
    assert {record.id for shard in text_shards for record in shard.records} == {
        record.id for record in records
    }
    assert secret_text not in encoded


def test_cursor_is_bound_to_query_revision_and_page_size() -> None:
    snapshot = _snapshot(tuple(_record(number) for number in range(3)))
    cursor = snapshot.page(page_size=1).page.next_cursor
    assert cursor is not None

    with pytest.raises(ValueError, match="page_size"):
        snapshot.page(cursor=cursor, page_size=2)

    other = ResearchSnapshot(
        research_id="research_other",
        contract=ResearchContract(
            query="보완수사권",
            as_of=datetime(2026, 7, 13, tzinfo=UTC),
        ),
        index_revision=snapshot.index_revision,
        build_sha=snapshot.build_sha,
        coverage=_coverage(),
        evidence=snapshot.evidence,
    )
    with pytest.raises(ValueError, match="another research query"):
        other.page(cursor=cursor, page_size=1)


def test_partial_coverage_is_explicit_even_when_page_is_finished() -> None:
    snapshot = ResearchSnapshot(
        research_id="research_partial",
        contract=_contract(),
        index_revision="index-1",
        build_sha="dev",
        coverage=_coverage(complete=False),
        evidence=(_record(1),),
    )

    payload = snapshot.page(page_size=10).to_dict()
    assert payload["page"]["complete"] is True
    assert payload["coverage"]["complete"] is False
    assert payload["complete"] is False


def test_snapshot_metadata_does_not_copy_large_evidence_text() -> None:
    payload = snapshot_payload(_snapshot((_record(1, text="가" * 50_000),)))

    assert payload["evidence_total"] == 1
    assert "evidence" not in payload


def test_citation_rejects_non_official_source() -> None:
    with pytest.raises(ValueError, match="official Assembly"):
        EvidenceCitation(
            official_url="https://example.com/not-official",
            source_locator="p.1",
            source_hash="0" * 64,
            retrieved_at=datetime(2026, 7, 13, tzinfo=UTC),
        )
