"""Immutable, secret-free storage for durable research execution artifacts.

Artifacts contain public research state, never request credentials.  Every
payload is normalized to canonical JSON, recursively checked for secret
material, hashed, and stored either by its content hash or at a logical
write-once path.  Repeating the same write is idempotent; attempting to change
an existing logical artifact is an explicit conflict.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
import urllib.parse
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

_SCHEMA_VERSION = 1
_RESEARCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_LOGICAL_KEY_CHARS = 512
_MAX_JSON_DEPTH = 100
_REDACTED_VALUES = {"", "***", "redacted", "[redacted]", "<redacted>"}
_NON_SECRET_TOKEN_FIELDS = {"max_tokens", "token_budget", "token_count", "token_usage"}
_SECRET_FIELD_NAMES = {
    "api_key",
    "apikey",
    "access_token",
    "assembly_open_api_key",
    "authorization",
    "capability",
    "client_secret",
    "cookie",
    "credential",
    "credential_capability",
    "credentials",
    "id_token",
    "password",
    "refresh_token",
    "secret",
    "token",
}
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)^bearer\s+\S{8,}$"),
    re.compile(r"^sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}$"),
    re.compile(r"^gAAAA[A-Za-z0-9_-]{32,}$"),
    re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$"),
    re.compile(r"(?i)(?:^|/)mcp/t/[A-Za-z0-9_-]{24,}(?:$|[/?#])"),
)


class ArtifactKind(StrEnum):
    PLAN = "plan"
    PARTITION = "partition"
    METADATA = "metadata"
    RESOLUTION = "resolution"
    MANIFEST = "manifest"
    OUTCOME = "outcome"
    RESULT_PAGE = "result_page"


class ArtifactError(RuntimeError):
    """Base class whose messages never include upstream bodies or credentials."""


class SecretMaterialError(ValueError):
    """Raised before an artifact containing credential material can be written."""


class ArtifactConflictError(ArtifactError):
    """Raised when a logical write-once path already contains different bytes."""


class ArtifactIntegrityError(ArtifactError):
    """Raised when stored canonical bytes, identity, or hashes do not match."""


class ArtifactBackendError(ArtifactError):
    """Raised for sanitized filesystem or Blob backend failures."""


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    research_id: str
    kind: ArtifactKind
    object_path: str
    content_hash: str
    byte_size: int
    logical_key: str | None = None

    def __post_init__(self) -> None:
        _validate_research_id(self.research_id)
        if not _SHA256.fullmatch(self.content_hash):
            raise ValueError("content_hash must be a SHA-256 hex digest")
        if self.byte_size < 1:
            raise ValueError("artifact byte_size must be positive")
        if self.logical_key is not None:
            _validate_logical_key(self.logical_key)
        expected = _object_path(
            self.research_id,
            self.kind,
            self.content_hash,
            self.logical_key,
        )
        if self.object_path != expected:
            raise ValueError("artifact object_path does not match its immutable identity")


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    ref: ArtifactRef
    payload: Any


class ResearchArtifactStore(Protocol):
    def write(
        self,
        research_id: str,
        kind: ArtifactKind,
        payload: Any,
        *,
        logical_key: str | None = None,
    ) -> ArtifactRef: ...

    def read(self, ref: ArtifactRef) -> StoredArtifact | None: ...

    def read_logical(
        self,
        research_id: str,
        kind: ArtifactKind,
        logical_key: str,
    ) -> StoredArtifact | None: ...

    def list(
        self, research_id: str, kind: ArtifactKind | None = None
    ) -> tuple[ArtifactRef, ...]: ...


class BaseResearchArtifactStore(ABC):
    """Backend contract plus artifact-type-specific write-once conventions."""

    @abstractmethod
    def write(
        self,
        research_id: str,
        kind: ArtifactKind,
        payload: Any,
        *,
        logical_key: str | None = None,
    ) -> ArtifactRef:
        raise NotImplementedError

    @abstractmethod
    def read(self, ref: ArtifactRef) -> StoredArtifact | None:
        raise NotImplementedError

    @abstractmethod
    def read_logical(
        self,
        research_id: str,
        kind: ArtifactKind,
        logical_key: str,
    ) -> StoredArtifact | None:
        """Read a write-once artifact without listing its sibling objects."""

        raise NotImplementedError

    @abstractmethod
    def list(
        self, research_id: str, kind: ArtifactKind | None = None
    ) -> tuple[ArtifactRef, ...]:
        raise NotImplementedError

    def write_plan(self, research_id: str, payload: Any) -> ArtifactRef:
        return self.write(research_id, ArtifactKind.PLAN, payload, logical_key="plan")

    def write_partition(
        self, research_id: str, partition_id: str, payload: Any
    ) -> ArtifactRef:
        return self.write(
            research_id,
            ArtifactKind.PARTITION,
            payload,
            logical_key=partition_id,
        )

    def write_metadata(self, research_id: str, payload: Any) -> ArtifactRef:
        return self.write(research_id, ArtifactKind.METADATA, payload)

    def write_resolution(
        self, research_id: str, resolution_id: str, payload: Any
    ) -> ArtifactRef:
        return self.write(
            research_id,
            ArtifactKind.RESOLUTION,
            payload,
            logical_key=resolution_id,
        )

    def write_manifest(self, research_id: str, payload: Any) -> ArtifactRef:
        return self.write(
            research_id,
            ArtifactKind.MANIFEST,
            payload,
            logical_key="manifest",
        )

    def write_outcome(self, research_id: str, payload: Any) -> ArtifactRef:
        return self.write(
            research_id,
            ArtifactKind.OUTCOME,
            payload,
            logical_key="outcome",
        )

    def write_result_page(
        self, research_id: str, page_id: str | int, payload: Any
    ) -> ArtifactRef:
        return self.write(
            research_id,
            ArtifactKind.RESULT_PAGE,
            payload,
            logical_key=str(page_id),
        )

    # Familiar aliases for integrations that use object-store terminology.
    def put(
        self,
        research_id: str,
        kind: ArtifactKind,
        payload: Any,
        *,
        logical_key: str | None = None,
    ) -> ArtifactRef:
        return self.write(research_id, kind, payload, logical_key=logical_key)

    def get(self, ref: ArtifactRef) -> StoredArtifact | None:
        return self.read(ref)


class FilesystemResearchArtifactStore(BaseResearchArtifactStore):
    """Private local artifact store using atomic, no-overwrite file creation."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._resolved_root = self.root.resolve(strict=True)
        except OSError as exc:
            raise ArtifactBackendError("artifact filesystem initialization failed") from exc

    def write(
        self,
        research_id: str,
        kind: ArtifactKind,
        payload: Any,
        *,
        logical_key: str | None = None,
    ) -> ArtifactRef:
        encoded, ref = _encode_artifact(research_id, kind, payload, logical_key)
        path = self._safe_path(ref.object_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return self._verify_existing(path, encoded, ref)
        except OSError as exc:
            raise ArtifactBackendError("artifact filesystem write failed") from exc
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            # A partial O_EXCL file remains immutable and will fail integrity
            # checks rather than being silently overwritten.
            raise ArtifactBackendError("artifact filesystem write failed") from exc
        return ref

    def read(self, ref: ArtifactRef) -> StoredArtifact | None:
        path = self._safe_path(ref.object_path)
        if not path.exists():
            return None
        raw = self._read_bytes(path)
        stored = _decode_artifact(raw, expected_path=ref.object_path)
        if stored.ref != ref:
            raise ArtifactIntegrityError("stored artifact reference does not match")
        return stored

    def read_logical(
        self,
        research_id: str,
        kind: ArtifactKind,
        logical_key: str,
    ) -> StoredArtifact | None:
        object_path = _logical_object_path(research_id, kind, logical_key)
        path = self._safe_path(object_path)
        if not path.exists():
            return None
        stored = _decode_artifact(
            self._read_bytes(path),
            expected_path=object_path,
        )
        if (
            stored.ref.research_id != research_id
            or stored.ref.kind is not kind
            or stored.ref.logical_key != logical_key
        ):
            raise ArtifactIntegrityError("stored logical artifact identity does not match")
        return stored

    def list(
        self, research_id: str, kind: ArtifactKind | None = None
    ) -> tuple[ArtifactRef, ...]:
        _validate_research_id(research_id)
        kinds = (kind,) if kind is not None else tuple(ArtifactKind)
        references: list[ArtifactRef] = []
        for selected in kinds:
            directory = self._safe_path(f"{research_id}/{selected.value}")
            if not directory.exists():
                continue
            try:
                paths = sorted(directory.glob("*.json"))
            except OSError as exc:
                raise ArtifactBackendError("artifact filesystem list failed") from exc
            for path in paths:
                relative = path.relative_to(self.root).as_posix()
                references.append(
                    _decode_artifact(
                        self._read_bytes(path),
                        expected_path=relative,
                    ).ref
                )
        return tuple(sorted(references, key=lambda item: item.object_path))

    def _verify_existing(
        self, path: Path, expected: bytes, ref: ArtifactRef
    ) -> ArtifactRef:
        existing = self._read_bytes(path)
        if existing != expected:
            raise ArtifactConflictError(
                "immutable artifact path already contains different content"
            )
        stored = _decode_artifact(existing, expected_path=ref.object_path)
        if stored.ref != ref:
            raise ArtifactIntegrityError("stored artifact reference does not match")
        return ref

    def _read_bytes(self, path: Path) -> bytes:
        try:
            if path.is_symlink():
                raise ArtifactIntegrityError("artifact path must not be a symbolic link")
            return path.read_bytes()
        except ArtifactIntegrityError:
            raise
        except OSError as exc:
            raise ArtifactBackendError("artifact filesystem read failed") from exc

    def _safe_path(self, relative: str) -> Path:
        parts = PurePosixPath(relative)
        if parts.is_absolute() or not parts.parts or ".." in parts.parts:
            raise ArtifactIntegrityError("artifact path is outside the configured store")
        path = self.root.joinpath(*parts.parts)
        try:
            resolved = path.resolve(strict=False)
        except OSError as exc:
            raise ArtifactBackendError("artifact filesystem path validation failed") from exc
        if resolved != self._resolved_root and self._resolved_root not in resolved.parents:
            raise ArtifactIntegrityError("artifact path is outside the configured store")
        return path


class BlobObjectClient(Protocol):
    """Small injectable surface implemented by the optional Vercel SDK adapter."""

    def put(
        self,
        pathname: str,
        body: bytes,
        *,
        access: str,
        add_random_suffix: bool,
        allow_overwrite: bool,
        content_type: str,
    ) -> object: ...

    def get(self, pathname: str) -> bytes | None: ...

    def list(self, *, prefix: str) -> Iterable[str]: ...


class VercelBlobResearchArtifactStore(BaseResearchArtifactStore):
    """Immutable private Vercel Blob adapter with a lazily imported SDK."""

    def __init__(
        self,
        *,
        prefix: str = "research-artifacts",
        access: str = "private",
        client: BlobObjectClient | None = None,
        token_provider: Callable[[], str | None] | None = None,
    ) -> None:
        normalized_prefix = prefix.strip("/")
        if not normalized_prefix or any(
            part in {"", ".", ".."} for part in normalized_prefix.split("/")
        ):
            raise ValueError("invalid Vercel Blob artifact prefix")
        if access != "private":
            raise ValueError("research artifacts require private Vercel Blob access")
        self.prefix = normalized_prefix
        self.access = access
        self._injected_client = client
        self._loaded_client: BlobObjectClient | None = None
        self._token_provider = token_provider or (
            lambda: os.getenv("BLOB_READ_WRITE_TOKEN", "")
            or os.getenv("VERCEL_BLOB_READ_WRITE_TOKEN", "")
        )

    def write(
        self,
        research_id: str,
        kind: ArtifactKind,
        payload: Any,
        *,
        logical_key: str | None = None,
    ) -> ArtifactRef:
        encoded, ref = _encode_artifact(research_id, kind, payload, logical_key)
        pathname = self._blob_path(ref.object_path)
        client = self._client()
        existing = self._safe_get(client, pathname)
        if existing is not None:
            return self._verify_existing(existing, encoded, ref)
        try:
            client.put(
                pathname,
                encoded,
                access=self.access,
                add_random_suffix=False,
                allow_overwrite=False,
                content_type="application/json; charset=utf-8",
            )
        except Exception:
            # Resolve a possible create race without ever including the SDK's
            # exception or response body in our public error.
            raced = self._safe_get(client, pathname, suppress_errors=True)
            if raced is not None:
                return self._verify_existing(raced, encoded, ref)
            raise ArtifactBackendError("Vercel Blob artifact write failed") from None
        return ref

    def read(self, ref: ArtifactRef) -> StoredArtifact | None:
        raw = self._safe_get(self._client(), self._blob_path(ref.object_path))
        if raw is None:
            return None
        stored = _decode_artifact(raw, expected_path=ref.object_path)
        if stored.ref != ref:
            raise ArtifactIntegrityError("stored artifact reference does not match")
        return stored

    def read_logical(
        self,
        research_id: str,
        kind: ArtifactKind,
        logical_key: str,
    ) -> StoredArtifact | None:
        object_path = _logical_object_path(research_id, kind, logical_key)
        raw = self._safe_get(self._client(), self._blob_path(object_path))
        if raw is None:
            return None
        stored = _decode_artifact(raw, expected_path=object_path)
        if (
            stored.ref.research_id != research_id
            or stored.ref.kind is not kind
            or stored.ref.logical_key != logical_key
        ):
            raise ArtifactIntegrityError("stored logical artifact identity does not match")
        return stored

    def list(
        self, research_id: str, kind: ArtifactKind | None = None
    ) -> tuple[ArtifactRef, ...]:
        _validate_research_id(research_id)
        kinds = (kind,) if kind is not None else tuple(ArtifactKind)
        client = self._client()
        refs: list[ArtifactRef] = []
        for selected in kinds:
            prefix = self._blob_path(f"{research_id}/{selected.value}/")
            try:
                pathnames = tuple(client.list(prefix=prefix))
            except Exception:
                raise ArtifactBackendError("Vercel Blob artifact list failed") from None
            for pathname in pathnames:
                relative = self._relative_blob_path(pathname)
                raw = self._safe_get(client, pathname)
                if raw is None:
                    raise ArtifactIntegrityError("listed Vercel Blob artifact is missing")
                refs.append(_decode_artifact(raw, expected_path=relative).ref)
        return tuple(sorted(refs, key=lambda item: item.object_path))

    def _verify_existing(
        self, existing: bytes, expected: bytes, ref: ArtifactRef
    ) -> ArtifactRef:
        if existing != expected:
            raise ArtifactConflictError(
                "immutable artifact path already contains different content"
            )
        stored = _decode_artifact(existing, expected_path=ref.object_path)
        if stored.ref != ref:
            raise ArtifactIntegrityError("stored artifact reference does not match")
        return ref

    def _safe_get(
        self,
        client: BlobObjectClient,
        pathname: str,
        *,
        suppress_errors: bool = False,
    ) -> bytes | None:
        try:
            value = client.get(pathname)
        except Exception:
            if suppress_errors:
                return None
            raise ArtifactBackendError("Vercel Blob artifact read failed") from None
        if value is not None and not isinstance(value, bytes):
            raise ArtifactIntegrityError("Vercel Blob artifact content is not bytes")
        return value

    def _client(self) -> BlobObjectClient:
        if self._injected_client is not None:
            return self._injected_client
        if self._loaded_client is not None:
            return self._loaded_client
        try:
            token = str(self._token_provider() or "").strip()
        except Exception:
            raise ArtifactBackendError("Vercel Blob credential provider failed") from None
        if not token:
            raise ArtifactBackendError("Vercel Blob credentials are not configured")
        try:
            module = importlib.import_module("vercel.blob")
            self._loaded_client = _VercelBlobModuleClient(module, token)
        except (ImportError, AttributeError, TypeError):
            raise ArtifactBackendError(
                "optional Vercel Blob SDK is not installed or compatible"
            ) from None
        return self._loaded_client

    def _blob_path(self, object_path: str) -> str:
        return f"{self.prefix}/{object_path}"

    def _relative_blob_path(self, pathname: str) -> str:
        prefix = self.prefix + "/"
        if not pathname.startswith(prefix):
            raise ArtifactIntegrityError("Vercel Blob listed a path outside the store prefix")
        relative = pathname[len(prefix) :]
        parts = PurePosixPath(relative)
        if parts.is_absolute() or ".." in parts.parts:
            raise ArtifactIntegrityError("Vercel Blob listed an invalid artifact path")
        return relative


class _VercelBlobModuleClient:
    """Adapter for the official ``vercel.blob`` Python SDK."""

    def __init__(self, module: Any, token: str) -> None:
        for name in ("put", "get"):
            if not callable(getattr(module, name, None)):
                raise AttributeError(f"Vercel Blob SDK lacks {name}")
        if not any(
            callable(getattr(module, name, None))
            for name in ("iter_objects", "list_objects")
        ):
            raise AttributeError("Vercel Blob SDK lacks iter_objects/list_objects")
        self.module = module
        self.token = token

    def put(
        self,
        pathname: str,
        body: bytes,
        *,
        access: str,
        add_random_suffix: bool,
        allow_overwrite: bool,
        content_type: str,
    ) -> object:
        return self.module.put(
            pathname,
            body,
            access=access,
            add_random_suffix=add_random_suffix,
            overwrite=allow_overwrite,
            content_type=content_type,
            token=self.token,
        )

    def get(self, pathname: str) -> bytes | None:
        try:
            result = self.module.get(
                pathname,
                access="private",
                token=self.token,
                use_cache=False,
            )
        except Exception as exc:
            not_found = getattr(self.module, "BlobNotFoundError", None)
            if isinstance(not_found, type) and isinstance(exc, not_found):
                return None
            raise
        if result is None:
            return None
        if isinstance(result, bytes):
            return result
        if isinstance(result, Mapping):
            content = result.get("content") or result.get("body")
            if isinstance(content, bytes):
                return content
        content = getattr(result, "content", None)
        if isinstance(content, bytes):
            return content
        reader = getattr(result, "read", None)
        if callable(reader):
            read = reader()
            if isinstance(read, bytes):
                return read
        raise TypeError("Vercel Blob SDK returned unsupported content")

    def list(self, *, prefix: str) -> Iterable[str]:
        iterator = getattr(self.module, "iter_objects", None)
        if callable(iterator):
            return _blob_pathnames(iterator(prefix=prefix, token=self.token))
        paths: list[str] = []

        # ``vercel.blob`` 0.6 exposes cursor-based ``list_objects`` rather
        # than ``iter_objects``. Exhaust every page: artifact inventories are
        # part of the completeness contract and silently accepting the first
        # page would make large investigations appear finished too early.
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            result = self.module.list_objects(
                prefix=prefix,
                cursor=cursor,
                token=self.token,
            )
            values = (
                result.get("blobs", ())
                if isinstance(result, Mapping)
                else result.blobs
            )
            paths.extend(_blob_pathnames(values))
            has_more = bool(
                result.get("has_more", result.get("hasMore", False))
                if isinstance(result, Mapping)
                else result.has_more
            )
            if not has_more:
                return tuple(paths)
            next_cursor = (
                result.get("cursor")
                if isinstance(result, Mapping)
                else result.cursor
            )
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen_cursors
            ):
                raise TypeError("Vercel Blob SDK returned an invalid list cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor


def _blob_pathnames(values: Iterable[Any]) -> tuple[str, ...]:
    paths: list[str] = []
    for item in values:
        if isinstance(item, str):
            paths.append(item)
        elif isinstance(item, Mapping):
            pathname = item.get("pathname") or item.get("path")
            if isinstance(pathname, str):
                paths.append(pathname)
        else:
            pathname = getattr(item, "pathname", None)
            if isinstance(pathname, str):
                paths.append(pathname)
    return tuple(paths)


def canonical_json(payload: Any) -> bytes:
    """Return stable UTF-8 JSON after recursively rejecting secret material."""

    normalized = _normalize_json(payload, path="$", active=set(), depth=0)
    try:
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("artifact payload is not canonical JSON") from exc


def canonical_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def assert_secret_free(payload: Any) -> None:
    """Validate JSON compatibility and recursively reject credentials or tokens."""

    _normalize_json(payload, path="$", active=set(), depth=0)


def _normalize_json(
    value: Any,
    *,
    path: str,
    active: set[int],
    depth: int,
) -> Any:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("artifact JSON exceeds the maximum nesting depth")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("artifact JSON numbers must be finite")
        return value
    if isinstance(value, str):
        _check_secret_string(value, path)
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError("artifact JSON must not contain reference cycles")
        active.add(identity)
        try:
            normalized: dict[str, Any] = {}
            for raw_key, item in value.items():
                if not isinstance(raw_key, str):
                    raise ValueError("artifact JSON object keys must be strings")
                _check_secret_field(raw_key, item)
                normalized[raw_key] = _normalize_json(
                    item,
                    path=f"{path}.{raw_key}",
                    active=active,
                    depth=depth + 1,
                )
            return normalized
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise ValueError("artifact JSON must not contain reference cycles")
        active.add(identity)
        try:
            return [
                _normalize_json(
                    item,
                    path=f"{path}[{index}]",
                    active=active,
                    depth=depth + 1,
                )
                for index, item in enumerate(value)
            ]
        finally:
            active.remove(identity)
    raise ValueError("artifact payload contains a non-JSON value")


def _check_secret_field(name: str, value: Any) -> None:
    normalized = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")
    prohibited = bool(
        normalized in _SECRET_FIELD_NAMES
        or normalized.endswith("_api_key")
        or (
            normalized.endswith("_token")
            and normalized not in _NON_SECRET_TOKEN_FIELDS
        )
        or normalized.endswith("_secret")
        or normalized.endswith("_capability")
        or normalized.startswith("credential_")
    )
    if normalized == "key" and isinstance(value, str):
        prohibited = len(value.strip()) >= 16 and value.strip().casefold() not in _REDACTED_VALUES
    if prohibited:
        raise SecretMaterialError("artifact contains a prohibited credential field")


def _check_secret_string(value: str, path: str) -> None:
    stripped = value.strip()
    if any(pattern.search(stripped) for pattern in _SECRET_VALUE_PATTERNS):
        raise SecretMaterialError("artifact contains prohibited credential material")
    if re.fullmatch(r"[A-Za-z0-9_-]{80,}", stripped) and not re.fullmatch(
        r"[0-9a-fA-F]{64}|[0-9a-fA-F]{128}", stripped
    ):
        raise SecretMaterialError("artifact contains opaque credential-like material")
    if stripped.startswith(("http://", "https://")):
        try:
            parsed = urllib.parse.urlsplit(stripped)
            if parsed.username or parsed.password:
                raise SecretMaterialError("artifact URL contains embedded credentials")
            for name, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
                normalized = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")
                if (
                    normalized in _SECRET_FIELD_NAMES
                    or normalized == "key"
                    or normalized.endswith("_token")
                    or normalized.endswith("_api_key")
                ) and item.strip().casefold() not in _REDACTED_VALUES:
                    raise SecretMaterialError("artifact URL contains credential parameters")
        except SecretMaterialError:
            raise
        except ValueError as exc:
            raise ValueError(f"artifact contains an invalid URL at {path}") from exc


def _encode_artifact(
    research_id: str,
    kind: ArtifactKind,
    payload: Any,
    logical_key: str | None,
) -> tuple[bytes, ArtifactRef]:
    _validate_research_id(research_id)
    if logical_key is not None:
        _validate_logical_key(logical_key)
    normalized = _normalize_json(payload, path="$", active=set(), depth=0)
    content_hash = canonical_hash(normalized)
    envelope = {
        "schema_version": _SCHEMA_VERSION,
        "research_id": research_id,
        "kind": kind.value,
        "logical_key": logical_key,
        "content_hash": content_hash,
        "payload": normalized,
    }
    encoded = canonical_json(envelope)
    object_path = _object_path(research_id, kind, content_hash, logical_key)
    return encoded, ArtifactRef(
        research_id=research_id,
        kind=kind,
        object_path=object_path,
        content_hash=content_hash,
        byte_size=len(encoded),
        logical_key=logical_key,
    )


def _decode_artifact(raw: bytes, *, expected_path: str) -> StoredArtifact:
    try:
        envelope = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise ArtifactIntegrityError("stored artifact is not valid JSON") from None
    if not isinstance(envelope, dict) or envelope.get("schema_version") != _SCHEMA_VERSION:
        raise ArtifactIntegrityError("stored artifact schema is invalid")
    try:
        raw_research_id = envelope["research_id"]
        raw_kind = envelope["kind"]
        raw_hash = envelope["content_hash"]
        if not isinstance(raw_research_id, str) or not isinstance(raw_kind, str):
            raise ValueError("artifact identity fields must be strings")
        if not isinstance(raw_hash, str):
            raise ValueError("artifact content hash must be a string")
        research_id = raw_research_id
        kind = ArtifactKind(raw_kind)
        logical_value = envelope.get("logical_key")
        if logical_value is not None and not isinstance(logical_value, str):
            raise ValueError("artifact logical key must be a string")
        logical_key = logical_value
        stored_hash = raw_hash
        payload = envelope["payload"]
        if canonical_json(envelope) != raw:
            raise ArtifactIntegrityError("stored artifact JSON is not canonical")
        actual_hash = canonical_hash(payload)
    except SecretMaterialError:
        raise ArtifactIntegrityError("stored artifact contains prohibited material") from None
    except (KeyError, TypeError, ValueError):
        raise ArtifactIntegrityError("stored artifact identity is invalid") from None
    if stored_hash != actual_hash:
        raise ArtifactIntegrityError("stored artifact content hash does not match")
    object_path = _object_path(research_id, kind, stored_hash, logical_key)
    if object_path != expected_path:
        raise ArtifactIntegrityError("stored artifact path does not match its identity")
    ref = ArtifactRef(
        research_id,
        kind,
        object_path,
        stored_hash,
        len(raw),
        logical_key,
    )
    return StoredArtifact(ref, payload)


def _object_path(
    research_id: str,
    kind: ArtifactKind,
    content_hash: str,
    logical_key: str | None,
) -> str:
    if logical_key is None:
        filename = f"sha256-{content_hash}.json"
        return PurePosixPath(research_id, kind.value, filename).as_posix()
    return _logical_object_path(research_id, kind, logical_key)


def _logical_object_path(
    research_id: str,
    kind: ArtifactKind,
    logical_key: str,
) -> str:
    _validate_research_id(research_id)
    _validate_logical_key(logical_key)
    key_hash = hashlib.sha256(logical_key.encode()).hexdigest()
    filename = f"write-once-{key_hash}.json"
    return PurePosixPath(research_id, kind.value, filename).as_posix()


def _validate_research_id(value: str) -> None:
    if not _RESEARCH_ID.fullmatch(value):
        raise ValueError("research_id contains invalid path characters")


def _validate_logical_key(value: str) -> None:
    if not value.strip() or len(value) > _MAX_LOGICAL_KEY_CHARS:
        raise ValueError("logical artifact key has an invalid length")
    _check_secret_string(value, "logical_key")


FilesystemArtifactStore = FilesystemResearchArtifactStore
VercelBlobArtifactStore = VercelBlobResearchArtifactStore
ArtifactStore = ResearchArtifactStore

__all__ = [
    "ArtifactBackendError",
    "ArtifactConflictError",
    "ArtifactError",
    "ArtifactIntegrityError",
    "ArtifactKind",
    "ArtifactRef",
    "ArtifactStore",
    "BaseResearchArtifactStore",
    "BlobObjectClient",
    "FilesystemArtifactStore",
    "FilesystemResearchArtifactStore",
    "ResearchArtifactStore",
    "SecretMaterialError",
    "StoredArtifact",
    "VercelBlobArtifactStore",
    "VercelBlobResearchArtifactStore",
    "assert_secret_free",
    "canonical_hash",
    "canonical_json",
]
