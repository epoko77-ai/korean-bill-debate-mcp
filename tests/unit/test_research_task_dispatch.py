from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from typing import Any

import pytest

from kasm.research import task_dispatch as dispatch_module
from kasm.research.queue import ResearchTask, ResearchTaskStage
from kasm.research.task_dispatch import (
    ResearchTaskDispatchASGI,
    ResearchTaskDispatcher,
)

_SECRET = "dispatch-secret-with-at-least-32-bytes"
_CAPABILITY = "opaque-capability-that-must-never-be-returned-000000"
_BODY_MARKER = "private-task-body-marker"


class _Engine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ResearchTask]] = []
        self.failures: list[tuple[ResearchTask, str]] = []
        self.failure: Exception | None = None

    def _record(self, stage: str, task: ResearchTask) -> object:
        self.calls.append((stage, task))
        if self.failure is not None:
            raise self.failure
        return object()

    def process_metadata_task(self, task: ResearchTask) -> object:
        return self._record("metadata", task)

    def process_document_task(self, task: ResearchTask) -> object:
        return self._record("document", task)

    def process_finalize_task(self, task: ResearchTask) -> object:
        return self._record("finalize", task)

    def fail_task(self, task: ResearchTask, *, error_code: str) -> object:
        self.failures.append((task, error_code))
        return object()


def _task(stage: ResearchTaskStage) -> ResearchTask:
    return ResearchTask(
        research_id="research_dispatch_test",
        stage=stage,
        work_id=f"work_{stage.value}",
        query_fingerprint="a" * 64,
        index_revision="index-test",
        payload=(("topic", _BODY_MARKER),),
        credential_capability=_CAPABILITY,
    )


def _request(
    app: ResearchTaskDispatchASGI,
    body: bytes,
    *,
    secret: str = _SECRET,
    extra_headers: Iterable[tuple[bytes, bytes]] = (),
    chunks: tuple[bytes, ...] | None = None,
) -> tuple[int, dict[str, Any], list[tuple[bytes, bytes]]]:
    messages = list(chunks or (body,))
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        chunk = messages.pop(0)
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": bool(messages),
        }

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    headers = [
        (b"content-type", b"application/json"),
        (b"x-kbd-internal-secret", secret.encode()),
        *extra_headers,
    ]
    asyncio.run(
        app(
            {
                "type": "http",
                "method": "POST",
                "path": "/_internal/research/dispatch",
                "headers": headers,
            },
            receive,
            send,
        )
    )
    start = next(item for item in sent if item["type"] == "http.response.start")
    response_body = b"".join(
        item.get("body", b"")
        for item in sent
        if item["type"] == "http.response.body"
    )
    return start["status"], json.loads(response_body), start["headers"]


@pytest.mark.parametrize(
    ("stage", "expected_method"),
    [
        (ResearchTaskStage.COLLECT_METADATA, "metadata"),
        (ResearchTaskStage.HYDRATE_DOCUMENT, "document"),
        (ResearchTaskStage.FINALIZE, "finalize"),
    ],
)
def test_valid_task_is_schema_decoded_and_dispatched_by_stage(
    stage: ResearchTaskStage,
    expected_method: str,
) -> None:
    engine = _Engine()
    app = ResearchTaskDispatchASGI(ResearchTaskDispatcher(engine), secret=_SECRET)
    body = json.dumps(_task(stage).to_queue_payload()).encode()

    status, response, headers = _request(app, body)

    assert status == 200
    assert response == {"ok": True, "stage": stage.value}
    assert engine.calls == [(expected_method, _task(stage))]
    assert _CAPABILITY not in json.dumps(response)
    assert _BODY_MARKER not in json.dumps(response)
    assert (b"cache-control", b"no-store") in headers


def test_secret_is_compared_with_constant_time_primitive(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _Engine()
    app = ResearchTaskDispatchASGI(ResearchTaskDispatcher(engine), secret=_SECRET)
    compared: list[tuple[bytes, bytes]] = []

    def compare_digest(left: bytes, right: bytes) -> bool:
        compared.append((left, right))
        return False

    monkeypatch.setattr(dispatch_module.hmac, "compare_digest", compare_digest)
    body = json.dumps(_task(ResearchTaskStage.FINALIZE).to_queue_payload()).encode()

    status, response, _headers = _request(app, body, secret="x" * 32)

    assert status == 401
    assert response == {"ok": False, "error": "unauthorized"}
    assert compared == [(b"x" * 32, _SECRET.encode())]
    assert engine.calls == []


def test_invalid_schema_and_oversized_stream_are_rejected_before_dispatch() -> None:
    engine = _Engine()
    app = ResearchTaskDispatchASGI(
        ResearchTaskDispatcher(engine), secret=_SECRET, max_request_bytes=1024
    )

    invalid_status, invalid_response, _ = _request(
        app, json.dumps({"schema_version": 99, "credential_capability": _CAPABILITY}).encode()
    )
    large_status, large_response, _ = _request(
        app,
        b"",
        chunks=(b"{" + b"x" * 800, b"y" * 800 + b"}"),
    )

    assert (invalid_status, invalid_response) == (
        400,
        {"ok": False, "error": "invalid_task"},
    )
    assert (large_status, large_response) == (
        413,
        {"ok": False, "error": "request_too_large"},
    )
    assert _CAPABILITY not in json.dumps(invalid_response)
    assert engine.calls == []


def test_engine_failure_is_sanitized_non_2xx_then_same_delivery_can_retry() -> None:
    engine = _Engine()
    engine.failure = RuntimeError(
        f"upstream failed with {_CAPABILITY} and {_BODY_MARKER}"
    )
    app = ResearchTaskDispatchASGI(ResearchTaskDispatcher(engine), secret=_SECRET)
    body = json.dumps(_task(ResearchTaskStage.HYDRATE_DOCUMENT).to_queue_payload()).encode()

    failed_status, failed_response, _ = _request(app, body)
    engine.failure = None
    retry_status, retry_response, _ = _request(app, body)

    assert failed_status == 503
    assert failed_response == {"ok": False, "error": "dispatch_failed"}
    assert _CAPABILITY not in json.dumps(failed_response)
    assert _BODY_MARKER not in json.dumps(failed_response)
    assert retry_status == 200
    assert retry_response == {"ok": True, "stage": "hydrate_document"}
    assert [method for method, _task_value in engine.calls] == ["document", "document"]


def test_final_failed_delivery_is_recorded_and_acknowledged() -> None:
    engine = _Engine()
    engine.failure = RuntimeError(f"persistent {_CAPABILITY} {_BODY_MARKER}")
    app = ResearchTaskDispatchASGI(ResearchTaskDispatcher(engine), secret=_SECRET)
    task = _task(ResearchTaskStage.COLLECT_METADATA)
    body = json.dumps(task.to_queue_payload()).encode()

    status, response, _headers = _request(
        app,
        body,
        extra_headers=((b"x-kbd-delivery-count", b"10"),),
    )

    assert status == 200
    assert response == {"ok": True, "stage": "collect_metadata"}
    assert engine.failures == [(task, "task_retry_budget_exhausted")]
    assert _CAPABILITY not in json.dumps(response)
    assert _BODY_MARKER not in json.dumps(response)


@pytest.mark.parametrize("value", (b"0", b"not-a-number", b"1000001"))
def test_invalid_delivery_count_is_rejected_before_dispatch(value: bytes) -> None:
    engine = _Engine()
    app = ResearchTaskDispatchASGI(ResearchTaskDispatcher(engine), secret=_SECRET)

    status, response, _headers = _request(
        app,
        json.dumps(_task(ResearchTaskStage.FINALIZE).to_queue_payload()).encode(),
        extra_headers=((b"x-kbd-delivery-count", value),),
    )

    assert status == 400
    assert response == {"ok": False, "error": "invalid_delivery_count"}
    assert engine.calls == []


def test_missing_configuration_fails_closed_without_reading_task() -> None:
    app = ResearchTaskDispatchASGI(None, secret="")

    status, response, _headers = _request(app, _CAPABILITY.encode())

    assert status == 503
    assert response == {"ok": False, "error": "dispatcher_unavailable"}
    assert _CAPABILITY not in json.dumps(response)
