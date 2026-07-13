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
    assert captured["body"]["max_output_tokens"] == 8000
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
    assert captured["body"]["max_tokens"] == 8000
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


def test_large_evidence_is_forwarded_without_compaction() -> None:
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
    forwarded = json.loads(serialized)

    assert forwarded == evidence
    assert len(forwarded["bills"][0]["documents"][0]["text_excerpt"]) > 10000


def test_evidence_fields_are_not_compacted() -> None:
    evidence = {
        "bills": [{"bill_no": "2219564", "custom_field": "원본 필드 유지"}],
        "speeches": [{"text": "상세 발언", "matched_terms": ["보완수사권"]}],
    }

    prompt = _evidence_prompt("질문", evidence)
    serialized = prompt.split("<official_research_data>\n", 1)[1].split(
        "\n</official_research_data>", 1
    )[0]

    assert json.loads(serialized) == evidence
    assert "evidence_compacted" not in serialized


def test_openai_answer_continues_after_output_limit() -> None:
    calls = 0

    def opener(request, timeout):
        nonlocal calls
        del request, timeout
        calls += 1
        if calls == 1:
            return FakeResponse(
                {
                    "output_text": "전문위원 검토사항 첫 부분",
                    "incomplete_details": {"reason": "max_output_tokens"},
                }
            )
        return FakeResponse({"output_text": "나머지 검토와 공식 원문"})

    answer, _model = synthesize("openai", "private-key", "질문", {}, opener=opener)

    assert answer == "전문위원 검토사항 첫 부분\n\n나머지 검토와 공식 원문"
    assert calls == 2


def test_anthropic_answer_continues_after_output_limit() -> None:
    message_calls = 0

    def opener(request, timeout):
        nonlocal message_calls
        del timeout
        if "/v1/models" in request.full_url:
            return FakeResponse({"data": [{"id": "claude-sonnet-test"}]})
        message_calls += 1
        if message_calls == 2:
            return FakeResponse(
                {
                    "content": [{"type": "text", "text": "나머지 검토와 공식 원문"}],
                    "stop_reason": "end_turn",
                }
            )
        return FakeResponse(
            {
                "content": [{"type": "text", "text": "전문위원 검토사항 첫 부분"}],
                "stop_reason": "max_tokens",
            }
        )

    answer, _model = synthesize("anthropic", "private-key", "질문", {}, opener=opener)

    assert answer == "전문위원 검토사항 첫 부분\n\n나머지 검토와 공식 원문"
    assert message_calls == 2
