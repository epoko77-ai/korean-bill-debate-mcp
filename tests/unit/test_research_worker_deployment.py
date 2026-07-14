from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from kasm.research.queue import ResearchTask, ResearchTaskStage
from kasm.research.worker_deployment import create_research_dispatch_app

_SECRET = "w" * 48


class _Engine:
    def __init__(self) -> None:
        self.tasks: list[ResearchTask] = []
        self.completions: list[ResearchTask] = []

    def task_completed(self, task: ResearchTask) -> bool:
        return task in self.completions

    def process_metadata_task(self, task: ResearchTask) -> None:
        self.tasks.append(task)

    def process_document_task(self, task: ResearchTask) -> None:
        self.tasks.append(task)

    def process_finalize_task(self, task: ResearchTask) -> None:
        self.tasks.append(task)

    def complete_task(self, task: ResearchTask) -> None:
        self.completions.append(task)

    def fail_task(self, task: ResearchTask, *, error_code: str) -> None:
        del error_code
        self.tasks.append(task)


def _configure(monkeypatch: Any) -> None:
    monkeypatch.setenv("KBD_INTERNAL_TASK_SECRET", _SECRET)
    monkeypatch.setenv("KBD_RESEARCH_CREDENTIAL_SECRET", "c" * 48)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "blob-token")
    monkeypatch.setenv("VERCEL_DEPLOYMENT_ID", "dpl_worker_test")
    monkeypatch.setenv("VERCEL_URL", "kbd-worker-test.vercel.app")


def _task() -> ResearchTask:
    return ResearchTask(
        research_id="research_worker_test",
        stage=ResearchTaskStage.COLLECT_METADATA,
        work_id="metadata-page-1",
        query_fingerprint="a" * 64,
        index_revision="index-test",
        payload=(("page", 1),),
    )


def _post(app: Any, body: bytes) -> tuple[int, dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(
        app(
            {
                "type": "http",
                "method": "POST",
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"x-kbd-internal-secret", _SECRET.encode()),
                    (b"x-vercel-oidc-token", b"request-token"),
                ],
            },
            receive,
            send,
        )
    )
    start = next(item for item in sent if item["type"] == "http.response.start")
    payload = b"".join(
        item.get("body", b"")
        for item in sent
        if item["type"] == "http.response.body"
    )
    return int(start["status"]), json.loads(payload)


def test_dedicated_worker_dispatches_without_importing_public_mcp(
    monkeypatch: Any,
) -> None:
    _configure(monkeypatch)
    engine = _Engine()
    app = create_research_dispatch_app(
        runtime_factory=lambda **_values: SimpleNamespace(engine=engine)
    )

    status, result = _post(app, json.dumps(_task().to_queue_payload()).encode())

    assert (status, result) == (
        200,
        {"ok": True, "stage": "collect_metadata"},
    )
    assert engine.tasks == [_task()]
    assert engine.completions == [_task()]
    source = (
        Path(__file__).resolve().parents[2]
        / "src/kasm/research/worker_deployment.py"
    ).read_text()
    assert "kasm.mcp" not in source


def test_partial_worker_configuration_fails_closed_before_factory(
    monkeypatch: Any,
) -> None:
    _configure(monkeypatch)
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN")
    monkeypatch.delenv("VERCEL_BLOB_READ_WRITE_TOKEN", raising=False)

    def unexpected(**_values: Any) -> Any:
        raise AssertionError("partial worker configuration must not build a runtime")

    app = create_research_dispatch_app(runtime_factory=unexpected)
    status, result = _post(app, json.dumps(_task().to_queue_payload()).encode())

    assert (status, result) == (
        503,
        {"ok": False, "error": "dispatcher_unavailable"},
    )


def test_worker_factory_configuration_error_retains_delivery(
    monkeypatch: Any,
) -> None:
    _configure(monkeypatch)

    def invalid(**_values: Any) -> Any:
        raise RuntimeError("invalid hosted configuration")

    app = create_research_dispatch_app(runtime_factory=invalid)
    status, result = _post(app, json.dumps(_task().to_queue_payload()).encode())

    assert (status, result) == (
        503,
        {"ok": False, "error": "dispatcher_unavailable"},
    )
