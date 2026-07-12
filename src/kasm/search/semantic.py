"""Semantic retrieval over a versioned local vector index."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kasm.indexing.embeddings import EmbeddingProvider
from kasm.indexing.vector import SearchableVectorIndex

from .filters import SearchFilters


class SemanticSearch:
    def __init__(
        self,
        provider: EmbeddingProvider,
        index: SearchableVectorIndex,
        hydrate: Callable[[str], object | None],
    ) -> None:
        if provider.model_name != index.metadata.model_name:
            raise ValueError("embedding provider and vector index model do not match")
        if provider.dimensions != index.metadata.dimensions:
            raise ValueError("embedding provider and vector index dimensions do not match")
        self.provider = provider
        self.index = index
        self.hydrate = hydrate

    def search(
        self,
        query: str,
        filters: SearchFilters | None = None,
        *,
        candidate_limit: int = 50,
    ) -> list[dict[str, Any]]:
        filters = filters or SearchFilters()
        # Retrieve extra candidates before structured filtering so restrictive
        # filters still have a useful semantic result set.
        candidates = self.index.search(
            self.provider.embed_query(query), limit=max(candidate_limit * 5, candidate_limit)
        )
        results: list[dict[str, Any]] = []
        for item_id, score in candidates:
            item = self.hydrate(item_id)
            if item is None or not filters.matches(item):
                continue
            results.append({"id": item_id, "semantic_score": score, "item": item})
            if len(results) == candidate_limit:
                break
        return results
