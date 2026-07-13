import io
import json
import urllib.error

import pytest

from kasm.workspace.llm import LlmError, _evidence_prompt, synthesize


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
    assert captured["body"]["max_output_tokens"] == 4800
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
    assert captured["body"]["max_tokens"] == 4096
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


def test_large_evidence_is_compacted_as_valid_bounded_json(monkeypatch) -> None:
    monkeypatch.setenv("KBD_WORKSPACE_MAX_EVIDENCE_CHARS", "2000")
    evidence = {
        "bill_number_validation": {
            "requested": ["2219564"],
            "matched": ["2219564"],
            "exact_match": True,
        },
        "bills": [
            {
                "bill_no": "2219564",
                "name": "형사소송법 일부개정법률안",
                "status": "위원회 심사",
                "official_url": "https://likms.assembly.go.kr/bill/2219564",
                "documents": [
                    {
                        "title": "전문위원 검토보고서",
                        "official_url": "https://likms.assembly.go.kr/review.pdf",
                        "text_excerpt": "검토 내용 " * 5000,
                    }
                ],
            }
        ],
        "speeches": [{"speaker": "위원", "text": "발언 " * 5000}] * 20,
        "discussion_threads": [],
    }

    prompt = _evidence_prompt("보완수사권 쟁점", evidence)
    serialized = prompt.split("<official_research_data>\n", 1)[1].split(
        "\n</official_research_data>", 1
    )[0]
    compact = json.loads(serialized)

    assert len(serialized) <= 2000
    assert compact["bill_number_validation"]["exact_match"] is True
    assert compact["bills"][0]["bill_no"] == "2219564"


def test_openai_partial_answer_is_not_returned() -> None:
    def opener(request, timeout):
        del request, timeout
        return FakeResponse(
            {
                "output_text": "전문위원 검토사항에서 잘린 답변",
                "incomplete_details": {"reason": "max_output_tokens"},
            }
        )

    with pytest.raises(LlmError, match="부분 답변은 표시하지 않았습니다"):
        synthesize("openai", "private-key", "질문", {}, opener=opener)


def test_anthropic_partial_answer_is_not_returned() -> None:
    def opener(request, timeout):
        del timeout
        if "/v1/models" in request.full_url:
            return FakeResponse({"data": [{"id": "claude-sonnet-test"}]})
        return FakeResponse(
            {
                "content": [{"type": "text", "text": "전문위원 검토사항에서 잘린 답변"}],
                "stop_reason": "max_tokens",
            }
        )

    with pytest.raises(LlmError, match="부분 답변은 표시하지 않았습니다"):
        synthesize("anthropic", "private-key", "질문", {}, opener=opener)
