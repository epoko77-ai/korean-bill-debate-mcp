from datetime import date

import pytest

from kasm.search.filters import SearchFilters
from kasm.search.ranking import reciprocal_rank_fusion, rrf_fuse


def test_rrf_rewards_items_present_in_both_lists_and_tracks_ranks():
    results = rrf_fuse(["a", "b", "c"], ["b", "d", "a"], k=60)
    assert [item.item_id for item in results[:2]] == ["b", "a"]
    assert results[0].lexical_rank == 2
    assert results[0].semantic_rank == 1


def test_rrf_is_stable_and_ignores_duplicate_occurrences():
    assert reciprocal_rank_fusion([["a", "a"], ["b"]], k=0) == [("a", 1.0), ("b", 1.0)]
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([["a"]], weights=[])


def test_structured_filters_match_mapping_and_validate_range():
    filters = SearchFilters(
        assembly_term=22, committee="과방위", date_from="2024-01-01", date_to=date(2025, 1, 1)
    )
    assert filters.matches(
        {"assembly_term": 22, "committee_name": "국회 과방위", "date": "2024-06-01"}
    )
    assert not filters.matches(
        {"assembly_term": 21, "committee_name": "국회 과방위", "date": "2024-06-01"}
    )
    with pytest.raises(ValueError):
        SearchFilters(date_from="2025-01-02", date_to="2025-01-01")
