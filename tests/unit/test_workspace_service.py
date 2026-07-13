import json
from pathlib import Path

import pytest

from kasm.app import create_services
from kasm.mcp.tools import ServiceContext
from kasm.workspace.service import WorkspaceError, _official_sources, run_workspace_research


def test_workspace_uses_request_scoped_temp_dir_and_returns_no_keys() -> None:
    captured = {}

    def services_factory(**kwargs):
        captured.update(kwargs)
        captured["data_path"] = Path(kwargs["data_dir"])
        assert captured["data_path"].is_dir()
        return create_services()

    def synthesizer(provider, api_key, question, research):
        assert provider == "openai"
        assert api_key == "llm-secret"
        assert question == "국내 파운데이션 모델 논의"
        assert research["speeches"]
        return "검증된 답변", "test-model"

    result = run_workspace_research(
        question="국내 파운데이션 모델 논의",
        assembly_api_key="assembly-secret",
        llm_provider="openai",
        llm_api_key="llm-secret",
        services_factory=services_factory,
        synthesizer=synthesizer,
    )

    assert captured["api_key"] == "assembly-secret"
    assert not captured["data_path"].exists()
    serialized = json.dumps(result, ensure_ascii=False)
    assert "assembly-secret" not in serialized
    assert "llm-secret" not in serialized
    assert result["answer"] == "검증된 답변"
    assert result["answer_delivery"]["status"] == "complete"
    assert result["answer_delivery"]["partial"] is False
    assert result["answer_delivery"]["workspace_hard_limits"] == {
        "output_tokens_per_chunk": 16000,
        "chunks": 5,
    }
    assert result["evidence"]["speech_count"] > 0
    assert all(source["url"].startswith("https://") for source in result["evidence"]["sources"])


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"question": ""}, "질문은"),
        ({"assembly_api_key": ""}, "열린국회"),
        ({"llm_provider": "other"}, "OpenAI 또는 Anthropic"),
        ({"llm_api_key": ""}, "LLM API 키"),
    ],
)
def test_workspace_rejects_invalid_input(override, message) -> None:
    values = {
        "question": "질문",
        "assembly_api_key": "assembly-key",
        "llm_provider": "openai",
        "llm_api_key": "llm-key",
        **override,
    }
    with pytest.raises(WorkspaceError, match=message):
        run_workspace_research(**values)


def test_workspace_never_synthesizes_when_explicit_bill_number_is_unverified() -> None:
    synthesized = False

    def synthesizer(*_args):
        nonlocal synthesized
        synthesized = True
        return "잘못된 답변", "test-model"

    with pytest.raises(WorkspaceError, match="정확히 일치"):
        run_workspace_research(
            question="의안번호 2219564 보완수사권",
            assembly_api_key="assembly-key",
            llm_provider="openai",
            llm_api_key="llm-key",
            services_factory=lambda **_kwargs: create_services(),
            synthesizer=synthesizer,
        )

    assert synthesized is False


def test_workspace_removes_unlinked_speeches_for_an_explicit_bill_number() -> None:
    class Catalog:
        def explore_issue(self, query: str, limit: int):
            del query, limit
            return {
                "bills": [],
                "speeches": [
                    {"speech_id": "speech-exact", "speaker": "법사위원", "text": "정확 근거"},
                    {"speech_id": "speech-wrong", "speaker": "타위원", "text": "다른 법안"},
                ],
                "discussion_threads": [
                    {
                        "meeting_id": "meeting-exact",
                        "matched_speech_ids": ["speech-exact"],
                        "turns": [{"speech_id": "speech-exact", "text": "정확 문맥"}],
                    },
                    {
                        "meeting_id": "meeting-wrong",
                        "matched_speech_ids": ["speech-wrong"],
                        "turns": [{"speech_id": "speech-wrong", "text": "다른 문맥"}],
                    },
                ],
                "links": [
                    {
                        "bill_id": "kna:bill:2219564",
                        "speech_id": "speech-exact",
                    },
                    {"bill_id": "kna:bill:2299999", "speech_id": "speech-wrong"},
                ],
                "timeline": [
                    {
                        "event_type": "debate",
                        "meeting_id": "meeting-exact",
                        "title": "정확 회의",
                    },
                    {
                        "event_type": "debate",
                        "meeting_id": "meeting-wrong",
                        "title": "다른 회의",
                    },
                ],
                "quality": {"warnings": []},
            }

        def get_bill_status(self, bill_no: str):
            assert bill_no == "2219564"
            return {
                "id": "kna:bill:2219564",
                "bill_no": "2219564",
                "name": "형사소송법 일부개정법률안",
                "status": "위원회 심사",
                "documents": [],
            }

    captured: dict[str, object] = {}

    def synthesizer(provider, api_key, question, research):
        del provider, api_key, question
        captured.update(research)
        return "정확한 근거만 사용", "test-model"

    catalog = Catalog()
    result = run_workspace_research(
        question="의안번호 2219564 보완수사권 쟁점",
        assembly_api_key="assembly-key",
        llm_provider="openai",
        llm_api_key="llm-key",
        services_factory=lambda **_kwargs: ServiceContext(
            search=catalog,  # type: ignore[arg-type]
            repository=catalog,  # type: ignore[arg-type]
            catalog=catalog,  # type: ignore[arg-type]
        ),
        synthesizer=synthesizer,
    )

    assert result["answer"] == "정확한 근거만 사용"
    assert [speech["speech_id"] for speech in captured["speeches"]] == [  # type: ignore[index]
        "speech-exact"
    ]
    assert [thread["meeting_id"] for thread in captured["discussion_threads"]] == [  # type: ignore[index]
        "meeting-exact"
    ]
    assert [event["meeting_id"] for event in captured["timeline"]] == [  # type: ignore[index]
        "meeting-exact"
    ]
    assert captured["exact_bill_evidence_validation"] == {
        "requested_bill_numbers": ["2219564"],
        "unlinked_speeches_removed": 1,
        "unlinked_threads_removed": 1,
        "policy": "명시적 의안번호와 공식 연결이 증명된 발언·회의 맥락만 유지",
    }


def test_workspace_source_list_does_not_hide_inventory_after_forty_items() -> None:
    research = {
        "scope_inventory": {
            "meeting_candidates": {
                "items": [
                    {
                        "title": f"제{index}차 회의",
                        "official_url": (
                            f"https://record.assembly.go.kr/assembly/minutes/meeting-{index}.pdf"
                        ),
                        "full_text_loaded": index < 3,
                    }
                    for index in range(45)
                ]
            }
        }
    }

    sources = _official_sources(research)

    assert len(sources) == 45
    assert sources[-1]["url"].endswith("meeting-44.pdf")


def test_workspace_source_cards_preserve_full_text_and_disclose_display_truncation() -> None:
    title = "긴 의안 제목 " * 40
    detail = "긴 처리상태 설명 " * 40
    research = {
        "bills": [
            {
                "name": title,
                "status": detail,
                "official_url": "https://likms.assembly.go.kr/bill/example",
                "documents": [],
            }
        ]
    }

    source = _official_sources(research)[0]
    presentation = source["presentation"]

    assert source["title"] == title
    assert source["detail"] == detail
    assert presentation["title"].endswith("…")
    assert presentation["detail"].endswith("…")
    assert len(presentation["title"]) <= 180
    assert len(presentation["detail"]) <= 240
    assert presentation["title_truncated"] is True
    assert presentation["detail_truncated"] is True
    assert presentation["title_original_characters"] == len(title)
    assert presentation["detail_original_characters"] == len(detail)
    assert presentation["title_displayed_characters"] == len(presentation["title"])
    assert presentation["detail_displayed_characters"] == len(presentation["detail"])
    assert presentation["title_limit"] == 180
    assert presentation["detail_limit"] == 240
