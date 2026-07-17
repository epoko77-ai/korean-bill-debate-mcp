import base64
import json
import sys
import types
import urllib.request

import pytest

from kasm.research.queue import (
    CONTROL_WORK_KINDS,
    LeasedResearchTask,
    ResearchTask,
    ResearchTaskStage,
    VercelResearchTaskQueue,
    _default_oidc_token,
    _HttpResponse,
)


def _task(
    *,
    capability: str | None = None,
    work_kind: str | None = None,
    queue_lane: str | None = None,
) -> ResearchTask:
    payload: tuple[tuple[str, str | int], ...] = (
        ("assembly_term", 22),
        ("query", "최근 AI 입법"),
    )
    if work_kind is not None:
        payload += (("work_kind", work_kind),)
    if queue_lane is not None:
        payload += (("queue_lane", queue_lane),)
    return ResearchTask(
        research_id="research_123",
        stage=ResearchTaskStage.COLLECT_METADATA,
        work_id="scope",
        query_fingerprint="a" * 64,
        index_revision="index-1",
        payload=payload,
        credential_capability=capability,
    )


def test_task_public_payload_never_exposes_encrypted_capability() -> None:
    capability = "g" * 120
    task = _task(capability=capability)

    assert task.to_queue_payload()["credential_capability"] == capability
    public = task.public_payload()
    assert public["has_credential_capability"] is True
    assert capability not in json.dumps(public)
    assert "credential_capability" not in public


def test_task_round_trip_and_idempotency_ignore_credential_rotation() -> None:
    first = _task(capability="a" * 120)
    second = _task(capability="b" * 120)

    assert ResearchTask.from_queue_payload(first.to_queue_payload()) == first
    assert first.idempotency_key == second.idempotency_key


def test_publish_uses_oidc_and_idempotency_without_leaking_token() -> None:
    requests: list[urllib.request.Request] = []

    def transport(request: urllib.request.Request, timeout: float) -> _HttpResponse:
        assert timeout == 2.0
        requests.append(request)
        return _HttpResponse(201, {}, b'{"messageId":"msg_1"}')

    queue = VercelResearchTaskQueue(
        region="icn1",
        oidc_token_provider=lambda: "secret-oidc",
        deployment_id_provider=lambda: "dpl_current",
        timeout=2.0,
        transport=transport,
    )
    message_id = queue.publish(_task(), retention_seconds=3600, delay_seconds=15)

    assert message_id == "msg_1"
    request = requests[0]
    assert request.full_url == "https://icn1.vercel-queue.com/api/v3/topic/kbd-research"
    assert request.get_header("Authorization") == "Bearer secret-oidc"
    assert request.get_header("Vqs-delay-seconds") == "15"
    assert request.get_header("Vqs-idempotency-key") == _task().idempotency_key
    assert request.get_header("Vqs-deployment-id") == "dpl_current"
    assert b"secret-oidc" not in (request.data or b"")


def test_publish_routes_only_interactive_coordinators_and_barriers_to_control_topic() -> None:
    requests: list[urllib.request.Request] = []

    def transport(request: urllib.request.Request, _timeout: float) -> _HttpResponse:
        requests.append(request)
        return _HttpResponse(201, {}, b'{"messageId":"msg_1"}')

    queue = VercelResearchTaskQueue(
        topic="kbd-research",
        control_topic="kbd-research-control",
        bulk_topic="kbd-research-bulk",
        oidc_token_provider=lambda: "oidc",
        deployment_id_provider=lambda: "dpl_current",
        transport=transport,
    )

    for work_kind in sorted(CONTROL_WORK_KINDS):
        task = _task(work_kind=work_kind, queue_lane="interactive")
        queue.publish(task)
        request = requests[-1]
        assert request.full_url.endswith("/topic/kbd-research-control")
        assert request.get_header("Vqs-idempotency-key") == task.idempotency_key
        assert request.get_header("Vqs-deployment-id") == "dpl_current"

    for work_kind in ("metadata_page", "bill_documents", "document", "unknown"):
        queue.publish(_task(work_kind=work_kind))
        assert requests[-1].full_url.endswith("/topic/kbd-research")

    queue.publish(_task())
    assert requests[-1].full_url.endswith("/topic/kbd-research")


def test_publish_routes_every_explicit_bulk_task_to_bulk_topic() -> None:
    requests: list[urllib.request.Request] = []
    queue = VercelResearchTaskQueue(
        topic="kbd-research",
        control_topic="kbd-research-control",
        bulk_topic="kbd-research-bulk",
        oidc_token_provider=lambda: "oidc",
        deployment_id_provider=lambda: "dpl_current",
        transport=lambda request, _timeout: (
            requests.append(request) or _HttpResponse(201, {}, b'{"messageId":"msg_1"}')
        ),
    )

    bulk_task = _task(work_kind="metadata_page", queue_lane="bulk")
    queue.publish(bulk_task)
    assert requests[-1].full_url.endswith("/topic/kbd-research-bulk")
    assert requests[-1].get_header("Vqs-idempotency-key") == bulk_task.idempotency_key
    assert requests[-1].get_header("Vqs-deployment-id") == "dpl_current"

    bulk_control = _task(work_kind="phase_barrier", queue_lane="bulk")
    queue.publish(bulk_control)
    assert requests[-1].full_url.endswith("/topic/kbd-research-bulk")

    queue.publish(_task(work_kind="document", queue_lane="interactive"))
    assert requests[-1].full_url.endswith("/topic/kbd-research")

    queue.publish(_task(work_kind="bill_documents", queue_lane="unknown"))
    assert requests[-1].full_url.endswith("/topic/kbd-research")


@pytest.mark.parametrize(
    ("topic", "control_topic", "bulk_topic"),
    [
        ("kbd-research", "kbd-research", "kbd-research-bulk"),
        ("kbd-research", "kbd-research-control", "kbd-research"),
        ("kbd-research", "kbd-research-control", "kbd-research-control"),
    ],
)
def test_queue_topics_must_be_valid_and_mutually_distinct(
    topic: str,
    control_topic: str,
    bulk_topic: str,
) -> None:
    with pytest.raises(ValueError, match="distinct"):
        VercelResearchTaskQueue(
            topic=topic,
            control_topic=control_topic,
            bulk_topic=bulk_topic,
        )


@pytest.mark.parametrize(
    ("topic", "control_topic", "bulk_topic"),
    [
        ("invalid/topic", None, None),
        ("kbd-research", "", None),
        ("kbd-research", None, "topic with spaces"),
    ],
)
def test_every_configured_queue_topic_must_be_valid(
    topic: str,
    control_topic: str | None,
    bulk_topic: str | None,
) -> None:
    with pytest.raises(ValueError, match="invalid"):
        VercelResearchTaskQueue(
            topic=topic,
            control_topic=control_topic,
            bulk_topic=bulk_topic,
        )


def test_queue_omits_deployment_partition_only_when_not_configured() -> None:
    requests: list[urllib.request.Request] = []
    queue = VercelResearchTaskQueue(
        oidc_token_provider=lambda: "oidc",
        deployment_id_provider=lambda: None,
        transport=lambda request, _timeout: (
            requests.append(request) or _HttpResponse(201, {}, b'{"messageId":"msg_1"}')
        ),
    )

    queue.publish(_task())

    assert requests[0].get_header("Vqs-deployment-id") is None


def test_receive_decodes_ndjson_and_acknowledges_opaque_receipt() -> None:
    calls: list[tuple[str, str]] = []
    task_body = json.dumps(_task().to_queue_payload()).encode()
    line = json.dumps(
        {
            "messageId": "msg_1",
            "receiptHandle": "receipt/opaque+value",
            "deliveryCount": 2,
            "body": base64.b64encode(task_body).decode(),
        }
    ).encode()

    def transport(request: urllib.request.Request, _timeout: float) -> _HttpResponse:
        calls.append((request.method, request.full_url))
        if request.method == "POST":
            return _HttpResponse(200, {}, line + b"\n")
        return _HttpResponse(204, {}, b"")

    queue = VercelResearchTaskQueue(
        oidc_token_provider=lambda: "oidc",
        transport=transport,
    )
    received = queue.receive(max_messages=3, visibility_timeout_seconds=600)

    assert received == (LeasedResearchTask("msg_1", "receipt/opaque+value", 2, _task()),)
    queue.acknowledge(received[0].receipt_handle)
    assert calls[1][0] == "DELETE"
    assert calls[1][1].endswith("/lease/receipt%2Fopaque%2Bvalue")


def test_empty_queue_and_deferred_publish_are_explicit() -> None:
    responses = iter(
        (
            _HttpResponse(204, {}, b""),
            _HttpResponse(202, {}, b""),
        )
    )
    queue = VercelResearchTaskQueue(
        oidc_token_provider=lambda: "oidc",
        transport=lambda _request, _timeout: next(responses),
    )

    assert queue.receive() == ()
    assert queue.publish(_task()).startswith("deferred:")


def test_queue_errors_never_echo_upstream_body_or_token() -> None:
    queue = VercelResearchTaskQueue(
        oidc_token_provider=lambda: "super-secret",
        transport=lambda _request, _timeout: _HttpResponse(
            401, {}, b"echo super-secret and payload"
        ),
    )

    with pytest.raises(RuntimeError) as error:
        queue.publish(_task())
    assert "super-secret" not in str(error.value)
    assert "payload" not in str(error.value)


def test_default_oidc_provider_prefers_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "environment-token")

    assert _default_oidc_token() == "environment-token"


def test_default_oidc_provider_uses_official_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
    package = types.ModuleType("vercel")
    oidc = types.ModuleType("vercel.oidc")
    oidc.get_vercel_oidc_token_sync = lambda: "sdk-token"  # type: ignore[attr-defined]
    package.oidc = oidc  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "vercel", package)
    monkeypatch.setitem(sys.modules, "vercel.oidc", oidc)

    assert _default_oidc_token() == "sdk-token"


@pytest.mark.parametrize("retention", [0, 59, 604_801])
def test_queue_bounds_retention(retention: int) -> None:
    queue = VercelResearchTaskQueue(oidc_token_provider=lambda: "oidc")
    with pytest.raises(ValueError, match="retention"):
        queue.publish(_task(), retention_seconds=retention)


@pytest.mark.parametrize("delay", [-1, 3601])
def test_queue_bounds_delay_by_retention(delay: int) -> None:
    queue = VercelResearchTaskQueue(oidc_token_provider=lambda: "oidc")
    with pytest.raises(ValueError, match="delay"):
        queue.publish(_task(), retention_seconds=3600, delay_seconds=delay)
