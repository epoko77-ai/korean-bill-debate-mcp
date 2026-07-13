import json
from pathlib import Path

import pytest

from kasm.app import create_services
from kasm.workspace.service import WorkspaceError, run_workspace_research


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
