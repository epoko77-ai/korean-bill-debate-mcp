"""Private, bounded ASGI boundary for durable research queue deliveries.

The public queue consumer is a tiny TypeScript bridge.  It forwards one queue
message to this route on the *same Vercel deployment*.  This module deliberately
returns only a small receipt: queued credentials and task bodies never appear in
responses or exception messages produced by this boundary.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol

from kasm.vercel_context import bind_vercel_oidc_token

from .queue import ResearchTask, ResearchTaskStage

INTERNAL_DISPATCH_PATH = "/_internal/research/dispatch"
INTERNAL_DISPATCH_SECRET_HEADER = b"x-kbd-internal-secret"
INTERNAL_DISPATCH_DELIVERY_COUNT_HEADER = b"x-kbd-delivery-count"
MAX_RESEARCH_TASK_BYTES = 64 * 1024
MAX_RESEARCH_TASK_DELIVERIES = 10
_LOGGER = logging.getLogger(__name__)
_SAFE_WORK_KINDS = frozenset(
    {
        "bill_documents",
        "deferred_fanout",
        "discovery_fanout",
        "document",
        "document_fanout",
        "metadata_page",
        "page_fanout",
    }
)


class ResearchTaskEngine(Protocol):
    """The narrow engine surface a queue delivery is allowed to invoke."""

    def process_metadata_task(self, task: ResearchTask) -> object: ...

    def process_document_task(self, task: ResearchTask) -> object: ...

    def process_finalize_task(self, task: ResearchTask) -> object: ...

    def fail_task(self, task: ResearchTask, *, error_code: str) -> object: ...


@dataclass(frozen=True, slots=True)
class ResearchTaskDispatcher:
    """Dispatch a validated task to exactly one stage-specific engine method."""

    engine: ResearchTaskEngine

    def dispatch(self, task: ResearchTask) -> None:
        if task.stage is ResearchTaskStage.COLLECT_METADATA:
            self.engine.process_metadata_task(task)
            return
        if task.stage is ResearchTaskStage.HYDRATE_DOCUMENT:
            self.engine.process_document_task(task)
            return
        if task.stage is ResearchTaskStage.FINALIZE:
            self.engine.process_finalize_task(task)
            return
        raise ValueError("unsupported research task stage")  # pragma: no cover

    def fail(self, task: ResearchTask, *, error_code: str) -> None:
        self.engine.fail_task(task, error_code=error_code)


class ResearchTaskDispatchASGI:
    """Shared-secret-protected ASGI endpoint used only by the queue bridge."""

    def __init__(
        self,
        dispatcher: ResearchTaskDispatcher | None,
        *,
        secret: str | None = None,
        max_request_bytes: int = MAX_RESEARCH_TASK_BYTES,
    ) -> None:
        if max_request_bytes < 1024:
            raise ValueError("max_request_bytes must be at least 1024")
        configured_secret = (
            secret if secret is not None else os.getenv("KBD_INTERNAL_TASK_SECRET", "")
        )
        if configured_secret:
            try:
                secret_bytes = configured_secret.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError("internal dispatch secret must be ASCII") from exc
            if not 32 <= len(secret_bytes) <= 512 or any(
                not 33 <= character <= 126 for character in secret_bytes
            ):
                raise ValueError(
                    "internal dispatch secret must be 32-512 printable ASCII bytes"
                )
        else:
            secret_bytes = b""
        self.dispatcher = dispatcher
        self._secret = secret_bytes
        self.max_request_bytes = max_request_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await _json_response(send, 404, "not_found")
            return
        if str(scope.get("method") or "").upper() != "POST":
            await _json_response(send, 405, "method_not_allowed")
            return
        if not self._secret or self.dispatcher is None:
            await _json_response(send, 503, "dispatcher_unavailable")
            return

        headers = _headers(scope)
        provided = headers.get(INTERNAL_DISPATCH_SECRET_HEADER, b"")
        if not hmac.compare_digest(provided, self._secret):
            await _json_response(send, 401, "unauthorized")
            return
        content_type = headers.get(b"content-type", b"").partition(b";")[0].strip().lower()
        if content_type != b"application/json":
            await _json_response(send, 415, "unsupported_media_type")
            return
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                await _json_response(send, 400, "invalid_request")
                return
            if declared_size < 0:
                await _json_response(send, 400, "invalid_request")
                return
            if declared_size > self.max_request_bytes:
                await _json_response(send, 413, "request_too_large")
                return

        body = await _read_bounded_body(receive, self.max_request_bytes)
        if body is None:
            await _json_response(send, 413, "request_too_large")
            return
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("research task must be an object")
            task = ResearchTask.from_queue_payload(payload)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            await _json_response(send, 400, "invalid_task")
            return

        try:
            delivery_count = _delivery_count(headers)
        except ValueError:
            await _json_response(send, 400, "invalid_delivery_count")
            return
        oidc_token = headers.get(b"x-vercel-oidc-token", b"").decode("latin-1")
        try:
            with bind_vercel_oidc_token(oidc_token):
                await asyncio.to_thread(self.dispatcher.dispatch, task)
        except Exception as exc:
            _log_dispatch_failure(exc, task, delivery_count)
            if delivery_count >= MAX_RESEARCH_TASK_DELIVERIES:
                try:
                    with bind_vercel_oidc_token(oidc_token):
                        await asyncio.to_thread(
                            self.dispatcher.fail,
                            task,
                            error_code="task_retry_budget_exhausted",
                        )
                except Exception as terminal_exc:
                    _log_dispatch_failure(terminal_exc, task, delivery_count)
                    await _json_response(send, 503, "dispatch_failed")
                    return
                await _json_response(send, 200, "failed", stage=task.stage.value)
                return
            # The non-2xx response makes the TypeScript bridge throw, so Vercel
            # Queues retains and retries the delivery.  Do not expose the engine
            # exception: it may contain an official URL or credential material.
            await _json_response(send, 503, "dispatch_failed")
            return
        await _json_response(send, 200, "ok", stage=task.stage.value)


def _log_dispatch_failure(
    error: Exception,
    task: ResearchTask,
    delivery_count: int,
) -> None:
    """Emit bounded diagnostics without exception text, traceback, or task data."""

    exception_class = type(error).__name__
    if (
        len(exception_class) > 128
        or not exception_class.isascii()
        or not exception_class.isidentifier()
    ):
        exception_class = "Exception"
    raw_work_kind = dict(task.payload).get("work_kind")
    work_kind = (
        raw_work_kind
        if isinstance(raw_work_kind, str) and raw_work_kind in _SAFE_WORK_KINDS
        else "unknown"
    )
    _LOGGER.error(
        "research_task_dispatch_failed",
        extra={
            "exception_class": exception_class,
            "task_stage": task.stage.value,
            "work_kind": work_kind,
            "delivery_count": delivery_count,
        },
    )


def _headers(scope: dict[str, Any]) -> dict[bytes, bytes]:
    return {
        bytes(name).lower(): bytes(value)
        for name, value in scope.get("headers", [])
    }


def _delivery_count(headers: dict[bytes, bytes]) -> int:
    raw = headers.get(INTERNAL_DISPATCH_DELIVERY_COUNT_HEADER, b"1")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("invalid delivery count") from exc
    if not 1 <= value <= 1_000_000:
        raise ValueError("invalid delivery count")
    return value


async def _read_bounded_body(receive: Any, limit: int) -> bytes | None:
    body = bytearray()
    while True:
        message = await receive()
        message_type = message.get("type")
        if message_type == "http.disconnect":
            return b""
        if message_type != "http.request":
            continue
        chunk = bytes(message.get("body", b""))
        if len(body) + len(chunk) > limit:
            return None
        body.extend(chunk)
        if not message.get("more_body", False):
            return bytes(body)


async def _json_response(
    send: Any,
    status: int,
    code: str,
    *,
    stage: str | None = None,
) -> None:
    payload: dict[str, str | bool] = {"ok": status < 300}
    if status < 300:
        payload["stage"] = stage or "unknown"
    else:
        payload["error"] = code
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


__all__ = [
    "INTERNAL_DISPATCH_PATH",
    "INTERNAL_DISPATCH_DELIVERY_COUNT_HEADER",
    "INTERNAL_DISPATCH_SECRET_HEADER",
    "MAX_RESEARCH_TASK_DELIVERIES",
    "MAX_RESEARCH_TASK_BYTES",
    "ResearchTaskDispatchASGI",
    "ResearchTaskDispatcher",
    "ResearchTaskEngine",
]
