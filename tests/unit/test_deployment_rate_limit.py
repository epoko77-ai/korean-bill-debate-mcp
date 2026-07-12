import asyncio

from kasm.mcp.middleware import FixedWindowRateLimit


def test_rate_limit_applies_only_to_mcp_path() -> None:
    called = []

    async def downstream(scope, receive, send) -> None:
        del receive
        called.append(scope["path"])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def exercise() -> None:
        app = FixedWindowRateLimit(downstream, requests_per_minute=1)

        async def receive():
            return {"type": "http.request", "body": b""}

        responses = []

        async def send(message) -> None:
            responses.append(message)

        base = {"type": "http", "client": ("127.0.0.1", 1)}
        await app({**base, "path": "/healthz"}, receive, send)
        await app({**base, "path": "/mcp"}, receive, send)
        await app({**base, "path": "/mcp"}, receive, send)
        assert any(message.get("status") == 429 for message in responses)

    asyncio.run(exercise())
    assert called == ["/healthz", "/mcp"]
