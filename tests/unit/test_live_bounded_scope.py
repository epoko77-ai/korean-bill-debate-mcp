from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from kasm.adapters.korea.bills import BILL_DATASET, BILL_STATUS_DATASET
from kasm.adapters.korea.client import ApiPage
from kasm.adapters.korea.sources import DATASET_BY_SOURCE, MeetingSource
from kasm.live import (
    LiveAssemblyServices,
    _bill_queries,
    _filter_bills_by_proposal_scope,
    _filter_meeting_rows_by_scope,
    _meeting_date_queries,
    _proposal_date_scope,
)
from kasm.storage.database import Database

QUERY = (
    "2026년 발의된 인공지능 관련 법안 중 중요도가 높은 법안을 5개 정도 "
    "정리하고, 이에 대한 소위원회, 상임위원회 논의 내용을 정리해줘."
)
INCIDENT_QUERY = (
    "최근 본회의를 통과한 닥터나우 금지법과 관련하여, 소위원회, 상임위원회, "
    "본회의에서 의원들의 주요 논의 내용을 정리해줘"
)


class RecordingClient:
    api_key = "fixture-key"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str | int]]] = []

    def fetch_page(
        self,
        dataset: str,
        *,
        page: int = 1,
        page_size: int = 100,
        parameters: dict[str, str | int] | None = None,
        refresh: bool = False,
    ) -> ApiPage:
        del refresh
        values = dict(parameters or {})
        self.calls.append((dataset, values))
        if dataset == BILL_DATASET:
            rows = (
                {
                    "BILL_ID": "PRC_2025",
                    "BILL_NO": "2210001",
                    "BILL_NAME": "인공지능 과거 법안",
                    "AGE": "22",
                    "PROPOSE_DT": "2025-11-01",
                },
                {
                    "BILL_ID": "PRC_2026",
                    "BILL_NO": "2210002",
                    "BILL_NAME": "AI 산업 진흥법안",
                    "AGE": "22",
                    "PROPOSE_DT": "2026-01-08",
                },
            )
        elif dataset == BILL_STATUS_DATASET:
            rows = (
                {
                    "BILL_ID": "PRC_2026",
                    "BILL_NO": "2210002",
                    "BILL_NAME": "AI 산업 진흥법안",
                    "AGE": "22",
                    "PROPOSE_DT": "2026-01-08",
                    "PROC_RESULT": "위원회 심사",
                },
            )
        else:
            rows = ()
        return ApiPage(
            dataset,
            page,
            page_size,
            len(rows),
            rows,
            "https://open.assembly.go.kr/portal/openapi/fixture",
            dataset,
        )


class IncidentClient:
    api_key = "fixture-key"

    def __init__(self, *, include_alternative_status: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, str | int]]] = []
        self.include_alternative_status = include_alternative_status

    def fetch_page(
        self,
        dataset: str,
        *,
        page: int = 1,
        page_size: int = 100,
        parameters: dict[str, str | int] | None = None,
        refresh: bool = False,
    ) -> ApiPage:
        del refresh
        values = dict(parameters or {})
        self.calls.append((dataset, values))
        rows: tuple[dict[str, Any], ...]
        if dataset == BILL_DATASET and values.get("BILL_NO") == "2205513":
            rows = (
                {
                    "BILL_ID": "PRC_DOCTOR_NOW_SOURCE",
                    "BILL_NO": "2205513",
                    "BILL_NAME": "약사법 일부개정법률안",
                    "AGE": "22",
                    "PROPOSER": "김윤의원 등 11인",
                    "COMMITTEE": "보건복지위원회",
                    "PROPOSE_DT": "2024-11-13",
                    "PROC_RESULT": "대안반영폐기",
                    "PROC_DT": "2026-08-20",
                    "CMT_PROC_DT": "2025-11-20",
                },
            )
        elif (
            dataset == BILL_STATUS_DATASET
            and values.get("BILL_NO") == "2214609"
            and self.include_alternative_status
        ):
            rows = (
                {
                    "BILL_ID": "PRC_DOCTOR_NOW_ALTERNATIVE",
                    "BILL_NO": "2214609",
                    "BILL_NAME": "약사법 일부개정법률안(대안)",
                    "AGE": "22",
                    "PROPOSER": "보건복지위원장",
                    "COMMITTEE": "보건복지위원회",
                    "PROPOSE_DT": "2025-11-26",
                    "PROC_RESULT": "수정가결",
                    "PROC_DT": "2026-08-20",
                    "DETAIL_LINK": (
                        "https://likms.assembly.go.kr/bill/billDetail.do?"
                        "billId=PRC_DOCTOR_NOW_ALTERNATIVE"
                    ),
                },
            )
        elif dataset in {BILL_DATASET, BILL_STATUS_DATASET}:
            rows = ()
        elif dataset == DATASET_BY_SOURCE[MeetingSource.COMMITTEE]:
            assert values == {
                "DAE_NUM": 22,
                "CONF_DATE": "2025",
                "SUB_NAME": "2205513",
                "COMM_NAME": "보건복지위원회",
            }
            rows = (
                _incident_meeting_row(
                    "2025-11-19",
                    "보건복지위원회 법안심사제1소위원회",
                    "2205513",
                    "subcommittee",
                ),
                _incident_meeting_row(
                    "2025-11-20",
                    "보건복지위원회 전체회의",
                    "2205513",
                    "committee",
                ),
                _incident_meeting_row(
                    "2025-11-20",
                    "무관한 전체회의",
                    "2299999",
                    "unrelated",
                ),
            )
        elif dataset == DATASET_BY_SOURCE[MeetingSource.PLENARY]:
            assert values == {
                "DAE_NUM": 22,
                "CONF_DATE": "2026",
                "SUB_NAME": "2214609",
            }
            rows = (
                _incident_meeting_row(
                    "2026-08-20",
                    "제2차 본회의",
                    "2214609",
                    "plenary",
                ),
            )
        else:
            raise AssertionError((dataset, values))
        return ApiPage(
            dataset,
            page,
            page_size,
            len(rows),
            rows,
            "https://open.assembly.go.kr/portal/openapi/fixture",
            dataset,
        )


def _incident_meeting_row(
    meeting_date: str,
    title: str,
    bill_no: str,
    suffix: str,
) -> dict[str, Any]:
    return {
        "DAE_NUM": "22",
        "CONF_DATE": meeting_date,
        "COMM_NAME": title,
        "TITLE": title,
        "SUB_NAME": f"약사법 일부개정법률안 {bill_no}",
        "BILL_NO": bill_no,
        "CONF_ID": f"incident-{suffix}",
        "PDF_LINK_URL": f"https://record.assembly.go.kr/{suffix}.pdf",
    }


def test_exact_question_uses_only_three_topic_bill_queries() -> None:
    assert _bill_queries(QUERY) == ["인공지능", "AI", "인공지능 기본법"]


def test_exact_question_has_hard_proposal_scope_and_one_year_meeting_query() -> None:
    assert _proposal_date_scope(QUERY) == (
        date(2026, 1, 1),
        date(2026, 12, 31),
    )
    assert _meeting_date_queries(
        [f"2026-{month:02d}" for month in range(1, 13)]
    ) == ["2026"]
    assert _meeting_date_queries(["2026-01", "2026-02", "2026-03"]) == [
        "2026-01",
        "2026-02",
        "2026-03",
    ]
    assert _meeting_date_queries(
        [f"2026-{month:02d}" for month in range(1, 8)],
        as_of=date(2026, 7, 18),
    ) == ["2026"]


def test_meeting_rows_are_hard_filtered_to_effective_scope() -> None:
    rows = [
        {"CONF_DATE": "2025-12-31"},
        {"CONF_DT": "2026-01-01"},
        {"CONF_DATE": "2026-07-18"},
        {"CONF_DT": "2026-07-19"},
        {"CONF_DATE": "2026-12-01"},
        {"TITLE": "missing date"},
    ]

    assert _filter_meeting_rows_by_scope(
        rows,
        {
            "requested_date_from": "2026-01-01",
            "requested_date_to": "2026-07-18",
        },
        [f"2026-{month:02d}" for month in range(1, 8)],
    ) == rows[1:3]


def test_proposal_scope_rejects_2025_and_missing_proposal_dates() -> None:
    bills = [
        {"bill_no": "2210001", "proposed_at": "2025-11-01"},
        {"bill_no": "2210002", "proposed_at": "2026-01-08"},
        {"bill_no": "2210003", "RGS_PROC_DT": "2026-03-09"},
    ]

    assert [
        bill["bill_no"]
        for bill in _filter_bills_by_proposal_scope(
            bills,
            (date(2026, 1, 1), date(2026, 12, 31)),
        )
    ] == ["2210002"]


def test_live_metadata_calls_are_bounded_to_three_bill_and_three_meeting_queries(
    tmp_path,
) -> None:
    database = Database(tmp_path / "metadata-calls.sqlite3")
    database.initialize()
    client = RecordingClient()
    service = LiveAssemblyServices(
        database,
        client=client,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 18, tzinfo=UTC),
    )

    bills = service._refresh_bills(
        query=QUERY,
        assembly_term=22,
        include_documents=False,
    )
    bill_calls = [
        parameters
        for dataset, parameters in client.calls
        if dataset == BILL_DATASET
    ]

    assert [bill["BILL_NO"] for bill in bills] == ["2210002"]
    assert [call["BILL_NAME"] for call in bill_calls] == [
        "인공지능",
        "AI",
        "인공지능 기본법",
    ]

    client.calls.clear()
    service._refresh_meetings(
        query=QUERY,
        committee=None,
        months=[f"2026-{month:02d}" for month in range(1, 13)],
        assembly_term=22,
        ingest_minutes=False,
    )
    meeting_calls = [
        (dataset, parameters)
        for dataset, parameters in client.calls
        if dataset
        in {
            DATASET_BY_SOURCE[MeetingSource.COMMITTEE],
            DATASET_BY_SOURCE[MeetingSource.PLENARY],
            DATASET_BY_SOURCE[MeetingSource.SUBCOMMITTEE],
        }
    ]

    assert len(meeting_calls) == 3
    assert {
        parameters.get("CONF_DATE")
        for _dataset, parameters in meeting_calls
        if "CONF_DATE" in parameters
    } == {"2026"}


def test_incident_alias_uses_two_exact_meeting_calls_and_three_stage_minutes(
    tmp_path,
) -> None:
    database = Database(tmp_path / "incident-targeted.sqlite3")
    database.initialize()
    client = IncidentClient()
    service = LiveAssemblyServices(
        database,
        client=client,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        max_minutes_per_request=2,
        now=lambda: datetime(2026, 8, 23, tzinfo=UTC),
    )
    synced: list[dict[str, Any]] = []

    def sync_incident(row: dict[str, Any]):
        synced.append(dict(row))
        bill_no = str(row["BILL_NO"])
        return service.pipeline.ingestor.ingest(
            row,
            (
                f"1. 약사법 일부개정법률안 (김윤의원 대표발의) "
                f"(의안번호 {bill_no})\n"
                "○김윤 위원  닥터나우 비대면진료 플랫폼의 의약품 도매상 운영과 "
                "리베이트 위험을 어떻게 막을 것입니까?\n"
                "○보건복지부장관 정은경  의약품 유통 공공성과 이해충돌을 막는 "
                "규정이 필요합니다.\n"
                "2. 다른 약사법 일부개정법률안 (의안번호 2299999)\n"
                "○무관 위원  닥터나우 비대면진료와 의약품 도매상 리베이트를 "
                "언급하지만 다른 의안입니다."
            ),
            source_hash=f"fixture-{bill_no}",
            source_url=str(row["PDF_LINK_URL"]),
        )

    service.pipeline.sync = sync_incident  # type: ignore[method-assign]

    result = service.explore_issue(INCIDENT_QUERY, limit=20)

    meeting_calls = [
        (dataset, parameters)
        for dataset, parameters in client.calls
        if dataset
        in {
            DATASET_BY_SOURCE[MeetingSource.COMMITTEE],
            DATASET_BY_SOURCE[MeetingSource.PLENARY],
            DATASET_BY_SOURCE[MeetingSource.SUBCOMMITTEE],
        }
    ]
    assert len(meeting_calls) == 2
    assert all("SUB_NAME" in parameters for _dataset, parameters in meeting_calls)
    assert not any(
        dataset == DATASET_BY_SOURCE[MeetingSource.SUBCOMMITTEE]
        for dataset, _parameters in meeting_calls
    )
    assert service.last_refresh["months_queried"] == ["2025-11", "2026-08"]
    assert service.last_refresh["meeting_candidates"] == 3
    assert service.last_refresh["bounded_core_candidates"] == 3
    assert service.last_refresh["has_more"] is False
    assert len(synced) == 3
    assert {
        row["BILL_NO"] for row in synced
    } == {"2205513", "2214609"}
    assert result["target_resolution"]["primary_vehicle_bill_no"] == "2214609"
    assert result["target_resolution"]["live_verified_bill_numbers"] == [
        "2205513",
        "2214609",
    ]
    assert result["target_resolution"]["confidence"] == (
        "official_bill_and_agenda_identifiers_matched"
    )
    assert set(result["stage_coverage"]["stages"]) == {
        "subcommittee",
        "standing_committee",
        "plenary",
    }
    assert {
        stage["state"] for stage in result["stage_coverage"]["stages"].values()
    } == {"discussion_found"}
    assert result["research_pagination"]["completion_scope"] == (
        "bounded_targeted_core"
    )
    assert result["research_pagination"]["complete"] is True
    assert result["quality"]["score"] == 100
    assert result["quality"]["evidence_sufficient"] is True
    assert all(
        speech["attribution"]["state"]
        in {"exact_bill_number_in_turn_or_agenda", "exact_speech_bill_link"}
        for speech in result["speeches"]
    )
    assert {speech["speaker"] for speech in result["speeches"]} == {"김윤", "정은경"}
    assert {bill["bill_no"] for bill in result["bills"]} == {"2205513", "2214609"}


def test_incident_alias_stops_starting_work_after_aggregate_deadline(tmp_path) -> None:
    database = Database(tmp_path / "incident-deadline.sqlite3")
    database.initialize()
    client = IncidentClient()
    ticks = iter(float(value) for value in range(0, 1000, 50))
    service = LiveAssemblyServices(
        database,
        client=client,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        targeted_deadline_seconds=120,
        monotonic=lambda: next(ticks),
        now=lambda: datetime(2026, 8, 23, tzinfo=UTC),
    )
    synced: list[dict[str, Any]] = []
    service.pipeline.sync = lambda row: synced.append(dict(row))  # type: ignore[method-assign]

    result = service.explore_issue(INCIDENT_QUERY, limit=20)

    assert synced == []
    assert service.last_refresh["deadline_exceeded"] is True
    assert service.last_refresh["meeting_api_calls"] == 0
    assert result["research_pagination"]["complete"] is False
    assert result["research_pagination"]["deadline_exceeded"] is True
    assert {
        stage["state"] for stage in result["stage_coverage"]["stages"].values()
    } == {"deadline_exceeded"}
    assert result["quality"]["evidence_sufficient"] is False


def test_registry_hint_is_not_reported_as_live_bill_verification(tmp_path) -> None:
    database = Database(tmp_path / "incident-unverified-alternative.sqlite3")
    database.initialize()
    client = IncidentClient(include_alternative_status=False)
    service = LiveAssemblyServices(
        database,
        client=client,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 8, 23, tzinfo=UTC),
    )

    def sync_incident(row: dict[str, Any]):
        bill_no = str(row["BILL_NO"])
        return service.pipeline.ingestor.ingest(
            row,
            (
                f"1. 약사법 일부개정법률안 (의안번호 {bill_no})\n"
                "○김윤 위원  닥터나우 의약품 도매상 규제를 논의하겠습니다."
            ),
            source_hash=f"fixture-unverified-{bill_no}",
            source_url=str(row["PDF_LINK_URL"]),
        )

    service.pipeline.sync = sync_incident  # type: ignore[method-assign]

    result = service.explore_issue(INCIDENT_QUERY, limit=20)

    resolution = result["target_resolution"]
    assert resolution["live_verified_bill_numbers"] == ["2205513"]
    assert "2214609" in resolution["meeting_verified_bill_numbers"]
    assert resolution["confidence"] == "official_agenda_matched_bill_metadata_pending"
    primary_inventory = next(
        item
        for item in result["scope_inventory"]["bill_candidates"]["items"]
        if item["bill_no"] == "2214609"
    )
    assert primary_inventory["verification_state"] == (
        "registry_hint_official_metadata_not_returned"
    )


def test_proposal_year_meeting_scope_stops_at_today(tmp_path) -> None:
    service = LiveAssemblyServices(
        Database(tmp_path / "proposal-meeting-scope.sqlite3"),
        client=object(),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 18, tzinfo=UTC),
    )
    service._refresh_bills = lambda **_kwargs: []  # type: ignore[method-assign]
    captured: dict[str, Any] = {}
    service._refresh_meetings = (  # type: ignore[method-assign]
        lambda **kwargs: captured.update(kwargs)
    )

    service._hydrate_issue(QUERY, {"limit": 5})

    elapsed_months = [f"2026-{month:02d}" for month in range(1, 8)]
    assert captured["months"] == elapsed_months
    assert captured["temporal_scope"] == {
        "mode": "explicit",
        "explicit": True,
        "requested_date_from": "2026-01-01",
        "requested_date_to": "2026-07-18",
        "requested_months": elapsed_months,
        "queried_months": elapsed_months,
        "window_start_month": "2026-01",
        "window_end_month": "2026-07",
        "window_month_count": 7,
    }


def test_bounded_issue_filters_cache_by_year_ranks_five_and_skips_bill_pdfs(
    tmp_path,
) -> None:
    database = Database(tmp_path / "bounded.sqlite3")
    database.initialize()
    service = LiveAssemblyServices(
        database,
        client=object(),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 18, tzinfo=UTC),
    )
    service._hydrate_issue = lambda _query, _filters: 22  # type: ignore[method-assign]
    service._merge_selected_bill_inventory = lambda _bills: None  # type: ignore[method-assign]
    service._merge_cached_bill_inventory = lambda _items: None  # type: ignore[method-assign]
    service._hydrate_selected_bills = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bounded overview eagerly hydrated bill PDFs")
        )
    )
    service.last_refresh = {
        "has_more": False,
        "minutes_failures": 0,
        "months_queried": [f"2026-{month:02d}" for month in range(1, 8)],
        "temporal_scope": {
            "mode": "explicit",
            "explicit": True,
            "requested_months": [f"2026-{month:02d}" for month in range(1, 8)],
            "queried_months": [f"2026-{month:02d}" for month in range(1, 8)],
        },
    }
    calls: dict[str, Any] = {}

    def local_explore(
        query: str,
        limit: int,
        *,
        date_from: str | None,
        date_to: str | None,
        assembly_term: int,
    ) -> dict[str, Any]:
        calls.update(
            query=query,
            limit=limit,
            date_from=date_from,
            date_to=date_to,
            assembly_term=assembly_term,
        )
        bills = [
            {
                "id": f"bill-{number}",
                "bill_no": f"22{number:05d}",
                "name": f"인공지능 법안 {number}",
                "proposed_at": (
                    "2025-12-31" if number == 1 else f"2026-0{min(number, 6)}-01"
                ),
                "processed_at": "2026-06-01" if number == 2 else None,
                "committee": "과학기술정보방송통신위원회",
                "official_url": (
                    "https://likms.assembly.go.kr/bill/billDetail.do?"
                    f"billId=PRC_{number}"
                ),
                "documents": [{"text": "must not be returned"}],
                "selection_relevance": {"score": 30},
            }
            for number in range(1, 8)
        ]
        return {
            "query": query,
            "bills": bills,
            "speeches": [],
            "discussion_threads": [],
            "timeline": [],
            "links": [
                {"bill_id": "bill-2", "speech_id": "speech-1"},
                {"bill_id": "bill-2", "speech_id": "speech-2"},
            ],
            "scope_inventory": {
                "bill_candidates": {"items": bills},
                "selected_for_synthesis": {},
            },
        }

    service.local.explore_issue = local_explore  # type: ignore[method-assign]

    result = service.explore_issue(QUERY, limit=5)

    assert calls == {
        "query": QUERY,
        "limit": 50,
        "date_from": "2026-01-01",
        "date_to": "2026-07-18",
        "assembly_term": 22,
    }
    assert len(result["bills"]) == 5
    assert all(str(bill["proposed_at"]).startswith("2026-") for bill in result["bills"])
    assert result["bills"][0]["bill_no"] == "2200002"
    assert result["bills"][0]["importance"]["rank"] == 1
    assert all(
        bill["official_url"].startswith("https://likms.assembly.go.kr/")
        for bill in result["bills"]
    )
    assert all(bill["documents"] == [] for bill in result["bills"])
    assert all(
        bill["document_coverage"]["gap_reason"]
        == "targeted_get_bill_status_required"
        for bill in result["bills"]
    )
    assert result["proposal_date_scope"]["basis"] == "proposal_date"
    assert result["importance_selection"]["requested_count"] == 5
