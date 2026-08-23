from kasm.app import create_services, infer_bill_title_query, infer_issue_committee
from kasm.core.quality import issue_quality
from kasm.mcp.tools import KasmTools


def _complete_evidence_payload() -> dict:
    turns = [
        {
            "speaker": "김의원",
            "official_source": "https://example.test/minutes",
            "source_locator": "p. 1",
        }
    ]
    return {
        "bills": [{"bill_no": "2200001"}],
        "speeches": [
            {"speaker": "김철수"},
            {"speaker": "박영희"},
            {"speaker": "이민수"},
        ],
        "discussion_threads": [{"turns": turns}],
    }


def test_issue_research_reports_evidence_depth_and_provenance() -> None:
    result = KasmTools(create_services()).explore_issue("인공지능")
    quality = result["quality"]
    assert quality["score"] == 100
    assert quality["evidence_sufficient"] is True
    assert quality["bill_coverage"] is True
    assert quality["speech_matches"] == 3
    assert quality["context_turns"] == 3
    assert quality["provenance_rate"] == 1.0
    assert quality["warnings"] == []
    speech = result["speeches"][0]
    assert speech["citation"]["official_url"] == speech["official_source"]
    turn = result["discussion_threads"][0]["turns"][0]
    assert turn["citation"]["source_locator"] == turn["source_locator"]
    assert [event["event_type"] for event in result["timeline"]] == [
        "bill_proposed",
        "debate",
    ]
    assert all(event["official_url"] for event in result["timeline"])


def test_issue_quality_accepts_checked_stage_outcomes() -> None:
    payload = _complete_evidence_payload()
    payload["stage_coverage"] = {
        "requested_stages": ["subcommittee", "standing_committee", "plenary"],
        "stages": {
            "subcommittee": {"state": "discussion_found", "speech_count": 3},
            "standing_committee": {
                "state": "record_found_no_member_debate",
                "record_count": 1,
            },
            "plenary": {
                "state": "checked_no_matching_discussion",
                "record_count": 2,
            },
        },
    }

    quality = issue_quality(payload)

    assert quality["score"] == 100
    assert quality["evidence_sufficient"] is True
    assert quality["coverage_completeness"] == {
        "complete": True,
        "stage_coverage_provided": True,
        "stages_complete": True,
        "requested_stages": ["subcommittee", "standing_committee", "plenary"],
        "complete_stages": ["subcommittee", "standing_committee", "plenary"],
        "incomplete_stages": [],
        "missing_stages": [],
        "pending_stages": [],
        "not_checked_stages": [],
        "failed_stages": [],
        "unknown_stages": [],
        "stage_states": {
            "subcommittee": "discussion_found",
            "standing_committee": "record_found_no_member_debate",
            "plenary": "checked_no_matching_discussion",
        },
        "research_pagination_provided": False,
        "research_pagination_complete": None,
        "research_has_more": False,
    }
    assert quality["warnings"] == []


def test_issue_quality_rejects_missing_pending_unchecked_and_failed_stages() -> None:
    payload = _complete_evidence_payload()
    payload["stage_coverage"] = {
        "requested_stages": ["missing", "pending", "unchecked", "failed"],
        "stages": {
            "pending": {"state": "metadata_found_text_pending", "record_count": 1},
            "unchecked": {"state": "not_checked"},
            "failed": {"state": "failed", "failure_count": 1},
        },
    }

    quality = issue_quality(payload)
    coverage = quality["coverage_completeness"]

    assert quality["score"] < 100
    assert quality["evidence_sufficient"] is False
    assert coverage["complete"] is False
    assert coverage["incomplete_stages"] == [
        "missing",
        "pending",
        "unchecked",
        "failed",
    ]
    assert coverage["missing_stages"] == ["missing"]
    assert coverage["pending_stages"] == ["pending"]
    assert coverage["not_checked_stages"] == ["unchecked"]
    assert coverage["failed_stages"] == ["failed"]
    assert len(quality["warnings"]) == 4


def test_issue_quality_rejects_incomplete_or_has_more_research_pagination() -> None:
    incomplete_payload = _complete_evidence_payload()
    incomplete_payload["research_pagination"] = {"complete": False}
    incomplete = issue_quality(incomplete_payload)

    assert incomplete["score"] < 100
    assert incomplete["evidence_sufficient"] is False
    assert incomplete["coverage_completeness"]["complete"] is False
    assert incomplete["coverage_completeness"]["research_pagination_complete"] is False
    assert incomplete["warnings"] == [
        "요청 범위의 회의록 조사가 완료되지 않았습니다."
    ]

    has_more_payload = _complete_evidence_payload()
    has_more_payload["research_pagination"] = {
        "complete": True,
        "has_more": True,
        "next_minutes_offset": 2,
    }
    has_more = issue_quality(has_more_payload)

    assert has_more["score"] < 100
    assert has_more["evidence_sufficient"] is False
    assert has_more["coverage_completeness"]["research_has_more"] is True
    assert has_more["warnings"] == [
        "회의록 페이지가 더 남아 있어 요청 범위 조사가 완료되지 않았습니다."
    ]


def test_english_issue_research_preserves_request_and_uses_korean_evidence_query() -> None:
    result = KasmTools(create_services()).explore_issue(
        "How is the AI ecosystem bill evolving?",
        korean_query="인공지능 생태계",
    )

    assert result["query"] == "How is the AI ecosystem bill evolving?"
    assert result["query_language"] == "en"
    assert result["search_query_ko"] == "인공지능 생태계"
    assert result["query_translation"] == "client_supplied"
    assert result["source_language"] == "ko"
    assert result["bills"]
    assert result["speeches"]


def test_high_signal_topic_routes_to_relevant_committee() -> None:
    assert infer_issue_committee("검찰 보완수사권 폐지 논의") == "법제사법위원회"
    assert infer_issue_committee("국세청 세무조사 운영") == "재정경제기획위원회"
    assert infer_issue_committee("인공지능 산업과 국내 AI 생태계") == "과학기술정보방송통신위원회"
    assert infer_issue_committee("AI 대전환 입법 동력에 대한 과방위 의원 의견") == (
        "과학기술정보방송통신위원회"
    )
    assert infer_issue_committee("AI 기본법과 디지털 포용법 논의") == (
        "과학기술정보방송통신위원회"
    )
    assert infer_issue_committee("방송 개혁과 미디어 환경에 대한 의원 의견") == (
        "과학기술정보방송통신위원회"
    )
    assert infer_issue_committee("K-컬처와 문화예술 지원에 대한 문체위 의견") == (
        "문화체육관광위원회"
    )
    assert infer_issue_committee("공정한 시장질서와 자영업자 보호에 대한 정무위 논의") == (
        "정무위원회"
    )
    assert infer_issue_committee("일반적인 정책 의견") is None
    assert infer_bill_title_query("보완수사 요구가 작동하는가") == "형사소송법"
    assert infer_bill_title_query("일반적인 정책 의견") is None
