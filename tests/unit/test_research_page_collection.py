from __future__ import annotations

from typing import Any

import pytest

from kasm.adapters.korea.client import ApiPage, AssemblyApiError
from kasm.research.collector import MetadataKind, MetadataPartition
from kasm.research.page_collection import (
    MetadataPageWork,
    assemble_partition_pages,
    expand_first_page,
    page_artifact_payload,
    page_from_artifact,
    validate_fetched_page,
)


def _partition() -> MetadataPartition:
    return MetadataPartition.create(
        "bill-ai",
        MetadataKind.BILL,
        "ALLBILL",
        parameters={"AGE": 22, "BILL_NAME": "인공지능"},
        page_size=100,
    )


def _page(number: int, total: int = 237, *, duplicate: bool = False) -> ApiPage:
    start = (number - 1) * 100
    count = min(100, max(0, total - start))
    rows: list[dict[str, Any]] = [
        {"BILL_NO": f"{index:07d}", "TITLE": f"법안 {index}"}
        for index in range(start, start + count)
    ]
    if duplicate and rows:
        rows[-1] = dict(rows[0])
    return ApiPage(
        dataset="ALLBILL",
        page=number,
        page_size=100,
        total_count=total,
        rows=tuple(rows),
        source_url=(
            "https://open.assembly.go.kr/portal/openapi/ALLBILL?"
            f"KEY=%2A%2A%2A&pIndex={number}&pSize=100"
        ),
        source_hash=f"{number:064x}",
    )


def test_first_page_fans_out_every_remaining_page_without_fetching_them() -> None:
    expansion = expand_first_page(_partition(), _page(1))

    assert expansion.official_total == 237
    assert expansion.total_pages == 3
    assert expansion.pages == (
        MetadataPageWork("bill-ai", 2, 237),
        MetadataPageWork("bill-ai", 3, 237),
    )


def test_reassembles_all_rows_without_top_n_or_page_loss() -> None:
    result = assemble_partition_pages(_partition(), (_page(3), _page(1), _page(2)))

    assert result.total_count == 237
    assert len(result.rows) == 237
    assert tuple(page.page for page in result.pages) == (1, 2, 3)


def test_incomplete_distributed_pages_fail_instead_of_returning_partial_data() -> None:
    with pytest.raises(AssemblyApiError, match=r"missing=\[2\]"):
        assemble_partition_pages(_partition(), (_page(1), _page(3)))


def test_total_drift_fails_but_official_duplicate_rows_are_preserved() -> None:
    with pytest.raises(AssemblyApiError, match="total changed"):
        validate_fetched_page(
            _partition(),
            MetadataPageWork("bill-ai", 2, 237),
            _page(2, total=238),
        )
    duplicate = _page(1, duplicate=True)
    validate_fetched_page(
        _partition(),
        MetadataPageWork("bill-ai", 1),
        duplicate,
    )
    assert duplicate.rows[0] == duplicate.rows[-1]


def test_repeated_complete_page_fails_as_pagination_drift() -> None:
    first = _page(1, total=200)
    repeated = ApiPage(
        dataset=first.dataset,
        page=2,
        page_size=first.page_size,
        total_count=first.total_count,
        rows=first.rows,
        source_url=first.source_url.replace("pIndex=1", "pIndex=2"),
        source_hash="f" * 64,
    )

    with pytest.raises(AssemblyApiError, match="repeated complete page"):
        assemble_partition_pages(_partition(), (first, repeated))


def test_page_artifact_round_trip_is_lossless_and_credential_free() -> None:
    page = _page(1)
    payload = page_artifact_payload(page)

    assert page_from_artifact(payload) == page
    assert "api-key" not in repr(payload)
    assert payload["source_url"].count("%2A") == 3


def test_unredacted_or_nonofficial_provenance_is_rejected() -> None:
    page = _page(1)
    leaked = ApiPage(
        page.dataset,
        page.page,
        page.page_size,
        page.total_count,
        page.rows,
        "https://open.assembly.go.kr/portal/openapi/ALLBILL?KEY=secret",
        page.source_hash,
    )
    with pytest.raises(ValueError, match="unredacted"):
        page_artifact_payload(leaked)

    foreign = ApiPage(
        page.dataset,
        page.page,
        page.page_size,
        page.total_count,
        page.rows,
        "https://example.com/data?KEY=%2A%2A%2A",
        page.source_hash,
    )
    with pytest.raises(ValueError, match="official"):
        page_artifact_payload(foreign)
