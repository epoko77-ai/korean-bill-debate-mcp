import asyncio
import base64
import hashlib
import urllib.parse
from datetime import UTC, date, datetime

import httpx
from cryptography.fernet import Fernet
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from kasm.core.models import Bill, Meeting, Speech
from kasm.indexing.build import build_vector_index
from kasm.indexing.embeddings import HashEmbeddingProvider
from kasm.mcp.remote_auth import RemoteTokenAuth
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
                "remote_user_key": False,
                "durable_research": False,
                "mcp_tool_count": 8,
                "corpus_revision_configured": False,
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


def test_remote_user_key_page_and_authenticated_mcp_handshake(tmp_path, monkeypatch) -> None:
    secret = Fernet.generate_key().decode()
    monkeypatch.setenv("KBD_REMOTE_TOKEN_SECRET", secret)
    monkeypatch.setenv("KBD_DATA_DIR", str(tmp_path / "remote"))
    monkeypatch.setenv("KASM_ALLOWED_ORIGINS", "https://operator.example")
    monkeypatch.delenv("ASSEMBLY_OPEN_API_KEY", raising=False)
    monkeypatch.setattr("kasm.mcp.deployment._validate_remote_key", lambda _key: None)

    def fake_workspace(**values):
        assert values == {
            "question": "2219564번 의안",
            "assembly_api_key": "workspace-assembly-secret",
            "llm_provider": "openai",
            "llm_api_key": "workspace-llm-secret",
        }
        return {
            "answer": "공식 원문 기반 답변",
            "provider": "openai",
            "model": "test-model",
            "elapsed_seconds": 1.0,
            "evidence": {"bill_count": 1, "speech_count": 2, "thread_count": 1, "sources": []},
        }

    monkeypatch.setattr("kasm.mcp.deployment.run_workspace_research", fake_workspace)
    from kasm.mcp.deployment import create_asgi_app

    async def exercise() -> None:
        application = create_asgi_app()
        starlette = application.app.app
        transport = httpx.ASGITransport(app=application)
        async with (
            starlette.router.lifespan_context(starlette),
            httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client,
        ):
            setup = await client.get("/")
            assert "본인의 열린국회 API 키" in setup.text
            assert "Claude.ai·ChatGPT 연결 — 주소 하나로 시작" in setup.text
            assert "https://korean-bill-debate-mcp.vercel.app/mcp" in setup.text
            assert "Legacy personal MCP URL" in setup.text
            assert "Get an API key from Open Assembly" in setup.text
            assert "/workspace" in setup.text
            workspace = await client.get("/workspace")
            assert "국회 입법조사 워크스페이스" in workspace.text
            assert "localStorage.setItem" not in workspace.text
            assert workspace.headers["cache-control"].startswith("no-store")
            assert workspace.headers["referrer-policy"] == "no-referrer"
            assert "script-src 'self'" in workspace.headers["content-security-policy"]
            script = await client.get("/workspace/app.js")
            assert "localStorage" not in script.text
            researched = await client.post(
                "/workspace/research",
                json={
                    "question": "2219564번 의안",
                    "assembly_api_key": "workspace-assembly-secret",
                    "llm_provider": "openai",
                    "llm_api_key": "workspace-llm-secret",
                },
            )
            assert researched.status_code == 200
            assert researched.json()["answer"] == "공식 원문 기반 답변"
            assert "workspace-assembly-secret" not in researched.text
            assert "workspace-llm-secret" not in researched.text
            issued = await client.post("/connect", data={"api_key": "personal-key"})
            assert "/mcp/t/" in issued.text
            assert "personal-key" not in issued.text
            assert "Your personal MCP URL is ready" in issued.text
            assert "Claude.ai와 ChatGPT에는 이 개인 링크를 넣지 마세요" in issued.text
            assert "등록만 하면 끝이 아닙니다" in issued.text
            assert "+ 또는 도구 메뉴" in issued.text
            unauthenticated = await client.post("/mcp")
            assert unauthenticated.status_code == 401
            assert "oauth-protected-resource/mcp" in unauthenticated.headers["www-authenticate"]
            assert (
                'scope="mcp:tools offline_access"'
                in unauthenticated.headers["www-authenticate"]
            )

            resource_metadata = await client.get(
                "/.well-known/oauth-protected-resource/mcp"
            )
            assert resource_metadata.json()["resource"] == "http://127.0.0.1/mcp"
            authorization_metadata = await client.get(
                "/.well-known/oauth-authorization-server"
            )
            assert authorization_metadata.json()["registration_endpoint"].endswith(
                "/oauth/register"
            )
            assert authorization_metadata.json()["scopes_supported"] == [
                "mcp:tools",
                "offline_access",
            ]
            registered = await client.post(
                "/oauth/register",
                json={
                    "client_name": "Claude",
                    "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                },
            )
            assert registered.status_code == 201
            rejected_registration = await client.post(
                "/oauth/register",
                json={
                    "client_name": "Unsafe client",
                    "redirect_uris": ["http://attacker.example/callback"],
                },
            )
            assert rejected_registration.status_code == 400
            client_id = registered.json()["client_id"]
            verifier = "v" * 64
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()
            ).rstrip(b"=").decode()
            authorization_values = {
                "client_id": client_id,
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "mcp:tools offline_access",
                "resource": "http://127.0.0.1/mcp",
                "state": "test-state",
            }
            consent = await client.get("/oauth/authorize", params=authorization_values)
            assert consent.status_code == 200
            assert "본인의 열린국회 API 키" in consent.text
            assert (
                "form-action 'self' https://claude.ai"
                in consent.headers["content-security-policy"]
            )
            authorized = await client.post(
                "/oauth/authorize",
                data={**authorization_values, "api_key": "personal-key"},
            )
            assert authorized.status_code == 303
            callback = urllib.parse.urlsplit(authorized.headers["location"])
            callback_values = urllib.parse.parse_qs(callback.query)
            assert callback_values["state"] == ["test-state"]
            exchanged = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": callback_values["code"][0],
                    "client_id": client_id,
                    "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                    "code_verifier": verifier,
                    "resource": "http://127.0.0.1/mcp",
                },
            )
            assert exchanged.status_code == 200
            assert exchanged.json()["scope"] == "mcp:tools offline_access"
            access_token = exchanged.json()["access_token"]
            refreshed = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": exchanged.json()["refresh_token"],
                    "client_id": client_id,
                    "resource": "http://127.0.0.1/mcp",
                },
            )
            assert refreshed.status_code == 200
            assert refreshed.json()["access_token"] != access_token
            async with (
                httpx.AsyncClient(
                    transport=transport,
                    base_url="http://127.0.0.1",
                    headers={"Authorization": f"Bearer {access_token}"},
                ) as oauth_client,
                streamable_http_client(
                    "http://127.0.0.1/mcp", http_client=oauth_client
                ) as streams,
                ClientSession(streams[0], streams[1]) as session,
            ):
                await session.initialize()
                assert len((await session.list_tools()).tools) == 8

            token = RemoteTokenAuth(None, secret).issue("personal-key")
            for origin in (
                "https://operator.example",
                "https://claude.ai",
                "https://chatgpt.com",
                "https://chat.openai.com",
            ):
                async with (
                    httpx.AsyncClient(
                        transport=transport,
                        base_url="http://127.0.0.1",
                        headers={"Origin": origin},
                    ) as origin_client,
                    streamable_http_client(
                        f"http://127.0.0.1/mcp/t/{token}", http_client=origin_client
                    ) as streams,
                    ClientSession(streams[0], streams[1]) as session,
                ):
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    assert len(tools) == 8
                    explore = next(tool for tool in tools if tool.name == "explore_issue")
                    assert "korean_query" in explore.inputSchema["properties"]

    asyncio.run(exercise())


def test_remote_connection_rejects_invalid_key_before_issuing_link(tmp_path, monkeypatch) -> None:
    secret = Fernet.generate_key().decode()
    monkeypatch.setenv("KBD_REMOTE_TOKEN_SECRET", secret)
    monkeypatch.setenv("KBD_DATA_DIR", str(tmp_path / "remote-invalid"))

    def reject(_key: str) -> None:
        raise RuntimeError("열린국회 API 키가 유효하지 않습니다.")

    monkeypatch.setattr("kasm.mcp.deployment._validate_remote_key", reject)
    from kasm.mcp.deployment import create_asgi_app

    async def exercise() -> None:
        application = create_asgi_app()
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.post("/connect", data={"api_key": "invalid"})
            assert response.status_code == 400
            assert "유효하지 않습니다" in response.text
            assert "/mcp/t/" not in response.text

    asyncio.run(exercise())


def test_remote_oauth_preserves_injected_web_callbacks_and_allows_them_in_csp(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("KBD_REMOTE_TOKEN_SECRET", Fernet.generate_key().decode())
    monkeypatch.setenv("KBD_DATA_DIR", str(tmp_path / "remote-web-callbacks"))

    from kasm.mcp.deployment import create_asgi_app

    callbacks = (
        "https://claude.ai/api/mcp/auth_callback",
        # ChatGPT supplies its current callback in DCR. The production smoke
        # injects that exact URI; this representative URI proves the server
        # does not hard-code Claude's callback host or path.
        "https://chatgpt.com/connector/oauth/callback?surface=web",
    )

    async def exercise() -> None:
        application = create_asgi_app()
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
            follow_redirects=False,
        ) as client:
            for position, callback_uri in enumerate(callbacks):
                registered = await client.post(
                    "/oauth/register",
                    json={
                        "client_name": f"web-connector-{position}",
                        "redirect_uris": [callback_uri],
                        "token_endpoint_auth_method": "none",
                    },
                )
                assert registered.status_code == 201
                assert registered.json()["redirect_uris"] == [callback_uri]
                client_id = registered.json()["client_id"]
                verifier = f"web-connector-verifier-{position}-" + "v" * 48
                challenge = base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.encode()).digest()
                ).rstrip(b"=").decode()
                values = {
                    "client_id": client_id,
                    "redirect_uri": callback_uri,
                    "response_type": "code",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "scope": "mcp:tools offline_access",
                    "resource": "http://127.0.0.1/mcp",
                    "state": f"web-state-{position}",
                }
                consent = await client.get("/oauth/authorize", params=values)
                assert consent.status_code == 200
                callback = urllib.parse.urlsplit(callback_uri)
                callback_origin = f"{callback.scheme}://{callback.netloc}"
                assert (
                    f"form-action 'self' {callback_origin}"
                    in consent.headers["content-security-policy"]
                )
                authorized = await client.post(
                    "/oauth/authorize",
                    data={**values, "api_key": "web-user-key"},
                )
                assert authorized.status_code == 303
                returned = urllib.parse.urlsplit(authorized.headers["location"])
                assert (returned.scheme, returned.netloc, returned.path) == (
                    callback.scheme,
                    callback.netloc,
                    callback.path,
                )
                returned_values = urllib.parse.parse_qs(returned.query)
                for name, expected in urllib.parse.parse_qs(callback.query).items():
                    assert returned_values[name] == expected
                assert returned_values["state"] == [f"web-state-{position}"]
                exchanged = await client.post(
                    "/oauth/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": returned_values["code"][0],
                        "client_id": client_id,
                        "redirect_uri": callback_uri,
                        "code_verifier": verifier,
                        "resource": "http://127.0.0.1/mcp",
                    },
                )
                assert exchanged.status_code == 200
                assert exchanged.json()["scope"] == "mcp:tools offline_access"

    asyncio.run(exercise())


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
