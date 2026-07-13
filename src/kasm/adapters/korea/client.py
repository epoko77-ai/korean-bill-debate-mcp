"""Client for dataset-specific APIs on the official Open Assembly portal."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any

from kasm import __version__

OPEN_ASSEMBLY_BASE_URL = "https://open.assembly.go.kr/portal/openapi"
_USER_AGENT = f"Mozilla/5.0 (compatible; KASM/{__version__})"


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


@dataclass(frozen=True, slots=True)
class ApiResult:
    """A complete, validated snapshot of one Open Assembly query.

    ``pages`` retains the redacted source URL and content hash for every response so
    callers do not have to trade provenance for the convenience of flattened rows.
    """

    dataset: str
    page_size: int
    total_count: int
    rows: tuple[dict[str, Any], ...]
    pages: tuple[ApiPage, ...]

    @property
    def source_urls(self) -> tuple[str, ...]:
        return tuple(page.source_url for page in self.pages)

    @property
    def source_hashes(self) -> tuple[str, ...]:
        return tuple(page.source_hash for page in self.pages)

    @property
    def source_hash(self) -> str:
        """Return a deterministic hash representing all page payloads in order."""

        digest = hashlib.sha256()
        for page in self.pages:
            digest.update(f"{page.page}:".encode())
            digest.update(page.source_hash.encode())
            digest.update(b"\n")
        return digest.hexdigest()


class AssemblyOpenApiClient:
    """Cacheable client without third-party HTTP dependencies."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        api_key_provider: Callable[[], str | None] | None = None,
        cache_dir: str | Path | None = None,
        timeout: float = 30.0,
        cache_ttl_seconds: float = 900.0,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.api_key = (
            api_key or os.getenv("ASSEMBLY_OPEN_API_KEY") or _dotenv_key() or _stored_key()
        )
        self._api_key_provider = api_key_provider
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
        api_key = self._api_key_provider() if self._api_key_provider else self.api_key
        if not api_key:
            raise AssemblyApiError(
                "ASSEMBLY_OPEN_API_KEY is required; issue a key at open.assembly.go.kr"
            )
        if not dataset.isalnum():
            raise ValueError("dataset must be the alphanumeric Open Assembly dataset code")
        if page < 1 or not 1 <= page_size <= 1000:
            raise ValueError("page must be positive and page_size must be between 1 and 1000")
        supplied_parameters = dict(parameters or {})
        reserved = {"key", "type", "pindex", "psize"}
        invalid = sorted(key for key in supplied_parameters if key.lower() in reserved)
        if invalid:
            raise ValueError(
                "parameters must not override pagination or authentication fields: "
                + ", ".join(invalid)
            )
        query: dict[str, str | int] = {
            "KEY": api_key,
            "Type": "json",
            "pIndex": page,
            "pSize": page_size,
        }
        query.update((key, supplied_parameters[key]) for key in sorted(supplied_parameters))
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
                    "User-Agent": _USER_AGENT,
                    "Referer": "https://open.assembly.go.kr/",
                    "Accept": "application/json",
                },
            )
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    raw = response.read()
            except OSError as exc:
                safe_error = str(exc).replace(api_key, "***")
                raise AssemblyApiError(f"Open Assembly request failed: {safe_error}") from exc
            if cache_path:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(raw)
        return self._decode(dataset, page, page_size, url, raw)

    def iter_pages(
        self,
        dataset: str,
        *,
        page_size: int = 100,
        parameters: Mapping[str, str | int] | None = None,
        refresh: bool = False,
    ) -> Iterator[ApiPage]:
        """Yield every page in a query, failing rather than returning partial data.

        The first response fixes the expected total for this iteration. Later pages
        must report the same total, contain the exact number of rows implied by that
        total, and must not repeat an entire earlier page.  Individual identical rows
        are preserved: several official datasets legitimately publish duplicate-looking
        records (for example, one meeting repeated for multiple agenda entries).  If the
        dataset changes during pagination, callers receive an explicit error and can
        retry to obtain a coherent snapshot.
        """

        first = self.fetch_page(
            dataset,
            page=1,
            page_size=page_size,
            parameters=parameters,
            refresh=refresh,
        )
        if first.total_count is None or first.total_count < 0:
            raise AssemblyApiError(f"{dataset} pagination failed at page 1: invalid total_count")

        total_count = first.total_count
        total_pages = max(1, (total_count + page_size - 1) // page_size)
        seen_pages: set[str] = set()

        for page_number in range(1, total_pages + 1):
            if page_number == 1:
                current = first
            else:
                try:
                    current = self.fetch_page(
                        dataset,
                        page=page_number,
                        page_size=page_size,
                        parameters=parameters,
                        refresh=refresh,
                    )
                except (AssemblyApiError, OSError) as exc:
                    raise AssemblyApiError(
                        f"{dataset} pagination failed at page {page_number}: {exc}"
                    ) from exc

            if current.total_count != total_count:
                raise AssemblyApiError(
                    f"{dataset} pagination failed at page {page_number}: "
                    f"total_count changed from {total_count} to {current.total_count}"
                )

            expected_rows = min(
                page_size,
                max(0, total_count - ((page_number - 1) * page_size)),
            )
            if len(current.rows) != expected_rows:
                raise AssemblyApiError(
                    f"{dataset} pagination failed at page {page_number}: "
                    f"expected {expected_rows} rows but received {len(current.rows)}"
                )

            page_fingerprint = _page_fingerprint(current.rows)
            if current.rows and page_fingerprint in seen_pages:
                raise AssemblyApiError(
                    f"{dataset} pagination failed at page {page_number}: "
                    "received a repeated page"
                )
            seen_pages.add(page_fingerprint)
            yield current

    def fetch_all(
        self,
        dataset: str,
        *,
        page_size: int = 100,
        parameters: Mapping[str, str | int] | None = None,
        refresh: bool = False,
    ) -> ApiResult:
        """Fetch a complete dataset query and retain page-level provenance."""

        pages = tuple(
            self.iter_pages(
                dataset,
                page_size=page_size,
                parameters=parameters,
                refresh=refresh,
            )
        )
        rows = tuple(row for page in pages for row in page.rows)
        total_count = pages[0].total_count
        if total_count is None or len(rows) != total_count:
            raise AssemblyApiError(
                f"{dataset} pagination failed: expected {total_count} total rows "
                f"but received {len(rows)}"
            )
        return ApiResult(dataset, page_size, total_count, rows, pages)

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
            if isinstance(top_result, dict):
                code, message = _safe_result(top_result, url)
                if code == "INFO-200":
                    return ApiPage(dataset, page, page_size, 0, (), _redact_key(url), source_hash)
                raise AssemblyApiError(f"{code}: {message}".rstrip())
            sections = payload[dataset]
            head = sections[0]["head"]
            result = head[1]["RESULT"]
            if result["CODE"] != "INFO-000":
                code, message = _safe_result(result, url)
                raise AssemblyApiError(f"{code}: {message}".rstrip())
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


def _safe_result(result: Mapping[str, Any], url: str) -> tuple[str, str]:
    """Return bounded API diagnostics with the request credential removed."""

    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    api_keys = tuple(key for key in query.get("KEY", ()) if key)
    code = _safe_result_field(
        result.get("CODE"), fallback="UNKNOWN", limit=64, secrets=api_keys
    )
    message = _safe_result_field(
        result.get("MESSAGE"), fallback="", limit=500, secrets=api_keys
    )
    return code, message


def _safe_result_field(
    value: Any, *, fallback: str, limit: int, secrets: tuple[str, ...]
) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return fallback
    normalized = " ".join(str(value).split()) or fallback
    for api_key in secrets:
        if api_key:
            normalized = normalized.replace(api_key, "***")
            encoded_key = urllib.parse.quote_plus(api_key)
            normalized = normalized.replace(encoded_key, "***")
    return normalized[:limit]


def _row_fingerprint(row: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _page_fingerprint(rows: tuple[dict[str, Any], ...]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_row_fingerprint(row).encode())
        digest.update(b"\n")
    return digest.hexdigest()


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
