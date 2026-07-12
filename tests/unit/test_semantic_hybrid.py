from kasm.indexing.embeddings import HashEmbeddingProvider
from kasm.indexing.vector import ExactVectorIndex, VectorIndexMetadata
from kasm.search.filters import SearchFilters
from kasm.search.hybrid import HybridSearch
from kasm.search.semantic import SemanticSearch


class Lexical:
    def search(self, query, filters=None, *, candidate_limit=50):
        del query, filters, candidate_limit
        return [{"id": "s2", "lexical_score": 2.0}, {"id": "s1", "lexical_score": 1.0}]


def test_semantic_filters_and_hybrid_ranks_are_reproducible() -> None:
    provider = HashEmbeddingProvider(dimensions=16)
    index = ExactVectorIndex(VectorIndexMetadata(provider.model_name, 16, "hash"))
    documents = {
        "s1": {"speaker": "김미래", "meeting_type": "committee", "text": "소버린 인공지능"},
        "s2": {"speaker": "박정책", "meeting_type": "plenary", "text": "다른 발언"},
    }
    for item_id, item in documents.items():
        index.upsert(item_id, provider.embed_documents([item["text"]])[0])
    semantic = SemanticSearch(provider, index, documents.get)
    assert semantic.search("소버린", SearchFilters(meeting_type="committee"))[0]["id"] == "s1"
    results = HybridSearch(Lexical(), semantic).search("소버린", limit=2)
    assert [item["id"] for item in results] == ["s2", "s1"]
    assert results[0]["lexical_rank"] == 1
    assert results[0]["semantic_rank"] == 2
