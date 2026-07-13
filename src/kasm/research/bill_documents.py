"""Exact, one-bill-at-a-time discovery of official bill documents."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping
from typing import Any

from kasm.adapters.korea.documents import (
    BillDocumentIdentityError,
    BillDocumentsClient,
)

from .contracts import EvidenceType
from .documents import OfficialDocumentKind
from .engine import (
    BillDocumentDiscovery,
    BillDocumentDiscoveryError,
    DocumentWorkItem,
)
from .planner import ResearchPlan
from .resolver import CandidateDecision

_BILL_NUMBER = re.compile(r"\d{7}")
_BILL_ID = re.compile(r"[A-Za-z0-9_]+")
_DETAIL_HOST = "likms.assembly.go.kr"


class OfficialBillDocumentDiscoverer:
    """Adapt the official bill-detail index to the durable research engine.

    One invocation checks exactly one already-resolved bill.  It never performs
    fuzzy lookup and it never substitutes a different bill when the detail link
    is incomplete.  An empty official index is a successful check with zero
    documents; inability to identify or reach that index is an explicit gap.
    """

    def __init__(self, client: BillDocumentsClient) -> None:
        self.client = client

    def discover_one(
        self,
        plan: ResearchPlan,
        bill: CandidateDecision,
    ) -> BillDocumentDiscovery:
        row = bill.candidate
        bill_number = _required_bill_number(row)
        bill_id = _external_bill_id(row)
        if bill_id is None:
            return BillDocumentDiscovery(
                bill_number,
                failure_reason="official_bill_id_missing",
            )
        requested = set(plan.contract.evidence_types)
        try:
            links = self.client.documents(
                bill_id,
                bill_number,
                include_bill_text=EvidenceType.BILL_TEXT in requested,
                include_review_reports=EvidenceType.REVIEW_REPORTS in requested,
            )
        except BillDocumentIdentityError:
            # The index endpoint is billId-driven and does not echo billNo.
            # Never retry or attach its links when the official detail page
            # failed to prove the exact identifier pair.
            return BillDocumentDiscovery(
                bill_number,
                failure_reason="official_bill_identity_unverified",
            )
        except (OSError, RuntimeError, TimeoutError):
            # The queue must redeliver rather than recording a false empty index.
            raise BillDocumentDiscoveryError(
                "official bill-document index is temporarily unavailable",
                code="bill_document_index_unavailable",
                retryable=True,
            ) from None

        items: list[DocumentWorkItem] = []
        for link in links:
            if link.file_format.casefold() != "pdf":
                continue
            if (
                link.document_type == "bill_text"
                and EvidenceType.BILL_TEXT in requested
            ):
                items.append(
                    DocumentWorkItem.create(
                        OfficialDocumentKind.BILL_TEXT,
                        link.official_url,
                        evidence_types=(EvidenceType.BILL_TEXT,),
                        related_bill_numbers=(bill_number,),
                    )
                )
            elif (
                link.document_type == "committee_review_report"
                and EvidenceType.REVIEW_REPORTS in requested
            ):
                items.append(
                    DocumentWorkItem.create(
                        OfficialDocumentKind.REVIEW_REPORT,
                        link.official_url,
                        evidence_types=(EvidenceType.REVIEW_REPORTS,),
                        related_bill_numbers=(bill_number,),
                    )
                )
        return BillDocumentDiscovery(bill_number, tuple(items))


def _required_bill_number(row: Mapping[str, Any]) -> str:
    value = str(row.get("BILL_NO", row.get("bill_no", ""))).strip()
    if not _BILL_NUMBER.fullmatch(value):
        raise ValueError("resolved bill candidate lacks an exact seven-digit bill number")
    return value


def _external_bill_id(row: Mapping[str, Any]) -> str | None:
    direct = str(row.get("BILL_ID", row.get("bill_id", ""))).strip()
    if direct:
        return direct if _BILL_ID.fullmatch(direct) else None
    for field in ("DETAIL_LINK", "LINK_URL", "official_url"):
        raw = str(row.get(field) or "").strip()
        if not raw:
            continue
        try:
            parsed = urllib.parse.urlsplit(raw)
        except ValueError:
            continue
        if parsed.scheme != "https" or parsed.hostname != _DETAIL_HOST:
            continue
        candidate = urllib.parse.parse_qs(parsed.query).get("billId", [None])[0]
        if candidate and _BILL_ID.fullmatch(candidate):
            return candidate
    return None


__all__ = ["OfficialBillDocumentDiscoverer"]
