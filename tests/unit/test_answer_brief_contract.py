"""Golden contract for the bounded, answer-ready legislative brief.

The fixture models the DoctorNow incident because it exercises a source member
bill, a committee alternative, and all three requested deliberative stages.
The assertions are deliberately generic: the production builder must derive the
same contract for any payload with equivalent official metadata and evidence.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from typing import Any

from kasm.core.answer_brief import build_answer_brief
from kasm.core.response_budget import enforce_bounded_response_budget

REQUESTED_STAGES = ("subcommittee", "standing_committee", "plenary")
READINESS_DIMENSIONS = {
    "content",
    "accuracy",
    "completeness",
    "depth",
    "breadth",
    "detail",
}


def _citation(url: str, page: str, speaker: str) -> dict[str, str]:
    return {
        "official_url": url,
        "source_locator": page,
        "speaker": speaker,
    }


def _turn(
    speech_id: str,
    speaker: str,
    role: str,
    text: str,
    *,
    meeting_id: str,
    meeting: str,
    date: str,
    meeting_type: str,
    url: str,
    page: str,
) -> dict[str, Any]:
    return {
        "speech_id": speech_id,
        "speaker": speaker,
        "speaker_role": role,
        "text": text,
        "meeting_id": meeting_id,
        "meeting": meeting,
        "committee": "보건복지위원회",
        "date": date,
        "meeting_type": meeting_type,
        "official_source": url,
        "source_locator": page,
        "citation": _citation(url, page, speaker),
        "attribution": {
            "state": "exact_bill_number_in_turn_or_agenda",
            "bill_numbers": ["2205513", "2214609"],
            "is_legislator": role in {"위원", "의원"},
        },
        # Upstream ranking metadata is not evidence of a political position.
        # The brief must not turn it into a stance label.
        "selection_relevance": {"score": 0.99, "label": "찬성 추정"},
    }


def _thread(
    meeting_id: str,
    meeting: str,
    date: str,
    meeting_type: str,
    url: str,
    turns: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "meeting_id": meeting_id,
        "meeting": meeting,
        "committee": "보건복지위원회",
        "date": date,
        "meeting_type": meeting_type,
        "participants": [turn["speaker"] for turn in turns],
        "turns": turns,
        "official_url": url,
    }


def _doctor_now_payload() -> dict[str, Any]:
    sub_url = "https://record.assembly.go.kr/assembly/viewer/minutes/download/pdf.do?id=55851"
    committee_url = "https://record.assembly.go.kr/assembly/viewer/minutes/download/pdf.do?id=55861"
    plenary_url = "https://record.assembly.go.kr/assembly/viewer/minutes/download/pdf.do?id=57175"
    sub_turns = [
        _turn(
            "sub-1",
            "이지민",
            "수석전문위원",
            "입법취지는 타당하나 사실상 지배의 의미를 명확히 할 필요가 있습니다.",
            meeting_id="meeting-sub",
            meeting="법안심사제1소위원회",
            date="2025-11-18",
            meeting_type="subcommittee",
            url=sub_url,
            page="p.11",
        ),
        _turn(
            "sub-2",
            "김윤",
            "위원",
            "약가와 납품가 차익을 이용한 새로운 유형의 유인·알선을 규율해야 합니다.",
            meeting_id="meeting-sub",
            meeting="법안심사제1소위원회",
            date="2025-11-18",
            meeting_type="subcommittee",
            url=sub_url,
            page="p.12",
        ),
        _turn(
            "sub-3",
            "서영석",
            "위원",
            "개연성이 높은 이해충돌은 분란이 생기기 전에 명확히 규정해야 합니다.",
            meeting_id="meeting-sub",
            meeting="법안심사제1소위원회",
            date="2025-11-18",
            meeting_type="subcommittee",
            url=sub_url,
            page="p.13",
        ),
        _turn(
            "sub-4",
            "김미애",
            "소위원장",
            "원격의료산업협의회의 영업 자유 침해 의견을 어떻게 조율했는지 설명이 필요합니다.",
            meeting_id="meeting-sub",
            meeting="법안심사제1소위원회",
            date="2025-11-18",
            meeting_type="subcommittee",
            url=sub_url,
            page="p.14",
        ),
        _turn(
            "sub-5",
            "이형훈",
            "보건복지부 제2차관",
            "기존 업체에는 부칙의 경과규정을 두고 거래 제한은 우회 가능성을 보완하겠습니다.",
            meeting_id="meeting-sub",
            meeting="법안심사제1소위원회",
            date="2025-11-18",
            meeting_type="subcommittee",
            url=sub_url,
            page="p.15",
        ),
    ]
    committee_turns = [
        _turn(
            "committee-1",
            "박희승",
            "위원",
            "불공정행위 규제에는 동의하지만 플랫폼 시장과 국민 편익이 축소될 수 있습니다.",
            meeting_id="meeting-committee",
            meeting="보건복지위원회 전체회의",
            date="2025-11-20",
            meeting_type="committee",
            url=committee_url,
            page="p.28",
        ),
        _turn(
            "committee-2",
            "정은경",
            "보건복지부장관",
            "시행 과정에서 환자 편익과 의약품 유통의 공정성을 함께 살피겠습니다.",
            meeting_id="meeting-committee",
            meeting="보건복지위원회 전체회의",
            date="2025-11-20",
            meeting_type="committee",
            url=committee_url,
            page="p.29",
        ),
    ]
    plenary_turns = [
        _turn(
            "plenary-1",
            "이수진",
            "보건복지위원장대리",
            "세 건을 통합 조정한 위원회 대안의 심사 경과와 주요 내용을 보고드립니다.",
            meeting_id="meeting-plenary",
            meeting="제438회 제1차 본회의",
            date="2026-08-20",
            meeting_type="plenary",
            url=plenary_url,
            page="p.42",
        ),
        _turn(
            "plenary-2",
            "이소영",
            "의원",
            "우려만으로 사업을 전면 금지하기보다 이미 마련된 행위규제를 먼저 시행해야 합니다.",
            meeting_id="meeting-plenary",
            meeting="제438회 제1차 본회의",
            date="2026-08-20",
            meeting_type="plenary",
            url=plenary_url,
            page="p.44",
        ),
    ]
    threads = [
        _thread(
            "meeting-sub",
            "법안심사제1소위원회",
            "2025-11-18",
            "subcommittee",
            sub_url,
            sub_turns,
        ),
        _thread(
            "meeting-committee",
            "보건복지위원회 전체회의",
            "2025-11-20",
            "committee",
            committee_url,
            committee_turns,
        ),
        _thread(
            "meeting-plenary",
            "제438회 제1차 본회의",
            "2026-08-20",
            "plenary",
            plenary_url,
            plenary_turns,
        ),
    ]
    stage_coverage = {
        "requested_stages": list(REQUESTED_STAGES),
        "complete": True,
        "exact_measure_check": True,
        "stages": {
            "subcommittee": {
                "state": "discussion_found",
                "observed_candidate_count": 3,
                "candidate_count": 2,
                "unselected_candidate_count": 1,
                "checked_count": 2,
                "matched_discussion_count": 1,
                "failed_count": 0,
                "pending_count": 0,
                "meetings": [
                    {
                        "meeting_id": "meeting-sub",
                        "date": "2025-11-18",
                        "title": "법안심사제1소위원회",
                        "official_url": sub_url,
                        "full_text_loaded": True,
                    },
                    {
                        "meeting_id": "meeting-sub-followup",
                        "date": "2025-11-19",
                        "title": "법안심사제1소위원회 산회 기록",
                        "official_url": f"{sub_url}&part=2",
                        "full_text_loaded": True,
                    },
                ],
            },
            "standing_committee": {
                "state": "discussion_found",
                "observed_candidate_count": 2,
                "candidate_count": 2,
                "unselected_candidate_count": 0,
                "checked_count": 2,
                "matched_discussion_count": 1,
                "failed_count": 0,
                "pending_count": 0,
                "meetings": [
                    {
                        "meeting_id": "meeting-committee",
                        "date": "2025-11-20",
                        "title": "보건복지위원회 전체회의",
                        "official_url": committee_url,
                        "full_text_loaded": True,
                    },
                    {
                        "meeting_id": "meeting-committee-report",
                        "date": "2025-11-20",
                        "title": "보건복지위원회 의결 기록",
                        "official_url": f"{committee_url}&part=2",
                        "full_text_loaded": True,
                    },
                ],
            },
            "plenary": {
                "state": "discussion_found",
                "observed_candidate_count": 2,
                "candidate_count": 1,
                "unselected_candidate_count": 1,
                "checked_count": 1,
                "matched_discussion_count": 1,
                "failed_count": 0,
                "pending_count": 0,
                "meetings": [
                    {
                        "meeting_id": "meeting-plenary",
                        "date": "2026-08-20",
                        "title": "제438회 제1차 본회의",
                        "official_url": plenary_url,
                        "full_text_loaded": True,
                    }
                ],
            },
        },
    }
    return {
        "query": (
            "최근 본회의를 통과한 닥터나우 금지법과 관련하여, 소위원회, "
            "상임위원회, 본회의에서 의원들의 주요 논의 내용을 정리해줘"
        ),
        "target_resolution": {
            "alias_key": "doctor_now_pharmaceutical_wholesale_restriction",
            "matched_alias": "닥터나우 금지법",
            "committee": "보건복지위원회",
            "measure_family": [
                {
                    "bill_no": "2205513",
                    "role": "source_member_bill",
                    "name": "약사법 일부개정법률안",
                    "official_url": "https://example.test/bill/2205513",
                },
                {
                    "bill_no": "2214609",
                    "role": "committee_alternative_primary_vehicle",
                    "name": "약사법 일부개정법률안(대안)",
                    "official_url": "https://example.test/bill/2214609",
                },
            ],
            "primary_vehicle_bill_no": "2214609",
            "live_verified_bill_numbers": ["2205513", "2214609"],
            "meeting_verified_bill_numbers": ["2205513", "2214609"],
            "confidence": "official_bill_and_agenda_identifiers_matched",
            "not_evidence": True,
        },
        "bills": [
            {
                "id": "kna:bill:2205513",
                "bill_no": "2205513",
                "name": "약사법 일부개정법률안",
                "proposer": "김윤의원 등 11인",
                "proposed_at": "2024-11-13",
                "processed_at": "2026-08-20",
                "process_result": "대안반영폐기",
                "official_url": "https://example.test/bill/2205513",
            },
            {
                "id": "kna:bill:2214609",
                "bill_no": "2214609",
                "name": "약사법 일부개정법률안(대안)",
                "proposer": "보건복지위원장",
                "proposed_at": "2025-11-26",
                "processed_at": "2026-08-20",
                "process_result": "수정가결",
                "official_url": "https://example.test/bill/2214609",
            },
        ],
        "speeches": [turn for thread in threads for turn in thread["turns"]],
        "discussion_threads": threads,
        "stage_coverage": stage_coverage,
        "research_pagination": {
            "complete": True,
            "has_more": False,
            "completion_scope": "bounded_targeted_core",
            "candidate_inventory_complete": False,
            "unselected_candidate_count": 2,
        },
        "timeline": [
            {
                "date": "2024-11-13",
                "event_type": "bill_proposed",
                "bill_no": "2205513",
                "title": "약사법 일부개정법률안",
                "official_url": "https://example.test/bill/2205513",
            },
            *[
                {
                    "date": thread["date"],
                    "event_type": "debate",
                    "meeting_id": thread["meeting_id"],
                    "title": thread["meeting"],
                    "official_url": thread["turns"][0]["official_source"],
                }
                for thread in threads
            ],
            {
                "date": "2026-08-20",
                "event_type": "bill_processed",
                "bill_no": "2214609",
                "title": "약사법 일부개정법률안(대안)",
                "detail": "수정가결",
                "official_url": "https://example.test/bill/2214609",
            },
        ],
    }


def _section_ids(sections: Iterable[Any]) -> set[str]:
    return {str(item.get("id")) if isinstance(item, Mapping) else str(item) for item in sections}


def _direct_evidence(stage: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        item for item in stage["evidence"] if item.get("evidence_use") == "direct_claim_evidence"
    ]


def _long_enumerated_opposition() -> str:
    return (
        "이 법안을 반대하는 이유를 세 가지로 말씀드리겠습니다. "
        "첫 번째, 아직 확인되지 않은 우려만으로 사업 전체를 금지해서는 안 됩니다. "
        + ("이미 마련된 행위규제를 먼저 시행하고 그 결과를 검증해야 합니다. " * 28)
        + "공정거래위원회도 사후 제재가 경쟁과 소비자 후생에 더 적절하다고 보았습니다. "
        "두 번째, 허용되던 사업을 사후 입법으로 금지하면 창업과 벤처투자가 위축됩니다. "
        + ("예측 가능한 규제 환경이 혁신 생태계의 기본 조건입니다. " * 12)
        + "마지막으로, 환자가 처방약 재고가 있는 약국을 찾지 못하는 불편이 계속됩니다. "
        + ("약 보유 정보와 가격 비교 편익을 함께 해결할 제3의 방안을 찾아야 합니다. " * 10)
    )


def _expanded_stage_payload(*, turns_per_stage: int, long_text: bool) -> dict[str, Any]:
    payload = _doctor_now_payload()
    templates = {
        "subcommittee": next(
            item for item in payload["speeches"] if item["meeting_type"] == "subcommittee"
        ),
        "standing_committee": next(
            item for item in payload["speeches"] if item["meeting_type"] == "committee"
        ),
        "plenary": next(item for item in payload["speeches"] if item["meeting_type"] == "plenary"),
    }
    expanded: list[dict[str, Any]] = []
    for stage, template in templates.items():
        for index in range(turns_per_stage):
            item = copy.deepcopy(template)
            item["speech_id"] = f"{stage}-expanded-{index:02d}"
            item["speaker"] = f"{stage}-발언자-{index:02d}"
            item["citation"]["speaker"] = item["speaker"]
            if long_text:
                item["text"] = "상세 발언 " + ("가나다라마바사" * 400)
            expanded.append(item)
    payload["speeches"] = expanded
    return payload


def test_doctor_now_brief_preserves_lineage_and_every_requested_stage() -> None:
    brief = build_answer_brief(_doctor_now_payload(), requested_stages=REQUESTED_STAGES)

    assert {
        "schema_version",
        "measure",
        "scope",
        "processing",
        "stages",
        "participant_index",
        "evidence_ledger",
        "comparison_readiness",
        "gaps",
        "required_answer_sections",
        "synthesis_contract",
    } <= set(brief)
    assert brief["schema_version"]
    assert brief["measure"]["primary_vehicle_bill_no"] == "2214609"
    family = {item["bill_no"]: item["role"] for item in brief["measure"]["family"]}
    assert family == {
        "2205513": "source_member_bill",
        "2214609": "committee_alternative_primary_vehicle",
    }
    assert brief["measure"]["lineage"] == [
        {
            "from_bill_no": "2205513",
            "to_bill_no": "2214609",
            "relation": "source_to_primary_vehicle",
            "evidence_status": "retrieval_metadata_not_evidence",
        }
    ]
    assert set(brief["stages"]) == set(REQUESTED_STAGES)
    assert all(brief["stages"][stage]["state"] == "discussion_found" for stage in REQUESTED_STAGES)


def test_brief_returns_all_selected_participants_with_verbatim_citations() -> None:
    brief = build_answer_brief(_doctor_now_payload(), requested_stages=REQUESTED_STAGES)
    expected_speakers = {
        "subcommittee": {"이지민", "김윤", "서영석", "김미애", "이형훈"},
        "standing_committee": {"박희승", "정은경"},
        "plenary": {"이수진", "이소영"},
    }

    for stage, speakers in expected_speakers.items():
        evidence = brief["stages"][stage]["evidence"]
        assert {item["speaker"] for item in evidence} == speakers
        assert len({item["evidence_id"] for item in evidence}) == len(evidence)
        for item in evidence:
            assert item["excerpt_verbatim"]
            assert item["citation"]["official_url"].startswith("https://record.assembly.go.kr/")
            assert item["citation"]["source_locator"].startswith("p.")
            assert not {"stance", "claim_summary", "interpretation"}.intersection(item)


def test_evidence_ledger_is_arithmetically_truthful_about_omissions() -> None:
    brief = build_answer_brief(_doctor_now_payload(), requested_stages=REQUESTED_STAGES)
    ledger = brief["evidence_ledger"]
    by_stage = ledger["by_stage"]
    assert set(by_stage) == set(REQUESTED_STAGES)

    for stage in REQUESTED_STAGES:
        counts = by_stage[stage]
        discovered = counts["discovered_count"]
        checked = counts["checked_count"]
        returned = counts["returned_count"]
        omitted = counts["omitted_count"]
        assert 0 <= returned <= checked <= discovered
        assert omitted == discovered - returned
        meeting_counts = counts["meeting_counts"]
        assert (
            0
            <= meeting_counts["returned_count"]
            <= meeting_counts["checked_count"]
            <= meeting_counts["discovered_count"]
        )
        assert meeting_counts["omitted_count"] == (
            meeting_counts["discovered_count"] - meeting_counts["returned_count"]
        )
        assert meeting_counts["failed_count"] >= 0
        assert meeting_counts["pending_count"] >= 0

    totals = ledger["totals"]
    for name in (
        "discovered_count",
        "checked_count",
        "returned_count",
        "omitted_count",
    ):
        assert totals[name] == sum(by_stage[stage][name] for stage in REQUESTED_STAGES)


def test_comparison_readiness_covers_six_product_acceptance_dimensions() -> None:
    brief = build_answer_brief(_doctor_now_payload(), requested_stages=REQUESTED_STAGES)
    readiness = brief["comparison_readiness"]
    assert set(readiness["dimensions"]) == READINESS_DIMENSIONS
    for dimension in READINESS_DIMENSIONS:
        item = readiness["dimensions"][dimension]
        assert item["status"] in {"ready", "partial", "not_ready"}
        assert isinstance(item["signals"], list) and item["signals"]
        assert isinstance(item["metrics"], Mapping) and item["metrics"]
        assert isinstance(item["gaps"], list)

    required = _section_ids(brief["required_answer_sections"])
    assert {
        "executive_summary",
        "measure_identity_and_effect",
        "timeline",
        "issue_map",
        "stage_by_stage_discussion",
        "argument_exchanges",
        "government_and_expert_views",
        "changes_and_outcome",
        "vote",
        "limitations",
        "sources",
    } <= required
    assert brief["synthesis_contract"]["allow_stance_inference"] is False
    assert brief["synthesis_contract"]["require_official_citations"] is True
    assert brief["synthesis_contract"]["citation_required_per_factual_claim"] is True
    assert brief["synthesis_contract"]["official_url_and_locator_required"] is True


def test_fully_cited_unrelated_bill_without_target_resolution_is_not_ready() -> None:
    payload = _doctor_now_payload()
    payload.pop("target_resolution")
    payload["bills"] = [
        {
            "bill_no": "2210037",
            "name": "장애인 차별조항 정비 법률안",
            "official_url": "https://example.test/bill/2210037",
        }
    ]

    brief = build_answer_brief(payload, requested_stages=REQUESTED_STAGES)

    content = brief["comparison_readiness"]["dimensions"]["content"]
    accuracy = brief["comparison_readiness"]["dimensions"]["accuracy"]
    assert content["status"] == "not_ready"
    assert accuracy["status"] == "not_ready"
    assert accuracy["metrics"]["fully_cited_direct_evidence_count"] == 9
    assert accuracy["metrics"]["target_attributed_direct_evidence_count"] == 0
    assert "primary_measure_unresolved" in accuracy["gaps"]
    assert "official_measure_identity_or_agenda_unverified" in accuracy["gaps"]


def test_official_bill_without_agenda_verification_is_not_accuracy_ready() -> None:
    payload = _doctor_now_payload()
    payload["target_resolution"]["meeting_verified_bill_numbers"] = []
    payload["target_resolution"]["confidence"] = "official_bill_matched_vehicle_agenda_pending"

    brief = build_answer_brief(payload, requested_stages=REQUESTED_STAGES)

    accuracy = brief["comparison_readiness"]["dimensions"]["accuracy"]
    assert accuracy["status"] == "partial"
    assert accuracy["metrics"]["primary_in_official_bill_results"] is True
    assert accuracy["metrics"]["measure_family_in_official_agenda"] is False
    assert "official_measure_identity_or_agenda_unverified" in accuracy["gaps"]


def test_context_only_stages_do_not_satisfy_content_or_breadth() -> None:
    payload = _doctor_now_payload()
    payload["speeches"] = [
        item for item in payload["speeches"] if item["meeting_type"] == "committee"
    ]

    brief = build_answer_brief(payload, requested_stages=REQUESTED_STAGES)

    assert {item["evidence_use"] for item in brief["stages"]["subcommittee"]["evidence"]} == {
        "context_only"
    }
    assert {item["evidence_use"] for item in brief["stages"]["plenary"]["evidence"]} == {
        "context_only"
    }
    content = brief["comparison_readiness"]["dimensions"]["content"]
    breadth = brief["comparison_readiness"]["dimensions"]["breadth"]
    assert content["status"] == "partial"
    assert content["metrics"]["discussion_stage_without_target_direct_count"] == 2
    assert "discussion_stage_without_target_direct_evidence" in content["gaps"]
    assert breadth["status"] == "partial"
    assert breadth["metrics"]["represented_or_verified_absent_stage_count"] == 1
    assert "requested_stage_not_directly_represented_or_verified_absent" in breadth["gaps"]


def test_checked_exact_absence_can_represent_a_requested_stage() -> None:
    payload = _doctor_now_payload()
    payload["speeches"] = [
        item for item in payload["speeches"] if item["meeting_type"] != "plenary"
    ]
    payload["discussion_threads"] = [
        thread for thread in payload["discussion_threads"] if thread["meeting_type"] != "plenary"
    ]
    plenary = payload["stage_coverage"]["stages"]["plenary"]
    plenary.update(
        {
            "state": "checked_no_matching_discussion",
            "observed_candidate_count": 1,
            "candidate_count": 1,
            "unselected_candidate_count": 0,
            "matched_discussion_count": 0,
            "failed_count": 0,
            "pending_count": 0,
        }
    )

    brief = build_answer_brief(payload, requested_stages=REQUESTED_STAGES)

    breadth = brief["comparison_readiness"]["dimensions"]["breadth"]
    assert breadth["status"] == "ready"
    assert breadth["metrics"]["represented_or_verified_absent_stage_count"] == 3


def test_answer_brief_survives_transport_budget_with_stage_and_ledger_contract() -> None:
    payload = _doctor_now_payload()
    payload["answer_brief"] = build_answer_brief(payload, requested_stages=REQUESTED_STAGES)
    payload["unrelated_large_section"] = [
        {"id": f"noise-{index}", "text": "무관한 대용량 본문 " * 2_000} for index in range(100)
    ]
    for speech in payload["speeches"]:
        speech["context_before"] = "앞 문맥 " * 2_000
        speech["context_after"] = "뒤 문맥 " * 2_000

    result = enforce_bounded_response_budget(payload, max_bytes=32_768)

    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 32_768
    assert result["response_budget"]["truncated"] is True
    brief = result["answer_brief"]
    assert brief["measure"]["primary_vehicle_bill_no"] == "2214609"
    assert set(brief["stages"]) == set(REQUESTED_STAGES)
    assert set(brief["evidence_ledger"]["by_stage"]) == set(REQUESTED_STAGES)
    assert set(brief["comparison_readiness"]["dimensions"]) == READINESS_DIMENSIONS
    for counts in brief["evidence_ledger"]["by_stage"].values():
        assert (
            0 <= counts["returned_count"] <= counts["checked_count"] <= counts["discovered_count"]
        )
        assert counts["omitted_count"] == (counts["discovered_count"] - counts["returned_count"])
        meetings = counts["meeting_counts"]
        assert (
            0
            <= meetings["returned_count"]
            <= meetings["checked_count"]
            <= meetings["discovered_count"]
        )
        assert meetings["omitted_count"] == (
            meetings["discovered_count"] - meetings["returned_count"]
        )


def test_stage_evidence_cap_reconciles_returned_omitted_totals_and_gap() -> None:
    payload = _doctor_now_payload()
    template = next(item for item in payload["speeches"] if item["meeting_type"] == "subcommittee")
    other_stages = [item for item in payload["speeches"] if item["meeting_type"] != "subcommittee"]
    expanded = []
    for index in range(20):
        item = copy.deepcopy(template)
        item["speech_id"] = f"sub-cap-{index:02d}"
        item["speaker"] = f"소위발언자-{index:02d}"
        item["citation"]["speaker"] = item["speaker"]
        expanded.append(item)
    payload["speeches"] = expanded + other_stages

    brief = build_answer_brief(payload, requested_stages=REQUESTED_STAGES)
    evidence = _direct_evidence(brief["stages"]["subcommittee"])
    counts = brief["evidence_ledger"]["by_stage"]["subcommittee"]

    assert len(evidence) == 16
    assert counts["discovered_count"] == 20
    assert counts["checked_count"] == 20
    assert counts["returned_count"] == len({item["evidence_id"] for item in evidence})
    assert counts["omitted_count"] == 4
    assert counts["selection_omitted_count"] == 4
    assert counts["transport_omitted_count"] == 0
    assert any(
        gap.get("kind") == "attributed_evidence_omitted"
        and gap.get("stage") == "subcommittee"
        and gap.get("count") == 4
        for gap in brief["gaps"]
    )
    assert brief["evidence_ledger"]["totals"]["returned_count"] == sum(
        stage_counts["returned_count"]
        for stage_counts in brief["evidence_ledger"]["by_stage"].values()
    )


def test_32k_transport_preserves_three_cited_stages_and_reconciles_derivatives() -> None:
    payload = _expanded_stage_payload(turns_per_stage=16, long_text=True)
    payload["answer_brief"] = build_answer_brief(payload, requested_stages=REQUESTED_STAGES)

    result = enforce_bounded_response_budget(payload, max_bytes=32_768)

    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 32_768
    assert "answer_brief" in result
    brief = result["answer_brief"]
    assert set(brief["stages"]) == set(REQUESTED_STAGES)
    actual_ids: set[str] = set()
    for stage_name in REQUESTED_STAGES:
        stage = brief["stages"][stage_name]
        direct = _direct_evidence(stage)
        assert direct
        assert all(
            item["citation"]["official_url"].startswith("https://record.assembly.go.kr/")
            and item["citation"]["source_locator"].startswith("p.")
            for item in direct
        )
        direct_ids = {item["evidence_id"] for item in direct}
        context_ids = {
            item["evidence_id"]
            for item in stage["evidence"]
            if item.get("evidence_use") == "context_only"
        }
        actual_ids.update(item["evidence_id"] for item in stage["evidence"])
        counts = brief["evidence_ledger"]["by_stage"][stage_name]
        assert counts["returned_count"] == len(direct_ids)
        assert counts["context_only_returned_count"] == len(context_ids)
        assert counts["omitted_count"] == (counts["discovered_count"] - counts["returned_count"])
        represented_meetings = {
            item["meeting_id"] for item in stage["evidence"] if item.get("meeting_id")
        }
        assert counts["meeting_counts"]["represented_in_returned_evidence_count"] == len(
            represented_meetings
        )

    for participant in brief["participant_index"]:
        assert set(participant["evidence_ids"]) <= actual_ids
        assert set(participant["claim_eligible_evidence_ids"]) <= actual_ids
    totals = brief["evidence_ledger"]["totals"]
    for name in (
        "discovered_count",
        "checked_count",
        "returned_count",
        "omitted_count",
        "failed_count",
        "pending_count",
    ):
        assert totals[name] == sum(
            brief["evidence_ledger"]["by_stage"][stage][name] for stage in REQUESTED_STAGES
        )
    omitted = totals["omitted_count"]
    completeness = brief["comparison_readiness"]["dimensions"]["completeness"]
    assert omitted > 0
    assert completeness["status"] == "partial"
    assert completeness["metrics"]["omitted_direct_evidence_count"] == omitted
    detail = brief["comparison_readiness"]["dimensions"]["detail"]
    assert detail["status"] == "ready"
    assert detail["metrics"]["minimum_direct_excerpt_bytes"] >= 450
    assert any(gap.get("kind") == "transport_evidence_omitted" for gap in brief["gaps"])


def test_default_128k_keeps_all_selected_direct_turns_and_long_excerpts() -> None:
    payload = _expanded_stage_payload(turns_per_stage=12, long_text=True)
    brief = build_answer_brief(payload, requested_stages=REQUESTED_STAGES)
    before = {stage: len(_direct_evidence(brief["stages"][stage])) for stage in REQUESTED_STAGES}
    payload["answer_brief"] = brief

    result = enforce_bounded_response_budget(payload)

    assert "answer_brief" in result
    returned = result["answer_brief"]
    after = {stage: len(_direct_evidence(returned["stages"][stage])) for stage in REQUESTED_STAGES}
    assert after == before
    excerpts = [
        len(item["excerpt_verbatim"].encode("utf-8"))
        for stage in REQUESTED_STAGES
        for item in _direct_evidence(returned["stages"][stage])
    ]
    assert excerpts and min(excerpts) >= 1_590
    assert max(excerpts) <= 2_400
    assert all(
        returned["evidence_ledger"]["by_stage"][stage]["transport_omitted_count"] == 0
        for stage in REQUESTED_STAGES
    )


def test_default_128k_prefers_2400_byte_direct_excerpts_when_space_allows() -> None:
    payload = _doctor_now_payload()
    for speech in payload["speeches"]:
        speech["text"] = "상세 발언 " + ("가나다라마바사" * 400)
    payload["answer_brief"] = build_answer_brief(payload, requested_stages=REQUESTED_STAGES)

    result = enforce_bounded_response_budget(payload)

    direct = [
        item
        for stage in REQUESTED_STAGES
        for item in _direct_evidence(result["answer_brief"]["stages"][stage])
    ]
    assert direct
    excerpt_bytes = [len(item["excerpt_verbatim"].encode("utf-8")) for item in direct]
    assert min(excerpt_bytes) >= 2_300
    assert max(excerpt_bytes) <= 2_400


def test_long_enumerated_speech_preserves_later_arguments_as_supplemental_excerpts() -> None:
    payload = _doctor_now_payload()
    opposition = next(item for item in payload["speeches"] if item["speech_id"] == "plenary-2")
    opposition["text"] = _long_enumerated_opposition()

    brief = build_answer_brief(payload, requested_stages=REQUESTED_STAGES)

    projected = next(
        item
        for item in _direct_evidence(brief["stages"]["plenary"])
        if item["evidence_id"] == "plenary-2"
    )
    assert len(projected["excerpt_verbatim"].encode("utf-8")) <= 2_400
    assert "두 번째" not in projected["excerpt_verbatim"]
    supplements = projected["supplemental_excerpts"]
    assert {item["argument_marker"] for item in supplements} >= {
        "두 번째",
        "마지막으로",
    }
    supplemental_text = " ".join(item["excerpt_verbatim"] for item in supplements)
    assert "공정거래위원회" in supplemental_text
    assert "창업과 벤처투자" in supplemental_text
    assert "처방약 재고" in supplemental_text
    assert projected["supplemental_excerpt_selected_before_transport_count"] == len(supplements)
    assert projected["supplemental_excerpt_returned_count"] == len(supplements)
    assert projected["supplemental_excerpt_transport_omitted_count"] == 0
    assert brief["synthesis_contract"]["cover_every_supplemental_excerpt"] is True
    plenary_counts = brief["evidence_ledger"]["by_stage"]["plenary"]
    assert plenary_counts["supplemental_returned_count"] == len(supplements)
    assert plenary_counts["supplemental_transport_omitted_count"] == 0


def test_default_128k_preserves_direct_rows_and_core_supplemental_arguments() -> None:
    payload = _doctor_now_payload()
    opposition = next(item for item in payload["speeches"] if item["speech_id"] == "plenary-2")
    opposition["text"] = _long_enumerated_opposition()
    brief = build_answer_brief(payload, requested_stages=REQUESTED_STAGES)
    before_direct = {
        stage: len(_direct_evidence(brief["stages"][stage])) for stage in REQUESTED_STAGES
    }
    before_supplemental = brief["evidence_ledger"]["totals"]["supplemental_returned_count"]
    payload["answer_brief"] = brief
    payload["unrelated_large_section"] = [
        {"id": f"noise-{index}", "text": "무관한 원문 " * 2_000} for index in range(50)
    ]

    result = enforce_bounded_response_budget(payload)

    returned = result["answer_brief"]
    assert {
        stage: len(_direct_evidence(returned["stages"][stage])) for stage in REQUESTED_STAGES
    } == before_direct
    totals = returned["evidence_ledger"]["totals"]
    assert totals["supplemental_returned_count"] == before_supplemental
    assert totals["supplemental_transport_omitted_count"] == 0
    opposition_after = next(
        item
        for item in _direct_evidence(returned["stages"]["plenary"])
        if item["evidence_id"] == "plenary-2"
    )
    supplemental_text = " ".join(
        item["excerpt_verbatim"] for item in opposition_after["supplemental_excerpts"]
    )
    assert "창업과 벤처투자" in supplemental_text
    assert "처방약 재고" in supplemental_text


def test_32k_drops_supplemental_arguments_before_direct_evidence_rows() -> None:
    payload = _expanded_stage_payload(turns_per_stage=16, long_text=False)
    for speech in payload["speeches"]:
        speech["text"] = _long_enumerated_opposition()
    payload["answer_brief"] = build_answer_brief(payload, requested_stages=REQUESTED_STAGES)
    selected_supplemental = payload["answer_brief"]["evidence_ledger"]["totals"][
        "supplemental_selected_before_transport_count"
    ]
    assert selected_supplemental > 0

    result = enforce_bounded_response_budget(payload, max_bytes=32_768)

    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 32_768
    returned = result["answer_brief"]
    for stage in REQUESTED_STAGES:
        assert _direct_evidence(returned["stages"][stage])
    totals = returned["evidence_ledger"]["totals"]
    assert totals["supplemental_selected_before_transport_count"] == selected_supplemental
    assert totals["supplemental_returned_count"] < selected_supplemental
    assert totals["supplemental_transport_omitted_count"] == (
        selected_supplemental - totals["supplemental_returned_count"]
    )
    if totals["transport_omitted_count"]:
        assert totals["supplemental_returned_count"] == 0
    assert any(gap.get("kind") == "supplemental_evidence_omitted" for gap in returned["gaps"])
