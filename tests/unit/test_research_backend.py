from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from typing import Any

import pytest

from kasm.research.backend import DurableResearchBackend
from kasm.research.collector import MetadataKind
from kasm.research.contracts import CoverageLedger, EvidenceCoverage, EvidenceType, ResearchContract
from kasm.research.documents import (
    FilesystemOfficialDocumentStore,
    OfficialDocumentKind,
    ParsedOfficialDocument,
    RawOfficialDocument,
    TextSegment,
)
from kasm.research.engine import DerivedResearchStatus, GatewayReceipt
from kasm.research.jobs import JobStatus
from kasm.research.overview import (
    ProvisionalCandidateEntry,
    ProvisionalFamilyAccounting,
    ProvisionalResearchOverview,
    ProvisionalSourceAccounting,
)
from kasm.research.overview_transport import build_overview_transport
from kasm.research.results import (
    EvidenceCitation,
    EvidenceIndexEntry,
    EvidenceRecord,
    ResearchSnapshot,
    ResearchSnapshotSummary,
)
from kasm.research.status_storage import BoundedResearchStatusView

NOW = datetime(2026, 7, 13, tzinfo=UTC)


class Runs:
    def __init__(self, snapshot: ResearchSnapshot | None = None, outcomes=()) -> None:
        self.snapshot = snapshot
        self.outcomes = outcomes

    def get_snapshot(self, research_id: str):
        assert research_id == "research_1"
        return self.snapshot

    def get_snapshot_summary(self, research_id: str):
        assert research_id == "research_1"
        return (
            ResearchSnapshotSummary.from_snapshot(self.snapshot)
            if self.snapshot is not None
            else None
        )

    def get_result_page(
        self,
        research_id: str,
        *,
        cursor: str | None = None,
        page_size: int = 20,
    ):
        assert research_id == "research_1"
        return (
            self.snapshot.page(cursor=cursor, page_size=page_size).to_index_dict()
            if self.snapshot is not None
            else None
        )

    def get_overview_page(
        self,
        research_id: str,
        *,
        offset: int = 0,
        page_size: int = 20,
    ):
        assert research_id == "research_1"
        if self.snapshot is None:
            return None
        bundle = build_overview_transport(self.snapshot)
        payload = bundle.manifest.to_dict()
        payload["catalog"] = bundle.page(offset=offset, page_size=page_size).to_dict()
        payload["core_full_text_required_ids"] = [
            route.evidence_id for route in bundle.manifest.core if not route.text_inline_complete
        ]
        return payload

    def get_discovery(self, research_id: str):
        assert research_id == "research_1"
        return None

    def get_evidence_index_entry(self, research_id: str, evidence_id: str):
        assert research_id == "research_1"
        if self.snapshot is None:
            return None
        for evidence in self.snapshot.evidence:
            if evidence.id == evidence_id:
                return EvidenceIndexEntry.from_record(evidence)
        raise LookupError(evidence_id)

    def get_overflow_evidence_record(self, research_id: str, evidence_id: str):
        assert research_id == "research_1"
        if self.snapshot is None:
            return None
        for evidence in self.snapshot.evidence:
            if evidence.id == evidence_id:
                return evidence
        raise LookupError(evidence_id)

    def get_next_full_text_evidence_id(self, research_id: str, after_evidence_id: str):
        assert research_id == "research_1"
        if self.snapshot is None:
            return None
        found = False
        for evidence in self.snapshot.evidence:
            if found and len(evidence.text) > 4_000:
                return evidence.id
            if evidence.id == after_evidence_id:
                found = True
        if not found:
            raise LookupError(after_evidence_id)
        return None

    def get_next_core_evidence_id(self, research_id: str, after_evidence_id: str):
        return self.get_next_full_text_evidence_id(research_id, after_evidence_id)

    def document_outcomes(self, research_id: str):
        assert research_id == "research_1"
        return self.outcomes


class Jobs:
    def get(self, research_id: str):
        assert research_id == "research_1"
        return None


class Engine:
    def __init__(self, snapshot: ResearchSnapshot | None = None, outcomes=()) -> None:
        self.runs = Runs(snapshot, outcomes)
        self.jobs = Jobs()
        self.calls: list[dict[str, Any]] = []

    def gateway(self, query: str, **values: Any):
        self.calls.append({"query": query, **values})
        return GatewayReceipt(
            "research_1",
            JobStatus.QUEUED,
            "queued",
            "a" * 64,
            "index-v1",
            {"original_query": query},
            1,
        )

    def derive_status(self, research_id: str):
        assert research_id == "research_1"
        return DerivedResearchStatus(
            research_id,
            "metadata_discovery",
            1,
            0,
            1,
            0,
            0,
            0,
            0,
            0,
            0,
            self.runs.snapshot is not None,
            self.runs.snapshot is not None,
            bool(self.runs.snapshot and self.runs.snapshot.coverage.complete),
        )


def snapshot(text: str = "근거") -> ResearchSnapshot:
    contract = ResearchContract(
        "AI 입법",
        NOW,
        evidence_types=(EvidenceType.REVIEW_REPORTS,),
    )
    coverage = CoverageLedger(
        contract.evidence_types,
        (EvidenceCoverage(EvidenceType.REVIEW_REPORTS, 1, 1, 1),),
    )
    evidence = EvidenceRecord(
        "evidence_1",
        EvidenceType.REVIEW_REPORTS,
        "2026-01-01:1",
        "전문위원 검토보고서",
        text,
        EvidenceCitation(
            "https://likms.assembly.go.kr/file/review.pdf",
            "p.1:0-2",
            "b" * 64,
            NOW,
        ),
    )
    return ResearchSnapshot("research_1", contract, "index-v1", "build", coverage, (evidence,))


def backend(tmp_path, engine: Engine, key: str | None = "user-key"):
    return DurableResearchBackend(  # type: ignore[arg-type]
        engine,
        FilesystemOfficialDocumentStore(tmp_path),
        assembly_api_key_provider=lambda: key,
    )


def test_start_reads_user_key_but_never_returns_it_and_preserves_structured_scope(tmp_path) -> None:
    engine = Engine()
    result = backend(tmp_path, engine).start_research(
        "최근 AI 입법",
        assembly_term=21,
        committees=("법제사법위원회",),
        date_from="2025-01-01",
        date_to="2025-06-30",
    )

    assert result["research_id"] == "research_1"
    assert "user-key" not in repr(result)
    assert engine.calls[0]["assembly_api_key"] == "user-key"
    assert engine.calls[0]["date_from"] == date(2025, 1, 1)
    assert engine.calls[0]["committees"] == ("법제사법위원회",)


def test_start_preserves_missing_scope_as_none_for_natural_language_planner(tmp_path) -> None:
    engine = Engine()

    backend(tmp_path, engine).start_research("제21대 법사위의 플랫폼 노동 관련 논의를 조사해줘")

    assert engine.calls[0]["assembly_term"] is None
    assert engine.calls[0]["committees"] is None


def test_start_requires_request_scoped_assembly_key(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="인증키"):
        backend(tmp_path, Engine(), key=None).start_research("AI 입법")


def test_status_uses_bounded_store_view_without_exhaustive_derivation(tmp_path) -> None:
    class BoundedRuns(Runs):
        def get_status_view(self, research_id: str) -> BoundedResearchStatusView:
            assert research_id == "research_1"
            return BoundedResearchStatusView(
                DerivedResearchStatus(
                    research_id,
                    "documents",
                    4,
                    4,
                    7,
                    7,
                    2,
                    2,
                    3,
                    0,
                    0,
                    True,
                    False,
                    False,
                ),
                None,
            )

        def get_snapshot_summary(self, research_id: str):
            raise AssertionError("bounded status must reuse its summary result")

    class BoundedEngine(Engine):
        def __init__(self) -> None:
            super().__init__()
            self.runs = BoundedRuns()

        def derive_status(self, research_id: str):
            raise AssertionError("bounded status must not scan run artifacts")

    research = backend(tmp_path, BoundedEngine())
    result = research.get_research_status("research_1")

    assert result["status"] == "running"
    assert result["stage"] == "documents"
    assert result["work"]["metadata_pages_complete"] == 7
    assert result["work"]["documents_complete"] == 0
    assert result["work"]["snapshot_ready"] is False
    assert result["retry_after_seconds"] == 1
    assert result["terminal"] is False
    assert result["provisional"] is True
    assert result["source_complete"] is False
    assert result["pending_total"] is None
    assert result["pending_total_known"] is False
    assert result["coverage"]["state"] == "pending"

    # Early/optimistic tool calls must report the same compact checkpoint.
    # Falling back to exhaustive derive_status here would rescan every hosted
    # metadata page merely to construct a not-ready error.
    with pytest.raises(RuntimeError, match="현재 단계: documents"):
        research.get_research_overview("research_1")
    with pytest.raises(RuntimeError, match="현재 단계: documents"):
        research.get_research_page("research_1")
    with pytest.raises(RuntimeError, match="현재 단계: documents"):
        research.get_evidence_document("research_1", "missing")


def test_metadata_overview_uses_compact_complete_accounting(tmp_path) -> None:
    overview = ProvisionalResearchOverview(
        "AI 입법",
        "a" * 64,
        (
            ProvisionalCandidateEntry(
                0,
                MetadataKind.BILL,
                "bill:2219564",
                12,
                ("issue_term:인공지능",),
                (("bill_no", "2219564"),),
                "인공지능 기본법 일부개정법률안",
            ),
        ),
        (
            ProvisionalFamilyAccounting(
                MetadataKind.BILL,
                3,
                1,
                2,
                (("no_relevance_signal", 2),),
            ),
            ProvisionalFamilyAccounting(MetadataKind.MEETING, 4, 0, 4, (("low_score", 4),)),
        ),
        ProvisionalSourceAccounting(True, 7, 7, 3, 3, 4, 4),
    )

    class CompactRuns(Runs):
        def get_provisional_overview(self, research_id: str):
            assert research_id == "research_1"
            return overview

        def get_discovery(self, research_id: str):
            raise AssertionError("compact overview must not load discovery")

    class CompactEngine(Engine):
        def __init__(self) -> None:
            super().__init__()
            self.runs = CompactRuns()

    result = backend(tmp_path, CompactEngine()).get_research_overview("research_1", page_size=20)

    assert result["accepted_total"] == 1
    assert result["rejected_total"] == 6
    assert result["pending_total"] is None
    assert result["pending_total_known"] is False
    assert result["source_complete"] is False
    assert result["metadata_inventory_complete"] is True
    assert result["family_accounting_scope"] == "complete_metadata_discovery"
    assert result["source"]["scope"] == "metadata_discovery_partitions"
    assert result["source"]["scope_complete"] is True
    assert result["coverage"]["complete"] is False
    assert result["substantive_conclusion_available"] is False
    assert result["catalog"]["complete"] is True
    assert result["catalog"]["inventory_complete"] is True
    assert result["catalog_scope"] == "accepted_metadata_inventory_page"
    assert "status" in result["catalog_completion_meaning"]
    assert "next_action" not in result


def test_first_page_preview_is_explicitly_observed_and_incomplete(tmp_path) -> None:
    overview = ProvisionalResearchOverview(
        "AI 입법",
        "b" * 64,
        tuple(
            ProvisionalCandidateEntry(
                index,
                MetadataKind.BILL,
                f"bill:{2219500 + index:07d}",
                12,
                ("issue_term:인공지능",),
                (("bill_no", f"{2219500 + index:07d}"),),
                f"인공지능 기본법 제{index}호 일부개정법률안",
            )
            for index in range(25)
        ),
        (
            ProvisionalFamilyAccounting(MetadataKind.BILL, 1000, 25, 975, (("low_score", 975),)),
            ProvisionalFamilyAccounting(MetadataKind.MEETING, 3, 0, 3, (("low_score", 3),)),
        ),
        ProvisionalSourceAccounting(False, 18_293, 1_003, 1_000, 1_000, 3, 3),
    )

    class PreviewRuns(Runs):
        def get_provisional_overview(self, research_id: str):
            assert research_id == "research_1"
            return overview

    class PreviewEngine(Engine):
        def __init__(self) -> None:
            super().__init__()
            self.runs = PreviewRuns()

    result = backend(tmp_path, PreviewEngine()).get_research_overview("research_1")

    assert result["metadata_stage"] == "first_page_preview"
    assert result["accepted_total_scope"] == "observed_first_pages"
    assert result["family_accounting_scope"] == "observed_first_pages"
    assert result["metadata_inventory_complete"] is False
    assert result["source_complete"] is False
    assert result["pending_total"] is None
    assert result["pending_total_known"] is False
    assert result["catalog"]["complete"] is True
    assert result["catalog"]["inventory_complete"] is False
    assert result["catalog"]["returned_count"] == 20
    assert result["catalog"]["truncated"] is True
    assert result["catalog_scope"] == "observed_first_pages_core_orientation"
    assert result["catalog"]["next_offset"] is None
    assert result["catalog"]["observed_accepted_total"] == 25
    assert "metadata_source_prefix_incomplete" in result["warning_codes"]

    with pytest.raises(RuntimeError, match="추가 페이지를 제공하지"):
        backend(tmp_path, PreviewEngine()).get_research_overview("research_1", offset=20)


def test_complete_metadata_pagination_remains_pinned_when_final_appears(tmp_path) -> None:
    entries = tuple(
        ProvisionalCandidateEntry(
            index,
            MetadataKind.BILL,
            f"bill:{2219500 + index:07d}",
            12,
            ("issue_term:인공지능",),
            (("bill_no", f"{2219500 + index:07d}"),),
            f"인공지능 기본법 제{index}호 일부개정법률안",
        )
        for index in range(25)
    )
    overview = ProvisionalResearchOverview(
        "AI 입법",
        "c" * 64,
        entries,
        (
            ProvisionalFamilyAccounting(MetadataKind.BILL, 25, 25, 0, ()),
            ProvisionalFamilyAccounting(MetadataKind.MEETING, 0, 0, 0, ()),
        ),
        ProvisionalSourceAccounting(True, 25, 25, 25, 25, 0, 0),
    )

    class RacingRuns(Runs):
        def get_provisional_overview(self, research_id: str):
            assert research_id == "research_1"
            return overview

    class RacingEngine(Engine):
        def __init__(self) -> None:
            super().__init__()
            self.runs = RacingRuns()

    engine = RacingEngine()
    research = backend(tmp_path, engine)
    first = research.get_research_overview("research_1", page_size=20)
    assert first["phase"] == "metadata"
    assert first["catalog"]["next_offset"] == 20

    engine.runs.snapshot = snapshot()
    pinned = research.get_research_overview(
        "research_1",
        offset=20,
        page_size=20,
        view_source_hash=first["source_hash"],
    )
    assert pinned["phase"] == "metadata"
    assert pinned["source_hash"] == first["source_hash"]
    assert pinned["catalog"]["returned_count"] == 5
    assert pinned["catalog"]["complete"] is True

    latest = research.get_research_overview("research_1", page_size=20)
    assert latest["phase"] == "final"

    with pytest.raises(RuntimeError, match="후보 지도 버전을 찾을 수 없습니다"):
        research.get_research_overview("research_1", view_source_hash="d" * 64)


def test_final_overview_and_status_share_complete_partial_truth_table(tmp_path) -> None:
    complete_snapshot = snapshot()
    partial_coverage = CoverageLedger(
        complete_snapshot.contract.evidence_types,
        (
            EvidenceCoverage(
                EvidenceType.REVIEW_REPORTS,
                1,
                0,
                0,
                failed_count=1,
                gap_reasons=("document_failed:review:parse_error",),
            ),
        ),
    )
    partial_snapshot = ResearchSnapshot(
        complete_snapshot.research_id,
        complete_snapshot.contract,
        complete_snapshot.index_revision,
        complete_snapshot.build_sha,
        partial_coverage,
        (),
    )

    for value, expected_complete in (
        (complete_snapshot, True),
        (partial_snapshot, False),
    ):
        research = backend(tmp_path, Engine(value))
        status = research.get_research_status("research_1")
        overview = research.get_research_overview("research_1")

        for payload in (status, overview):
            assert payload["terminal"] is True
            assert payload["provisional"] is (not expected_complete)
            assert payload["source_complete"] is expected_complete
            assert payload["pending_total"] == 0
            assert payload["pending_total_known"] is True
            assert payload["coverage"]["complete"] is expected_complete
            assert payload["coverage"]["state"] == ("complete" if expected_complete else "partial")


def test_compact_overview_readiness_none_never_falls_back_to_discovery(
    tmp_path,
) -> None:
    class PartialCompactRuns(Runs):
        def get_provisional_overview(self, research_id: str):
            assert research_id == "research_1"
            return None

        def get_discovery(self, research_id: str):
            raise AssertionError("readiness must not be bypassed")

    class PartialCompactEngine(Engine):
        def __init__(self) -> None:
            super().__init__()
            self.runs = PartialCompactRuns()

    with pytest.raises(RuntimeError, match="metadata_discovery"):
        backend(tmp_path, PartialCompactEngine()).get_research_overview("research_1")


def test_pages_index_large_evidence_without_sending_a_false_preview(tmp_path) -> None:
    text = "가" * 130_000
    result = backend(tmp_path, Engine(snapshot(text))).get_research_page("research_1", page_size=20)
    item = result["evidence"][0]
    assert "text" not in item
    assert item["text_inline_complete"] is False
    assert item["text_delivery"] == "get_evidence_document"
    assert item["text_characters"] == len(text)
    assert item["text_hash"] == hashlib.sha256(text.encode()).hexdigest()
    assert result["full_text_required_ids"] == ["evidence_1"]
    assert result["page"]["complete"] is True


def test_evidence_record_document_pages_reconstruct_exact_unicode_text(tmp_path) -> None:
    text = ("한글🙂abc" * 20_000) + "끝"
    research = backend(tmp_path, Engine(snapshot(text)))
    cursor = None
    chunks: list[str] = []
    expected_character_start = 0
    expected_byte_start = 0

    while True:
        page = research.get_evidence_document(
            "research_1",
            "evidence_1",
            cursor=cursor,
            max_characters=17_777,
        )
        returned = page["returned_range"]
        assert returned["character_start"] == expected_character_start
        assert returned["byte_start"] == expected_byte_start
        assert returned["characters"] == len(page["text"])
        assert returned["bytes"] == len(page["text"].encode())
        assert page["total_characters"] == len(text)
        assert page["total_bytes"] == len(text.encode())
        assert page["total_segments"] == 1
        assert all("text" not in segment for segment in page["segments"])
        chunks.append(page["text"])
        expected_character_start = returned["character_end"]
        expected_byte_start = returned["byte_end"]
        cursor = page["next_cursor"]
        if page["complete"]:
            assert cursor is None
            break
        assert cursor

    assert "".join(chunks) == text
    assert expected_character_start == len(text)
    assert expected_byte_start == len(text.encode())


def test_document_lookup_returns_the_exact_indexed_source_page_without_pdf_duplication(
    tmp_path,
) -> None:
    value = snapshot()
    store = FilesystemOfficialDocumentStore(tmp_path)
    raw = RawOfficialDocument(
        OfficialDocumentKind.REVIEW_REPORT,
        "https://likms.assembly.go.kr/file/review.pdf",
        "application/pdf",
        b"%PDF-full",
        NOW,
    )
    store.put_raw(raw)
    first_segment = "검" * 80_000
    second_segment = "🙂답" * 35_000
    parsed = ParsedOfficialDocument(
        OfficialDocumentKind.REVIEW_REPORT,
        raw.official_url,
        raw.source_hash,
        "parser-v1",
        NOW,
        (TextSegment("p.1", first_segment), TextSegment("p.2", second_segment)),
    )
    store.put_parsed(parsed)
    # Evidence must cite the exact preserved source hash.
    cited = value.evidence[0]
    rebound = ResearchSnapshot(
        value.research_id,
        value.contract,
        value.index_revision,
        value.build_sha,
        value.coverage,
        (
            EvidenceRecord(
                cited.id,
                cited.evidence_type,
                cited.sort_key,
                cited.title,
                first_segment,
                EvidenceCitation(
                    cited.citation.official_url,
                    "p.1",
                    raw.source_hash,
                    NOW,
                ),
                (
                    ("document_kind", OfficialDocumentKind.REVIEW_REPORT.value),
                    ("parser_version", "parser-v1"),
                ),
            ),
        ),
    )
    result_object = type(
        "Result",
        (),
        {
            "source_hash": raw.source_hash,
            "document": parsed,
        },
    )()
    outcome = type("Outcome", (), {"result": result_object})()
    engine = Engine(rebound, (outcome,))
    research = DurableResearchBackend(  # type: ignore[arg-type]
        engine,
        store,
        assembly_api_key_provider=lambda: "key",
    )

    cursor = None
    chunks: list[str] = []
    locators: set[str] = set()
    while True:
        result = research.get_evidence_document(
            "research_1",
            "evidence_1",
            cursor=cursor,
            max_characters=23_000,
        )
        chunks.append(result["text"])
        locators.update(segment["locator"] for segment in result["segments"])
        assert result["text_characters"] == len(first_segment)
        assert result["total_segments"] == 1
        assert all("text" not in segment for segment in result["segments"])
        cursor = result["next_cursor"]
        if result["complete"]:
            break

    assert "".join(chunks) == first_segment
    assert locators == {"p.1"}

    with pytest.raises(LookupError):
        research.get_evidence_document("research_1", "from_another_research")


def test_document_cursor_detects_tampering_and_is_bound_to_scope_and_chunk_size(
    tmp_path,
) -> None:
    base = snapshot("가" * 120_000)
    first = base.evidence[0]
    second = EvidenceRecord(
        "evidence_2",
        first.evidence_type,
        "2026-01-01:2",
        "두 번째 근거",
        "나" * 120_000,
        first.citation,
    )
    multi = ResearchSnapshot(
        base.research_id,
        base.contract,
        base.index_revision,
        base.build_sha,
        base.coverage,
        (first, second),
    )
    research = backend(tmp_path, Engine(multi))
    first_page = research.get_evidence_document("research_1", "evidence_1", max_characters=10_000)
    cursor = first_page["next_cursor"]
    assert isinstance(cursor, str)

    middle = len(cursor) // 2
    replacement = "A" if cursor[middle] != "A" else "B"
    tampered = cursor[:middle] + replacement + cursor[middle + 1 :]
    with pytest.raises(ValueError, match="invalid evidence document cursor"):
        research.get_evidence_document(
            "research_1",
            "evidence_1",
            cursor=tampered,
            max_characters=10_000,
        )

    with pytest.raises(ValueError, match="another evidence document"):
        research.get_evidence_document(
            "research_1",
            "evidence_2",
            cursor=cursor,
            max_characters=10_000,
        )

    with pytest.raises(ValueError, match="must match the cursor"):
        research.get_evidence_document(
            "research_1",
            "evidence_1",
            cursor=cursor,
            max_characters=10_001,
        )
