"""Content-addressed preservation of official PDFs and their lossless parsed text."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import urllib.parse
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

_OFFICIAL_HOSTS = {
    "open.assembly.go.kr",
    "record.assembly.go.kr",
    "likms.assembly.go.kr",
}


class OfficialDocumentKind(StrEnum):
    MINUTES = "minutes"
    REVIEW_REPORT = "review_report"
    BILL_TEXT = "bill_text"


@dataclass(frozen=True, slots=True)
class RawOfficialDocument:
    """Immutable bytes downloaded from one official URL."""

    kind: OfficialDocumentKind
    official_url: str
    media_type: str
    content: bytes
    retrieved_at: datetime

    def __post_init__(self) -> None:
        _validate_official_url(self.official_url)
        if not self.media_type.strip() or not self.content:
            raise ValueError("official document media type and content are required")
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")

    @property
    def source_hash(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def object_key(self) -> str:
        return f"official/raw/{self.source_hash}"


@dataclass(frozen=True, slots=True)
class OfficialDocumentSource:
    """Small, integrity-bound description of one preserved official PDF."""

    kind: OfficialDocumentKind
    official_url: str
    media_type: str
    source_hash: str
    retrieved_at: datetime
    byte_count: int

    def __post_init__(self) -> None:
        _validate_official_url(self.official_url)
        _validate_sha256(self.source_hash)
        if not self.media_type.strip() or self.byte_count < 1:
            raise ValueError("official document media type and byte count are required")
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")

    @property
    def object_key(self) -> str:
        return f"official/raw/{self.source_hash}"


@dataclass(frozen=True, slots=True)
class TextSegment:
    """One parsed locator, normally an original PDF page."""

    locator: str
    text: str

    def __post_init__(self) -> None:
        if not self.locator.strip():
            raise ValueError("text segment locator is required")


@dataclass(frozen=True, slots=True)
class ParsedOfficialDocument:
    """Complete parser output linked to the immutable source bytes."""

    kind: OfficialDocumentKind
    official_url: str
    source_hash: str
    parser_version: str
    parsed_at: datetime
    segments: tuple[TextSegment, ...]
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_official_url(self.official_url)
        _validate_sha256(self.source_hash)
        if not self.parser_version.strip():
            raise ValueError("parser_version is required")
        if self.parsed_at.tzinfo is None:
            raise ValueError("parsed_at must be timezone-aware")
        if not self.segments:
            raise ValueError("parsed document must contain at least one segment")
        locators = [segment.locator for segment in self.segments]
        if len(locators) != len(set(locators)):
            raise ValueError("parsed segment locators must be unique")

    @property
    def full_text(self) -> str:
        return "\n\n".join(segment.text for segment in self.segments)

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.full_text.encode()).hexdigest()

    @property
    def object_key(self) -> str:
        version_hash = hashlib.sha256(self.parser_version.encode()).hexdigest()[:16]
        return f"official/parsed/{self.source_hash}/{version_hash}.json"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": self.kind.value,
            "official_url": self.official_url,
            "source_hash": self.source_hash,
            "parser_version": self.parser_version,
            "parsed_at": self.parsed_at.isoformat(),
            "segments": [
                {"locator": segment.locator, "text": segment.text}
                for segment in self.segments
            ],
            "warnings": list(self.warnings),
            "text_hash": self.text_hash,
            "text_characters": len(self.full_text),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ParsedOfficialDocument:
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported parsed document schema")
        raw_segments = payload.get("segments")
        if not isinstance(raw_segments, list):
            raise ValueError("parsed document segments must be a list")
        segments: list[TextSegment] = []
        for item in raw_segments:
            if not isinstance(item, dict):
                raise ValueError("parsed document segment must be an object")
            segments.append(
                TextSegment(
                    locator=str(item.get("locator") or ""),
                    text=str(item.get("text") or ""),
                )
            )
        raw_warnings = payload.get("warnings") or []
        if not isinstance(raw_warnings, list):
            raise ValueError("parsed document warnings must be a list")
        result = cls(
            kind=OfficialDocumentKind(str(payload.get("kind") or "")),
            official_url=str(payload.get("official_url") or ""),
            source_hash=str(payload.get("source_hash") or ""),
            parser_version=str(payload.get("parser_version") or ""),
            parsed_at=datetime.fromisoformat(str(payload.get("parsed_at") or "")),
            segments=tuple(segments),
            warnings=tuple(str(value) for value in raw_warnings),
        )
        if payload.get("text_hash") != result.text_hash:
            raise ValueError("parsed document text hash does not match")
        if payload.get("text_characters") != len(result.full_text):
            raise ValueError("parsed document text length does not match")
        return result


class OfficialDocumentStore(Protocol):
    def put_raw(self, document: RawOfficialDocument) -> str: ...

    def get_raw(self, source_hash: str) -> RawOfficialDocument | None: ...

    def latest_raw_for_url(self, official_url: str) -> RawOfficialDocument | None: ...

    def latest_source_for_url(
        self, official_url: str
    ) -> OfficialDocumentSource | None: ...

    def put_parsed(self, document: ParsedOfficialDocument) -> str: ...

    def get_parsed(
        self, source_hash: str, parser_version: str
    ) -> ParsedOfficialDocument | None: ...


class OfficialDocumentBlobClient(Protocol):
    """Small injectable surface matching the official Vercel Blob SDK."""

    def put(
        self,
        pathname: str,
        body: bytes,
        *,
        access: str,
        add_random_suffix: bool,
        overwrite: bool,
        content_type: str,
    ) -> object: ...

    def get(self, pathname: str) -> bytes | None: ...

    def size(self, pathname: str) -> int: ...

    def iter_objects(self, *, prefix: str) -> Iterable[str]: ...


class FilesystemOfficialDocumentStore:
    """Durable local reference store used by CLI and long-running workers."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put_raw(self, document: RawOfficialDocument) -> str:
        content_path = self.root / document.object_key
        metadata_path = content_path.with_suffix(".json")
        content_path.parent.mkdir(parents=True, exist_ok=True)
        if content_path.exists():
            existing = content_path.read_bytes()
            if hashlib.sha256(existing).hexdigest() != document.source_hash:
                raise RuntimeError("content-addressed official document is corrupt")
        else:
            _atomic_write(content_path, document.content)
        metadata = {
            "schema_version": 1,
            "kind": document.kind.value,
            "official_url": document.official_url,
            "media_type": document.media_type,
            "source_hash": document.source_hash,
            "retrieved_at": document.retrieved_at.isoformat(),
        }
        _atomic_write(metadata_path, _json_bytes(metadata))
        pointer_path = self._url_pointer_path(document.official_url)
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            pointer_path,
            _json_bytes(
                {
                    "schema_version": 1,
                    "official_url": document.official_url,
                    "source_hash": document.source_hash,
                }
            ),
        )
        return document.object_key

    def get_raw(self, source_hash: str) -> RawOfficialDocument | None:
        _validate_sha256(source_hash)
        content_path = self.root / f"official/raw/{source_hash}"
        metadata_path = content_path.with_suffix(".json")
        if not content_path.exists() or not metadata_path.exists():
            return None
        content = content_path.read_bytes()
        if hashlib.sha256(content).hexdigest() != source_hash:
            raise RuntimeError("stored official document hash does not match")
        metadata = _read_json(metadata_path)
        if metadata.get("source_hash") != source_hash:
            raise RuntimeError("stored official document metadata does not match")
        return RawOfficialDocument(
            kind=OfficialDocumentKind(str(metadata.get("kind") or "")),
            official_url=str(metadata.get("official_url") or ""),
            media_type=str(metadata.get("media_type") or ""),
            content=content,
            retrieved_at=datetime.fromisoformat(str(metadata.get("retrieved_at") or "")),
        )

    def latest_raw_for_url(self, official_url: str) -> RawOfficialDocument | None:
        _validate_official_url(official_url)
        pointer_path = self._url_pointer_path(official_url)
        if not pointer_path.exists():
            return None
        pointer = _read_json(pointer_path)
        if pointer.get("official_url") != official_url:
            raise RuntimeError("official URL cache pointer does not match")
        return self.get_raw(str(pointer.get("source_hash") or ""))

    def latest_source_for_url(
        self, official_url: str
    ) -> OfficialDocumentSource | None:
        """Resolve a cached source without loading the preserved PDF bytes."""

        _validate_official_url(official_url)
        pointer_path = self._url_pointer_path(official_url)
        if not pointer_path.exists():
            return None
        pointer = _read_json(pointer_path)
        if pointer.get("official_url") != official_url:
            raise RuntimeError("official URL cache pointer does not match")
        source_hash = str(pointer.get("source_hash") or "")
        _validate_sha256(source_hash)
        content_path = self.root / f"official/raw/{source_hash}"
        metadata_path = content_path.with_suffix(".json")
        if not content_path.exists() or not metadata_path.exists():
            raise RuntimeError("official URL pointer refers to a missing raw document")
        metadata = _read_json(metadata_path)
        return _source_from_metadata(
            metadata,
            source_hash=source_hash,
            official_url=official_url,
            byte_count=content_path.stat().st_size,
        )

    def put_parsed(self, document: ParsedOfficialDocument) -> str:
        raw = self.get_raw(document.source_hash)
        if raw is None:
            raise ValueError("raw official document must be preserved before parsed text")
        if raw.official_url != document.official_url or raw.kind is not document.kind:
            raise ValueError("parsed document does not match its preserved raw source")
        path = self.root / document.object_key
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = _json_bytes(document.to_dict())
        if path.exists():
            _verify_equivalent_parsed(path.read_bytes(), document)
        else:
            try:
                _atomic_write_once(path, encoded)
            except FileExistsError:
                _verify_equivalent_parsed(path.read_bytes(), document)
        return document.object_key

    def get_parsed(
        self, source_hash: str, parser_version: str
    ) -> ParsedOfficialDocument | None:
        _validate_sha256(source_hash)
        version_hash = hashlib.sha256(parser_version.encode()).hexdigest()[:16]
        path = self.root / f"official/parsed/{source_hash}/{version_hash}.json"
        if not path.exists():
            return None
        result = ParsedOfficialDocument.from_dict(_read_json(path))
        if result.source_hash != source_hash or result.parser_version != parser_version:
            raise RuntimeError("stored parsed document identity does not match")
        return result

    def _url_pointer_path(self, official_url: str) -> Path:
        url_hash = hashlib.sha256(official_url.encode()).hexdigest()
        return self.root / f"official/by-url/{url_hash}.json"


class VercelBlobOfficialDocumentStore:
    """Private, immutable official-document storage for hosted workers.

    Raw bytes and parser outputs use their existing content-addressed logical
    keys.  A URL can legitimately serve new bytes over time, so its pointer is
    an append-only set of content-addressed observations.  Readers choose the
    greatest ``(retrieved_at, source_hash, pointer_hash)`` tuple, making a
    concurrent update deterministic without overwriting history.
    """

    def __init__(
        self,
        *,
        prefix: str = "official-documents",
        access: str = "private",
        client: OfficialDocumentBlobClient | None = None,
        token_provider: Callable[[], str | None] | None = None,
    ) -> None:
        normalized_prefix = prefix.strip("/")
        if not normalized_prefix or any(
            part in {"", ".", ".."} for part in normalized_prefix.split("/")
        ):
            raise ValueError("invalid Vercel Blob official-document prefix")
        if access != "private":
            raise ValueError("official documents require private Vercel Blob access")
        self.prefix = normalized_prefix
        self.access = access
        self._injected_client = client
        self._loaded_client: OfficialDocumentBlobClient | None = None
        self._token_provider = token_provider or (
            lambda: os.getenv("BLOB_READ_WRITE_TOKEN", "")
            or os.getenv("VERCEL_BLOB_READ_WRITE_TOKEN", "")
        )

    def put_raw(self, document: RawOfficialDocument) -> str:
        client = self._client()
        content_path = self._blob_path(document.object_key)
        metadata_path = f"{content_path}.json"
        metadata = _raw_metadata(document)

        # Preserve the source bytes first.  Neither metadata nor a URL pointer
        # can become visible before the immutable source exists.
        self._put_immutable(
            client,
            content_path,
            document.content,
            content_type=document.media_type,
        )
        self._put_raw_metadata(
            client,
            metadata_path,
            _json_bytes(metadata),
            document=document,
        )

        pointer = {
            "schema_version": 1,
            "official_url": document.official_url,
            "source_hash": document.source_hash,
            "retrieved_at": document.retrieved_at.isoformat(),
        }
        encoded_pointer = _json_bytes(pointer)
        pointer_hash = hashlib.sha256(encoded_pointer).hexdigest()
        pointer_path = f"{self._url_pointer_prefix(document.official_url)}{pointer_hash}.json"
        self._put_immutable(
            client,
            pointer_path,
            encoded_pointer,
            content_type="application/json; charset=utf-8",
        )
        return document.object_key

    def get_raw(self, source_hash: str) -> RawOfficialDocument | None:
        _validate_sha256(source_hash)
        client = self._client()
        content_path = self._blob_path(f"official/raw/{source_hash}")
        content = self._safe_get(client, content_path)
        metadata = self._safe_get(client, f"{content_path}.json")
        if content is None or metadata is None:
            return None
        return _decode_raw_document(content, metadata, source_hash=source_hash)

    def latest_raw_for_url(self, official_url: str) -> RawOfficialDocument | None:
        source = self.latest_source_for_url(official_url)
        if source is None:
            return None
        raw = self.get_raw(source.source_hash)
        if raw is None:
            raise RuntimeError("official URL pointer refers to a missing raw document")
        if raw.official_url != official_url:
            raise RuntimeError("official URL pointer does not match its raw document")
        # The fixed raw metadata records the first successful preservation.  A
        # later observation of identical bytes is represented by its pointer.
        return RawOfficialDocument(
            kind=raw.kind,
            official_url=raw.official_url,
            media_type=raw.media_type,
            content=raw.content,
            retrieved_at=source.retrieved_at,
        )

    def latest_source_for_url(
        self, official_url: str
    ) -> OfficialDocumentSource | None:
        """Resolve a cached source without transferring the preserved PDF body."""

        _validate_official_url(official_url)
        client = self._client()
        prefix = self._url_pointer_prefix(official_url)
        try:
            pathnames = tuple(client.iter_objects(prefix=prefix))
        except Exception:
            raise RuntimeError("Vercel Blob official URL pointer list failed") from None
        if not pathnames:
            return None
        observations = tuple(
            self._read_pointer(
                client,
                pathname,
                prefix=prefix,
                official_url=official_url,
            )
            for pathname in pathnames
        )
        retrieved_at, source_hash, _pointer_hash = max(
            observations,
            key=lambda item: (item[0].astimezone(UTC), item[1], item[2]),
        )
        content_path = self._blob_path(f"official/raw/{source_hash}")
        encoded_metadata = self._safe_get(client, f"{content_path}.json")
        if encoded_metadata is None:
            raise RuntimeError("official URL pointer refers to missing raw metadata")
        try:
            size_reader = client.size
        except AttributeError:
            content = self._safe_get(client, content_path)
            if content is None:
                raise RuntimeError(
                    "official URL pointer refers to a missing raw document"
                ) from None
            byte_count = len(content)
        else:
            try:
                byte_count = int(size_reader(content_path))
            except Exception:
                raise RuntimeError("Vercel Blob official document head failed") from None
        return _source_from_metadata(
            _decode_blob_json(encoded_metadata),
            source_hash=source_hash,
            official_url=official_url,
            byte_count=byte_count,
            retrieved_at=retrieved_at,
        )

    def put_parsed(self, document: ParsedOfficialDocument) -> str:
        raw = self.get_raw(document.source_hash)
        if raw is None:
            raise ValueError("raw official document must be preserved before parsed text")
        if raw.official_url != document.official_url or raw.kind is not document.kind:
            raise ValueError("parsed document does not match its preserved raw source")
        self._put_parsed_immutable(
            self._client(),
            self._blob_path(document.object_key),
            document,
        )
        return document.object_key

    def _put_parsed_immutable(
        self,
        client: OfficialDocumentBlobClient,
        pathname: str,
        document: ParsedOfficialDocument,
    ) -> None:
        encoded = _json_bytes(document.to_dict())
        existing = self._safe_get(client, pathname)
        if existing is not None:
            _verify_equivalent_parsed(existing, document)
            return
        try:
            client.put(
                pathname,
                encoded,
                access=self.access,
                add_random_suffix=False,
                overwrite=False,
                content_type="application/json; charset=utf-8",
            )
        except Exception:
            raced = self._safe_get(client, pathname, suppress_errors=True)
            if raced is not None:
                _verify_equivalent_parsed(raced, document)
                return
            raise RuntimeError("Vercel Blob official document write failed") from None

    def get_parsed(
        self, source_hash: str, parser_version: str
    ) -> ParsedOfficialDocument | None:
        _validate_sha256(source_hash)
        version_hash = hashlib.sha256(parser_version.encode()).hexdigest()[:16]
        pathname = self._blob_path(
            f"official/parsed/{source_hash}/{version_hash}.json"
        )
        encoded = self._safe_get(self._client(), pathname)
        if encoded is None:
            return None
        try:
            result = ParsedOfficialDocument.from_dict(_decode_blob_json(encoded))
        except (TypeError, ValueError, OverflowError):
            raise RuntimeError("stored parsed official document is corrupt") from None
        if result.source_hash != source_hash or result.parser_version != parser_version:
            raise RuntimeError("stored parsed document identity does not match")
        return result

    def _put_immutable(
        self,
        client: OfficialDocumentBlobClient,
        pathname: str,
        content: bytes,
        *,
        content_type: str,
    ) -> None:
        existing = self._safe_get(client, pathname)
        if existing is not None:
            self._verify_exact(existing, content)
            return
        try:
            client.put(
                pathname,
                content,
                access=self.access,
                add_random_suffix=False,
                overwrite=False,
                content_type=content_type,
            )
        except Exception:
            # Another writer may have won after our read.  Resolve that race by
            # accepting only the exact immutable value we intended to create.
            raced = self._safe_get(client, pathname, suppress_errors=True)
            if raced is None:
                raise RuntimeError(
                    "Vercel Blob official document write failed"
                ) from None
            self._verify_exact(raced, content)

    def _put_raw_metadata(
        self,
        client: OfficialDocumentBlobClient,
        pathname: str,
        encoded: bytes,
        *,
        document: RawOfficialDocument,
    ) -> None:
        existing = self._safe_get(client, pathname)
        if existing is not None:
            _verify_raw_metadata(existing, document)
            return
        try:
            client.put(
                pathname,
                encoded,
                access=self.access,
                add_random_suffix=False,
                overwrite=False,
                content_type="application/json; charset=utf-8",
            )
        except Exception:
            raced = self._safe_get(client, pathname, suppress_errors=True)
            if raced is None:
                raise RuntimeError(
                    "Vercel Blob official document metadata write failed"
                ) from None
            _verify_raw_metadata(raced, document)

    def _read_pointer(
        self,
        client: OfficialDocumentBlobClient,
        pathname: str,
        *,
        prefix: str,
        official_url: str,
    ) -> tuple[datetime, str, str]:
        if not isinstance(pathname, str) or not pathname.startswith(prefix):
            raise RuntimeError("Vercel Blob listed an invalid official URL pointer")
        relative = pathname[len(prefix) :]
        if not relative.endswith(".json") or "/" in relative:
            raise RuntimeError("Vercel Blob listed an invalid official URL pointer")
        pointer_hash = relative[:-5]
        try:
            _validate_sha256(pointer_hash)
        except ValueError:
            raise RuntimeError("Vercel Blob listed an invalid official URL pointer") from None
        encoded = self._safe_get(client, pathname)
        if encoded is None:
            raise RuntimeError("listed official URL pointer is missing")
        if hashlib.sha256(encoded).hexdigest() != pointer_hash:
            raise RuntimeError("stored official URL pointer hash does not match")
        payload = _decode_blob_json(encoded)
        try:
            if payload.get("schema_version") != 1:
                raise ValueError
            if payload.get("official_url") != official_url:
                raise ValueError
            source_hash = str(payload.get("source_hash") or "")
            _validate_sha256(source_hash)
            retrieved_at = datetime.fromisoformat(
                str(payload.get("retrieved_at") or "")
            )
            if retrieved_at.tzinfo is None:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise RuntimeError("stored official URL pointer is corrupt") from None
        return retrieved_at, source_hash, pointer_hash

    @staticmethod
    def _verify_exact(existing: bytes, expected: bytes) -> None:
        if existing != expected:
            raise RuntimeError("immutable Vercel Blob object contains different content")

    @staticmethod
    def _safe_get(
        client: OfficialDocumentBlobClient,
        pathname: str,
        *,
        suppress_errors: bool = False,
    ) -> bytes | None:
        try:
            value = client.get(pathname)
        except Exception:
            if suppress_errors:
                return None
            raise RuntimeError("Vercel Blob official document read failed") from None
        if value is not None and not isinstance(value, bytes):
            raise RuntimeError("Vercel Blob official document content is not bytes")
        return value

    def _client(self) -> OfficialDocumentBlobClient:
        if self._injected_client is not None:
            return self._injected_client
        if self._loaded_client is not None:
            return self._loaded_client
        try:
            token = str(self._token_provider() or "").strip()
        except Exception:
            raise RuntimeError("Vercel Blob credential provider failed") from None
        if not token:
            raise RuntimeError("Vercel Blob credentials are not configured")
        try:
            module = importlib.import_module("vercel.blob")
            self._loaded_client = _VercelBlobOfficialDocumentModuleClient(module, token)
        except (ImportError, AttributeError, TypeError):
            raise RuntimeError(
                "optional Vercel Blob SDK is not installed or compatible"
            ) from None
        return self._loaded_client

    def _blob_path(self, object_key: str) -> str:
        return f"{self.prefix}/{object_key}"

    def _url_pointer_prefix(self, official_url: str) -> str:
        url_hash = hashlib.sha256(official_url.encode()).hexdigest()
        return self._blob_path(f"official/by-url/{url_hash}/")


class _VercelBlobOfficialDocumentModuleClient:
    """Adapter for the official ``vercel.blob`` 0.6 SDK."""

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
        overwrite: bool,
        content_type: str,
    ) -> object:
        return self.module.put(
            pathname,
            body,
            access=access,
            add_random_suffix=add_random_suffix,
            overwrite=overwrite,
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
            content = result.get("content")
            if isinstance(content, bytes):
                return content
            body = result.get("body")
            if isinstance(body, bytes):
                return body
        content = getattr(result, "content", None)
        if isinstance(content, bytes):
            return content
        reader = getattr(result, "read", None)
        if callable(reader):
            content = reader()
            if isinstance(content, bytes):
                return content
        raise TypeError("Vercel Blob SDK returned unsupported content")

    def size(self, pathname: str) -> int:
        head = getattr(self.module, "head", None)
        if callable(head):
            result = head(pathname, token=self.token)
            value = (
                result.get("size")
                if isinstance(result, Mapping)
                else getattr(result, "size", None)
            )
            if isinstance(value, int) and value >= 0:
                return value
            raise TypeError("Vercel Blob head returned an invalid size")
        content = self.get(pathname)
        if content is None:
            raise RuntimeError("Vercel Blob object is missing")
        return len(content)

    def iter_objects(self, *, prefix: str) -> Iterable[str]:
        pathnames: list[str] = []
        iterator = getattr(self.module, "iter_objects", None)
        if callable(iterator):
            pathnames.extend(
                _official_blob_pathnames(
                    iterator(prefix=prefix, token=self.token)
                )
            )
            return tuple(pathnames)

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
            pathnames.extend(_official_blob_pathnames(values))
            has_more = bool(
                result.get("has_more", result.get("hasMore", False))
                if isinstance(result, Mapping)
                else result.has_more
            )
            if not has_more:
                return tuple(pathnames)
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


def _official_blob_pathnames(values: Iterable[Any]) -> tuple[str, ...]:
    pathnames: list[str] = []
    for item in values:
        if isinstance(item, str):
            pathnames.append(item)
        elif isinstance(item, Mapping):
            pathname = item.get("pathname") or item.get("path")
            if isinstance(pathname, str):
                pathnames.append(pathname)
        else:
            pathname = getattr(item, "pathname", None)
            if isinstance(pathname, str):
                pathnames.append(pathname)
    return tuple(pathnames)


def _validate_official_url(value: str) -> None:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in _OFFICIAL_HOSTS:
        raise ValueError("document URL must use an official Assembly HTTPS host")


def _validate_sha256(value: str) -> None:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("source_hash must be a SHA-256 hex digest")


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _raw_metadata(document: RawOfficialDocument) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": document.kind.value,
        "official_url": document.official_url,
        "media_type": document.media_type,
        "source_hash": document.source_hash,
        "retrieved_at": document.retrieved_at.isoformat(),
    }


def _source_from_metadata(
    metadata: Mapping[str, Any],
    *,
    source_hash: str,
    official_url: str,
    byte_count: int,
    retrieved_at: datetime | None = None,
) -> OfficialDocumentSource:
    try:
        if metadata.get("schema_version") != 1:
            raise ValueError
        if metadata.get("source_hash") != source_hash:
            raise ValueError
        if metadata.get("official_url") != official_url:
            raise ValueError
        stored_retrieved_at = datetime.fromisoformat(
            str(metadata.get("retrieved_at") or "")
        )
        if stored_retrieved_at.tzinfo is None:
            raise ValueError
        result = OfficialDocumentSource(
            kind=OfficialDocumentKind(str(metadata.get("kind") or "")),
            official_url=official_url,
            media_type=str(metadata.get("media_type") or ""),
            source_hash=source_hash,
            retrieved_at=retrieved_at or stored_retrieved_at,
            byte_count=byte_count,
        )
    except (TypeError, ValueError, OverflowError):
        raise RuntimeError("stored official document metadata is corrupt") from None
    return result


def _decode_blob_json(encoded: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(encoded)
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError("stored Vercel Blob metadata is corrupt") from None
    if not isinstance(payload, dict):
        raise RuntimeError("stored Vercel Blob metadata is not an object")
    return payload


def _decode_raw_document(
    content: bytes,
    encoded_metadata: bytes,
    *,
    source_hash: str,
) -> RawOfficialDocument:
    if hashlib.sha256(content).hexdigest() != source_hash:
        raise RuntimeError("stored official document hash does not match")
    metadata = _decode_blob_json(encoded_metadata)
    try:
        if metadata.get("schema_version") != 1:
            raise ValueError
        if metadata.get("source_hash") != source_hash:
            raise ValueError
        result = RawOfficialDocument(
            kind=OfficialDocumentKind(str(metadata.get("kind") or "")),
            official_url=str(metadata.get("official_url") or ""),
            media_type=str(metadata.get("media_type") or ""),
            content=content,
            retrieved_at=datetime.fromisoformat(
                str(metadata.get("retrieved_at") or "")
            ),
        )
    except (TypeError, ValueError, OverflowError):
        raise RuntimeError("stored official document metadata is corrupt") from None
    if result.source_hash != source_hash:
        raise RuntimeError("stored official document metadata does not match")
    return result


def _verify_raw_metadata(encoded: bytes, document: RawOfficialDocument) -> None:
    """Accept a repeat observation but reject a conflicting hash identity."""

    existing = _decode_raw_document(
        document.content,
        encoded,
        source_hash=document.source_hash,
    )
    if (
        existing.kind is not document.kind
        or existing.official_url != document.official_url
        or existing.media_type != document.media_type
    ):
        raise RuntimeError("immutable official document metadata conflicts")


def _verify_equivalent_parsed(
    encoded: bytes, document: ParsedOfficialDocument
) -> None:
    try:
        existing = ParsedOfficialDocument.from_dict(_decode_blob_json(encoded))
    except (TypeError, ValueError, OverflowError):
        raise RuntimeError("stored parsed official document is corrupt") from None
    if (
        existing.kind is not document.kind
        or existing.official_url != document.official_url
        or existing.source_hash != document.source_hash
        or existing.parser_version != document.parser_version
        or existing.segments != document.segments
        or existing.warnings != document.warnings
        or existing.text_hash != document.text_hash
    ):
        raise RuntimeError("parser version produced non-deterministic output")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("stored official document metadata is corrupt") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("stored official document metadata is not an object")
    return payload


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_once(path: Path, content: bytes) -> None:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_bytes(content)
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def now_utc() -> datetime:
    """Clock helper for fetch workers."""

    return datetime.now(UTC)
