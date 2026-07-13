from __future__ import annotations

import pytest

from kasm.corpus import (
    CorpusObjectConflictError,
    VercelBlobCorpusObjectStore,
)


class BlobClient:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[tuple[str, str, bool, bool, str]] = []

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
        if pathname in self.objects and not allow_overwrite:
            raise RuntimeError("exists")
        self.objects[pathname] = body
        self.puts.append(
            (
                pathname,
                access,
                add_random_suffix,
                allow_overwrite,
                content_type,
            )
        )
        return object()

    def get(self, pathname: str) -> bytes | None:
        return self.objects.get(pathname)


def test_private_blob_store_preserves_exact_immutable_bytes() -> None:
    client = BlobClient()
    store = VercelBlobCorpusObjectStore(
        prefix="private/corpus",
        client=client,
    )

    store.put_immutable("revisions/a.json", b"first")
    store.put_immutable("revisions/a.json", b"first")

    assert store.get("revisions/a.json") == b"first"
    assert client.puts == [
        (
            "private/corpus/revisions/a.json",
            "private",
            False,
            False,
            "application/json; charset=utf-8",
        )
    ]
    with pytest.raises(CorpusObjectConflictError):
        store.put_immutable("revisions/a.json", b"different")


@pytest.mark.parametrize("prefix", ["", "/", "../corpus", "a/../b"])
def test_blob_store_rejects_unsafe_prefixes(prefix: str) -> None:
    with pytest.raises(ValueError, match="prefix"):
        VercelBlobCorpusObjectStore(prefix=prefix, client=BlobClient())


def test_blob_store_cannot_make_official_corpus_public() -> None:
    with pytest.raises(ValueError, match="private"):
        VercelBlobCorpusObjectStore(access="public", client=BlobClient())
