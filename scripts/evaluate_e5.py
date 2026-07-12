"""Run the checked-in Korean/English semantic retrieval gate."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from kasm.indexing.embeddings import SentenceTransformersProvider
from kasm.indexing.evaluation import RetrievalCase, evaluate_recall

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/search/cross_language.json"
RESULT = ROOT / "benchmarks/e5-cross-language.json"


def main() -> None:
    payload = json.loads(FIXTURE.read_text("utf-8"))
    provider = SentenceTransformersProvider()
    documents = {item["id"]: item["text"] for item in payload["documents"]}
    cases = [
        RetrievalCase(item["query"], frozenset(item["relevant_ids"])) for item in payload["queries"]
    ]
    recall_at_10 = evaluate_recall(provider, documents, cases, at_k=10)
    recall_at_3 = evaluate_recall(provider, documents, cases, at_k=3)
    result = {
        "model": provider.model_name,
        "dimensions": provider.dimensions,
        "documents": len(documents),
        "queries": len(cases),
        "recall_at_3": recall_at_3,
        "recall_at_10": recall_at_10,
        "required_recall_at_10": 0.8,
        "passed": recall_at_10 >= 0.8,
        "measured_at": datetime.now(UTC).isoformat(),
    }
    RESULT.parent.mkdir(exist_ok=True)
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps(result, ensure_ascii=False))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
