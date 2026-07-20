from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
from cryptography.fernet import Fernet
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from kasm import __version__
from kasm.mcp.remote_auth import RemoteTokenAuth
from kasm.research.queue import (
    ResearchTask,
    ResearchTaskStage,
    _default_oidc_token,
)


class Backend:
    def __init__(self) -> None:
        self.starts: list[tuple[str, dict[str, Any]]] = []
        self.oidc_tokens: list[str] = []

    def start_research(self, query: str, **options: Any) -> dict[str, Any]:
        self.oidc_tokens.append(_default_oidc_token())
        self.starts.append((query, options))
        return {"research_id": "research_hosted", "status": "queued"}

    def get_research_status(self, research_id: str) -> dict[str, Any]:
        return {"research_id": research_id, "status": "running"}

    def get_research_page(
        self,
        research_id: str,
        *,
        cursor: str | None = None,
        page_size: int = 20,
    ) -> dict[str, Any]:
        del cursor, page_size
        return {
            "research_id": research_id,
            "coverage": {"complete": True},
            "page": {"complete": True, "next_cursor": None},
            "evidence": [],
        }

    def get_evidence_document(
        self,
        research_id: str,
        evidence_id: str,
        *,
        cursor: str | None = None,
        max_characters: int = 20_000,
    ) -> dict[str, Any]:
        del cursor, max_characters
        return {
            "research_id": research_id,
            "evidence_id": evidence_id,
            "text": "원문",
            "next_cursor": None,
            "complete": True,
        }


class Engine:
    def __init__(self) -> None:
        self.tasks: list[ResearchTask] = []
        self.completions: list[ResearchTask] = []
        self.oidc_tokens: list[str] = []

    def process_metadata_task(self, task: ResearchTask) -> None:
        self.oidc_tokens.append(_default_oidc_token())
        self.tasks.append(task)

    def process_document_task(self, task: ResearchTask) -> None:
        self.tasks.append(task)

    def process_finalize_task(self, task: ResearchTask) -> None:
        self.tasks.append(task)

    def task_completed(self, task: ResearchTask) -> bool:
        return task in self.completions

    def complete_task(self, task: ResearchTask) -> None:
        self.completions.append(task)

    def fail_task(self, task: ResearchTask, *, error_code: str) -> None:
        del error_code
        self.tasks.append(task)


def _hosted_environment(monkeypatch, tmp_path) -> str:
    remote_secret = Fernet.generate_key().decode()
    monkeypatch.setenv("KBD_REMOTE_TOKEN_SECRET", remote_secret)
    monkeypatch.setenv("KBD_RESEARCH_CREDENTIAL_SECRET", Fernet.generate_key().decode())
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "blob-test-token")
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
    monkeypatch.setenv("VERCEL_DEPLOYMENT_ID", "dpl_test")
    monkeypatch.setenv("VERCEL_URL", "kbd-test-deployment.vercel.app")
    monkeypatch.setenv("KBD_INTERNAL_TASK_SECRET", "i" * 48)
    monkeypatch.setenv("KBD_DATA_DIR", str(tmp_path / "hosted"))
    return remote_secret


def _task() -> ResearchTask:
    return ResearchTask(
        research_id="research_hosted",
        stage=ResearchTaskStage.COLLECT_METADATA,
        work_id="metadata-page-1",
        query_fingerprint="a" * 64,
        index_revision="index-v1",
        payload=(("page", 1),),
        credential_capability="g" * 120,
    )


def test_complete_hosted_configuration_wires_one_runtime_to_mcp_and_worker(
    monkeypatch, tmp_path
) -> None:
    remote_secret = _hosted_environment(monkeypatch, tmp_path)
    backend = Backend()
    engine = Engine()
    factory_calls: list[Any] = []

    import kasm.mcp.deployment as deployment

    def runtime_factory(*, assembly_api_key_provider):
        factory_calls.append(assembly_api_key_provider)
        return SimpleNamespace(backend=backend, engine=engine)

    monkeypatch.setattr(deployment, "create_hosted_research_runtime", runtime_factory)
    application = deployment.create_asgi_app()
    starlette = application.app.app

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with (
            starlette.router.lifespan_context(starlette),
            httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
                headers={"x-vercel-oidc-token": "request-oidc-token"},
            ) as client,
        ):
            health = await client.get("/healthz")
            assert health.status_code == 200
            assert health.json()["version"] == __version__
            assert health.json()["durable_research"] is True
            assert health.json()["mcp_tool_count"] == 13
            assert health.json()["corpus_revision_configured"] is False

            dispatched = await client.post(
                "/_internal/research/dispatch",
                json=_task().to_queue_payload(),
                headers={"x-kbd-internal-secret": "i" * 48},
            )
            assert dispatched.status_code == 200

            token = RemoteTokenAuth(None, remote_secret).issue("personal-key")
            async with (
                streamable_http_client(
                    f"http://127.0.0.1/mcp/t/{token}", http_client=client
                ) as streams,
                ClientSession(streams[0], streams[1]) as session,
            ):
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert "start_research" in names
                result = await session.call_tool(
                    "start_research",
                    {"query": "제21대 법사위의 플랫폼 노동 논의를 조사해줘"},
                )
                assert not result.isError

    asyncio.run(exercise())

    assert len(factory_calls) == 1
    assert len(engine.tasks) == 1
    assert engine.completions == [_task()]
    assert engine.oidc_tokens == ["request-oidc-token"]
    assert backend.oidc_tokens == ["request-oidc-token"]
    assert backend.starts[0][1]["assembly_term"] is None
    assert backend.starts[0][1]["committees"] is None


def test_partial_hosted_configuration_exposes_neither_backend_nor_worker(
    monkeypatch, tmp_path
) -> None:
    remote_secret = _hosted_environment(monkeypatch, tmp_path)
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN")
    monkeypatch.delenv("VERCEL_BLOB_READ_WRITE_TOKEN", raising=False)

    import kasm.mcp.deployment as deployment

    def unexpected_factory(**_values):
        raise AssertionError("partial hosted configuration must not construct a runtime")

    monkeypatch.setattr(deployment, "create_hosted_research_runtime", unexpected_factory)
    application = deployment.create_asgi_app()
    starlette = application.app.app

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=application)
        async with (
            starlette.router.lifespan_context(starlette),
            httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client,
        ):
            health = await client.get("/healthz")
            assert health.status_code == 200
            assert health.json()["version"] == __version__
            assert health.json()["durable_research"] is False
            assert health.json()["mcp_tool_count"] == 8

            dispatched = await client.post(
                "/_internal/research/dispatch",
                json=_task().to_queue_payload(),
                headers={"x-kbd-internal-secret": "i" * 48},
            )
            assert dispatched.status_code == 503

            token = RemoteTokenAuth(None, remote_secret).issue("personal-key")
            async with (
                streamable_http_client(
                    f"http://127.0.0.1/mcp/t/{token}", http_client=client
                ) as streams,
                ClientSession(streams[0], streams[1]) as session,
            ):
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert "start_research" not in names
                assert len(names) == 8

    asyncio.run(exercise())
