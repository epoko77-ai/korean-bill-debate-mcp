import asyncio

from cryptography.fernet import Fernet

from kasm.mcp.remote_auth import RemoteTokenAuth, request_api_key


def test_remote_token_auth_requires_valid_token_and_scopes_user_key() -> None:
    calls = []

    async def downstream(scope, receive, send) -> None:
        del receive
        calls.append((scope["path"], scope["query_string"], request_api_key()))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def exercise() -> None:
        auth = RemoteTokenAuth(downstream, Fernet.generate_key().decode())
        token = auth.issue("personal-assembly-key")

        async def receive():
            return {"type": "http.request", "body": b""}

        responses = []

        async def send(message) -> None:
            responses.append(message)

        await auth(
            {
                "type": "http",
                "path": f"/mcp/t/{token}",
                "raw_path": f"/mcp/t/{token}".encode(),
                "query_string": b"",
            },
            receive,
            send,
        )
        await auth(
            {"type": "http", "path": "/mcp", "query_string": f"token={token}".encode()},
            receive,
            send,
        )
        await auth(
            {"type": "http", "path": "/mcp", "query_string": b""}, receive, send
        )
        assert any(message.get("status") == 401 for message in responses)

    asyncio.run(exercise())
    assert calls == [
        ("/mcp", b"", "personal-assembly-key"),
        ("/mcp", b"", "personal-assembly-key"),
    ]
    assert request_api_key() is None


def test_remote_token_never_contains_plain_api_key() -> None:
    auth = RemoteTokenAuth(None, Fernet.generate_key().decode())
    token = auth.issue("do-not-store-this-key")

    assert "do-not-store-this-key" not in token
    assert auth.reveal(token) == "do-not-store-this-key"


def test_oauth_access_token_is_bound_to_mcp_resource_and_scope() -> None:
    auth = RemoteTokenAuth(None, Fernet.generate_key().decode())
    token = auth.issue_payload(
        "access",
        {
            "api_key": "request-key",
            "resource": "https://example.test/mcp",
            "scope": "mcp:tools offline_access",
            "expires_at": 4_102_444_800,
        },
    )

    assert (
        auth.reveal(token, expected_resource="https://example.test/mcp")
        == "request-key"
    )
    for resource in ("https://other.test/mcp", "https://example.test/mcp/"):
        try:
            auth.reveal(token, expected_resource=resource)
        except ValueError as exc:
            assert str(exc) == "invalid connection token"
        else:
            raise AssertionError("OAuth access tokens must be audience-bound")

    wrong_scope = auth.issue_payload(
        "access",
        {
            "api_key": "request-key",
            "resource": "https://example.test/mcp",
            "scope": "offline_access",
            "expires_at": 4_102_444_800,
        },
    )
    try:
        auth.reveal(wrong_scope, expected_resource="https://example.test/mcp")
    except ValueError as exc:
        assert str(exc) == "invalid connection token"
    else:
        raise AssertionError("OAuth access tokens must carry mcp:tools")
