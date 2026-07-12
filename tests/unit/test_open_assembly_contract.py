import hashlib
import json
from pathlib import Path

from kasm.adapters.korea.ingestion import meeting_from_open_assembly_row

FIXTURES = Path(__file__).parents[1] / "fixtures" / "open_assembly"


def _row(name: str) -> dict[str, object]:
    payload = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    dataset = next(iter(payload))
    return payload[dataset][1]["row"][0]


def test_recorded_contract_fixtures_are_sanitized_and_unchanged() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["credentials_removed"] is True
    for name, expected_hash in manifest["fixtures"].items():
        raw = (FIXTURES / name).read_bytes()
        assert b'"KEY"' not in raw
        assert hashlib.sha256(raw).hexdigest() == expected_hash


def test_verified_plenary_contract() -> None:
    meeting = meeting_from_open_assembly_row(_row("plenary"), source_hash="fixture")
    assert meeting.meeting_type == "plenary"
    assert meeting.assembly_term == 22
    assert meeting.source_url.endswith("id=52695")


def test_verified_committee_contract_detects_subcommittee_name() -> None:
    meeting = meeting_from_open_assembly_row(_row("committee"), source_hash="fixture")
    assert meeting.meeting_type == "subcommittee"
    assert meeting.committee_id == "9700408"


def test_verified_subcommittee_contract() -> None:
    meeting = meeting_from_open_assembly_row(_row("subcommittee"), source_hash="fixture")
    assert meeting.meeting_type == "subcommittee"
    assert meeting.assembly_term == 22
    assert meeting.date.isoformat() == "2026-05-12"
