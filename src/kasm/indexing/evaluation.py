"""Small, reproducible retrieval evaluation helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .embeddings import EmbeddingProvider
from .vector import ExactVectorIndex, VectorIndexMetadata


@dataclass(frozen=True, slots=True)
class RetrievalCase:
    query: str
    relevant_ids: frozenset[str]


def reciprocal_rank(ids: list[str], relevant: set[str], *, at_k: int = 10) -> float:
    """Return reciprocal rank of the first relevant identifier within k results."""
    for rank, item_id in enumerate(ids[:at_k], 1):
        if item_id in relevant:
            return 1.0 / rank
    return 0.0


def recall_at_k(ids: list[str], relevant: set[str], *, at_k: int) -> float:
    if not relevant:
        raise ValueError("relevant identifiers must not be empty")
    return len(set(ids[:at_k]) & relevant) / len(relevant)


def ndcg_at_k(ids: list[str], relevant: set[str], *, at_k: int = 10) -> float:
    """Binary-relevance nDCG for a ranked identifier list."""
    if not relevant:
        raise ValueError("relevant identifiers must not be empty")
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, item_id in enumerate(ids[:at_k], 1)
        if item_id in relevant
    )
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(at_k, len(relevant)) + 1))
    return dcg / ideal


def evaluate_recall(
    provider: EmbeddingProvider,
    documents: dict[str, str],
    cases: list[RetrievalCase],
    *,
    at_k: int = 10,
) -> float:
    """Return macro recall@k for an embedding provider."""
    if not documents or not cases:
        raise ValueError("documents and evaluation cases must not be empty")
    index = ExactVectorIndex(VectorIndexMetadata(provider.model_name, provider.dimensions, "eval"))
    vectors = provider.embed_documents(list(documents.values()))
    for item_id, vector in zip(documents, vectors, strict=True):
        index.upsert(item_id, vector)
    recalls = []
    for case in cases:
        found = {
            item_id for item_id, _ in index.search(provider.embed_query(case.query), limit=at_k)
        }
        recalls.append(len(found & case.relevant_ids) / len(case.relevant_ids))
    return sum(recalls) / len(recalls)
