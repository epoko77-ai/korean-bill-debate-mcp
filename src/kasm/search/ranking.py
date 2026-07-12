"""Reciprocal Rank Fusion for backend-independent hybrid retrieval."""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass
from typing import TypeVar

Id = TypeVar("Id", bound=Hashable)


@dataclass(frozen=True, slots=True)
class RankedItem:
    item_id: Hashable
    hybrid_score: float
    lexical_rank: int | None = None
    semantic_rank: int | None = None


def reciprocal_rank_fusion(
    rankings: Iterable[Sequence[Id]], *, k: int = 60, weights: Sequence[float] | None = None
) -> list[tuple[Id, float]]:
    """Fuse ordered identifier lists using ``sum(weight / (k + rank))``.

    Duplicate IDs inside one ranking count only at their first (best) rank. Ties
    are resolved by first appearance, making results stable and reproducible.
    """

    if k < 0:
        raise ValueError("k must be non-negative")
    lists = list(rankings)
    if weights is None:
        weights = [1.0] * len(lists)
    if len(weights) != len(lists):
        raise ValueError("weights must have one entry per ranking")
    if any(weight < 0 for weight in weights):
        raise ValueError("weights cannot be negative")
    scores: dict[Id, float] = {}
    order: dict[Id, int] = {}
    for ranking, weight in zip(lists, weights, strict=True):
        seen: set[Id] = set()
        for rank, item_id in enumerate(ranking, 1):
            if item_id in seen:
                continue
            seen.add(item_id)
            order.setdefault(item_id, len(order))
            scores[item_id] = scores.get(item_id, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], order[pair[0]]))


def rrf_fuse(
    lexical_ids: Sequence[Id],
    semantic_ids: Sequence[Id],
    *,
    k: int = 60,
    lexical_weight: float = 1.0,
    semantic_weight: float = 1.0,
    limit: int | None = None,
) -> list[RankedItem]:
    """Fuse lexical and semantic result IDs while retaining provenance ranks."""

    def first_ranks(items: Sequence[Id]) -> dict[Id, int]:
        ranks: dict[Id, int] = {}
        for rank, item in enumerate(items, 1):
            ranks.setdefault(item, rank)
        return ranks

    lexical_rank = first_ranks(lexical_ids)
    semantic_rank = first_ranks(semantic_ids)
    fused = reciprocal_rank_fusion(
        (lexical_ids, semantic_ids), k=k, weights=(lexical_weight, semantic_weight)
    )
    if limit is not None:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        fused = fused[:limit]
    return [
        RankedItem(item, score, lexical_rank.get(item), semantic_rank.get(item))
        for item, score in fused
    ]
