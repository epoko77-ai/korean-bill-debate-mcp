from kasm.indexing.evaluation import ndcg_at_k, recall_at_k, reciprocal_rank


def test_reciprocal_rank_at_k() -> None:
    assert reciprocal_rank(["wrong", "right"], {"right"}) == 0.5
    assert reciprocal_rank(["right"], {"missing"}) == 0.0
    assert recall_at_k(["wrong", "right"], {"right"}, at_k=2) == 1.0
    assert ndcg_at_k(["right"], {"right"}) == 1.0
