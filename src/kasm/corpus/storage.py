"""Immutable byte-store boundary for corpus objects.

``CorpusObjectStore`` is intentionally smaller than any vendor SDK.  A private
Blob implementation only needs conditional create and exact read semantics;
the corpus repository never depends on filesystem listing or mutable pointers.
The reference filesystem implementation uses exclusive creation, canonical
paths, file and directory fsync, and exact-byte idempotency.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Protocol


class CorpusStorageError(RuntimeError):
    """Sanitized storage failure."""


class CorpusObjectConflictError(CorpusStorageError):
    """An immutable key already contains different bytes."""


class CorpusObjectIntegrityError(CorpusStorageError):
    """A stored object or path violates the immutable storage contract."""


class CorpusObjectStore(Protocol):
    """Vendor-neutral private object storage used by :class:`CorpusRepository`."""

    def put_immutable(self, key: str, content: bytes) -> None: ...

    def get(self, key: str) -> bytes | None: ...


class CorpusBlobClient(Protocol):
    """Minimal injectable surface implemented by the optional Vercel SDK."""

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


class FilesystemCorpusObjectStore:
    """Durable local reference implementation of ``CorpusObjectStore``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
            self._resolved_root = self.root.resolve(strict=True)
        except OSError as exc:
            raise CorpusStorageError("corpus filesystem initialization failed") from exc

    def put_immutable(self, key: str, content: bytes) -> None:
        if not content:
            raise ValueError("corpus object content must not be empty")
        path = self._safe_path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._reject_symlink_path(path)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = self._read_path(path)
            if existing != content:
                raise CorpusObjectConflictError(
                    "immutable corpus key already contains different content"
                ) from None
            return
        except CorpusObjectIntegrityError:
            raise
        except OSError as exc:
            raise CorpusStorageError("corpus filesystem write failed") from exc
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            self._fsync_directory(path.parent)
        except OSError as exc:
            # Exclusive creation prevents a later writer from silently
            # replacing a partial object.  Reads will surface integrity failure.
            raise CorpusStorageError("corpus filesystem write failed") from exc

    def get(self, key: str) -> bytes | None:
        path = self._safe_path(key)
        if not path.exists() and not path.is_symlink():
            return None
        return self._read_path(path)

    def _read_path(self, path: Path) -> bytes:
        try:
            self._reject_symlink_path(path)
            if not path.is_file():
                raise CorpusObjectIntegrityError("corpus object is not a regular file")
            return path.read_bytes()
        except CorpusObjectIntegrityError:
            raise
        except OSError as exc:
            raise CorpusStorageError("corpus filesystem read failed") from exc

    def _safe_path(self, key: str) -> Path:
        parts = PurePosixPath(key)
        if (
            parts.is_absolute()
            or not parts.parts
            or any(part in {"", ".", ".."} for part in parts.parts)
            or "\\" in key
            or len(key) > 1_024
        ):
            raise ValueError("corpus object key is invalid")
        path = self.root.joinpath(*parts.parts)
        try:
            resolved = path.resolve(strict=False)
        except OSError as exc:
            raise CorpusStorageError("corpus filesystem path validation failed") from exc
        if resolved != self._resolved_root and self._resolved_root not in resolved.parents:
            raise CorpusObjectIntegrityError(
                "corpus object path is outside the configured store"
            )
        return path

    def _reject_symlink_path(self, path: Path) -> None:
        current = path
        while current != self.root.parent:
            if current.is_symlink():
                raise CorpusObjectIntegrityError(
                    "corpus object path must not contain symbolic links"
                )
            if current == self.root:
                return
            current = current.parent
        raise CorpusObjectIntegrityError(
            "corpus object path is outside the configured store"
        )

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class VercelBlobCorpusObjectStore:
    """Private immutable Blob implementation for hosted corpus revisions."""

    def __init__(
        self,
        *,
        prefix: str = "kbd/research/corpus",
        access: str = "private",
        client: CorpusBlobClient | None = None,
        token_provider: Callable[[], str | None] | None = None,
    ) -> None:
        normalized = prefix.strip("/")
        parts = PurePosixPath(normalized)
        if (
            not normalized
            or parts.is_absolute()
            or any(part in {"", ".", ".."} for part in parts.parts)
        ):
            raise ValueError("invalid Vercel Blob corpus prefix")
        if access != "private":
            raise ValueError("corpus objects require private Vercel Blob access")
        self.prefix = normalized
        self.access = access
        self._injected_client = client
        self._loaded_client: CorpusBlobClient | None = None
        self._token_provider = token_provider or (
            lambda: os.getenv("BLOB_READ_WRITE_TOKEN", "")
            or os.getenv("VERCEL_BLOB_READ_WRITE_TOKEN", "")
        )

    def put_immutable(self, key: str, content: bytes) -> None:
        if not content:
            raise ValueError("corpus object content must not be empty")
        pathname = self._pathname(key)
        client = self._client()
        existing = self._safe_get(client, pathname)
        if existing is not None:
            if existing != content:
                raise CorpusObjectConflictError(
                    "immutable corpus key already contains different content"
                )
            return
        try:
            client.put(
                pathname,
                content,
                access=self.access,
                add_random_suffix=False,
                allow_overwrite=False,
                content_type="application/json; charset=utf-8",
            )
        except Exception:
            raced = self._safe_get(client, pathname, suppress_errors=True)
            if raced is not None:
                if raced != content:
                    raise CorpusObjectConflictError(
                        "immutable corpus key already contains different content"
                    ) from None
                return
            raise CorpusStorageError("Vercel Blob corpus write failed") from None

    def get(self, key: str) -> bytes | None:
        return self._safe_get(self._client(), self._pathname(key))

    def _safe_get(
        self,
        client: CorpusBlobClient,
        pathname: str,
        *,
        suppress_errors: bool = False,
    ) -> bytes | None:
        try:
            value = client.get(pathname)
        except Exception:
            if suppress_errors:
                return None
            raise CorpusStorageError("Vercel Blob corpus read failed") from None
        if value is not None and not isinstance(value, bytes):
            raise CorpusObjectIntegrityError(
                "Vercel Blob corpus content is not bytes"
            )
        return value

    def _client(self) -> CorpusBlobClient:
        if self._injected_client is not None:
            return self._injected_client
        if self._loaded_client is not None:
            return self._loaded_client
        try:
            token = str(self._token_provider() or "").strip()
        except Exception:
            raise CorpusStorageError(
                "Vercel Blob corpus credential provider failed"
            ) from None
        if not token:
            raise CorpusStorageError("Vercel Blob credentials are not configured")
        try:
            module = importlib.import_module("vercel.blob")
            self._loaded_client = _VercelBlobCorpusModuleClient(module, token)
        except (ImportError, AttributeError, TypeError):
            raise CorpusStorageError(
                "optional Vercel Blob SDK is not installed or compatible"
            ) from None
        return self._loaded_client

    def _pathname(self, key: str) -> str:
        parts = PurePosixPath(key)
        if (
            parts.is_absolute()
            or not parts.parts
            or any(part in {"", ".", ".."} for part in parts.parts)
            or "\\" in key
            or len(key) > 1_024
        ):
            raise ValueError("corpus object key is invalid")
        return f"{self.prefix}/{key}"


class _VercelBlobCorpusModuleClient:
    def __init__(self, module: Any, token: str) -> None:
        for name in ("put", "get"):
            if not callable(getattr(module, name, None)):
                raise AttributeError(f"Vercel Blob SDK lacks {name}")
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
        if result is None or isinstance(result, bytes):
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
            value = reader()
            if isinstance(value, bytes):
                return value
        raise TypeError("Vercel Blob SDK returned unsupported content")
