from __future__ import annotations

import json
import os
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from kasm.research.artifacts import (
    ArtifactBackendError,
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactKind,
    FilesystemResearchArtifactStore,
    SecretMaterialError,
    VercelBlobResearchArtifactStore,
    assert_secret_free,
    canonical_hash,
    canonical_json,
)


class FakeBlobClient:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.list_calls: list[str] = []
        self.error: Exception | None = None

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
        if self.error:
            raise self.error
        if pathname in self.objects and not allow_overwrite:
            raise RuntimeError("already exists")
        self.put_calls.append(
            {
                "pathname": pathname,
                "access": access,
                "add_random_suffix": add_random_suffix,
                "allow_overwrite": allow_overwrite,
                "content_type": content_type,
            }
        )
        self.objects[pathname] = body
        return {"pathname": pathname}

    def get(self, pathname: str) -> bytes | None:
        self.get_calls.append(pathname)
        if self.error:
            raise self.error
        return self.objects.get(pathname)

    def list(self, *, prefix: str) -> tuple[str, ...]:
        self.list_calls.append(prefix)
        if self.error:
            raise self.error
        return tuple(sorted(path for path in self.objects if path.startswith(prefix)))


def test_canonical_json_and_hash_ignore_mapping_order() -> None:
    first = {"나": [3, {"b": True, "a": None}], "가": "값"}
    second = {"가": "값", "나": [3, {"a": None, "b": True}]}

    assert canonical_json(first) == canonical_json(second)
    assert canonical_hash(first) == canonical_hash(second)
    assert canonical_json(first).decode() == '{"가":"값","나":[3,{"a":null,"b":true}]}'


def test_filesystem_store_writes_reads_and_lists_every_artifact_kind(tmp_path: Path) -> None:
    store = FilesystemResearchArtifactStore(tmp_path)
    research_id = "research_123"
    refs = (
        store.write_plan(research_id, {"query": "최근 AI 입법"}),
        store.write_partition(research_id, "bill:인공지능", {"rows": 72}),
        store.write_metadata(research_id, {"bill_no": "2219564"}),
        store.write_resolution(research_id, "bill-2219564", {"relevant": True}),
        store.write_manifest(research_id, {"documents": 9}),
        store.write_outcome(research_id, {"complete": True}),
        store.write_result_page(research_id, 1, {"items": [1, 2, 3]}),
        store.write(
            research_id,
            ArtifactKind.JOB_STATE,
            {"status": "queued"},
            logical_key="state",
        ),
    )

    assert {ref.kind for ref in refs} == set(ArtifactKind)
    assert store.list(research_id) == tuple(sorted(refs, key=lambda ref: ref.object_path))
    assert store.list(research_id, ArtifactKind.METADATA) == (refs[2],)
    for ref in refs:
        restored = store.read(ref)
        assert restored is not None
        assert restored.ref == ref
    metadata_path = tmp_path / refs[2].object_path
    assert metadata_path.name == f"sha256-{refs[2].content_hash}.json"
    assert os.stat(metadata_path).st_mode & 0o777 == 0o600


def test_content_addressed_write_is_idempotent(tmp_path: Path) -> None:
    store = FilesystemResearchArtifactStore(tmp_path)
    payload = {"source_hash": "a" * 64, "rows": [1, 2]}

    first = store.write_metadata("research_1", payload)
    second = store.write_metadata("research_1", {"rows": [1, 2], "source_hash": "a" * 64})

    assert first == second
    assert len(store.list("research_1", ArtifactKind.METADATA)) == 1


def test_filesystem_write_publishes_only_after_complete_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FilesystemResearchArtifactStore(tmp_path)
    link_started = Event()
    allow_publish = Event()
    real_link = os.link

    def delayed_link(source: str | bytes, destination: str | bytes) -> None:
        link_started.set()
        if not allow_publish.wait(timeout=5):
            raise RuntimeError("test did not release atomic artifact publish")
        real_link(source, destination)

    monkeypatch.setattr(os, "link", delayed_link)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            store.write_plan,
            "research_atomic",
            {"query": "인공지능 입법"},
        )
        try:
            assert link_started.wait(timeout=5)
            assert (
                store.read_logical(
                    "research_atomic",
                    ArtifactKind.PLAN,
                    "plan",
                )
                is None
            )
        finally:
            allow_publish.set()
        ref = future.result(timeout=5)

    restored = store.read_logical("research_atomic", ArtifactKind.PLAN, "plan")
    assert restored is not None and restored.ref == ref
    assert not tuple(tmp_path.rglob("*.tmp"))


def test_write_once_conflict_does_not_replace_original(tmp_path: Path) -> None:
    store = FilesystemResearchArtifactStore(tmp_path)
    original = store.write_plan("research_1", {"query": "인공지능"})

    with pytest.raises(ArtifactConflictError, match="different content"):
        store.write_plan("research_1", {"query": "해양사고"})

    restored = store.read(original)
    assert restored is not None
    assert restored.payload == {"query": "인공지능"}


def test_logical_reads_do_not_list_sibling_artifacts(tmp_path: Path) -> None:
    filesystem = FilesystemResearchArtifactStore(tmp_path / "filesystem")
    ref = filesystem.write_plan("research_1", {"query": "장애인 이동권"})
    for number in range(50):
        filesystem.write_metadata("research_1", {"number": number})

    restored = filesystem.read_logical(
        "research_1",
        ArtifactKind.PLAN,
        "plan",
    )

    assert restored is not None and restored.ref == ref
    assert (
        filesystem.read_logical("research_1", ArtifactKind.PLAN, "absent")
        is None
    )

    client = FakeBlobClient()
    blob = VercelBlobResearchArtifactStore(client=client, prefix="kbd/private")
    blob_ref = blob.write_manifest("research_2", {"documents": 3})
    client.get_calls.clear()
    client.list_calls.clear()

    blob_restored = blob.read_logical(
        "research_2",
        ArtifactKind.MANIFEST,
        "manifest",
    )

    assert blob_restored is not None and blob_restored.ref == blob_ref
    assert client.get_calls == ["kbd/private/" + blob_ref.object_path]
    assert client.list_calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"api_key": "open-assembly-secret"},
        {"nested": [{"credential_capability": "g" * 120}]},
        {"value": "g" * 120},
        {"auth": {"access_token": "opaque"}},
        {"auth": {"oauth_token": "opaque"}},
        {"value": "Bearer this-is-a-secret-token"},
        {"value": "sk-ant-api03-secret-material"},
        {"url": "https://example.test/mcp/t/gAAAAAabcdefghijklmnopqrstuvxyz012345"},
        {"url": "https://open.assembly.go.kr/openapi?KEY=actual-secret"},
    ],
)
def test_recursive_secret_scan_rejects_credentials(payload: Any, tmp_path: Path) -> None:
    store = FilesystemResearchArtifactStore(tmp_path)

    with pytest.raises(SecretMaterialError):
        store.write_metadata("research_secret", payload)
    assert store.list("research_secret") == ()


def test_redacted_source_url_and_public_hash_are_allowed() -> None:
    payload = {
        "source_url": "https://open.assembly.go.kr/openapi?KEY=%2A%2A%2A&pIndex=1",
        "source_hash": "f" * 64,
        "token_budget": 1000,
    }

    assert_secret_free(payload)
    assert len(canonical_hash(payload)) == 64


def test_cycles_nonfinite_numbers_and_path_traversal_are_rejected(tmp_path: Path) -> None:
    cyclic: list[Any] = []
    cyclic.append(cyclic)

    with pytest.raises(ValueError, match="cycles"):
        canonical_json(cyclic)
    with pytest.raises(ValueError, match="finite"):
        canonical_json({"score": float("nan")})
    with pytest.raises(ValueError, match="research_id"):
        FilesystemResearchArtifactStore(tmp_path).write_metadata("../outside", {})


def test_tampering_is_detected_by_canonical_and_content_hash_checks(tmp_path: Path) -> None:
    store = FilesystemResearchArtifactStore(tmp_path)
    ref = store.write_metadata("research_1", {"bill_no": "2219564"})
    path = tmp_path / ref.object_path
    envelope = json.loads(path.read_bytes())
    envelope["payload"]["bill_no"] = "9999999"
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(ArtifactIntegrityError):
        store.read(ref)


def test_blob_store_is_private_immutable_and_injectable() -> None:
    client = FakeBlobClient()
    store = VercelBlobResearchArtifactStore(client=client, prefix="kbd/private")

    ref = store.write_result_page("research_1", "page-1", {"items": [1]})
    # New immutable objects use the backend's atomic put-if-absent directly.
    # A preliminary existence GET would double every first-pass snapshot-shard
    # round trip and can push large finalization beyond its hosted deadline.
    assert client.get_calls == []
    assert store.read(ref) is not None
    assert store.list("research_1", ArtifactKind.RESULT_PAGE) == (ref,)
    assert client.put_calls == [
        {
            "pathname": "kbd/private/" + ref.object_path,
            "access": "private",
            "add_random_suffix": False,
            "allow_overwrite": False,
            "content_type": "application/json; charset=utf-8",
        }
    ]
    client.get_calls.clear()
    assert store.write_result_page("research_1", "page-1", {"items": [1]}) == ref
    assert len(client.put_calls) == 1
    assert client.get_calls == ["kbd/private/" + ref.object_path]
    client.get_calls.clear()
    with pytest.raises(ArtifactConflictError):
        store.write_result_page("research_1", "page-1", {"items": [2]})
    assert client.get_calls == ["kbd/private/" + ref.object_path]


def test_blob_put_first_recovers_an_ambiguous_committed_response() -> None:
    class AmbiguousCommittedClient(FakeBlobClient):
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
            super().put(
                pathname,
                body,
                access=access,
                add_random_suffix=add_random_suffix,
                allow_overwrite=allow_overwrite,
                content_type=content_type,
            )
            raise RuntimeError("response was lost after the object became durable")

    client = AmbiguousCommittedClient()
    store = VercelBlobResearchArtifactStore(client=client, prefix="kbd/private")

    ref = store.write_manifest("research_1", {"documents": 120})

    assert len(client.put_calls) == 1
    assert client.get_calls == ["kbd/private/" + ref.object_path]
    assert store.read(ref) is not None


def test_blob_sdk_is_lazy_and_public_access_is_forbidden(monkeypatch) -> None:
    imports: list[str] = []

    def missing(name: str):
        imports.append(name)
        raise ImportError("not installed")

    monkeypatch.setattr("kasm.research.artifacts.importlib.import_module", missing)
    store = VercelBlobResearchArtifactStore(token_provider=lambda: "blob-secret")
    assert imports == []

    with pytest.raises(ArtifactBackendError) as error:
        store.write_manifest("research_1", {"count": 1})
    assert imports == ["vercel.blob"]
    assert "blob-secret" not in str(error.value)
    with pytest.raises(ValueError, match="private"):
        VercelBlobResearchArtifactStore(access="public")


def test_blob_store_adapts_the_official_vercel_blob_sdk_shape(monkeypatch) -> None:
    objects: dict[str, bytes] = {}
    put_options: list[dict[str, Any]] = []
    list_options: list[dict[str, Any]] = []

    class BlobNotFoundError(Exception):
        pass

    def get(pathname: str, **options: Any) -> object:
        assert options["access"] == "private"
        assert options["use_cache"] is False
        if pathname not in objects:
            raise BlobNotFoundError
        return types.SimpleNamespace(content=objects[pathname])

    def put(pathname: str, body: bytes, **options: Any) -> object:
        put_options.append(options)
        assert options["overwrite"] is False
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
    monkeypatch.setattr(
        "kasm.research.artifacts.importlib.import_module",
        lambda name: module if name == "vercel.blob" else None,
    )
    store = VercelBlobResearchArtifactStore(token_provider=lambda: "blob-token")

    ref = store.write_manifest("research_1", {"documents": 3})
    metadata_refs = (
        store.write_metadata("research_1", {"bill_no": "2210001"}),
        store.write_metadata("research_1", {"bill_no": "2210002"}),
    )

    assert store.read(ref) is not None
    assert store.list("research_1", ArtifactKind.MANIFEST) == (ref,)
    assert store.list("research_1", ArtifactKind.METADATA) == tuple(
        sorted(metadata_refs, key=lambda item: item.object_path)
    )
    assert put_options[0]["access"] == "private"
    assert put_options[0]["token"] == "blob-token"
    assert [options["cursor"] for options in list_options] == [None, None, "1"]
    assert all(options["token"] == "blob-token" for options in list_options)


def test_blob_errors_never_echo_sdk_body_or_token() -> None:
    client = FakeBlobClient()
    client.error = RuntimeError("echo body with super-secret-token")
    store = VercelBlobResearchArtifactStore(client=client)

    with pytest.raises(ArtifactBackendError) as error:
        store.write_outcome("research_1", {"complete": True})
    message = str(error.value)
    assert "super-secret-token" not in message
    assert "echo body" not in message
