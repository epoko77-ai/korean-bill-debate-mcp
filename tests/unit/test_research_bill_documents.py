from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kasm.adapters.korea.documents import BillDocumentIdentityError, BillDocumentLink
from kasm.research.bill_documents import OfficialBillDocumentDiscoverer
from kasm.research.collector import MetadataKind
from kasm.research.documents import OfficialDocumentKind
from kasm.research.engine import BillDocumentDiscoveryError
from kasm.research.planner import plan_research
from kasm.research.resolver import CandidateDecision


class Client:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def documents(
        self,
        bill_id: str,
        bill_no: str,
        *,
        include_bill_text: bool = True,
        include_review_reports: bool = True,
    ):
        self.calls.append((bill_id, bill_no))
        if self.error:
            raise self.error
        links = []
        if include_bill_text:
            links.append(BillDocumentLink(
                "bill_text",
                "의안원문",
                "pdf",
                (
                    "https://likms.assembly.go.kr/bill/bi/bill/detail/"
                    "downloadDtlZip.do?billId=PRC_2219564&billNo=2219564&"
                    "billKindCd=%EB%B2%95%EB%A5%A0%EC%95%88&dwFileGbn=B"
                ),
            ))
        if include_review_reports:
            links.append(BillDocumentLink(
                "committee_review_report",
                "전문위원 검토보고서",
                "pdf",
                "https://likms.assembly.go.kr/file/review-2219564.pdf",
            ))
        return tuple(links)


def decision(**row: str) -> CandidateDecision:
    return CandidateDecision(
        MetadataKind.BILL,
        "bill:2219564",
        True,
        20,
        ("exact",),
        (),
        {"BILL_NO": "2219564", **row},
    )


def plan():
    return plan_research(
        "2219564 보완수사권 검토보고서",
        as_of=datetime(2026, 7, 13, tzinfo=UTC),
    )


def test_discovers_original_text_and_every_review_pdf_for_exact_bill() -> None:
    client = Client()
    found = OfficialBillDocumentDiscoverer(client).discover_one(
        plan(), decision(BILL_ID="PRC_2219564")
    )

    assert client.calls == [("PRC_2219564", "2219564")]
    assert found.failure_reason is None
    assert len(found.items) == 2
    assert {item.kind for item in found.items} == {
        OfficialDocumentKind.BILL_TEXT,
        OfficialDocumentKind.REVIEW_REPORT,
    }
    assert all(item.related_bill_numbers == ("2219564",) for item in found.items)


def test_extracts_bill_id_only_from_official_https_detail_link() -> None:
    client = Client()
    OfficialBillDocumentDiscoverer(client).discover_one(
        plan(),
        decision(
            DETAIL_LINK=(
                "https://likms.assembly.go.kr/bill/billDetail.do?billId=PRC_FROM_URL"
            )
        ),
    )
    assert client.calls == [("PRC_FROM_URL", "2219564")]


def test_missing_or_untrusted_bill_id_is_an_explicit_gap_not_a_fuzzy_lookup() -> None:
    client = Client()
    found = OfficialBillDocumentDiscoverer(client).discover_one(
        plan(),
        decision(DETAIL_LINK="https://example.com/?billId=PRC_WRONG"),
    )
    assert client.calls == []
    assert found.failure_reason == "official_bill_id_missing"


def test_transient_index_failure_forces_queue_redelivery() -> None:
    discoverer = OfficialBillDocumentDiscoverer(Client(error=TimeoutError("secret body")))
    with pytest.raises(BillDocumentDiscoveryError) as caught:
        discoverer.discover_one(plan(), decision(BILL_ID="PRC_2219564"))
    assert caught.value.retryable is True
    assert caught.value.code == "bill_document_index_unavailable"
    assert "secret body" not in str(caught.value)


def test_unverified_official_identity_is_a_permanent_gap_without_links() -> None:
    discoverer = OfficialBillDocumentDiscoverer(
        Client(error=BillDocumentIdentityError("wrong bill details"))
    )

    result = discoverer.discover_one(plan(), decision(BILL_ID="PRC_2219564"))

    assert result.items == ()
    assert result.failure_reason == "official_bill_identity_unverified"


def test_contract_can_request_bill_text_without_review_reports() -> None:
    client = Client()
    text_only = plan_research(
        "2219564 의안원문",
        as_of=datetime(2026, 7, 13, tzinfo=UTC),
        evidence_types=("bill_text",),
    )

    found = OfficialBillDocumentDiscoverer(client).discover_one(
        text_only,
        decision(BILL_ID="PRC_2219564"),
    )

    assert len(found.items) == 1
    assert found.items[0].kind is OfficialDocumentKind.BILL_TEXT
