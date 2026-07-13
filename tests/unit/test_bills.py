from datetime import UTC, datetime

from kasm.adapters.korea.bills import bill_from_open_assembly_row
from kasm.adapters.korea.ingestion import bills_from_agenda
from kasm.app import LocalServices, create_services
from kasm.core.models import Bill, Meeting, Speech
from kasm.mcp.tools import KasmTools, ServiceContext


def test_open_assembly_bill_mapping_and_status() -> None:
    bill = bill_from_open_assembly_row(
        {
            "BILL_ID": "B1",
            "BILL_NO": "2200001",
            "BILL_NAME": "인공지능법안",
            "AGE": "22",
            "PROPOSER": "홍길동의원 등 10인",
            "COMMITTEE": "과방위",
            "PROPOSE_DT": "2025-03-04",
            "PROC_RESULT": "대안반영폐기",
            "PROC_DT": "20250601",
            "DETAIL_LINK": "https://assembly.example/B1",
        },
        source_hash="fixture",
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert bill.status == "대안반영폐기"
    assert bill.proposed_at.isoformat() == "2025-03-04"  # type: ignore[union-attr]
    assert bill.id == "kna:bill:2200001"


def test_official_minutes_agenda_bootstraps_bill_nodes() -> None:
    meeting = Meeting(
        "m",
        22,
        "c",
        "법제사법위원회",
        None,
        "회의",
        "committee",
        "1",
        datetime(2026, 7, 8, tzinfo=UTC).date(),
        "https://record.assembly.go.kr/m",
        "hash",
        datetime(2026, 7, 8, tzinfo=UTC),
    )
    bills = bills_from_agenda(
        "64. 형사소송법 일부개정법률안(김용민 의원·박은정 의원 대표발의)(의안번호 2219564)",
        meeting,
    )
    assert [(bill.bill_no, bill.name) for bill in bills] == [
        ("2219564", "형사소송법 일부개정법률안")
    ]


def test_bill_tools_work_over_a_synthetic_local_fixture() -> None:
    tools = KasmTools(create_services())
    results = tools.search_bills("인공지능", status="pending")
    assert results["results"][0]["bill_no"] == "2200001"
    status = tools.get_bill_status("2200001")
    assert status["status"] == "계류"
    assert status["is_pending"] is True
    graph = tools.explore_issue("인공지능")
    assert graph["bills"] and graph["speeches"]
    assert graph["discussion_threads"][0]["turns"]
    assert "김미래" in graph["discussion_threads"][0]["participants"]
    assert "MENTIONS" in graph["graph"]["edge_types"]


def test_issue_discovers_bill_through_linked_speech_when_query_does_not_match_title() -> None:
    graph = KasmTools(create_services()).explore_issue("해외 기반 모델 의존")
    assert graph["bills"][0]["bill_no"] == "2200001"
    assert graph["bills"][0]["linked_by"] == "AGENDA_MATCH"


def test_issue_limit_is_core_first_and_complete_cache_map_remains_visible() -> None:
    graph = KasmTools(create_services()).explore_issue("인공지능", limit=1)

    assert len(graph["speeches"]) == 1
    inventory = graph["scope_inventory"]
    assert inventory["speech_candidates"]["complete"] is True
    assert inventory["speech_candidates"]["total"] == 3
    assert len(inventory["speech_candidates"]["items"]) == 3
    assert inventory["selected_for_synthesis"]["selection_limit"] == 1
    assert inventory["selected_for_synthesis"]["speech_selection_complete"] is False
    assert inventory["cache_scope"]["official_source_complete"] is False


def test_issue_core_does_not_backfill_weak_speeches_to_reach_limit() -> None:
    graph = KasmTools(create_services()).explore_issue("해외 기반 모델 의존", limit=3)

    assert [speech["speaker"] for speech in graph["speeches"]] == ["김미래"]
    assert graph["scope_inventory"]["speech_candidates"]["total"] == 2
    assert graph["scope_inventory"]["selected_for_synthesis"]["speech_count"] == 1


def test_broad_issue_keeps_noisy_bill_in_map_but_selects_reviewed_statute() -> None:
    services = create_services()
    local = services.catalog
    assert isinstance(local, LocalServices)
    retrieved_at = datetime(2026, 7, 14, tzinfo=UTC)
    local.bills.save(
        Bill(
            id="noisy-special-prosecutor-bill",
            bill_no="2299001",
            name="검찰 특별수사 폐지 및 지방선거 특별검사법안",
            assembly_term=22,
            proposer="가나다의원",
            committee="법제사법위원회",
            proposed_at=datetime(2026, 7, 13, tzinfo=UTC).date(),
            process_result=None,
            processed_at=None,
            official_url="https://likms.assembly.go.kr/bill/noisy",
            source_hash="fixture-noisy",
            retrieved_at=retrieved_at,
        )
    )
    local.bills.save(
        Bill(
            id="criminal-procedure-bill",
            bill_no="2299002",
            name="형사소송법 일부개정법률안",
            assembly_term=22,
            proposer="라마바의원",
            committee="법제사법위원회",
            proposed_at=datetime(2026, 7, 12, tzinfo=UTC).date(),
            process_result=None,
            processed_at=None,
            official_url="https://likms.assembly.go.kr/bill/criminal-procedure",
            source_hash="fixture-correct",
            retrieved_at=retrieved_at,
        )
    )

    graph = KasmTools(services).explore_issue(
        "2026년 7월 검찰 보완수사권 폐지 관련 법안과 의원 의견",
        limit=10,
    )

    assert [bill["bill_no"] for bill in graph["bills"]] == ["2299002"]
    candidates = graph["scope_inventory"]["bill_candidates"]["items"]
    assert {candidate["bill_no"] for candidate in candidates} == {
        "2299001",
        "2299002",
    }
    by_number = {candidate["bill_no"]: candidate for candidate in candidates}
    assert by_number["2299002"]["selection_relevance"][
        "selected_for_synthesis"
    ] is True
    assert by_number["2299001"]["selection_relevance"][
        "selected_for_synthesis"
    ] is False


def test_bill_status_returns_every_linked_speech_without_a_hidden_top_twenty() -> None:
    services = create_services()
    local = services.catalog
    assert isinstance(local, LocalServices)
    meeting = local.meetings.get("kna:22:committee:2025-03-18:sample-001")
    assert meeting is not None
    extra = [
        Speech(
            id=f"{meeting.id}:extra-{sequence:04d}",
            meeting_id=meeting.id,
            sequence=sequence,
            speaker_id=None,
            speaker_name=f"추가발언자{sequence}",
            speaker_role="국회의원",
            organization=None,
            text=f"인공지능 법안 추가 발언 {sequence}",
            agenda="인공지능",
            previous_speech_id=None,
            next_speech_id=None,
            source_locator=f"synthetic:extra-{sequence}",
            source_hash="synthetic-demo-v1",
            parser_version="demo-1",
        )
        for sequence in range(4, 29)
    ]
    local.speeches.save_many(extra)
    local.database.connection.executemany(
        """INSERT INTO speech_bill_links
           (speech_id, bill_id, relation_type, confidence, evidence)
           VALUES (?, 'synthetic-bill-ai-001', 'EXPLICIT_MENTION', 1.0, 'test')""",
        [(speech.id,) for speech in extra],
    )
    local.database.connection.commit()

    status = KasmTools(services).get_bill_status("2200001")

    assert status["related_speeches_count"] == 26
    assert status["related_speeches_complete"] is True
    assert len(status["related_speeches"]) == 26


def test_explicit_bill_number_can_never_be_replaced_by_a_fuzzy_bill() -> None:
    class ConfusedCatalog:
        def explore_issue(self, query: str, limit: int):
            del query, limit
            return {
                "bills": [
                    {"id": "wrong", "bill_no": "2299999", "name": "전혀 다른 법안"}
                ],
                "speeches": [
                    {"speech_id": "wrong-speech", "text": "엉뚱한 법안 발언"}
                ],
                "discussion_threads": [
                    {
                        "meeting_id": "wrong-meeting",
                        "matched_speech_ids": ["wrong-speech"],
                    }
                ],
                "links": [{"bill_id": "wrong", "speech_id": "wrong-speech"}],
                "timeline": [
                    {
                        "event_type": "bill_proposed",
                        "bill_no": "2299999",
                        "title": "전혀 다른 법안",
                    }
                ],
            }

        def get_bill_status(self, bill_no: str):
            assert bill_no == "2219564"
            return {
                "id": "kna:bill:2219564",
                "bill_no": "2219564",
                "name": "형사소송법 일부개정법률안",
                "status": "위원회 심사",
                "documents": [
                    {
                        "title": "전문위원 검토보고서",
                        "official_url": "https://likms.assembly.go.kr/review.pdf",
                    }
                ],
            }

    catalog = ConfusedCatalog()
    graph = KasmTools(
        ServiceContext(search=catalog, repository=catalog, catalog=catalog)  # type: ignore[arg-type]
    ).explore_issue("의안번호 2219564 보완수사권 쟁점")

    assert [bill["bill_no"] for bill in graph["bills"]] == ["2219564"]
    assert graph["bills"][0]["name"] == "형사소송법 일부개정법률안"
    assert graph["links"] == []
    assert graph["speeches"] == []
    assert graph["discussion_threads"] == []
    assert graph["timeline"] == []
    assert graph["bill_number_validation"]["requested"] == ["2219564"]
    assert graph["bill_number_validation"]["matched"] == ["2219564"]
    assert graph["bill_number_validation"]["exact_match"] is True
    assert graph["bill_number_validation"]["linked_speech_count"] == 0
