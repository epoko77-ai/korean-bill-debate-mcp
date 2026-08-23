import json

import pytest

from kasm.core.response_budget import (
    MAX_BOUNDED_RESPONSE_BYTES,
    enforce_bounded_response_budget,
)


def test_bounded_response_has_a_hard_byte_cap_and_truthful_truncation() -> None:
    payload = {
        "bills": [
            {"id": f"bill-{index}", "bill_no": f"22{index:05d}", "name": "약사법"}
            for index in range(50)
        ],
        "speeches": [
            {
                "speech_id": f"speech-{index}",
                "meeting_id": f"meeting-{index}",
                "text": "긴 발언 " * 2000,
                "context_before": "앞 맥락 " * 1000,
                "context_after": "뒤 맥락 " * 1000,
            }
            for index in range(30)
        ],
        "discussion_threads": [
            {
                "meeting_id": f"meeting-{index}",
                "turns": [
                    {"speech_id": f"turn-{index}-{turn}", "text": "토론 " * 2000}
                    for turn in range(10)
                ],
            }
            for index in range(20)
        ],
        "links": [{"bill_id": "bill-1", "speech_id": f"speech-{index}"} for index in range(500)],
        "scope_inventory": {
            name: {
                "complete": True,
                "total": 1000,
                "items": [
                    {"id": f"{name}-{index}", "detail": "후보 설명 " * 20}
                    for index in range(1000)
                ],
            }
            for name in (
                "bill_candidates",
                "meeting_candidates",
                "speech_candidates",
                "links",
            )
        },
    }

    result = enforce_bounded_response_budget(payload)
    encoded = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")

    assert len(encoded) <= MAX_BOUNDED_RESPONSE_BYTES
    assert result["response_budget"]["truncated"] is True
    assert result["response_budget"]["final_bytes"] <= MAX_BOUNDED_RESPONSE_BYTES
    assert result["response_budget"]["truncated_sections"]
    inventory = result["scope_inventory"]["bill_candidates"]
    assert inventory["observed_total"] == 1000
    assert inventory["returned_count"] < 1000
    assert inventory["truncated"] is True
    assert result["speeches"][0]["text_inline_complete"] is False
    assert result["speeches"][0]["text_length"] > len(result["speeches"][0]["text"])


def test_arbitrary_large_strings_and_nested_mappings_cannot_escape_cap() -> None:
    payload = {
        "query": "닥터나우 금지법 " * 100_000,
        "next_action": {
            "tool": "get_bill_status",
            "arguments": {"bill_no": "2214609"},
            "instruction": "continue " * 100_000,
        },
        "target_resolution": {
            "primary_vehicle_bill_no": "2214609",
            "confidence": "official_bill_and_agenda_identifiers_matched",
            "unrecognized_blob": {f"field-{index}": "값" * 20_000 for index in range(200)},
        },
        "stage_coverage": {
            "requested_stages": ["subcommittee", "standing_committee", "plenary"],
            "stages": {
                "subcommittee": {"state": "discussion_found", "meetings": []},
                "standing_committee": {"state": "discussion_found", "meetings": []},
                "plenary": {
                    "state": "record_found_no_member_debate",
                    "meetings": [],
                },
            },
        },
        "research_pagination": {"complete": True, "has_more": False},
        "arbitrary": {
            "nested": {"deeper": {"payload": "무제한 " * 200_000}},
        },
        "bills": [
            {
                "bill_no": "2214609",
                "official_url": "https://example.test/bills/2214609",
                "description": "설명 " * 100_000,
            }
        ],
    }

    result = enforce_bounded_response_budget(payload, max_bytes=16_384)
    encoded = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")

    assert len(encoded) <= 16_384
    assert result["response_budget"]["final_bytes"] == len(encoded)
    assert result["response_budget"]["truncated"] is True
    assert result["response_budget"]["compacted_values"]["query"]["inline_complete"] is False
    assert result["stage_coverage"]["stages"]["plenary"]["state"] == (
        "record_found_no_member_debate"
    )
    assert result["research_pagination"] == {"complete": True, "has_more": False}


def test_top_level_section_counts_and_quality_contract_are_truthful() -> None:
    payload = {
        "bills": [{"bill_no": f"22{index:05d}"} for index in range(90)],
        "speeches": [
            {"speech_id": f"speech-{index}", "speaker": f"의원{index}", "text": "발언"}
            for index in range(90)
        ],
        "discussion_threads": [
            {
                "meeting_id": f"meeting-{index}",
                "turns": [
                    {
                        "speech_id": f"turn-{index}-{turn}",
                        "official_source": "회의록",
                        "source_locator": f"p.{turn}",
                    }
                    for turn in range(8)
                ],
            }
            for index in range(30)
        ],
        "stage_coverage": {
            "requested_stages": ["subcommittee", "standing_committee", "plenary"],
            "stages": {
                name: {"state": "discussion_found"}
                for name in ("subcommittee", "standing_committee", "plenary")
            },
        },
        "research_pagination": {"complete": True, "has_more": False},
        "quality": {"score": 100, "evidence_sufficient": True},
    }

    result = enforce_bounded_response_budget(payload, max_bytes=24_000)
    budget = result["response_budget"]

    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 24_000
    for name, observed in (("bills", 90), ("speeches", 90), ("discussion_threads", 30)):
        counts = budget["section_counts"][name]
        assert counts["observed_count"] == observed
        assert counts["returned_count"] == len(result[name])
        assert counts["truncated"] is True
    assert budget["quality_contract"] == {
        "quality_present": True,
        "quality_inputs_changed": True,
        "quality_recompute_required": True,
        "stage_inputs_preserved": True,
        "instruction": (
            "quality_recompute_required가 true이면 현재 evidence 배열 기준으로 quality를 "
            "재계산해야 합니다."
        ),
    }


@pytest.mark.parametrize("max_bytes", [2, 32, 128, 512, 1_024, 4_096])
def test_hard_cap_holds_even_for_tiny_valid_budgets(max_bytes: int) -> None:
    result = enforce_bounded_response_budget(
        {"query": "질문" * 100_000, "nested": {"value": "본문" * 100_000}},
        max_bytes=max_bytes,
    )

    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= max_bytes
