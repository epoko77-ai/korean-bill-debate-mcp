from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kasm.research.contracts import EvidenceType
from kasm.research.partitioning import OfficialSourceKind, plan_partitions
from kasm.research.planner import plan_research

AS_OF = datetime(2026, 7, 15, 9, 30, tzinfo=UTC)


def _bill_parameters(query: str) -> list[dict[str, str | int]]:
    plan = plan_research(
        query,
        as_of=AS_OF,
        evidence_types=(EvidenceType.BILLS,),
    )
    partitions = plan_partitions(plan)
    return [
        item.partition.parameters_dict()
        for item in partitions.planned_partitions
        if item.source is OfficialSourceKind.BILL_METADATA
    ]


def test_representative_only_search_uses_official_proposer_prefilter() -> None:
    assert _bill_parameters("제18대 강명순 의원이 대표발의한 법안") == [
        {"AGE": 18, "PROPOSER": "강명순"}
    ]


@pytest.mark.parametrize(
    "query",
    (
        "제18대 김윤 의원이 공동발의한 법안",
        "제18대 김윤 의원이 발의한 법안",
    ),
)
def test_co_or_role_agnostic_search_keeps_complete_term_bill_universe(
    query: str,
) -> None:
    assert _bill_parameters(query) == [{"AGE": 18}]
