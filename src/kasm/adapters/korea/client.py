"""Client for dataset-specific APIs on the official Open Assembly portal."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any

OPEN_ASSEMBLY_BASE_URL = "https://open.assembly.go.kr/portal/openapi"


class AssemblyApiError(RuntimeError):
    """Raised when the official API returns an invalid or error response."""


@dataclass(frozen=True, slots=True)
class ApiPage:
    dataset: str
    page: int
    page_size: int
    total_count: int | None
    rows: tuple[dict[str, Any], ...]
    source_url: str
    source_hash: str


class AssemblyOpenApiClient:
    """Cacheable client without third-party HTTP dependencies."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        cache_dir: str | Path | None = None,
        timeout: float = 30.0,
        cache_ttl_seconds: float = 900.0,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.api_key = (
            api_key or os.getenv("ASSEMBLY_OPEN_API_KEY") or _dotenv_key() or _stored_key()
        )
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.timeout = timeout
        self.cache_ttl_seconds = cache_ttl_seconds
        self._opener = opener

    def fetch_page(
        self,
        dataset: str,
        *,
        page: int = 1,
        page_size: int = 100,
        parameters: Mapping[str, str | int] | None = None,
        refresh: bool = False,
    ) -> ApiPage:
        if not self.api_key:
            raise AssemblyApiError(
                "ASSEMBLY_OPEN_API_KEY is required; issue a key at open.assembly.go.kr"
            )
        if not dataset.isalnum():
            raise ValueError("dataset must be the alphanumeric Open Assembly dataset code")
        if page < 1 or not 1 <= page_size <= 1000:
            raise ValueError("page must be positive and page_size must be between 1 and 1000")
        query: dict[str, str | int] = {
            "KEY": self.api_key,
            "Type": "json",
            "pIndex": page,
            "pSize": page_size,
            **(parameters or {}),
        }
        url = f"{OPEN_ASSEMBLY_BASE_URL}/{dataset}?{urllib.parse.urlencode(query)}"
        cache_path = self._cache_path(dataset, query)
        cache_fresh = (
            cache_path
            and cache_path.exists()
            and time() - cache_path.stat().st_mtime <= self.cache_ttl_seconds
        )
        if cache_fresh and not refresh:
            assert cache_path is not None
            raw = cache_path.read_bytes()
        else:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; KASM/0.1)",
                    "Referer": "https://open.assembly.go.kr/",
                    "Accept": "application/json",
                },
            )
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    raw = response.read()
            except OSError as exc:
                safe_error = str(exc).replace(self.api_key, "***")
                raise AssemblyApiError(f"Open Assembly request failed: {safe_error}") from exc
            if cache_path:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(raw)
        return self._decode(dataset, page, page_size, url, raw)

    def _cache_path(self, dataset: str, query: Mapping[str, str | int]) -> Path | None:
        if self.cache_dir is None:
            return None
        safe_query = {key: value for key, value in query.items() if key != "KEY"}
        fingerprint = hashlib.sha256(json.dumps(safe_query, sort_keys=True).encode()).hexdigest()[
            :16
        ]
        return self.cache_dir / dataset / f"{fingerprint}.json"

    @staticmethod
    def _decode(dataset: str, page: int, page_size: int, url: str, raw: bytes) -> ApiPage:
        source_hash = hashlib.sha256(raw).hexdigest()
        try:
            payload = json.loads(raw)
            top_result = payload.get("RESULT") if isinstance(payload, dict) else None
            if isinstance(top_result, dict) and top_result.get("CODE") == "INFO-200":
                return ApiPage(dataset, page, page_size, 0, (), _redact_key(url), source_hash)
            sections = payload[dataset]
            head = sections[0]["head"]
            result = head[1]["RESULT"]
            if result["CODE"] != "INFO-000":
                raise AssemblyApiError(f"{result['CODE']}: {result.get('MESSAGE', '')}")
            total_count = int(head[0]["list_total_count"])
            rows = tuple(dict(row) for row in sections[1].get("row", []))
        except AssemblyApiError:
            raise
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AssemblyApiError("Unexpected Open Assembly response schema") from exc
        return ApiPage(dataset, page, page_size, total_count, rows, _redact_key(url), source_hash)


def _redact_key(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    safe = [(key, "***" if key.upper() == "KEY" else value) for key, value in pairs]
    return urllib.parse.urlunsplit((*parsed[:3], urllib.parse.urlencode(safe), parsed.fragment))


def _dotenv_key(path: Path = Path(".env")) -> str | None:
    """Read only the expected key from a local ignored env file."""
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == "ASSEMBLY_OPEN_API_KEY":
            return value.strip().strip("'\"") or None
    return None


def _stored_key() -> str | None:
    configured = os.getenv("KBD_CREDENTIALS_FILE")
    path = (
        Path(configured)
        if configured
        else Path.home() / ".config/korean-bill-debate-mcp/credentials.env"
    )
    return _dotenv_key(path)
