from kasm.indexing.embeddings import HashEmbeddingProvider
from kasm.indexing.evaluation import RetrievalCase, evaluate_recall


def test_evaluate_recall_at_k() -> None:
    provider = HashEmbeddingProvider(64)
    documents = {"budget": "예산 budget 심사", "climate": "기후 climate 대응"}
    cases = [RetrievalCase("budget", frozenset({"budget"}))]
    assert evaluate_recall(provider, documents, cases, at_k=1) == 1.0
