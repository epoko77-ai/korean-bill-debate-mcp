import io
import json
import urllib.error

import pytest

from kasm.workspace.llm import LlmError, synthesize


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


def test_openai_synthesis_uses_responses_api_without_storage(monkeypatch) -> None:
    monkeypatch.setenv("KBD_OPENAI_MODEL", "test-openai-model")
    captured = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeResponse(
            {"output": [{"content": [{"type": "output_text", "text": "공식 근거 답변"}]}]}
        )

    answer, model = synthesize(
        "openai", "private-openai-key", "질문", {"bills": []}, opener=opener
    )

    assert answer == "공식 근거 답변"
    assert model == "test-openai-model"
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["headers"]["Authorization"] == "Bearer private-openai-key"
    assert captured["body"]["store"] is False
    assert "private-openai-key" not in json.dumps(captured["body"])


def test_anthropic_synthesis_uses_messages_api(monkeypatch) -> None:
    monkeypatch.setenv("KBD_ANTHROPIC_MODEL", "test-claude-model")
    captured = {}

    def opener(request, timeout):
        if "/v1/models" in request.full_url:
            return FakeResponse({"data": [{"id": "test-claude-model"}]})
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeResponse({"content": [{"type": "text", "text": "클로드 답변"}]})

    answer, model = synthesize(
        "anthropic", "private-anthropic-key", "질문", {"speeches": []}, opener=opener
    )

    assert answer == "클로드 답변"
    assert model == "test-claude-model"
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["X-api-key"] == "private-anthropic-key"
    assert captured["headers"]["Anthropic-version"] == "2023-06-01"
    assert "private-anthropic-key" not in json.dumps(captured["body"])


def test_anthropic_chooses_an_available_sonnet_model(monkeypatch) -> None:
    monkeypatch.setenv("KBD_ANTHROPIC_MODEL", "unavailable-preferred-model")
    sent = {}

    def opener(request, timeout):
        del timeout
        if "/v1/models" in request.full_url:
            return FakeResponse(
                {"data": [{"id": "claude-sonnet-account-model"}, {"id": "claude-haiku"}]}
            )
        sent.update(json.loads(request.data))
        return FakeResponse({"content": [{"type": "text", "text": "자동 선택 성공"}]})

    answer, model = synthesize(
        "anthropic", "private-anthropic-key", "질문", {}, opener=opener
    )

    assert answer == "자동 선택 성공"
    assert model == "claude-sonnet-account-model"
    assert sent["model"] == "claude-sonnet-account-model"


def test_anthropic_credit_error_is_actionable_and_safe() -> None:
    secret = "must-never-appear"

    def opener(request, timeout):
        del timeout
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            {},
            io.BytesIO(
                b'{"error":{"type":"invalid_request_error",'
                b'"message":"Your credit balance is too low. Purchase credits in Billing."}}'
            ),
        )

    with pytest.raises(LlmError, match="크레딧 잔액") as error:
        synthesize("anthropic", secret, "질문", {}, opener=opener)

    assert secret not in str(error.value)


def test_provider_auth_error_never_echoes_key() -> None:
    secret = "must-never-appear"

    def opener(request, timeout):
        del timeout
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"error":"bad key must-never-appear"}'),
        )

    with pytest.raises(LlmError) as error:
        synthesize("openai", secret, "질문", {}, opener=opener)

    assert "키 또는 프로젝트 권한" in str(error.value)
    assert secret not in str(error.value)


def test_unknown_provider_is_rejected_before_network() -> None:
    with pytest.raises(LlmError, match="지원하지 않는"):
        synthesize("unknown", "key", "질문", {})
