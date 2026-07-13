from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from kasm.research.contracts import (
    DEFAULT_EVIDENCE_TYPES,
    CoverageLedger,
    EvidenceCoverage,
    EvidencePage,
    EvidenceType,
    ResearchContract,
    StableCursor,
)


def contract() -> ResearchContract:
    return ResearchContract(
        query="2026년부터 보완수사권 법안·회의록·검토보고서·발언을 연결해줘",
        as_of=datetime(2026, 7, 13, 12, tzinfo=UTC),
        date_from=date(2026, 1, 1),
        date_to=date(2026, 7, 13),
        bill_numbers=("2219564",),
    )


def test_research_contract_defaults_to_every_promised_evidence_axis() -> None:
    value = contract()

    assert value.evidence_types == DEFAULT_EVIDENCE_TYPES
    assert value.canonical_payload()["evidence_types"] == [
        item.value for item in DEFAULT_EVIDENCE_TYPES
    ]


def test_research_contract_rejects_an_invalid_scope() -> None:
    with pytest.raises(ValueError, match="date_from"):
        ResearchContract(
            query="기간 오류",
            as_of=datetime(2026, 7, 13, tzinfo=UTC),
            date_from=date(2026, 7, 2),
            date_to=date(2026, 7, 1),
        )


def test_coverage_is_not_complete_when_any_requested_evidence_is_unknown_or_failed() -> None:
    ledger = CoverageLedger(
        requested=(EvidenceType.BILLS, EvidenceType.REVIEW_REPORTS),
        entries=(
            EvidenceCoverage(EvidenceType.BILLS, 12, 12, 4),
            EvidenceCoverage(
                EvidenceType.REVIEW_REPORTS,
                4,
                3,
                2,
                failed_count=1,
                gap_reasons=("one official PDF timed out",),
            ),
        ),
    )

    assert ledger.complete is False
    assert ledger.to_dict()["evidence"]["review_reports"]["complete"] is False


def test_coverage_requires_an_entry_for_every_requested_axis() -> None:
    with pytest.raises(ValueError, match="missing requested evidence"):
        CoverageLedger(
            requested=(EvidenceType.BILLS, EvidenceType.SPEECHES),
            entries=(EvidenceCoverage(EvidenceType.BILLS, 1, 1, 1),),
        )


def test_stable_cursor_round_trips_and_detects_tampering() -> None:
    value = contract()
    cursor = StableCursor(
        query_fingerprint=value.fingerprint("index-2026-07-13"),
        index_revision="index-2026-07-13",
        sort_key="2026-07-09|2219875",
        item_id="kna:bill:2219875",
        page_size=25,
    )

    encoded = cursor.encode()

    assert StableCursor.decode(encoded) == cursor
    replacement = "A" if encoded[-1] != "A" else "B"
    with pytest.raises(ValueError, match="invalid research cursor"):
        StableCursor.decode(encoded[:-1] + replacement)


def test_incomplete_evidence_page_must_expose_a_continuation_cursor() -> None:
    with pytest.raises(ValueError, match="requires next_cursor"):
        EvidencePage(matched_total=237, returned_count=100, returned_through=100)

    page = EvidencePage(
        matched_total=237,
        returned_count=100,
        returned_through=100,
        next_cursor="stable-cursor",
    )
    assert page.complete is False


def test_complete_evidence_page_cannot_claim_another_cursor() -> None:
    page = EvidencePage(matched_total=12, returned_count=12, returned_through=12)

    assert page.to_dict()["complete"] is True
    with pytest.raises(ValueError, match="must not include next_cursor"):
        EvidencePage(
            matched_total=12,
            returned_count=12,
            returned_through=12,
            next_cursor="unexpected",
        )
