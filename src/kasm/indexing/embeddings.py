from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

TOKEN_RE = re.compile(r"[\w가-힣]+", re.UNICODE)


class EmbeddingProvider(Protocol):
    """Interface implemented by local semantic embedding backends."""

    model_name: str
    dimensions: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashEmbeddingProvider:
    """Dependency-free deterministic baseline for tests and instant demos.

    This is not presented as a multilingual semantic model. Production indexes
    should use the optional sentence-transformers provider added by deployments.
    """

    model_name = "kasm/hash-token-v1"

    def __init__(self, dimensions: int = 256) -> None:
        if dimensions < 8:
            raise ValueError("dimensions must be at least 8")
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed_one(text)

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in TOKEN_RE.findall(text.casefold()):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[bucket] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return vector if norm == 0 else [value / norm for value in vector]


class SentenceTransformersProvider:
    """Optional multilingual E5 backend; imported only when selected."""

    def __init__(self, model_name: str = "intfloat/multilingual-e5-small") -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("install the semantic extra to use sentence-transformers") from exc
        self.model_name = model_name
        self._model = SentenceTransformer(model_name)
        dimension_getter = getattr(
            self._model,
            "get_embedding_dimension",
            self._model.get_sentence_embedding_dimension,
        )
        self.dimensions = int(dimension_getter())

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed_documents(texts)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._model.encode(
            [f"passage: {text}" for text in texts], normalize_embeddings=True
        )
        return [list(map(float, vector)) for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        vector = self._model.encode([f"query: {text}"], normalize_embeddings=True)[0]
        return list(map(float, vector))
