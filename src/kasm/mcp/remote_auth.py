"""Request-scoped user-key authentication for remote MCP connections."""

# ruff: noqa: E501 - embedded setup HTML is kept readable as rendered markup

from __future__ import annotations

import html
import urllib.parse
from contextvars import ContextVar
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

_request_api_key: ContextVar[str | None] = ContextVar("kbd_request_api_key", default=None)


def request_api_key() -> str | None:
    """Return the current remote user's Open Assembly key, if authenticated."""
    return _request_api_key.get()


class RemoteTokenAuth:
    """Decrypt a connection token and expose its API key only during one ASGI request."""

    def __init__(self, app: Any, secret: str) -> None:
        self.app = app
        self.cipher = Fernet(secret.encode())

    def issue(self, api_key: str) -> str:
        normalized = api_key.strip()
        if not normalized or len(normalized) > 256:
            raise ValueError("Open Assembly API key must be between 1 and 256 characters")
        return self.cipher.encrypt(normalized.encode()).decode()

    def reveal(self, token: str) -> str:
        try:
            value = self.cipher.decrypt(token.encode()).decode()
        except (InvalidToken, UnicodeError) as exc:
            raise ValueError("invalid connection token") from exc
        if not value or len(value) > 256:
            raise ValueError("invalid connection token")
        return value

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        pairs = urllib.parse.parse_qsl(
            bytes(scope.get("query_string", b"")).decode(), keep_blank_values=True
        )
        token = next((value for name, value in pairs if name == "token"), "")
        try:
            api_key = self.reveal(token)
        except ValueError:
            await _json_error(send, 401, "A valid personal connection token is required")
            return
        clean_scope = dict(scope)
        clean_scope["query_string"] = urllib.parse.urlencode(
            [(name, value) for name, value in pairs if name != "token"]
        ).encode()
        context_token = _request_api_key.set(api_key)
        try:
            await self.app(clean_scope, receive, send)
        finally:
            _request_api_key.reset(context_token)


async def _json_error(send: Any, status: int, message: str) -> None:
    body = ("{\"error\":\"" + message + "\"}").encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"cache-control", b"no-store"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def setup_page(*, action: str = "/connect", error: str | None = None) -> str:
    message = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Korean Bill & Debate MCP 연결</title><style>
body{{margin:0;background:#061a31;color:#f7f2e8;font:16px/1.6 system-ui,sans-serif}}
main{{max-width:720px;margin:8vh auto;padding:36px}}h1{{font-size:clamp(30px,6vw,52px);line-height:1.15}}
.card{{background:#0b2945;border:1px solid #2b5a71;border-radius:20px;padding:28px}}
input{{box-sizing:border-box;width:100%;padding:15px;border-radius:10px;border:1px solid #6b8798;font-size:16px}}
button{{margin-top:14px;padding:14px 20px;border:0;border-radius:10px;background:#e5b85c;color:#071728;font-weight:800;font-size:16px}}
small{{color:#b8c8d4}}.error{{color:#ffb3a8}}code{{overflow-wrap:anywhere;color:#f0cc83}}
</style></head><body><main><h1>흩어진 국회 기록을,<br>법안 하나로 연결합니다.</h1>
<div class="card"><h2>웹 앱용 MCP 연결 링크 만들기</h2>
<p>본인의 열린국회 API 키를 입력하면 ChatGPT·Claude에 붙여 넣을 개인 연결 링크를 만듭니다.</p>
{message}<form method="post" action="{html.escape(action)}">
<label>열린국회 API 키<input name="api_key" type="password" required autocomplete="off"></label>
<button type="submit">개인 MCP 링크 만들기</button></form>
<p><small>키 원문은 데이터베이스나 파일에 저장하지 않습니다. 암호화된 연결 토큰을 발급하고,
요청 순간에만 사용자의 키로 열린국회 공식 API를 호출합니다.</small></p></div></main></body></html>"""


def result_page(mcp_url: str) -> str:
    escaped = html.escape(mcp_url)
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>MCP 연결 링크</title><style>
body{{margin:0;background:#061a31;color:#f7f2e8;font:16px/1.6 system-ui,sans-serif}}
main{{max-width:760px;margin:8vh auto;padding:36px}}.card{{background:#0b2945;border:1px solid #2b5a71;border-radius:20px;padding:28px}}
textarea{{box-sizing:border-box;width:100%;min-height:130px;padding:14px;font:14px/1.5 ui-monospace,monospace}}
h1{{font-size:40px}}strong{{color:#f0cc83}}</style></head><body><main><h1>개인 MCP 링크가 준비됐습니다.</h1>
<div class="card"><p>아래 주소 전체를 복사해 ChatGPT 또는 Claude의 커스텀 MCP 서버 URL에 붙여 넣으세요.</p>
<textarea readonly onclick="this.select()">{escaped}</textarea>
<p><strong>이 링크는 비밀번호처럼 보관하세요.</strong> 링크를 아는 사람은 사용자의 열린국회 API 할당량을 사용할 수 있습니다.</p>
<p>ChatGPT: 설정 → 앱 → 고급 설정 → 앱 만들기<br>Claude: 설정 → 커넥터 → 커스텀 커넥터 추가</p></div></main></body></html>"""
