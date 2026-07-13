"""Short-lived encrypted capabilities for background metadata collection."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken


@dataclass(frozen=True, slots=True)
class ResearchCredential:
    """A decrypted capability held only for the duration of one worker operation."""

    research_id: str
    query_fingerprint: str
    assembly_api_key: str
    expires_at: float


class ResearchCredentialCodec:
    """Bind a user's key to one research job without persisting the raw value."""

    def __init__(
        self,
        secret: str,
        *,
        now: Callable[[], float] | None = None,
    ) -> None:
        try:
            self._cipher = Fernet(secret.encode())
        except (TypeError, ValueError) as exc:
            raise ValueError("research credential secret must be a Fernet key") from exc
        self._now = now or time.time

    def issue(
        self,
        *,
        research_id: str,
        query_fingerprint: str,
        assembly_api_key: str,
        ttl_seconds: int = 3600,
    ) -> str:
        api_key = assembly_api_key.strip()
        if not research_id.strip():
            raise ValueError("research_id is required")
        _validate_fingerprint(query_fingerprint)
        if not 1 <= len(api_key) <= 256:
            raise ValueError("Open Assembly API key must be between 1 and 256 characters")
        if not 60 <= ttl_seconds <= 86_400:
            raise ValueError("credential ttl must be between 60 seconds and 24 hours")
        payload = json.dumps(
            {
                "schema_version": 1,
                "purpose": "research_metadata_collection",
                "research_id": research_id,
                "query_fingerprint": query_fingerprint,
                "assembly_api_key": api_key,
                "expires_at": self._now() + ttl_seconds,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return self._cipher.encrypt(payload).decode()

    def reveal(
        self,
        token: str,
        *,
        research_id: str,
        query_fingerprint: str,
    ) -> ResearchCredential:
        _validate_fingerprint(query_fingerprint)
        try:
            raw = self._cipher.decrypt(token.encode())
            payload = json.loads(raw)
        except (InvalidToken, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid or expired research credential") from exc
        if not isinstance(payload, dict):
            raise ValueError("invalid or expired research credential")
        if payload.get("schema_version") != 1 or payload.get("purpose") != (
            "research_metadata_collection"
        ):
            raise ValueError("invalid or expired research credential")
        if payload.get("research_id") != research_id:
            raise ValueError("research credential belongs to another job")
        if payload.get("query_fingerprint") != query_fingerprint:
            raise ValueError("research credential belongs to another query")
        expires_value = payload.get("expires_at")
        if not isinstance(expires_value, (str, int, float)):
            raise ValueError("invalid or expired research credential")
        try:
            expires_at = float(expires_value)
        except ValueError as exc:
            raise ValueError("invalid or expired research credential") from exc
        if expires_at <= self._now():
            raise ValueError("invalid or expired research credential")
        api_key = str(payload.get("assembly_api_key") or "")
        if not 1 <= len(api_key) <= 256:
            raise ValueError("invalid or expired research credential")
        return ResearchCredential(
            research_id=research_id,
            query_fingerprint=query_fingerprint,
            assembly_api_key=api_key,
            expires_at=expires_at,
        )


def _validate_fingerprint(value: str) -> None:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("query_fingerprint must be a SHA-256 hex digest")
