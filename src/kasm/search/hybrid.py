"""Hybrid lexical and semantic orchestration with reciprocal-rank fusion."""

from __future__ import annotations

from typing import Any, Protocol

from .filters import SearchFilters
from .ranking import rrf_fuse


class CandidateBackend(Protocol):
    def search(
        self,
        query: str,
        filters: SearchFilters | None = None,
        *,
        candidate_limit: int = 50,
    ) -> list[dict[str, Any]]: ...


class HybridSearch:
    def __init__(self, lexical: CandidateBackend, semantic: CandidateBackend) -> None:
        self.lexical = lexical
        self.semantic = semantic

    def search(
        self,
        query: str,
        filters: SearchFilters | None = None,
        *,
        limit: int = 10,
        candidate_limit: int = 50,
    ) -> list[dict[str, Any]]:
        lexical = self.lexical.search(query, filters, candidate_limit=candidate_limit)
        semantic = self.semantic.search(query, filters, candidate_limit=candidate_limit)
        lexical_by_id = {str(item["id"]): item for item in lexical}
        semantic_by_id = {str(item["id"]): item for item in semantic}
        fused = rrf_fuse(list(lexical_by_id), list(semantic_by_id), limit=limit)
        results = []
        for ranked in fused:
            item_id = str(ranked.item_id)
            source = lexical_by_id.get(item_id) or semantic_by_id[item_id]
            hydrated = source.get("item")
            if isinstance(hydrated, dict):
                source = {
                    **hydrated,
                    **{key: value for key, value in source.items() if key != "item"},
                }
            results.append(
                {
                    **source,
                    "id": item_id,
                    "lexical_rank": ranked.lexical_rank,
                    "semantic_rank": ranked.semantic_rank,
                    "hybrid_score": ranked.hybrid_score,
                }
            )
        return results
