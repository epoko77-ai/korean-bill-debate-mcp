from __future__ import annotations

from typing import Any

import pytest

from kasm.adapters.korea.client import ApiPage, ApiResult
from kasm.research.collector import (
    MetadataCollector,
    MetadataKind,
    MetadataPartition,
)


class FakeClient:
    def __init__(self, results: dict[tuple[str, tuple[tuple[str, str | int], ...]], ApiResult]):
        self.results = results
        self.calls: list[
            tuple[str, int, tuple[tuple[str, str | int], ...], bool]
        ] = []

    def fetch_all(
        self,
        dataset: str,
        *,
        page_size: int,
        parameters: dict[str, str | int],
        refresh: bool,
    ) -> ApiResult:
        canonical = tuple(sorted(parameters.items()))
        self.calls.append((dataset, page_size, canonical, refresh))
        return self.results[(dataset, canonical)]


def result(
    dataset: str,
    rows_by_page: list[list[dict[str, Any]]],
    *,
    hash_prefix: str,
) -> ApiResult:
    total = sum(len(rows) for rows in rows_by_page)
    pages = tuple(
        ApiPage(
            dataset=dataset,
            page=index,
            page_size=2,
            total_count=total,
            rows=tuple(rows),
            source_url=f"https://official.test/{dataset}?pIndex={index}&KEY=%2A%2A%2A",
            source_hash=f"{hash_prefix}-page-{index}",
        )
        for index, rows in enumerate(rows_by_page, start=1)
    )
    return ApiResult(
        dataset=dataset,
        page_size=2,
        total_count=total,
        rows=tuple(row for page in rows_by_page for row in page),
        pages=pages,
    )


def partition(
    partition_id: str,
    kind: MetadataKind,
    dataset: str,
    **parameters: str | int,
) -> MetadataPartition:
    return MetadataPartition.create(
        partition_id,
        kind,
        dataset,
        parameters=parameters,
        page_size=2,
    )


def test_collects_every_partition_and_preserves_page_provenance() -> None:
    bills = result(
        "BILLS",
        [
            [
                {"BILL_NO": "2210002", "BILL_NAME": "두 번째 법률안"},
                {"BILL_NO": "2210001", "BILL_NAME": "첫 번째 법률안"},
            ],
            [{"BILL_NO": "2210003", "BILL_NAME": "세 번째 법률안"}],
        ],
        hash_prefix="bills",
    )
    committee = result(
        "COMMITTEE",
        [
            [
                {
                    "PDF_LINK_URL": "https://record.test/minutes-1.pdf",
                    "SUB_NAME": "첫 번째 안건",
                    "BILL_NO": "2210001",
                },
                {
                    "PDF_LINK_URL": "https://record.test/minutes-1.pdf",
                    "SUB_NAME": "두 번째 안건",
                    "BILL_NO": "2210002",
                },
            ]
        ],
        hash_prefix="committee",
    )
    plenary = result(
        "PLENARY",
        [
            [
                {
                    "PDF_LINK_URL": "https://record.test/minutes-2.pdf",
                    "SUB_NAME": "본회의 안건",
                }
            ]
        ],
        hash_prefix="plenary",
    )
    results = {
        ("BILLS", (("AGE", 22),)): bills,
        ("COMMITTEE", (("CONF_DATE", "2026-01"), ("DAE_NUM", 22))): committee,
        ("PLENARY", (("CONF_DATE", "2026-01"), ("DAE_NUM", 22))): plenary,
    }
    client = FakeClient(results)
    collector = MetadataCollector(client)  # type: ignore[arg-type]

    collection = collector.collect(
        [
            partition(
                "meeting:plenary:2026-01",
                MetadataKind.MEETING,
                "PLENARY",
                DAE_NUM=22,
                CONF_DATE="2026-01",
            ),
            partition("bill:all", MetadataKind.BILL, "BILLS", AGE=22),
            partition(
                "meeting:committee:2026-01",
                MetadataKind.MEETING,
                "COMMITTEE",
                DAE_NUM=22,
                CONF_DATE="2026-01",
            ),
        ]
    )

    assert len(client.calls) == 3
    assert all(call[3] is False for call in client.calls)
    assert [row["BILL_NO"] for row in collection.bills] == [
        "2210001",
        "2210002",
        "2210003",
    ]
    assert len(collection.meetings) == 2
    assert collection.meetings[0]["agenda_items"] == [
        {"bill_no": "2210001", "title": "첫 번째 안건"},
        {"bill_no": "2210002", "title": "두 번째 안건"},
    ]
    bill_provenance = next(
        item for item in collection.partitions if item.partition_id == "bill:all"
    )
    assert bill_provenance.expected_rows == 3
    assert bill_provenance.fetched_rows == 3
    assert [page.row_count for page in bill_provenance.pages] == [2, 1]
    assert [page.source_hash for page in bill_provenance.pages] == [
        "bills-page-1",
        "bills-page-2",
    ]
    assert bill_provenance.result_hash == bills.source_hash
    assert collection.coverage.to_dict() == {
        "partitions_expected": 3,
        "partitions_complete": 3,
        "source_rows_expected": 6,
        "source_rows_fetched": 6,
        "bill_source_rows": 3,
        "bill_unique_records": 3,
        "bill_duplicate_rows": 0,
        "bill_rejected_rows": 0,
        "meeting_source_rows": 3,
        "meeting_unique_pdfs": 2,
        "meeting_rows_merged": 1,
        "meeting_rejected_rows": 0,
        "source_complete": True,
        "complete": True,
    }
    assert len(collection.source_hash) == 64


def test_bill_deduplication_requires_an_exact_seven_digit_bill_number() -> None:
    main = result(
        "BILLS",
        [
            [
                {"BILL_NO": "2210001", "BILL_NAME": "인공지능 기본법"},
                {"BILL_NO": "22100010", "BILL_NAME": "잘못된 번호"},
            ]
        ],
        hash_prefix="main",
    )
    status = result(
        "STATUS",
        [
            [
                {
                    "BILL_NO": "2210001",
                    "BILL_NAME": "충돌하는 이름은 덮어쓰지 않음",
                    "PROC_RESULT": "위원회 심사",
                },
                {"BILL_NAME": "번호 없는 법안"},
            ]
        ],
        hash_prefix="status",
    )
    client = FakeClient(
        {
            ("BILLS", (("AGE", 22),)): main,
            ("STATUS", (("AGE", 22),)): status,
        }
    )

    collection = MetadataCollector(client).collect(  # type: ignore[arg-type]
        [
            partition("bill:status", MetadataKind.BILL, "STATUS", AGE=22),
            partition("bill:main", MetadataKind.BILL, "BILLS", AGE=22),
        ],
        refresh=True,
    )

    assert collection.bills == (
        {
            "BILL_NO": "2210001",
            "BILL_NAME": "인공지능 기본법",
            "PROC_RESULT": "위원회 심사",
        },
    )
    assert collection.coverage.bill_source_rows == 4
    assert collection.coverage.bill_unique_records == 1
    assert collection.coverage.bill_duplicate_rows == 1
    assert collection.coverage.bill_rejected_rows == 2
    assert collection.coverage.source_complete
    assert not collection.coverage.complete
    assert all(call[3] is True for call in client.calls)


def test_meeting_rows_without_an_official_pdf_are_reported_not_silently_dropped() -> None:
    meetings = result(
        "MEETINGS",
        [[{"CONF_ID": "missing-pdf"}]],
        hash_prefix="meetings",
    )
    client = FakeClient({("MEETINGS", ()): meetings})

    collection = MetadataCollector(client).collect(  # type: ignore[arg-type]
        [partition("meeting:all", MetadataKind.MEETING, "MEETINGS")]
    )

    assert collection.meetings == ()
    assert collection.coverage.meeting_rejected_rows == 1
    assert collection.coverage.source_complete
    assert not collection.coverage.complete


def test_partition_ids_must_be_unique_before_any_fetch() -> None:
    client = FakeClient({})
    duplicate = partition("same", MetadataKind.BILL, "BILLS", AGE=22)

    with pytest.raises(ValueError, match="ids must be unique"):
        MetadataCollector(client).collect([duplicate, duplicate])  # type: ignore[arg-type]

    assert client.calls == []
