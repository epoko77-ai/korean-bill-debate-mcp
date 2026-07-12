from kasm.app import create_services, infer_bill_title_query, infer_issue_committee
from kasm.mcp.tools import KasmTools


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
