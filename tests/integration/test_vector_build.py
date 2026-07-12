from datetime import UTC, date, datetime

from kasm.core.models import Meeting, Speech
from kasm.indexing.build import build_vector_index
from kasm.indexing.embeddings import HashEmbeddingProvider
from kasm.indexing.vector import ExactVectorIndex, FaissVectorIndex
from kasm.storage.database import Database
from kasm.storage.repositories import MeetingRepository, SpeechRepository


def test_builds_versioned_index_and_embedding_records(tmp_path) -> None:
    with Database(":memory:") as database:
        meeting = Meeting(
            "kna:22:plenary:2025-01-23:1",
            22,
            None,
            None,
            None,
            "본회의",
            "plenary",
            "1",
            date(2025, 1, 23),
            "https://record.assembly.go.kr/1",
            "meeting-hash",
            datetime.now(UTC),
        )
        MeetingRepository(database).save(meeting)
        SpeechRepository(database).save(
            Speech(
                f"{meeting.id}:speech-0001",
                meeting.id,
                1,
                None,
                "김미래",
                "의원",
                None,
                "국산 인공지능 모델이 필요합니다.",
                None,
                None,
                None,
                "page:1",
                "speech-hash",
                "v1",
            )
        )
        provider = HashEmbeddingProvider(16)
        path = tmp_path / "vectors.json"
        metadata = build_vector_index(database, provider, path)
        assert metadata.corpus_hash
        assert ExactVectorIndex.load(path, expected=metadata).search(
            provider.embed_query("인공지능")
        )
        count = database.connection.execute("SELECT count(*) FROM embeddings").fetchone()[0]
        assert count == 1

        faiss_path = tmp_path / "vectors.faiss"
        faiss_metadata = build_vector_index(database, provider, faiss_path, backend="faiss")
        assert faiss_path.with_suffix(".faiss.json").is_file()
        assert FaissVectorIndex.load(faiss_path, expected=faiss_metadata).search(
            provider.embed_query("인공지능")
        )
