import asyncio
from datetime import UTC, date, datetime

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from kasm.core.models import Bill, Meeting, Speech
from kasm.indexing.build import build_vector_index
from kasm.indexing.embeddings import HashEmbeddingProvider
from kasm.storage.database import Database
from kasm.storage.repositories import BillRepository, MeetingRepository, SpeechRepository


def save_bill(database: Database) -> None:
    BillRepository(database).save(
        Bill(
            "bill",
            "2200001",
            "인공지능법안",
            22,
            "홍길동 의원",
            "위원회",
            date(2025, 1, 1),
            None,
            None,
            "https://likms.assembly.go.kr/bill",
            "hash",
            datetime.now(UTC),
        )
    )


def test_explicit_offline_cache_app_health(tmp_path, monkeypatch) -> None:
    path = tmp_path / "prepared.sqlite3"
    with Database(path) as database:
        meeting = Meeting(
            "meeting",
            22,
            "committee",
            "위원회",
            None,
            "회의",
            "committee",
            "1",
            date(2026, 1, 1),
            "https://record.assembly.go.kr/test",
            "hash",
            datetime.now(UTC),
        )
        MeetingRepository(database).save(meeting)
        SpeechRepository(database).save(
            Speech(
                "speech",
                meeting.id,
                1,
                None,
                "홍길동",
                "의원",
                None,
                "예산 심사 발언",
                None,
                None,
                None,
                "p.1",
                "hash",
                "v1",
            )
        )
        save_bill(database)
    monkeypatch.setenv("KASM_DATABASE", str(path))
    monkeypatch.delenv("KASM_VECTOR_INDEX", raising=False)
    monkeypatch.delenv("ASSEMBLY_OPEN_API_KEY", raising=False)
    from kasm.mcp.deployment import create_asgi_app

    async def exercise() -> None:
        application = create_asgi_app()
        starlette = application.app
        transport = httpx.ASGITransport(app=application)
        async with (
            starlette.router.lifespan_context(starlette),
            httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client,
        ):
            response = await client.get("/healthz")
            assert response.json() == {
                "status": "ok",
                "service": "korean-bill-debate-mcp",
                "meetings": 1,
                "speeches": 1,
                "bills": 1,
                "semantic_index": False,
            }
            async with streamable_http_client(
                "http://127.0.0.1/mcp", http_client=client
            ) as streams:
                read_stream, write_stream, _ = streams
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    assert len((await session.list_tools()).tools) == 8

    asyncio.run(exercise())


def test_explicit_offline_cache_can_use_a_hybrid_index(tmp_path, monkeypatch) -> None:
    path = tmp_path / "prepared.sqlite3"
    vector_path = tmp_path / "prepared-vectors.json"
    with Database(path) as database:
        meeting = Meeting(
            "meeting",
            22,
            "committee",
            "위원회",
            None,
            "회의",
            "committee",
            "1",
            date(2026, 1, 1),
            "https://record.assembly.go.kr/test",
            "hash",
            datetime.now(UTC),
        )
        MeetingRepository(database).save(meeting)
        SpeechRepository(database).save(
            Speech(
                "speech",
                meeting.id,
                1,
                None,
                "홍길동",
                "의원",
                None,
                "인공지능 예산 심사 발언",
                None,
                None,
                None,
                "p.1",
                "hash",
                "v1",
            )
        )
        save_bill(database)
        build_vector_index(database, HashEmbeddingProvider(), vector_path)
    monkeypatch.setenv("KASM_DATABASE", str(path))
    monkeypatch.setenv("KASM_VECTOR_INDEX", str(vector_path))
    monkeypatch.delenv("ASSEMBLY_OPEN_API_KEY", raising=False)
    from kasm.app import create_deployed_services

    services = create_deployed_services()
    result = services.search.search("인공지능", limit=10)
    assert result[0]["speech_id"] == "speech"
    assert result[0]["official_source"].startswith("https://record.assembly.go.kr/")
    assert result[0]["context_before"] is None


def test_deployed_services_reject_empty_prepared_database(tmp_path) -> None:
    path = tmp_path / "empty.sqlite3"
    with Database(path):
        pass
    from kasm.app import create_deployed_services

    try:
        create_deployed_services(str(path))
    except RuntimeError as exc:
        assert "empty tables: meetings, speeches, bills" in str(exc)
    else:
        raise AssertionError("empty public indexes must never start")
