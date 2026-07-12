"""Evaluate real FTS5 + multilingual E5 + RRF on bilingual queries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from kasm.indexing.embeddings import SentenceTransformersProvider
from kasm.indexing.evaluation import ndcg_at_k, recall_at_k, reciprocal_rank
from kasm.indexing.vector import ExactVectorIndex, VectorIndexMetadata
from kasm.search.lexical import LexicalSearch
from kasm.search.ranking import rrf_fuse
from kasm.storage.database import Database

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/search/cross_language.json"
RESULT = ROOT / "benchmarks/hybrid-bilingual.json"


def main() -> None:
    payload = json.loads(FIXTURE.read_text("utf-8"))
    documents = {item["id"]: item["text"] for item in payload["documents"]}
    provider = SentenceTransformersProvider()
    vector = ExactVectorIndex(VectorIndexMetadata(provider.model_name, provider.dimensions, "eval"))
    for item_id, embedding in zip(
        documents, provider.embed_documents(list(documents.values())), strict=True
    ):
        vector.upsert(item_id, embedding)

    scores: dict[str, dict[str, list[float]]] = {
        name: {"mrr": [], "recall5": [], "recall10": [], "ndcg10": []}
        for name in ("lexical", "semantic", "hybrid")
    }
    with Database(":memory:") as database:
        connection = database.connection
        connection.execute(
            """INSERT INTO meetings VALUES
            ('eval', 22, 'eval', '평가위원회', NULL, '평가', 'committee', '1',
             '2026-01-01', 'https://example.invalid/eval', 'synthetic',
             '2026-01-01T00:00:00+00:00')"""
        )
        connection.executemany(
            """INSERT INTO speeches
            (id, meeting_id, sequence, speaker_name, text, source_hash, parser_version)
            VALUES (?, 'eval', ?, '평가', ?, 'synthetic', 'eval')""",
            [
                (item_id, sequence, text)
                for sequence, (item_id, text) in enumerate(documents.items())
            ],
        )
        connection.commit()
        lexical_search = LexicalSearch(connection)
        for case in payload["queries"]:
            relevant = set(case["relevant_ids"])
            for query in (case["query"], case["query_ko"]):
                lexical = [row["id"] for row in lexical_search.search(query, candidate_limit=20)]
                semantic = [
                    item_id for item_id, _ in vector.search(provider.embed_query(query), limit=20)
                ]
                hybrid = [str(item.item_id) for item in rrf_fuse(lexical, semantic, limit=20)]
                for name, ranking in (
                    ("lexical", lexical),
                    ("semantic", semantic),
                    ("hybrid", hybrid),
                ):
                    scores[name]["mrr"].append(reciprocal_rank(ranking, relevant))
                    scores[name]["recall5"].append(recall_at_k(ranking, relevant, at_k=5))
                    scores[name]["recall10"].append(recall_at_k(ranking, relevant, at_k=10))
                    scores[name]["ndcg10"].append(ndcg_at_k(ranking, relevant))

    mean_scores = {
        name: {metric: sum(values) / len(values) for metric, values in metrics.items()}
        for name, metrics in scores.items()
    }
    result = {
        "fixture": "synthetic bilingual topic separation",
        "model": provider.model_name,
        "queries": len(scores["hybrid"]["mrr"]),
        "metrics": {
            name: {
                "recall_at_5": metrics["recall5"],
                "recall_at_10": metrics["recall10"],
                "mrr_at_10": metrics["mrr"],
                "ndcg_at_10": metrics["ndcg10"],
            }
            for name, metrics in mean_scores.items()
        },
        "required_hybrid_mrr_at_10": 0.8,
        "passed": mean_scores["hybrid"]["mrr"] >= 0.8,
        "measured_at": datetime.now(UTC).isoformat(),
    }
    RESULT.parent.mkdir(exist_ok=True)
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps(result, ensure_ascii=False))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
