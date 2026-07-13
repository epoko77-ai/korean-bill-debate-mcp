from datetime import UTC, datetime

from kasm.adapters.korea.bills import bill_from_open_assembly_row
from kasm.adapters.korea.ingestion import bills_from_agenda
from kasm.app import create_services
from kasm.core.models import Meeting
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


def test_explicit_bill_number_can_never_be_replaced_by_a_fuzzy_bill() -> None:
    class ConfusedCatalog:
        def explore_issue(self, query: str, limit: int):
            del query, limit
            return {
                "bills": [
                    {"id": "wrong", "bill_no": "2299999", "name": "전혀 다른 법안"}
                ],
                "speeches": [],
                "discussion_threads": [],
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
    assert graph["timeline"] == []
    assert graph["bill_number_validation"] == {
        "requested": ["2219564"],
        "matched": ["2219564"],
        "exact_match": True,
    }
