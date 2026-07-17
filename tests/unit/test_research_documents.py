from __future__ import annotations

import json
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kasm.research.documents import (
    FilesystemOfficialDocumentStore,
    OfficialDocumentKind,
    ParsedOfficialDocument,
    RawOfficialDocument,
    TextSegment,
    VercelBlobOfficialDocumentStore,
    _atomic_write,
)


class FakeOfficialBlobClient:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.race_values: dict[str, bytes] = {}
        self.read_error: Exception | None = None
        self.get_calls: list[str] = []
        self.size_calls: list[str] = []

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
        self.put_calls.append(
            {
                "pathname": pathname,
                "access": access,
                "add_random_suffix": add_random_suffix,
                "overwrite": overwrite,
                "content_type": content_type,
            }
        )
        if pathname in self.race_values:
            self.objects[pathname] = self.race_values.pop(pathname)
            raise RuntimeError("SDK response body includes blob-super-secret")
        if pathname in self.objects and not overwrite:
            raise RuntimeError("already exists")
        self.objects[pathname] = body
        return {"pathname": pathname}

    def get(self, pathname: str) -> bytes | None:
        self.get_calls.append(pathname)
        if self.read_error is not None:
            raise self.read_error
        return self.objects.get(pathname)

    def size(self, pathname: str) -> int:
        self.size_calls.append(pathname)
        if self.read_error is not None:
            raise self.read_error
        return len(self.objects[pathname])

    def iter_objects(self, *, prefix: str) -> tuple[str, ...]:
        if self.read_error is not None:
            raise self.read_error
        return tuple(sorted(path for path in self.objects if path.startswith(prefix)))


def _raw(content: bytes = b"%PDF official bytes") -> RawOfficialDocument:
    return RawOfficialDocument(
        kind=OfficialDocumentKind.MINUTES,
        official_url="https://record.assembly.go.kr/minutes/one.pdf",
        media_type="application/pdf",
        content=content,
        retrieved_at=datetime(2026, 7, 13, tzinfo=UTC),
    )


def test_raw_pdf_is_preserved_by_hash_and_official_url(tmp_path: Path) -> None:
    store = FilesystemOfficialDocumentStore(tmp_path)
    raw = _raw()
    key = store.put_raw(raw)

    assert key == f"official/raw/{raw.source_hash}"
    assert store.get_raw(raw.source_hash) == raw
    assert store.latest_raw_for_url(raw.official_url) == raw


def test_changed_official_url_content_preserves_both_versions(tmp_path: Path) -> None:
    store = FilesystemOfficialDocumentStore(tmp_path)
    first = _raw(b"first")
    second = _raw(b"second")

    store.put_raw(first)
    store.put_raw(second)

    assert store.get_raw(first.source_hash) == first
    assert store.get_raw(second.source_hash) == second
    assert store.latest_raw_for_url(first.official_url) == second


def test_parsed_text_is_lossless_and_requires_raw_source(tmp_path: Path) -> None:
    store = FilesystemOfficialDocumentStore(tmp_path)
    raw = _raw()
    long_text = "전문" * 100_000
    parsed = ParsedOfficialDocument(
        kind=raw.kind,
        official_url=raw.official_url,
        source_hash=raw.source_hash,
        parser_version="pdf-parser-2",
        parsed_at=datetime(2026, 7, 13, tzinfo=UTC),
        segments=(TextSegment("p.1", long_text), TextSegment("p.2", "끝")),
    )

    with pytest.raises(ValueError, match="raw official document"):
        store.put_parsed(parsed)
    store.put_raw(raw)
    store.put_parsed(parsed)

    restored = store.get_parsed(raw.source_hash, "pdf-parser-2")
    assert restored == parsed
    assert restored is not None
    assert restored.full_text == long_text + "\n\n끝"
    assert len(restored.full_text) == len(parsed.full_text)


def test_hash_corruption_is_detected(tmp_path: Path) -> None:
    store = FilesystemOfficialDocumentStore(tmp_path)
    raw = _raw()
    store.put_raw(raw)
    path = tmp_path / raw.object_key
    path.write_bytes(b"corrupt")

    with pytest.raises(RuntimeError, match="hash does not match"):
        store.get_raw(raw.source_hash)


def test_non_official_document_is_rejected() -> None:
    with pytest.raises(ValueError, match="official Assembly"):
        RawOfficialDocument(
            kind=OfficialDocumentKind.REVIEW_REPORT,
            official_url="https://example.com/report.pdf",
            media_type="application/pdf",
            content=b"pdf",
            retrieved_at=datetime(2026, 7, 13, tzinfo=UTC),
        )


def test_blob_store_preserves_private_raw_metadata_and_pointer() -> None:
    client = FakeOfficialBlobClient()
    store = VercelBlobOfficialDocumentStore(client=client, prefix="kbd/private")
    raw = _raw()

    assert store.put_raw(raw) == raw.object_key
    assert store.get_raw(raw.source_hash) == raw
    assert store.latest_raw_for_url(raw.official_url) == raw
    assert len(client.objects) == 3
    assert client.objects[f"kbd/private/{raw.object_key}"] == raw.content

    metadata = json.loads(client.objects[f"kbd/private/{raw.object_key}.json"])
    assert metadata["source_hash"] == raw.source_hash
    pointer_paths = [
        pathname for pathname in client.objects if "/official/by-url/" in pathname
    ]
    assert len(pointer_paths) == 1
    assert json.loads(client.objects[pointer_paths[0]])["source_hash"] == raw.source_hash
    assert all(call["access"] == "private" for call in client.put_calls)
    assert all(call["overwrite"] is False for call in client.put_calls)
    assert all(call["add_random_suffix"] is False for call in client.put_calls)

    client.get_calls.clear()
    source = store.latest_source_for_url(raw.official_url)
    assert source is not None
    assert source.source_hash == raw.source_hash
    assert source.byte_count == len(raw.content)
    assert f"kbd/private/{raw.object_key}" not in client.get_calls
    assert client.size_calls[-1] == f"kbd/private/{raw.object_key}"

    first_put_count = len(client.put_calls)
    assert store.put_raw(raw) == raw.object_key
    assert len(client.put_calls) == first_put_count
    with pytest.raises(ValueError, match="private"):
        VercelBlobOfficialDocumentStore(client=client, access="public")


def test_blob_url_history_is_deterministic_and_keeps_every_source() -> None:
    client = FakeOfficialBlobClient()
    store = VercelBlobOfficialDocumentStore(client=client, prefix="kbd")
    earlier = _raw(b"earlier")
    later = RawOfficialDocument(
        kind=earlier.kind,
        official_url=earlier.official_url,
        media_type=earlier.media_type,
        content=b"later",
        retrieved_at=datetime(2026, 7, 14, tzinfo=UTC),
    )

    # Completion order cannot make an older observation become latest.
    store.put_raw(later)
    store.put_raw(earlier)

    assert store.get_raw(earlier.source_hash) == earlier
    assert store.get_raw(later.source_hash) == later
    assert store.latest_raw_for_url(earlier.official_url) == later
    pointer_paths = [
        pathname for pathname in client.objects if "/official/by-url/" in pathname
    ]
    assert len(pointer_paths) == 2

    refreshed = RawOfficialDocument(
        kind=later.kind,
        official_url=later.official_url,
        media_type=later.media_type,
        content=later.content,
        retrieved_at=datetime(2026, 7, 15, tzinfo=UTC),
    )
    store.put_raw(refreshed)
    assert store.latest_raw_for_url(later.official_url) == refreshed
    assert store.get_raw(later.source_hash) == later


def test_blob_equal_time_pointer_race_has_a_stable_hash_tiebreak() -> None:
    client = FakeOfficialBlobClient()
    store = VercelBlobOfficialDocumentStore(client=client)
    first = _raw(b"same-time-first")
    second = _raw(b"same-time-second")
    expected = max((first, second), key=lambda item: item.source_hash)

    store.put_raw(second)
    store.put_raw(first)

    latest = store.latest_raw_for_url(first.official_url)
    assert latest is not None
    assert latest.source_hash == expected.source_hash


def test_blob_parsed_text_is_lossless_private_and_requires_raw() -> None:
    client = FakeOfficialBlobClient()
    store = VercelBlobOfficialDocumentStore(client=client)
    raw = _raw()
    long_text = "전문위원 검토" * 100_000
    parsed = ParsedOfficialDocument(
        kind=raw.kind,
        official_url=raw.official_url,
        source_hash=raw.source_hash,
        parser_version="pdf-parser-hosted-1",
        parsed_at=datetime(2026, 7, 13, tzinfo=UTC),
        segments=(TextSegment("p.1", long_text), TextSegment("p.2", "끝")),
    )

    with pytest.raises(ValueError, match="raw official document"):
        store.put_parsed(parsed)
    store.put_raw(raw)
    assert store.put_parsed(parsed) == parsed.object_key

    restored = store.get_parsed(raw.source_hash, parsed.parser_version)
    assert restored == parsed
    assert restored is not None
    assert restored.full_text == long_text + "\n\n끝"
    assert len(restored.full_text) == len(parsed.full_text)
    put_count = len(client.put_calls)
    assert store.put_parsed(parsed) == parsed.object_key
    assert len(client.put_calls) == put_count


def test_blob_parsed_cache_accepts_equivalent_concurrent_winner() -> None:
    client = FakeOfficialBlobClient()
    store = VercelBlobOfficialDocumentStore(client=client)
    raw = _raw()
    store.put_raw(raw)
    first = ParsedOfficialDocument(
        kind=raw.kind,
        official_url=raw.official_url,
        source_hash=raw.source_hash,
        parser_version="concurrent-parser-v1",
        parsed_at=datetime(2026, 7, 13, tzinfo=UTC),
        segments=(TextSegment("p.1", "동일한 전체 본문"),),
    )
    concurrent = ParsedOfficialDocument(
        kind=first.kind,
        official_url=first.official_url,
        source_hash=first.source_hash,
        parser_version=first.parser_version,
        parsed_at=datetime(2026, 7, 14, tzinfo=UTC),
        segments=first.segments,
    )
    conflicting = ParsedOfficialDocument(
        kind=first.kind,
        official_url=first.official_url,
        source_hash=first.source_hash,
        parser_version=first.parser_version,
        parsed_at=datetime(2026, 7, 15, tzinfo=UTC),
        segments=(TextSegment("p.1", "다른 본문"),),
    )

    assert store.put_parsed(first) == first.object_key
    assert store.put_parsed(concurrent) == concurrent.object_key
    assert store.get_parsed(raw.source_hash, first.parser_version) == first
    with pytest.raises(RuntimeError, match="non-deterministic"):
        store.put_parsed(conflicting)


def test_blob_create_race_accepts_only_the_same_immutable_bytes() -> None:
    raw = _raw()
    pathname = f"kbd/{raw.object_key}"
    same_client = FakeOfficialBlobClient()
    same_client.race_values[pathname] = raw.content
    same_store = VercelBlobOfficialDocumentStore(client=same_client, prefix="kbd")

    assert same_store.put_raw(raw) == raw.object_key
    assert same_store.get_raw(raw.source_hash) == raw

    conflict_client = FakeOfficialBlobClient()
    conflict_client.race_values[pathname] = b"different blob-super-secret"
    conflict_store = VercelBlobOfficialDocumentStore(
        client=conflict_client,
        prefix="kbd",
    )
    with pytest.raises(RuntimeError) as error:
        conflict_store.put_raw(raw)
    assert "different content" in str(error.value)
    assert "blob-super-secret" not in str(error.value)
    assert "SDK response body" not in str(error.value)


def test_blob_sdk_06_shape_is_lazy_private_and_handles_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imports: list[str] = []
    objects: dict[str, bytes] = {}
    put_options: list[dict[str, Any]] = []
    get_options: list[dict[str, Any]] = []
    list_options: list[dict[str, Any]] = []

    class BlobNotFoundError(Exception):
        pass

    def get(pathname: str, **options: Any) -> object:
        get_options.append(options)
        if pathname not in objects:
            raise BlobNotFoundError
        return types.SimpleNamespace(content=objects[pathname])

    def put(pathname: str, body: bytes, **options: Any) -> object:
        put_options.append(options)
        if pathname in objects and options["overwrite"] is False:
            raise RuntimeError("already exists")
        objects[pathname] = body
        return types.SimpleNamespace(pathname=pathname)

    def list_objects(**options: Any) -> object:
        list_options.append(options)
        prefix = options["prefix"]
        matching = [
            types.SimpleNamespace(pathname=pathname)
            for pathname in sorted(objects)
            if pathname.startswith(prefix)
        ]
        start = int(options.get("cursor") or 0)
        end = min(start + 1, len(matching))
        return types.SimpleNamespace(
            blobs=matching[start:end],
            cursor=str(end) if end < len(matching) else None,
            has_more=end < len(matching),
        )

    module = types.SimpleNamespace(
        BlobNotFoundError=BlobNotFoundError,
        get=get,
        put=put,
        list_objects=list_objects,
    )

    def import_module(name: str) -> object:
        imports.append(name)
        return module

    monkeypatch.setattr(
        "kasm.research.documents.importlib.import_module",
        import_module,
    )
    store = VercelBlobOfficialDocumentStore(
        prefix="hosted",
        token_provider=lambda: "blob-token-value",
    )
    assert imports == []

    raw = _raw()
    store.put_raw(raw)
    latest = RawOfficialDocument(
        kind=raw.kind,
        official_url=raw.official_url,
        media_type=raw.media_type,
        content=b"%PDF updated official bytes",
        retrieved_at=datetime(2026, 7, 14, tzinfo=UTC),
    )
    store.put_raw(latest)
    assert imports == ["vercel.blob"]
    assert store.latest_raw_for_url(raw.official_url) == latest
    assert all(options["access"] == "private" for options in put_options)
    assert all(options["overwrite"] is False for options in put_options)
    assert all(options["add_random_suffix"] is False for options in put_options)
    assert all(options["token"] == "blob-token-value" for options in put_options)
    assert all(options["access"] == "private" for options in get_options)
    assert all(options["use_cache"] is False for options in get_options)
    assert all(options["token"] == "blob-token-value" for options in get_options)
    assert [options["cursor"] for options in list_options] == [None, "1"]
    assert all(options["token"] == "blob-token-value" for options in list_options)


def test_blob_errors_do_not_echo_sdk_body_or_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeOfficialBlobClient()
    client.read_error = RuntimeError("response body has blob-token-and-secret")
    store = VercelBlobOfficialDocumentStore(client=client)

    with pytest.raises(RuntimeError) as error:
        store.put_raw(_raw())
    assert "blob-token-and-secret" not in str(error.value)
    assert "response body" not in str(error.value)

    def missing(_name: str) -> object:
        raise ImportError("request carried blob-token-and-secret")

    monkeypatch.setattr(
        "kasm.research.documents.importlib.import_module",
        missing,
    )
    lazy = VercelBlobOfficialDocumentStore(
        token_provider=lambda: "blob-token-and-secret"
    )
    with pytest.raises(RuntimeError) as lazy_error:
        lazy.get_raw("a" * 64)
    assert "blob-token-and-secret" not in str(lazy_error.value)


def test_atomic_write_uses_distinct_temporary_paths_per_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "shared.json"
    barrier = threading.Barrier(2)
    original_write_bytes = Path.write_bytes

    def synchronized_write(path: Path, content: bytes) -> int:
        written = original_write_bytes(path, content)
        barrier.wait(timeout=2)
        return written

    monkeypatch.setattr(Path, "write_bytes", synchronized_write)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_atomic_write, target, content)
            for content in (b"first", b"second")
        ]
        for future in futures:
            future.result(timeout=3)

    assert target.read_bytes() in {b"first", b"second"}
    assert list(tmp_path.glob(".*.tmp")) == []
