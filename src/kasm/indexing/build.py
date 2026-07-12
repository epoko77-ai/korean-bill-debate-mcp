"""Build versioned local speech vector indexes from SQLite."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kasm.core.models import EmbeddingRecord
from kasm.storage.repositories import EmbeddingRepository

from .embeddings import EmbeddingProvider
from .vector import ExactVectorIndex, FaissVectorIndex, VectorIndexMetadata


def corpus_hash(rows: list[tuple[str, str, str]]) -> str:
    digest = hashlib.sha256()
    for speech_id, source_hash, text in rows:
        digest.update(speech_id.encode())
        digest.update(b"\0")
        digest.update(source_hash.encode())
        digest.update(b"\0")
        digest.update(text.encode())
        digest.update(b"\n")
    return digest.hexdigest()


def build_vector_index(
    connection: sqlite3.Connection | Any,
    provider: EmbeddingProvider,
    output_path: str | Path,
    *,
    backend: str = "exact",
) -> VectorIndexMetadata:
    output_path = Path(output_path)
    if backend == "faiss" and output_path.suffix != ".faiss":
        raise ValueError("FAISS output path must end in .faiss")
    if backend == "exact" and output_path.suffix == ".faiss":
        raise ValueError("exact index output path must not end in .faiss")
    connection = getattr(connection, "connection", connection)
    raw_rows = connection.execute(
        "SELECT id, source_hash, text FROM speeches ORDER BY id"
    ).fetchall()
    rows = [(str(row[0]), str(row[1]), str(row[2])) for row in raw_rows]
    if not rows:
        raise ValueError("cannot build a vector index without speeches")
    metadata = VectorIndexMetadata(
        model_name=provider.model_name,
        dimensions=provider.dimensions,
        corpus_hash=corpus_hash(rows),
    )
    if backend not in {"exact", "faiss"}:
        raise ValueError("backend must be exact or faiss")
    index = FaissVectorIndex(metadata) if backend == "faiss" else ExactVectorIndex(metadata)
    vectors = provider.embed_documents([text for _, _, text in rows])
    if len(vectors) != len(rows):
        raise RuntimeError("embedding provider returned an unexpected vector count")
    repository = EmbeddingRepository(connection)
    with connection:
        for (speech_id, _, _), vector in zip(rows, vectors, strict=True):
            index.upsert(speech_id, vector)
            repository.save(
                EmbeddingRecord(
                    speech_id=speech_id,
                    model_name=provider.model_name,
                    dimensions=provider.dimensions,
                    vector_location=f"{output_path}#{speech_id}",
                    created_at=datetime.now(UTC),
                )
            )
    index.save(output_path)
    return metadata
