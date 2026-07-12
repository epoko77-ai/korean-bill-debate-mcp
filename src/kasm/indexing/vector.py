"""Portable exact vector index used as the deterministic backend and FAISS contract."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class VectorIndexMetadata:
    model_name: str
    dimensions: int
    corpus_hash: str
    index_version: str = "kasm-vector-v1"


class SearchableVectorIndex(Protocol):
    metadata: VectorIndexMetadata

    def search(self, query: list[float], *, limit: int = 50) -> list[tuple[str, float]]: ...


class ExactVectorIndex:
    def __init__(self, metadata: VectorIndexMetadata) -> None:
        self.metadata = metadata
        self._vectors: dict[str, list[float]] = {}

    def upsert(self, item_id: str, vector: list[float]) -> None:
        if len(vector) != self.metadata.dimensions:
            raise ValueError("vector dimensions do not match index metadata")
        self._vectors[item_id] = vector

    def search(self, query: list[float], *, limit: int = 50) -> list[tuple[str, float]]:
        if len(query) != self.metadata.dimensions:
            raise ValueError("query dimensions do not match index metadata")
        if limit < 1:
            raise ValueError("limit must be positive")
        query_norm = math.sqrt(sum(value * value for value in query))
        scores = []
        for item_id, vector in self._vectors.items():
            vector_norm = math.sqrt(sum(value * value for value in vector))
            denominator = query_norm * vector_norm
            dot_product = sum(a * b for a, b in zip(query, vector, strict=True))
            score = dot_product / denominator if denominator else 0.0
            scores.append((item_id, score))
        return sorted(scores, key=lambda item: (-item[1], item[0]))[:limit]

    def save(self, path: str | Path) -> None:
        payload = {"metadata": asdict(self.metadata), "vectors": self._vectors}
        Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(
        cls, path: str | Path, *, expected: VectorIndexMetadata | None = None
    ) -> ExactVectorIndex:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        metadata = VectorIndexMetadata(**payload["metadata"])
        if expected is not None and metadata != expected:
            raise ValueError("vector index metadata mismatch; rebuild the index")
        index = cls(metadata)
        for item_id, vector in payload["vectors"].items():
            index.upsert(item_id, [float(value) for value in vector])
        return index


class FaissVectorIndex:
    """FAISS inner-product index with stable IDs and a versioned JSON sidecar."""

    def __init__(self, metadata: VectorIndexMetadata) -> None:
        self.metadata = metadata
        self._item_ids: list[str] = []
        self._vectors: list[list[float]] = []
        self._index: Any | None = None

    @staticmethod
    def _dependencies() -> tuple[Any, Any]:
        try:
            faiss = import_module("faiss")
            numpy = import_module("numpy")
        except ImportError as exc:
            raise RuntimeError(
                "FAISS backend requires: pip install 'korean-bill-debate-mcp[semantic]'"
            ) from exc
        return faiss, numpy

    def upsert(self, item_id: str, vector: list[float]) -> None:
        if self._index is not None:
            raise RuntimeError("a loaded or finalized FAISS index is immutable")
        if len(vector) != self.metadata.dimensions:
            raise ValueError("vector dimensions do not match index metadata")
        if item_id in self._item_ids:
            position = self._item_ids.index(item_id)
            self._vectors[position] = vector
        else:
            self._item_ids.append(item_id)
            self._vectors.append(vector)
        self._index = None

    def _build(self) -> None:
        faiss, numpy = self._dependencies()
        index = faiss.IndexFlatIP(self.metadata.dimensions)
        if self._vectors:
            matrix = numpy.asarray(self._vectors, dtype="float32")
            faiss.normalize_L2(matrix)
            index.add(matrix)
        self._index = index

    def search(self, query: list[float], *, limit: int = 50) -> list[tuple[str, float]]:
        if len(query) != self.metadata.dimensions:
            raise ValueError("query dimensions do not match index metadata")
        if limit < 1:
            raise ValueError("limit must be positive")
        if not self._item_ids:
            return []
        if self._index is None:
            self._build()
        assert self._index is not None
        faiss, numpy = self._dependencies()
        matrix = numpy.asarray([query], dtype="float32")
        faiss.normalize_L2(matrix)
        scores, positions = self._index.search(matrix, min(limit, len(self._item_ids)))
        return [
            (self._item_ids[int(position)], float(score))
            for score, position in zip(scores[0], positions[0], strict=True)
            if position >= 0
        ]

    def save(self, path: str | Path) -> None:
        if self._index is None:
            self._build()
        assert self._index is not None
        faiss, _ = self._dependencies()
        path = Path(path)
        faiss.write_index(self._index, str(path))
        sidecar = {
            "metadata": asdict(self.metadata),
            "item_ids": self._item_ids,
        }
        path.with_suffix(path.suffix + ".json").write_text(
            json.dumps(sidecar, ensure_ascii=False), encoding="utf-8"
        )

    @classmethod
    def load(
        cls, path: str | Path, *, expected: VectorIndexMetadata | None = None
    ) -> FaissVectorIndex:
        faiss, _ = cls._dependencies()
        path = Path(path)
        payload = json.loads(path.with_suffix(path.suffix + ".json").read_text("utf-8"))
        metadata = VectorIndexMetadata(**payload["metadata"])
        if expected is not None and metadata != expected:
            raise ValueError("vector index metadata mismatch; rebuild the index")
        instance = cls(metadata)
        instance._item_ids = [str(item_id) for item_id in payload["item_ids"]]
        # v1 sidecars briefly included redundant vectors; accept them while
        # keeping new sidecars compact because FAISS already stores the matrix.
        instance._vectors = []
        instance._index = faiss.read_index(str(path))
        if instance._index.ntotal != len(instance._item_ids):
            raise ValueError("FAISS index and ID sidecar are inconsistent")
        return instance
