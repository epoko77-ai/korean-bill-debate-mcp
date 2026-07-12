import hashlib
import json
from pathlib import Path

from kasm.adapters.korea.parser import parse_transcript

FIXTURE = Path("tests/fixtures/parser/distinct_official_documents.json")


def test_twenty_distinct_official_documents_have_reviewed_parser_boundaries() -> None:
    payload = json.loads(FIXTURE.read_text("utf-8"))
    documents = payload["documents"]
    assert len(documents) == 20
    assert len({document["source_url"] for document in documents}) == 20
    assert all(
        document["source_url"].startswith("https://record.assembly.go.kr/assembly/viewer/minutes/")
        for document in documents
    )
    for document in documents:
        excerpt = document["excerpt"]
        assert hashlib.sha256(excerpt.encode()).hexdigest() == document["excerpt_sha256"]
        assert len(document["source_sha256"]) == 64
        result = parse_transcript(excerpt, locator_prefix=document["source_url"])
        assert result.speeches
        first = result.speeches[0]
        assert first.speaker_name == document["expected"]["speaker_name"]
        assert first.speaker_role == document["expected"]["speaker_role"]
        assert first.source_locator
        assert first.text.strip()
