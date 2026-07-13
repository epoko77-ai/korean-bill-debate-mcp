"""Small stateless OAuth 2.1 authorization server for hosted MCP users."""

# ruff: noqa: E501 - embedded consent HTML is kept readable as rendered markup

from __future__ import annotations

import base64
import hashlib
import html
import time
import urllib.parse
from typing import Any

from .remote_auth import RemoteTokenAuth

_SCOPE = "mcp:tools"


class RemoteOAuth:
    """Issue per-user MCP access tokens without persisting Open Assembly keys."""

    def __init__(self, tokens: RemoteTokenAuth) -> None:
        self.tokens = tokens

    @staticmethod
    def protected_resource_metadata(base: str) -> dict[str, Any]:
        return {
            "resource": f"{base}/mcp",
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
            "scopes_supported": [_SCOPE],
            "resource_documentation": base,
        }

    @staticmethod
    def authorization_server_metadata(base: str) -> dict[str, Any]:
        return {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "scopes_supported": [_SCOPE],
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
        }

    def register(self, metadata: dict[str, Any]) -> dict[str, Any]:
        redirect_uris = metadata.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not 1 <= len(redirect_uris) <= 5:
            raise ValueError("redirect_uris is required")
        normalized = [str(uri) for uri in redirect_uris]
        if any(not _allowed_redirect(uri) for uri in normalized):
            raise ValueError("redirect_uri must use HTTPS or a loopback HTTP address")
        client_name = str(metadata.get("client_name") or "MCP client")[:120]
        client_id = self.tokens.issue_payload(
            "oauth_client", {"redirect_uris": normalized, "client_name": client_name}
        )
        return {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": normalized,
            "client_name": client_name,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }

    def validate_authorization(self, values: dict[str, str]) -> dict[str, Any]:
        client_id = values.get("client_id", "")
        client = self.tokens.reveal_payload(
            client_id, "oauth_client", ttl_seconds=365 * 24 * 60 * 60
        )
        redirect_uri = values.get("redirect_uri", "")
        if redirect_uri not in client.get("redirect_uris", []):
            raise ValueError("redirect_uri does not match the registered client")
        if values.get("response_type") != "code":
            raise ValueError("response_type must be code")
        challenge = values.get("code_challenge", "")
        if values.get("code_challenge_method") != "S256" or not 43 <= len(challenge) <= 128:
            raise ValueError("PKCE S256 is required")
        state = values.get("state", "")
        if len(state) > 2048:
            raise ValueError("state is too long")
        resource = values.get("resource", "")
        scope = values.get("scope") or _SCOPE
        if _SCOPE not in scope.split():
            raise ValueError("mcp:tools scope is required")
        return {
            **values,
            "client_name": str(client.get("client_name") or "AI client"),
            "redirect_uri": redirect_uri,
            "resource": resource,
            "scope": _SCOPE,
        }

    def authorization_page(self, values: dict[str, str], *, error: str = "") -> str:
        validated = self.validate_authorization(values)
        hidden = "".join(
            f'<input type="hidden" name="{html.escape(name)}" value="{html.escape(value)}">'
            for name, value in validated.items()
            if name != "client_name"
        )
        message = f'<p class="error">{html.escape(error)}</p>' if error else ""
        return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>MCP 연결 승인</title><style>
body{{margin:0;background:#061a31;color:#f7f2e8;font:16px/1.6 system-ui,sans-serif}}
main{{max-width:680px;margin:8vh auto;padding:32px}}.card{{background:#0b2945;border:1px solid #2b5a71;border-radius:20px;padding:28px}}
input{{box-sizing:border-box;width:100%;padding:14px;border-radius:10px;border:1px solid #789;font-size:16px}}
button{{margin-top:14px;padding:14px 20px;border:0;border-radius:10px;background:#e5b85c;color:#071728;font-weight:800}}
.error{{color:#ffb3a8}}small{{color:#b8c8d4}}a{{color:#f0cc83}}</style></head><body><main>
<h1>Korean Bill &amp; Debate MCP 연결</h1><div class="card">
<p><strong>{html.escape(str(validated['client_name']))}</strong>에서 국회 조사 도구를 사용하도록 승인합니다.</p>
{message}<form method="post" action="/oauth/authorize">{hidden}
<label>본인의 열린국회 API 키<input name="api_key" type="password" required autocomplete="off"></label>
<button type="submit">AI에 연결 승인</button></form>
<p><a href="https://open.assembly.go.kr/portal/openapi/openApiNaListPage.do">열린국회에서 API 키 발급</a></p>
<p><small>키 원문은 데이터베이스나 파일에 저장하지 않습니다. 암호화된 접근 토큰으로 바꿔
MCP 요청 순간에만 사용합니다.</small></p></div></main></body></html>"""

    def authorize(self, values: dict[str, str], api_key: str) -> str:
        validated = self.validate_authorization(values)
        code = self.tokens.issue_payload(
            "oauth_code",
            {
                "api_key": api_key,
                "client_hash": _digest(validated["client_id"]),
                "redirect_uri": validated["redirect_uri"],
                "code_challenge": validated["code_challenge"],
                "resource": validated["resource"],
                "scope": validated["scope"],
            },
        )
        query = {"code": code}
        if validated.get("state"):
            query["state"] = validated["state"]
        return str(validated["redirect_uri"]) + "?" + urllib.parse.urlencode(query)

    def token(self, values: dict[str, str]) -> dict[str, Any]:
        grant_type = values.get("grant_type", "")
        if grant_type == "authorization_code":
            code = self.tokens.reveal_payload(
                values.get("code", ""), "oauth_code", ttl_seconds=10 * 60
            )
            if _digest(values.get("client_id", "")) != code.get("client_hash"):
                raise ValueError("client_id does not match authorization code")
            if values.get("redirect_uri", "") != code.get("redirect_uri"):
                raise ValueError("redirect_uri does not match authorization code")
            if values.get("resource") and values["resource"] != code.get("resource"):
                raise ValueError("resource does not match authorization code")
            if _pkce_challenge(values.get("code_verifier", "")) != code.get(
                "code_challenge"
            ):
                raise ValueError("PKCE verification failed")
            api_key = str(code.get("api_key") or "")
            resource = str(code.get("resource") or "")
            scope = str(code.get("scope") or _SCOPE)
            client_hash = str(code.get("client_hash") or "")
        elif grant_type == "refresh_token":
            refresh = self.tokens.reveal_payload(
                values.get("refresh_token", ""),
                "oauth_refresh",
                ttl_seconds=90 * 24 * 60 * 60,
            )
            if _digest(values.get("client_id", "")) != refresh.get("client_hash"):
                raise ValueError("client_id does not match refresh token")
            if values.get("resource") and values["resource"] != refresh.get("resource"):
                raise ValueError("resource does not match refresh token")
            api_key = str(refresh.get("api_key") or "")
            resource = str(refresh.get("resource") or "")
            scope = str(refresh.get("scope") or _SCOPE)
            client_hash = str(refresh.get("client_hash") or "")
        else:
            raise ValueError("unsupported grant_type")
        if not api_key:
            raise ValueError("authorization contains no API key")
        access_token = self.tokens.issue_payload(
            "access",
            {
                "api_key": api_key,
                "resource": resource,
                "scope": scope,
                "expires_at": time.time() + 60 * 60,
            },
        )
        refresh_token = self.tokens.issue_payload(
            "oauth_refresh",
            {
                "api_key": api_key,
                "resource": resource,
                "scope": scope,
                "client_hash": client_hash,
            },
        )
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": refresh_token,
            "scope": scope,
        }


def _allowed_redirect(uri: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(uri)
    except ValueError:
        return False
    if parsed.fragment or parsed.username or parsed.password:
        return False
    if parsed.scheme == "https" and bool(parsed.hostname):
        return True
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _pkce_challenge(verifier: str) -> str:
    if not 43 <= len(verifier) <= 128:
        return ""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
