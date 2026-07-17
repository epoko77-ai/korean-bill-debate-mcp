"""Durable research task contracts and a dependency-free Vercel Queue adapter."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

_QUEUE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_REGION = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
_MAX_TASK_BYTES = 64 * 1024

# These tasks coordinate bounded windows or prove that a phase can advance.
# Interactive instances use the control topic; broad instances intentionally
# remain on their fully isolated bulk topic.
CONTROL_WORK_KINDS = frozenset(
    {
        "deferred_fanout",
        "discovery_fanout",
        "document_finalize_barrier",
        "document_fanout",
        "document_window_barrier",
        "metadata_fanout_dispatch",
        "metadata_window_barrier",
        "page_fanout",
        "page_window_barrier",
        "phase_barrier",
    }
)


class ResearchTaskStage(StrEnum):
    COLLECT_METADATA = "collect_metadata"
    HYDRATE_DOCUMENT = "hydrate_document"
    FINALIZE = "finalize"


@dataclass(frozen=True, slots=True)
class ResearchTask:
    """Idempotent work unit. Credentials, when needed, are opaque encrypted capabilities."""

    research_id: str
    stage: ResearchTaskStage
    work_id: str
    query_fingerprint: str
    index_revision: str
    payload: tuple[tuple[str, str | int | float | bool | None], ...] = ()
    credential_capability: str | None = None

    def __post_init__(self) -> None:
        if not self.research_id.strip() or not self.work_id.strip():
            raise ValueError("research_id and work_id are required")
        if len(self.query_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.query_fingerprint
        ):
            raise ValueError("query_fingerprint must be a SHA-256 hex digest")
        if not self.index_revision.strip():
            raise ValueError("index_revision is required")
        names = [name for name, _value in self.payload]
        if len(names) != len(set(names)) or any(not name.strip() for name in names):
            raise ValueError("task payload names must be non-empty and unique")
        object.__setattr__(self, "payload", tuple(sorted(self.payload)))
        if self.credential_capability is not None:
            if not 40 <= len(self.credential_capability) <= 4096:
                raise ValueError("credential capability has an invalid length")
            if any(character.isspace() for character in self.credential_capability):
                raise ValueError("credential capability must be an opaque token")

    @property
    def idempotency_key(self) -> str:
        identity = f"{self.research_id}\0{self.stage.value}\0{self.work_id}"
        return hashlib.sha256(identity.encode()).hexdigest()

    def to_queue_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "research_id": self.research_id,
            "stage": self.stage.value,
            "work_id": self.work_id,
            "query_fingerprint": self.query_fingerprint,
            "index_revision": self.index_revision,
            "payload": dict(self.payload),
            "credential_capability": self.credential_capability,
        }

    def public_payload(self) -> dict[str, Any]:
        """Return diagnostics that can never disclose a queued credential capability."""

        return {
            "research_id": self.research_id,
            "stage": self.stage.value,
            "work_id": self.work_id,
            "query_fingerprint": self.query_fingerprint,
            "index_revision": self.index_revision,
            "payload": dict(self.payload),
            "has_credential_capability": self.credential_capability is not None,
        }

    @classmethod
    def from_queue_payload(cls, payload: dict[str, Any]) -> ResearchTask:
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported research task schema")
        values = payload.get("payload")
        if not isinstance(values, dict):
            raise ValueError("research task payload must be an object")
        allowed = (str, int, float, bool, type(None))
        if any(not isinstance(value, allowed) for value in values.values()):
            raise ValueError("research task payload values must be scalar")
        capability = payload.get("credential_capability")
        if capability is not None and not isinstance(capability, str):
            raise ValueError("credential capability must be a string")
        return cls(
            research_id=str(payload.get("research_id") or ""),
            stage=ResearchTaskStage(str(payload.get("stage") or "")),
            work_id=str(payload.get("work_id") or ""),
            query_fingerprint=str(payload.get("query_fingerprint") or ""),
            index_revision=str(payload.get("index_revision") or ""),
            payload=tuple((str(name), value) for name, value in values.items()),
            credential_capability=capability,
        )


@dataclass(frozen=True, slots=True)
class LeasedResearchTask:
    message_id: str
    receipt_handle: str
    delivery_count: int
    task: ResearchTask

    def __post_init__(self) -> None:
        if not self.message_id or not self.receipt_handle:
            raise ValueError("leased queue messages require an id and receipt handle")
        if self.delivery_count < 1:
            raise ValueError("delivery_count must be positive")


class ResearchTaskQueue(Protocol):
    def publish(
        self,
        task: ResearchTask,
        *,
        retention_seconds: int = 86_400,
        delay_seconds: int = 0,
    ) -> str: ...

    def receive(
        self,
        *,
        max_messages: int = 1,
        visibility_timeout_seconds: int = 300,
    ) -> tuple[LeasedResearchTask, ...]: ...

    def acknowledge(self, receipt_handle: str) -> None: ...

    def extend(self, receipt_handle: str, visibility_timeout_seconds: int) -> None: ...


@dataclass(frozen=True, slots=True)
class _HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


_Transport = Callable[[urllib.request.Request, float], _HttpResponse]


class VercelResearchTaskQueue:
    """Publish and poll Vercel Queue messages through its documented HTTP API."""

    def __init__(
        self,
        *,
        topic: str = "kbd-research",
        control_topic: str | None = None,
        bulk_topic: str | None = None,
        consumer: str = "kbd-workers",
        region: str | None = None,
        oidc_token_provider: Callable[[], str] | None = None,
        deployment_id_provider: Callable[[], str | None] | None = None,
        timeout: float = 10.0,
        transport: _Transport | None = None,
    ) -> None:
        selected_region = region or os.getenv("VERCEL_REGION") or "iad1"
        configured_topics = tuple(
            value for value in (topic, control_topic, bulk_topic) if value is not None
        )
        if any(not _QUEUE_NAME.fullmatch(value) for value in configured_topics) or not (
            _QUEUE_NAME.fullmatch(consumer)
        ):
            raise ValueError("queue topic and consumer names contain invalid characters")
        if len(configured_topics) != len(set(configured_topics)):
            raise ValueError("queue topics must be mutually distinct")
        if not _REGION.fullmatch(selected_region):
            raise ValueError("invalid Vercel queue region")
        if timeout <= 0:
            raise ValueError("queue timeout must be positive")
        self.topic = topic
        self.control_topic = control_topic
        self.bulk_topic = bulk_topic
        self.consumer = consumer
        self.region = selected_region
        self._oidc_token_provider = oidc_token_provider or _default_oidc_token
        self._deployment_id_provider = deployment_id_provider or (
            lambda: os.getenv("VERCEL_DEPLOYMENT_ID")
        )
        self.timeout = timeout
        self._transport = transport or _urlopen_transport

    @property
    def base_url(self) -> str:
        return f"https://{self.region}.vercel-queue.com/api/v3"

    def publish(
        self,
        task: ResearchTask,
        *,
        retention_seconds: int = 86_400,
        delay_seconds: int = 0,
    ) -> str:
        if not 60 <= retention_seconds <= 604_800:
            raise ValueError("queue retention must be between 60 seconds and 7 days")
        if not 0 <= delay_seconds <= retention_seconds:
            raise ValueError("queue delay must be between zero and the retention period")
        body = _canonical_json(task.to_queue_payload())
        if len(body) > _MAX_TASK_BYTES:
            raise ValueError("research task exceeds the queue payload limit")
        response = self._request(
            "POST",
            f"/topic/{urllib.parse.quote(self._publish_topic(task), safe='')}",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Vqs-Retention-Seconds": str(retention_seconds),
                "Vqs-Delay-Seconds": str(delay_seconds),
                "Vqs-Idempotency-Key": task.idempotency_key,
            },
            expected={200, 201, 202},
        )
        # The Queue API deliberately returns 202 without a message ID when it
        # accepted the publish for deferred processing. The official SDK does
        # not require (or parse) a JSON body for this response.
        if response.status == 202:
            return "deferred:" + task.idempotency_key
        payload = _json_object(response.body)
        message_id = str(payload.get("messageId") or "")
        if not message_id:
            raise RuntimeError("Vercel Queue response did not contain a message id")
        return message_id

    def _publish_topic(self, task: ResearchTask) -> str:
        payload = dict(task.payload)
        work_kind = payload.get("work_kind")
        # Broad coordinators and barriers stay with their isolated bulk leaves.
        # Exact/interactive control work keeps the dedicated control topic, so
        # a wave of broad dispatches cannot delay an exact result boundary.
        if payload.get("queue_lane") == "bulk" and self.bulk_topic is not None:
            return self.bulk_topic
        if work_kind in CONTROL_WORK_KINDS:
            return self.control_topic or self.topic
        return self.topic

    def receive(
        self,
        *,
        max_messages: int = 1,
        visibility_timeout_seconds: int = 300,
    ) -> tuple[LeasedResearchTask, ...]:
        if not 1 <= max_messages <= 10:
            raise ValueError("max_messages must be between 1 and 10")
        if not 0 <= visibility_timeout_seconds <= 3600:
            raise ValueError("visibility timeout must be between 0 and 3600 seconds")
        response = self._request(
            "POST",
            (
                f"/topic/{urllib.parse.quote(self.topic, safe='')}/consumer/"
                f"{urllib.parse.quote(self.consumer, safe='')}"
            ),
            headers={
                "Accept": "application/x-ndjson",
                "Vqs-Max-Messages": str(max_messages),
                "Vqs-Visibility-Timeout-Seconds": str(visibility_timeout_seconds),
            },
            expected={200, 204},
        )
        if response.status == 204:
            return ()
        leased: list[LeasedResearchTask] = []
        for raw_line in response.body.splitlines():
            if not raw_line.strip():
                continue
            message = _json_object(raw_line)
            try:
                decoded_body = base64.b64decode(str(message["body"]), validate=True)
                task_payload = _json_object(decoded_body)
                leased.append(
                    LeasedResearchTask(
                        message_id=str(message["messageId"]),
                        receipt_handle=str(message["receiptHandle"]),
                        delivery_count=int(message["deliveryCount"]),
                        task=ResearchTask.from_queue_payload(task_payload),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("Vercel Queue returned an invalid research task") from exc
        return tuple(leased)

    def acknowledge(self, receipt_handle: str) -> None:
        if not receipt_handle:
            raise ValueError("receipt_handle is required")
        self._request(
            "DELETE",
            (
                f"/topic/{urllib.parse.quote(self.topic, safe='')}/consumer/"
                f"{urllib.parse.quote(self.consumer, safe='')}/lease/"
                f"{urllib.parse.quote(receipt_handle, safe='')}"
            ),
            expected={200, 204},
        )

    def extend(self, receipt_handle: str, visibility_timeout_seconds: int) -> None:
        if not receipt_handle:
            raise ValueError("receipt_handle is required")
        if not 0 <= visibility_timeout_seconds <= 3600:
            raise ValueError("visibility timeout must be between 0 and 3600 seconds")
        self._request(
            "PATCH",
            (
                f"/topic/{urllib.parse.quote(self.topic, safe='')}/consumer/"
                f"{urllib.parse.quote(self.consumer, safe='')}/lease/"
                f"{urllib.parse.quote(receipt_handle, safe='')}"
            ),
            body=_canonical_json({"visibilityTimeoutSeconds": visibility_timeout_seconds}),
            headers={"Content-Type": "application/json"},
            expected={200, 204},
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        expected: set[int],
    ) -> _HttpResponse:
        token = self._oidc_token_provider().strip()
        if not token:
            raise RuntimeError("VERCEL_OIDC_TOKEN is required for the durable research queue")
        try:
            deployment_id = str(self._deployment_id_provider() or "").strip()
        except Exception:
            raise RuntimeError("Vercel Queue deployment identity is unavailable") from None
        if deployment_id and (
            len(deployment_id) > 256
            or any(not 33 <= ord(character) <= 126 for character in deployment_id)
        ):
            raise RuntimeError("Vercel Queue deployment identity is invalid")
        common_headers = {"Authorization": f"Bearer {token}"}
        if deployment_id:
            # Push-mode topics are deployment-partitioned. Pinning publications
            # and poll-mode operations prevents one rollout from consuming a
            # different deployment's task schema.
            common_headers["Vqs-Deployment-Id"] = deployment_id
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={**common_headers, **(headers or {})},
        )
        try:
            response = self._transport(request, self.timeout)
        except (OSError, urllib.error.URLError) as exc:
            raise RuntimeError("Vercel Queue request failed") from exc
        if response.status not in expected:
            # Never include upstream bodies: they can echo payload or auth diagnostics.
            raise RuntimeError(f"Vercel Queue returned HTTP {response.status}")
        return response


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _default_oidc_token() -> str:
    """Resolve Vercel workload identity without making it a stored application secret.

    Local and older runtimes may expose the token directly.  Current Vercel
    runtimes can refresh it through the official SDK, so use that path lazily
    and keep the package optional for local/stdio installations.
    """

    token = os.getenv("VERCEL_OIDC_TOKEN", "").strip()
    if token:
        return token
    try:
        from vercel.oidc import get_vercel_oidc_token_sync
    except ImportError:
        return ""
    try:
        resolved = get_vercel_oidc_token_sync()
    except Exception:  # pragma: no cover - SDK/runtime-specific failure
        return ""
    return resolved.strip() if isinstance(resolved, str) else ""


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Vercel Queue returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Vercel Queue returned a non-object JSON value")
    return payload


def _urlopen_transport(request: urllib.request.Request, timeout: float) -> _HttpResponse:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return _HttpResponse(
                status=int(response.status),
                headers={str(name): str(value) for name, value in response.headers.items()},
                body=response.read(),
            )
    except urllib.error.HTTPError as exc:
        return _HttpResponse(
            status=exc.code,
            headers={str(name): str(value) for name, value in exc.headers.items()},
            body=exc.read(),
        )
