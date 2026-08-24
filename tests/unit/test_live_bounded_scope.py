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
    _filter_bills_by_temporal_scope,
    _filter_meeting_rows_by_scope,
    _measure_discussion_segment_rows,
    _meeting_date_queries,
    _proposal_date_scope,
    _scope_target_measure_turn_text,
)
from kasm.search.measure_aliases import resolve_measure_alias
from kasm.storage.database import Database

QUERY = (
    "2026년 발의된 인공지능 관련 법안 중 중요도가 높은 법안을 5개 정도 "
    "정리하고, 이에 대한 소위원회, 상임위원회 논의 내용을 정리해줘."
)
INCIDENT_QUERY = (
    "최근 본회의를 통과한 닥터나우 금지법과 관련하여, 소위원회, 상임위원회, "
    "본회의에서 의원들의 주요 논의 내용을 정리해줘"
)
AI_BASIC_ACT_QUERY = (
    "제22대 국회 AI 기본법의 법안소위·과방위 전체회의·본회의 주요 논의를 "
    "정확하고 충분하게 정리해줘."
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
            assert values["DAE_NUM"] == 22
            assert set(values) == {"DAE_NUM", "CONF_DATE", "SUB_NAME"}
            if values == {
                "DAE_NUM": 22,
                "CONF_DATE": "2024",
                "SUB_NAME": "2205513",
            }:
                rows = ()
            elif values == {
                "DAE_NUM": 22,
                "CONF_DATE": "2025",
                "SUB_NAME": "2205513",
            }:
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
            else:
                assert values["SUB_NAME"] == "2214609"
                assert values["CONF_DATE"] in {"2025", "2026"}
                rows = ()
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


class AiBasicActClient:
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
        rows: tuple[dict[str, Any], ...]
        if dataset == BILL_DATASET and values.get("BILL_NO") == "2203072":
            rows = (
                {
                    "BILL_ID": "PRC_AI_BASIC_ACT_SOURCE",
                    "BILL_NO": "2203072",
                    "BILL_NAME": "인공지능 기본법안",
                    "AGE": "22",
                    "PROPOSER": "한민수의원 등 10인",
                    "COMMITTEE": "과학기술정보방송통신위원회",
                    "PROPOSE_DT": "2024-08-22",
                    "PROC_RESULT": "대안반영폐기",
                    "CMT_PROC_DT": "2024-11-26",
                    "PROC_DT": "2024-12-26",
                },
            )
        elif dataset == BILL_DATASET:
            rows = ()
        elif dataset == BILL_STATUS_DATASET and values.get("BILL_NO") == "2206772":
            rows = (
                {
                    "BILL_ID": "PRC_AI_BASIC_ACT_VEHICLE",
                    "BILL_NO": "2206772",
                    "BILL_NAME": (
                        "인공지능 발전과 신뢰 기반 조성 등에 관한 "
                        "기본법안(대안)"
                    ),
                    "AGE": "22",
                    "PROPOSER": "과학기술정보방송통신위원장",
                    "COMMITTEE": "과학기술정보방송통신위원회",
                    "PROPOSE_DT": "2024-12-20",
                    "PROC_RESULT": "원안가결",
                    "PROC_DT": "2024-12-26",
                },
            )
        elif dataset == BILL_STATUS_DATASET:
            rows = ()
        elif dataset == DATASET_BY_SOURCE[MeetingSource.COMMITTEE]:
            assert values == {
                "DAE_NUM": 22,
                "CONF_DATE": "2024",
                "SUB_NAME": "2203072",
            }
            rows = (
                _ai_basic_act_meeting_row(
                    "2024-11-21",
                    "과학기술정보방송통신위원회 정보통신방송법안심사소위원회",
                    "2203072",
                    "ai-subcommittee",
                ),
                _ai_basic_act_meeting_row(
                    "2024-11-26",
                    "과학기술정보방송통신위원회 전체회의",
                    "2203072",
                    "ai-standing",
                ),
            )
        elif dataset == DATASET_BY_SOURCE[MeetingSource.PLENARY]:
            assert values == {
                "DAE_NUM": 22,
                "CONF_DATE": "2024",
                "SUB_NAME": "2206772",
            }
            rows = (
                _ai_basic_act_meeting_row(
                    "2024-12-26",
                    "제420회 제1차 국회본회의",
                    "2206772",
                    "ai-plenary",
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


class ManyIncidentMeetingsClient(IncidentClient):
    """Expose more exact agenda candidates than one bounded targeted window."""

    def fetch_page(
        self,
        dataset: str,
        *,
        page: int = 1,
        page_size: int = 100,
        parameters: dict[str, str | int] | None = None,
        refresh: bool = False,
    ) -> ApiPage:
        values = dict(parameters or {})
        if (
            dataset == DATASET_BY_SOURCE[MeetingSource.COMMITTEE]
            and values.get("SUB_NAME") == "2205513"
            and values.get("CONF_DATE") == "2025"
        ):
            del refresh
            self.calls.append((dataset, values))
            rows = tuple(
                _incident_meeting_row(
                    f"2025-11-{10 + index:02d}",
                    (
                        f"보건복지위원회 법안심사제1소위원회 {index}"
                        if index < 2
                        else f"보건복지위원회 전체회의 {index}"
                    ),
                    "2205513",
                    f"many-{index}",
                )
                for index in range(7)
            )
            return ApiPage(
                dataset,
                page,
                page_size,
                len(rows),
                rows,
                "https://open.assembly.go.kr/portal/openapi/fixture",
                dataset,
            )
        return super().fetch_page(
            dataset,
            page=page,
            page_size=page_size,
            parameters=parameters,
            refresh=refresh,
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


def _ai_basic_act_meeting_row(
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
        "SUB_NAME": f"인공지능 기본법안 의안번호 {bill_no}",
        "CONF_ID": suffix,
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


def test_ai_basic_act_alias_resolves_source_and_final_vehicle() -> None:
    hint = resolve_measure_alias(AI_BASIC_ACT_QUERY)

    assert hint is not None
    assert hint.key == "artificial_intelligence_basic_act_2024"
    assert hint.assembly_term == 22
    assert hint.committee == "과학기술정보방송통신위원회"
    assert hint.bill_numbers == ("2203072", "2206772")
    assert hint.primary_vehicle_bill_no == "2206772"
    assert [identity.role for identity in hint.identities] == [
        "source_member_bill",
        "committee_alternative_primary_vehicle",
    ]


def test_ai_basic_act_uses_source_for_committee_and_vehicle_for_plenary(
    tmp_path,
) -> None:
    database = Database(tmp_path / "ai-basic-act-targeted.sqlite3")
    database.initialize()
    client = AiBasicActClient()
    service = LiveAssemblyServices(
        database,
        client=client,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        max_minutes_per_request=1,
        now=lambda: datetime(2026, 8, 24, tzinfo=UTC),
    )
    synced: list[str] = []

    def sync_ai_basic_act(row: dict[str, Any]):
        suffix = str(row["CONF_ID"])
        bill_no = "2206772" if suffix == "ai-plenary" else "2203072"
        synced.append(suffix)
        if suffix == "ai-subcommittee":
            body = (
                f"1. 인공지능 기본법안 (의안번호 {bill_no})\n"
                "○한민수 위원  인공지능 산업 육성과 고영향 인공지능의 "
                "안전·신뢰 기반을 함께 논의해야 합니다.\n"
                "○과학기술정보통신부장관 유상임  산업 진흥과 투명성 의무를 "
                "균형 있게 집행하겠습니다."
            )
        elif suffix == "ai-standing":
            body = (
                f"7. 인공지능 기본법안 (의안번호 {bill_no})\n"
                "○이정헌 위원  인공지능 발전과 신뢰 기반 조성을 위해 "
                "산업 진흥과 규제의 균형을 보장해야 합니다.\n"
                "○최민희 위원장  위원회 대안으로 채택하였음을 선포합니다."
            )
        else:
            body = (
                "25. 인공지능 발전과 신뢰 기반 조성 등에 관한 "
                f"기본법안(대안) (의안번호 {bill_no})\n"
                "○최형두 의원  AI G3 도약을 위한 법제와 예산 기반이 "
                "필요합니다.\n"
                "○의장 우원식  재석 264인 중 찬성 260인, 반대 1인, "
                "기권 3인으로 가결되었음을 선포합니다."
            )
        return service.pipeline.ingestor.ingest(
            row,
            body,
            source_hash=f"fixture-{suffix}",
            source_url=str(row["PDF_LINK_URL"]),
        )

    service.pipeline.sync = sync_ai_basic_act  # type: ignore[method-assign]

    result = service.explore_issue(AI_BASIC_ACT_QUERY, limit=20)

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
    assert meeting_calls == [
        (
            DATASET_BY_SOURCE[MeetingSource.COMMITTEE],
            {"DAE_NUM": 22, "CONF_DATE": "2024", "SUB_NAME": "2203072"},
        ),
        (
            DATASET_BY_SOURCE[MeetingSource.PLENARY],
            {"DAE_NUM": 22, "CONF_DATE": "2024", "SUB_NAME": "2206772"},
        ),
    ]
    assert service.last_refresh["meeting_api_calls"] == 2
    assert service.last_refresh["meeting_candidates"] == 3
    assert service.last_refresh["checked_candidate_count"] == 3
    assert service.last_refresh["unselected_candidate_count"] == 0
    assert service.last_refresh["has_more"] is False
    assert set(synced) == {"ai-subcommittee", "ai-standing", "ai-plenary"}
    assert result["target_resolution"]["live_verified_bill_numbers"] == [
        "2203072",
        "2206772",
    ]
    assert result["target_resolution"]["meeting_verified_bill_numbers"] == [
        "2203072",
        "2206772",
    ]
    assert result["stage_coverage"]["exact_measure_check"] is True
    assert {
        stage: coverage["state"]
        for stage, coverage in result["stage_coverage"]["stages"].items()
    } == {
        "subcommittee": "discussion_found",
        "standing_committee": "discussion_found",
        "plenary": "discussion_found",
    }
    assert result["stage_coverage"]["complete"] is True
    assert {bill["bill_no"] for bill in result["bills"]} == {
        "2203072",
        "2206772",
    }
    assert result["speeches"]
    assert all(
        set(speech["attribution"]["bill_numbers"]).intersection(
            {"2203072", "2206772"}
        )
        for speech in result["speeches"]
    )
    assert all(
        speech["attribution"]["state"]
        in {
            "exact_bill_number_in_turn_or_agenda",
            "exact_speech_bill_link",
            "exact_measure_discussion_segment",
            "exact_agenda_segment_context",
        }
        for speech in result["speeches"]
    )


def test_ai_basic_act_high_signal_turns_anchor_without_repeating_bill_number() -> None:
    hint = resolve_measure_alias(AI_BASIC_ACT_QUERY)
    assert hint is not None
    rows = [
        {
            "id": "ai-standing:1",
            "meeting_id": "ai-standing",
            "sequence": 1,
            "speaker_name": "이해민",
            "speaker_role": "위원",
            "agenda": "복수 의사일정 제1항부터 제19항까지 일괄 심사",
            "text": (
                "인공지능 기본법은 규제 범위의 문제점을 계속 모니터링하며 "
                "법의 완결성을 더해야 합니다."
            ),
        },
        {
            "id": "ai-standing:2",
            "meeting_id": "ai-standing",
            "sequence": 2,
            "speaker_name": "과학기술정보통신부장관",
            "speaker_role": "장관",
            "agenda": "복수 의사일정 제1항부터 제19항까지 일괄 심사",
            "text": "고영향 인공지능과 투명성 의무를 균형 있게 집행하겠습니다.",
        },
        {
            "id": "ai-standing:3",
            "meeting_id": "ai-standing",
            "sequence": 3,
            "speaker_name": "전문위원",
            "speaker_role": "전문위원",
            "agenda": "복수 의사일정",
            "text": (
                "인공지능 산업 육성도 중요합니다. 장애인 차별조항 정비를 위한 "
                "과학기술정보방송통신위원회 소관 6개 법률 일부개정을 위한 "
                "법률안을 보고드리겠습니다."
            ),
        },
        {
            "id": "ai-standing:4",
            "meeting_id": "ai-standing",
            "sequence": 4,
            "speaker_name": "박충권",
            "speaker_role": "의원",
            "agenda": "복수 의사일정",
            "text": "KBS 수신료에 관한 방송법 개정안을 반대합니다.",
        },
        {
            "id": "ai-standing:5",
            "meeting_id": "ai-standing",
            "sequence": 5,
            "speaker_name": "의장",
            "speaker_role": "의장",
            "agenda": "복수 의사일정",
            "text": "박충권 의원 수고하셨습니다. 다음 토론자를 부르겠습니다.",
        },
        {
            "id": "ai-standing:6",
            "meeting_id": "ai-standing",
            "sequence": 6,
            "speaker_name": "우원식",
            "speaker_role": "의장",
            "agenda": "복수 의사일정",
            "text": (
                "인공지능 발전과 신뢰 기반 조성 등에 관한 기본법안(대안)은 "
                "가결되었음을 선포합니다."
            ),
        },
    ]

    segments = _measure_discussion_segment_rows(
        rows,
        exact_numbers=set(hint.bill_numbers),
        linked_numbers_by_speech={},
        hint=hint,
        target_agenda_numbers_by_meeting={"ai-standing": {7}},
    )

    assert segments == {
        "ai-standing:1": "anchor",
        "ai-standing:2": "anchor",
        "ai-standing:6": "outcome",
    }


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


def test_explicit_temporal_bill_scope_rejects_later_official_compact_date() -> None:
    bills = [
        {
            "BILL_NO": "2203072",
            "BILL_NAME": "인공지능 기본법안",
            "PROPOSE_DT": "20240822",
            "CMT_PROC_DT": "20241126",
        },
        {
            "BILL_NO": "2210037",
            "BILL_NAME": (
                "장애인 차별조항 정비를 위한 과학기술정보방송통신위원회 "
                "소관 6개 법률 일부개정을 위한 법률안"
            ),
            "PROPOSE_DT": "20250422",
        },
    ]

    scoped = _filter_bills_by_temporal_scope(
        bills,
        date_from="2024-01-01",
        date_to="2024-12-31",
    )

    assert [bill["BILL_NO"] for bill in scoped] == ["2203072"]


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


def test_incident_alias_checks_full_vehicle_path_and_three_stage_minutes(
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
    assert len(meeting_calls) == 3
    assert all("SUB_NAME" in parameters for _dataset, parameters in meeting_calls)
    assert not any(
        dataset == DATASET_BY_SOURCE[MeetingSource.SUBCOMMITTEE]
        for dataset, _parameters in meeting_calls
    )
    assert service.last_refresh["months_queried"][0] == "2024-11"
    assert service.last_refresh["months_queried"][-1] == "2026-08"
    assert len(service.last_refresh["months_queried"]) == 22
    assert service.last_refresh["query_marker_months"] == [
        "2024-11",
        "2025-11",
        "2026-08",
    ]
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
        in {
            "exact_bill_number_in_turn_or_agenda",
            "exact_speech_bill_link",
            "exact_measure_discussion_segment",
        }
        for speech in result["speeches"]
    )
    assert {speech["speaker"] for speech in result["speeches"]} == {"김윤", "정은경"}
    assert {bill["bill_no"] for bill in result["bills"]} == {"2205513", "2214609"}


def test_target_agenda_segment_keeps_followup_speakers_without_repeated_bill_number(
    tmp_path,
) -> None:
    database = Database(tmp_path / "incident-agenda-segment.sqlite3")
    database.initialize()
    service = LiveAssemblyServices(
        database,
        client=IncidentClient(),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        max_minutes_per_request=2,
        now=lambda: datetime(2026, 8, 23, tzinfo=UTC),
    )

    def sync_segment(row: dict[str, Any]):
        bill_no = str(row["BILL_NO"])
        return service.pipeline.ingestor.ingest(
            row,
            (
                "1. 약사법 일부개정법률안 심사\n"
                f"○김윤 위원  의안번호 {bill_no}의 이해충돌 방지 취지를 설명하겠습니다.\n"
                "○서영석 위원  같은 의제의 규율 필요성에 관해 이어서 말씀드리겠습니다.\n"
                "○보건복지부차관 이형훈  경과규정과 집행 방안을 답변드리겠습니다.\n"
                "2. 다른 법률안 심사\n"
                "○무관 위원  의안번호 2299999에 관한 별도 의견입니다."
            ),
            source_hash=f"fixture-segment-{bill_no}",
            source_url=str(row["PDF_LINK_URL"]),
        )

    service.pipeline.sync = sync_segment  # type: ignore[method-assign]

    result = service.explore_issue(INCIDENT_QUERY, limit=20)

    speakers = {speech["speaker"] for speech in result["speeches"]}
    assert {"김윤", "서영석", "이형훈"} <= speakers
    assert "무관" not in speakers
    followups = [
        speech
        for speech in result["speeches"]
        if speech["speaker"] in {"서영석", "이형훈"}
    ]
    assert followups
    assert {
        speech["attribution"]["state"] for speech in followups
    } == {"exact_agenda_segment_context"}


def test_realistic_measure_segments_keep_target_exchanges_and_stage_outcomes() -> None:
    hint = resolve_measure_alias(INCIDENT_QUERY)
    assert hint is not None

    def row(
        meeting_id: str,
        sequence: int,
        text: str,
        *,
        speaker: str = "김미애",
        role: str = "위원",
    ) -> dict[str, Any]:
        return {
            "id": f"{meeting_id}:{sequence}",
            "meeting_id": meeting_id,
            "sequence": sequence,
            "speaker_name": speaker,
            "speaker_role": role,
            "agenda": "복수 의사일정 제1항~제80항 일괄 심사",
            "text": text,
        }

    subcommittee = "subcommittee"
    subcommittee_rows = [
        row(subcommittee, 103, "앞선 의료법 안건에 대한 의견입니다."),
        row(subcommittee, 104, "의사일정 제10항 약사법 일부개정법률안을 심사하겠습니다."),
        row(
            subcommittee,
            105,
            "비대면진료 플랫폼의 의약품 도매상 운영과 리베이트 규율을 검토합니다.",
            speaker="이지민",
            role="수석전문위원",
        ),
        row(subcommittee, 106, "정부 측 의견 듣겠습니다.", role="소위원장"),
        row(
            subcommittee,
            107,
            "비대면진료 플랫폼사업자와 의약품 도매상 관계를 분리할 필요가 있습니다.",
            speaker="이형훈",
            role="차관",
        ),
        row(subcommittee, 113, "예, 그렇습니다.", speaker="이형훈", role="차관"),
        row(
            subcommittee,
            118,
            "닥터나우로 불리는 한 플랫폼의 도매상 소유와 처방 유인을 규제해야 합니다.",
            speaker="김윤",
        ),
        row(
            subcommittee,
            125,
            "기존 비대면진료 플랫폼 도매상은 어떻게 정리합니까?",
            speaker="서명옥",
        ),
        row(
            subcommittee,
            126,
            "부칙의 경과규정 기간에 법에 부합하도록 소유관계를 정리해야 합니다.",
            speaker="이형훈",
            role="차관",
        ),
        row(subcommittee, 127, "소급적용하나요?", speaker="서명옥"),
        row(subcommittee, 128, "그것은 안 되겠지요.", role="소위원장"),
        row(
            subcommittee,
            130,
            "의사일정 제10항은 심사한 후 의결하고 의사일정 제11항을 심사하겠습니다.",
            role="소위원장",
        ),
        row(subcommittee, 131, "다른 약사법 안건을 보고드리겠습니다."),
        row(
            subcommittee,
            177,
            "추가질의 없습니까? 의사일정 제10항부터 제12항까지 약사법 일부개정법률안은……",
            role="소위원장",
        ),
        row(subcommittee, 179, "시행일은 1년으로 하고 예산은 별도입니다."),
        row(
            subcommittee,
            182,
            "식약처 예산과 심평원 예산을 반영하면 됩니다.",
            speaker="김선민",
        ),
        row(
            subcommittee,
            184,
            "의사일정 제10항부터 제12항까지 위원회 대안으로 채택합니다. "
            "가결되었음을 선포합니다.",
            role="소위원장",
        ),
    ]
    subcommittee_segments = _measure_discussion_segment_rows(
        subcommittee_rows,
        exact_numbers=set(hint.bill_numbers),
        linked_numbers_by_speech={},
        hint=hint,
        target_agenda_numbers_by_meeting={subcommittee: {10}},
    )

    assert subcommittee_segments[f"{subcommittee}:104"] == "anchor"
    assert subcommittee_segments[f"{subcommittee}:113"] == "short_context"
    assert subcommittee_segments[f"{subcommittee}:127"] == "short_context"
    assert subcommittee_segments[f"{subcommittee}:128"] == "short_context"
    assert subcommittee_segments[f"{subcommittee}:184"] == "outcome"
    assert not {
        f"{subcommittee}:{sequence}" for sequence in (103, 131, 177, 179, 182)
    }.intersection(subcommittee_segments)

    plenary = "plenary"
    plenary_rows = [
        row(plenary, 30, "특허법 표결 결과를 선포합니다.", speaker="조정식", role="의장"),
        row(
            plenary,
            31,
            "의사일정 제43항 약사법 일부개정법률안(대안)부터 제49항까지 상정합니다.",
            speaker="조정식",
            role="의장",
        ),
        row(
            plenary,
            32,
            "약사법 일부개정법률안(대안)은 비대면진료 중개업자와 의약품 도매상을 "
            "분리하려는 것입니다.",
            speaker="이수진",
            role="위원장대리",
        ),
        row(
            plenary,
            33,
            "의사일정 제43항에 토론 신청이 있으므로 토론을 듣겠습니다.",
            speaker="조정식",
            role="의장",
        ),
        row(
            plenary,
            34,
            "닥터나우 금지법으로 불리는 전면 금지 조항에 반대합니다.",
            speaker="이소영",
            role="의원",
        ),
        row(
            plenary,
            35,
            "약사법 일부개정법률안(대안) 투표 결과 재석 178인, 찬성 95인, "
            "반대 34인, 기권 49인으로 가결되었음을 선포합니다.",
            speaker="조정식",
            role="의장",
        ),
        row(plenary, 36, "의사일정 제50항을 상정합니다.", speaker="조정식", role="의장"),
    ]
    plenary_segments = _measure_discussion_segment_rows(
        plenary_rows,
        exact_numbers=set(hint.bill_numbers),
        linked_numbers_by_speech={},
        hint=hint,
        target_agenda_numbers_by_meeting={plenary: {43}},
    )

    assert {
        int(speech_id.rsplit(":", 1)[1]) for speech_id in plenary_segments
    } == {31, 32, 33, 34, 35}
    assert plenary_segments[f"{plenary}:35"] == "outcome"


def test_target_outcome_text_is_scoped_to_the_relevant_grouped_vote() -> None:
    hint = resolve_measure_alias(INCIDENT_QUERY)
    assert hint is not None
    omnibus = (
        "의사일정 제13항 의료법 일부개정법률안(대안)을 채택합니다. "
        "가결되었음을 선포합니다. "
        "의사일정 제17항 약사법 일부개정법률안(대안)을 채택하고 "
        "의사일정 제14항부터 제16항까지는 본회의에 부의하지 않습니다. "
        "가결되었음을 선포합니다. "
        "의사일정 제18항 마약류 관리에 관한 법률 일부개정법률안을 의결합니다."
    )

    scoped = _scope_target_measure_turn_text(
        omnibus,
        target_agenda_numbers={14},
        hint=hint,
        segment_kind="outcome",
    )

    assert scoped.startswith("의사일정 제17항 약사법")
    assert scoped.endswith("가결되었음을 선포합니다.")
    assert "의사일정 제13항" not in scoped
    assert "의사일정 제18항" not in scoped


def test_target_procedure_keeps_named_from_to_agenda_range() -> None:
    hint = resolve_measure_alias(INCIDENT_QUERY)
    assert hint is not None
    grouped = (
        "의사일정 제43항 약사법 일부개정법률안(대안)부터 의사일정 제49항 국\n"
        "민연금법 일부개정법률안(대안)까지 이상 7건을 상정합니다.\n"
        "보건복지위원회 이수진 위원 나오셔서 7건에 대해 심사보고해 주시기 바랍니다."
    )

    scoped = _scope_target_measure_turn_text(
        grouped,
        target_agenda_numbers={43},
        hint=hint,
        segment_kind="anchor",
    )

    assert scoped == grouped
    assert "의사일정 제49항" in scoped
    assert scoped.endswith("심사보고해 주시기 바랍니다.")


def test_targeted_candidates_continue_until_exact_candidate_scope_is_checked(
    tmp_path,
) -> None:
    database = Database(tmp_path / "incident-targeted-pagination.sqlite3")
    database.initialize()
    service = LiveAssemblyServices(
        database,
        client=ManyIncidentMeetingsClient(),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 8, 23, tzinfo=UTC),
    )
    synced_urls: list[str] = []

    def sync_candidate(row: dict[str, Any]):
        synced_urls.append(str(row["PDF_LINK_URL"]))
        bill_no = str(row["BILL_NO"])
        return service.pipeline.ingestor.ingest(
            row,
            (
                f"1. 약사법 일부개정법률안 (의안번호 {bill_no})\n"
                "○김윤 위원  비대면진료 중개업자의 도매상 이해충돌을 논의합니다.\n"
                "○보건복지부차관 이형훈  정부 검토 의견을 답변드립니다."
            ),
            source_hash=f"fixture-page-{bill_no}-{row['CONF_ID']}",
            source_url=str(row["PDF_LINK_URL"]),
        )

    service.pipeline.sync = sync_candidate  # type: ignore[method-assign]

    first = service.explore_issue(INCIDENT_QUERY, limit=20)

    assert first["research_pagination"]["complete"] is False
    assert first["research_pagination"]["next_minutes_offset"] == 6
    assert first["research_pagination"]["unselected_candidate_count"] == 2
    assert first["stage_coverage"]["complete"] is False
    assert first["quality"]["evidence_sufficient"] is False
    assert len(synced_urls) == 6

    second = service.explore_issue(INCIDENT_QUERY, limit=20, minutes_offset=6)

    assert second["research_pagination"]["complete"] is True
    assert second["research_pagination"]["next_minutes_offset"] is None
    assert second["research_pagination"]["unselected_candidate_count"] == 0
    assert second["stage_coverage"]["complete"] is True
    assert len(synced_urls) == 8


def test_warm_targeted_repeat_reuses_current_parser_rows(tmp_path) -> None:
    database = Database(tmp_path / "incident-warm-reuse.sqlite3")
    database.initialize()
    service = LiveAssemblyServices(
        database,
        client=IncidentClient(),  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        max_minutes_per_request=2,
        now=lambda: datetime(2026, 8, 23, tzinfo=UTC),
    )
    sync_count = 0

    def sync_once(row: dict[str, Any]):
        nonlocal sync_count
        sync_count += 1
        bill_no = str(row["BILL_NO"])
        return service.pipeline.ingestor.ingest(
            row,
            (
                f"1. 약사법 일부개정법률안 (의안번호 {bill_no})\n"
                "○김윤 위원  관련 의제를 논의합니다."
            ),
            source_hash=f"fixture-reuse-{bill_no}",
            source_url=str(row["PDF_LINK_URL"]),
        )

    service.pipeline.sync = sync_once  # type: ignore[method-assign]

    service.explore_issue(INCIDENT_QUERY, limit=20)
    assert sync_count == 3
    repeated = service.explore_issue(INCIDENT_QUERY, limit=20)

    assert sync_count == 3
    assert repeated["live_refresh"]["minutes_cache_reused"] == 3


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
