"""Request-scoped user-key authentication for remote MCP connections."""

# ruff: noqa: E501 - embedded setup HTML is kept readable as rendered markup

from __future__ import annotations

import html
import json
import os
import time
import urllib.parse
from contextvars import ContextVar
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

_request_api_key: ContextVar[str | None] = ContextVar("kbd_request_api_key", default=None)
_TOKEN_PATH_PREFIX = "/mcp/t/"


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

    def issue_payload(self, purpose: str, values: dict[str, Any]) -> str:
        payload = json.dumps(
            {"purpose": purpose, **values}, ensure_ascii=False, separators=(",", ":")
        )
        return self.cipher.encrypt(payload.encode()).decode()

    def reveal_payload(
        self, token: str, purpose: str, *, ttl_seconds: int
    ) -> dict[str, Any]:
        try:
            value = self.cipher.decrypt(token.encode(), ttl=ttl_seconds).decode()
            payload = json.loads(value)
        except (InvalidToken, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid connection token") from exc
        if not isinstance(payload, dict) or payload.get("purpose") != purpose:
            raise ValueError("invalid connection token")
        return payload

    def reveal(self, token: str) -> str:
        try:
            value = self.cipher.decrypt(token.encode()).decode()
        except (InvalidToken, UnicodeError) as exc:
            raise ValueError("invalid connection token") from exc
        if value.startswith("{"):
            try:
                payload = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("invalid connection token") from exc
            if (
                not isinstance(payload, dict)
                or payload.get("purpose") != "access"
                or float(payload.get("expires_at") or 0) < time.time()
            ):
                raise ValueError("invalid connection token")
            value = str(payload.get("api_key") or "")
        if not value or len(value) > 256:
            raise ValueError("invalid connection token")
        return value

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = str(scope.get("path", ""))
        if scope.get("type") != "http" or not path.startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        pairs = urllib.parse.parse_qsl(
            bytes(scope.get("query_string", b"")).decode(), keep_blank_values=True
        )
        path_token = ""
        if path.startswith(_TOKEN_PATH_PREFIX):
            path_token = urllib.parse.unquote(path.removeprefix(_TOKEN_PATH_PREFIX))
            if not path_token or "/" in path_token:
                path_token = ""
        headers = {
            bytes(name).decode("latin-1").lower(): bytes(value).decode("latin-1")
            for name, value in scope.get("headers", [])
        }
        authorization = headers.get("authorization", "")
        bearer_token = (
            authorization.removeprefix("Bearer ").strip()
            if authorization.startswith("Bearer ")
            else ""
        )
        token = (
            bearer_token
            or path_token
            or next((value for name, value in pairs if name == "token"), "")
        )
        try:
            api_key = self.reveal(token)
        except ValueError:
            _log_mcp_access(scope, authenticated=False, path_authenticated=bool(path_token))
            base = _public_base(scope)
            await _json_error(
                send,
                401,
                "OAuth authorization or a valid personal connection token is required",
                headers=[
                    (
                        b"www-authenticate",
                        (
                            'Bearer realm="Korean Bill & Debate MCP", '
                            f'resource_metadata="{base}/.well-known/'
                            'oauth-protected-resource/mcp", scope="mcp:tools"'
                        ).encode(),
                    )
                ],
            )
            return
        _log_mcp_access(scope, authenticated=True, path_authenticated=bool(path_token))
        clean_scope = dict(scope)
        if path_token:
            clean_scope["path"] = "/mcp"
            clean_scope["raw_path"] = b"/mcp"
        clean_scope["query_string"] = urllib.parse.urlencode(
            [(name, value) for name, value in pairs if name != "token"]
        ).encode()
        context_token = _request_api_key.set(api_key)
        try:
            await self.app(clean_scope, receive, send)
        finally:
            _request_api_key.reset(context_token)


async def _json_error(
    send: Any,
    status: int,
    message: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = ("{\"error\":\"" + message + "\"}").encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"cache-control", b"no-store"),
                (b"content-length", str(len(body)).encode()),
                *(headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _public_base(scope: dict[str, Any]) -> str:
    configured = os.getenv("KBD_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    headers = {
        bytes(name).decode("latin-1").lower(): bytes(value).decode("latin-1")
        for name, value in scope.get("headers", [])
    }
    host = headers.get("x-forwarded-host") or headers.get("host") or "localhost"
    scheme = headers.get("x-forwarded-proto") or str(scope.get("scheme") or "https")
    return f"{scheme}://{host}"


def _log_mcp_access(
    scope: dict[str, Any], *, authenticated: bool, path_authenticated: bool
) -> None:
    """Emit connector diagnostics without logging a path, token, query, or API key."""
    headers = {
        bytes(name).decode("latin-1").lower(): bytes(value).decode("latin-1")
        for name, value in scope.get("headers", [])
    }
    print(
        json.dumps(
            {
                "event": "mcp_access",
                "authenticated": authenticated,
                "path_authenticated": path_authenticated,
                "user_agent": headers.get("user-agent", "")[:160],
            },
            ensure_ascii=True,
        ),
        flush=True,
    )


def setup_page(*, action: str = "/connect", error: str | None = None) -> str:
    message = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Korean Bill & Debate MCP 연결 / Connect</title><style>
body{{margin:0;background:#061a31;color:#f7f2e8;font:16px/1.6 system-ui,sans-serif}}
main{{max-width:720px;margin:8vh auto;padding:36px}}h1{{font-size:clamp(30px,6vw,52px);line-height:1.15}}
.card{{background:#0b2945;border:1px solid #2b5a71;border-radius:20px;padding:28px}}
input{{box-sizing:border-box;width:100%;padding:15px;border-radius:10px;border:1px solid #6b8798;font-size:16px}}
button{{margin-top:14px;padding:14px 20px;border:0;border-radius:10px;background:#e5b85c;color:#071728;font-weight:800;font-size:16px}}
small,.english{{color:#b8c8d4}}.error{{color:#ffb3a8}}code{{overflow-wrap:anywhere;color:#f0cc83}}
a{{color:#f0cc83}}
</style></head><body><main><h1>흩어진 국회 기록을,<br>법안 하나로 연결합니다.</h1>
<p class="english">Connect scattered National Assembly records around a single bill.</p>
<p><a href="/workspace">설치 없이 바로 조사하기 — 입법조사 워크스페이스 alpha →</a></p>
<p><a href="https://open.assembly.go.kr/portal/openapi/openApiNaListPage.do">열린국회에서 API 키 발급 / Get an API key from Open Assembly</a><br>
<small>공식 발급 사이트의 화면과 원문 데이터는 한국어로 제공됩니다. / The official issuance site and source records are in Korean.</small></p>
<div class="card"><h2>Claude.ai·ChatGPT 연결 — 주소 하나로 시작</h2>
<p>사용하는 AI의 <strong>커스텀 커넥터·앱 추가</strong> 화면에 아래 주소를 입력하세요.
연결 승인 화면이 열리면 그때 본인의 열린국회 API 키를 입력합니다.</p>
<p><code>https://korean-bill-debate-mcp.vercel.app/mcp</code></p>
<p><small>개인 링크가 아니라 위 공용 주소를 사용해야 표준 OAuth 연결이 시작됩니다.<br>
For Claude.ai or ChatGPT, add the public URL above and complete OAuth approval.</small></p></div>
<br><div class="card"><h2>호환용 개인 MCP 링크<br><span class="english">Legacy personal MCP URL</span></h2>
<p>OAuth를 지원하지 않는 다른 클라이언트에서만 사용합니다. Claude.ai와 ChatGPT에는 위 공용
주소를 사용하세요.<br><span class="english">Only use this with clients that cannot complete OAuth.</span></p>
{message}<form method="post" action="{html.escape(action)}">
<label>열린국회 API 키 / Open Assembly API key<input name="api_key" type="password" required autocomplete="off"></label>
<button type="submit">호환용 링크 만들기 / Create legacy link</button></form>
<p><small>키 원문은 데이터베이스나 파일에 저장하지 않습니다. 암호화된 연결 토큰을 발급하고,
요청 순간에만 사용자의 키로 열린국회 공식 API를 호출합니다.<br>
The raw key is not stored in a database or file. It is used only while requesting official records.</small></p></div></main></body></html>"""


def result_page(mcp_url: str) -> str:
    escaped = html.escape(mcp_url)
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>MCP 연결 링크 / Connection URL</title><style>
body{{margin:0;background:#061a31;color:#f7f2e8;font:16px/1.6 system-ui,sans-serif}}
main{{max-width:760px;margin:8vh auto;padding:36px}}.card{{background:#0b2945;border:1px solid #2b5a71;border-radius:20px;padding:28px}}
textarea{{box-sizing:border-box;width:100%;min-height:130px;padding:14px;font:14px/1.5 ui-monospace,monospace}}
h1{{font-size:40px}}strong{{color:#f0cc83}}.english{{color:#b8c8d4}}</style></head><body><main><h1>개인 MCP 링크가 준비됐습니다.<br><span class="english">Your personal MCP URL is ready.</span></h1>
<div class="card"><p>아래 주소는 OAuth를 지원하지 않는 호환 클라이언트에서만 사용하세요.<br>
<span class="english">Use this URL only with a legacy client that cannot complete OAuth.</span></p>
<textarea readonly onclick="this.select()">{escaped}</textarea>
<p><strong>이 링크는 비밀번호처럼 보관하세요. / Treat this URL like a password.</strong><br>
링크를 아는 사람은 사용자의 열린국회 API 할당량을 사용할 수 있습니다.<br>
<span class="english">Anyone holding it can consume your Open Assembly API quota.</span></p>
<p><strong>Claude.ai와 ChatGPT에는 이 개인 링크를 넣지 마세요.</strong> 두 서비스 모두
<code>https://korean-bill-debate-mcp.vercel.app/mcp</code>를 등록하고 OAuth 승인을 완료해야 합니다.</p>
<p><strong>등록만 하면 끝이 아닙니다.</strong><br>
새 채팅을 열고 입력창 아래의 <strong>+ 또는 도구 메뉴</strong>에서 방금 만든
<strong>Korean Bill &amp; Debate</strong> 앱·커넥터를 선택하세요. 선택한 뒤 질문해야 의안 원문과
회의록 도구가 호출됩니다.<br><span class="english"><strong>One final step:</strong> open a new
chat and enable Korean Bill &amp; Debate from the + / Tools menu before asking your question.</span></p>
</div></main></body></html>"""
