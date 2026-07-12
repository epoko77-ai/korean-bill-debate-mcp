from kasm.indexing.embeddings import HashEmbeddingProvider


def test_hash_embeddings_are_deterministic_and_normalized() -> None:
    provider = HashEmbeddingProvider(dimensions=32)
    first, second = provider.embed(["소버린 AI 모델", "소버린 AI 모델"])
    assert first == second
    assert abs(sum(value * value for value in first) - 1.0) < 1e-9


def test_empty_embedding_is_zero_vector() -> None:
    assert HashEmbeddingProvider(8).embed([""])[0] == [0.0] * 8
