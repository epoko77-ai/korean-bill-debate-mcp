import importlib.util
import json

import pytest

from kasm.indexing.vector import ExactVectorIndex, FaissVectorIndex, VectorIndexMetadata


def test_vector_index_is_stable_and_persistent(tmp_path) -> None:
    metadata = VectorIndexMetadata("test-model", 3, "corpus-hash")
    index = ExactVectorIndex(metadata)
    index.upsert("speech-b", [0.0, 1.0, 0.0])
    index.upsert("speech-a", [1.0, 0.0, 0.0])
    assert index.search([1.0, 0.0, 0.0])[0][0] == "speech-a"
    path = tmp_path / "index.json"
    index.save(path)
    loaded = ExactVectorIndex.load(path, expected=metadata)
    assert loaded.search([0.0, 1.0, 0.0])[0][0] == "speech-b"


def test_vector_index_rejects_metadata_or_dimension_mismatch(tmp_path) -> None:
    metadata = VectorIndexMetadata("test-model", 2, "hash")
    index = ExactVectorIndex(metadata)
    try:
        index.upsert("bad", [1.0])
    except ValueError as exc:
        assert "dimensions" in str(exc)
    else:
        raise AssertionError("dimension mismatch must fail")
    path = tmp_path / "index.json"
    index.save(path)
    wrong = VectorIndexMetadata("other-model", 2, "hash")
    try:
        ExactVectorIndex.load(path, expected=wrong)
    except ValueError as exc:
        assert "metadata mismatch" in str(exc)
    else:
        raise AssertionError("metadata mismatch must fail")


@pytest.mark.skipif(importlib.util.find_spec("faiss") is None, reason="semantic extra missing")
def test_faiss_vector_index_round_trip(tmp_path) -> None:
    metadata = VectorIndexMetadata("test-model", 3, "corpus-hash")
    index = FaissVectorIndex(metadata)
    index.upsert("speech-b", [0.0, 1.0, 0.0])
    index.upsert("speech-a", [1.0, 0.0, 0.0])
    path = tmp_path / "speeches.faiss"
    index.save(path)
    assert "vectors" not in json.loads(path.with_suffix(".faiss.json").read_text("utf-8"))
    loaded = FaissVectorIndex.load(path, expected=metadata)
    assert loaded.search([1.0, 0.0, 0.0])[0][0] == "speech-a"
