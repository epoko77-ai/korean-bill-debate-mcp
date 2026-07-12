import json
from pathlib import Path

import pytest

from kasm.adapters.korea.parser import parse_transcript
from kasm.adapters.korea.validation import GoldenBoundary, evaluate_parse

FIXTURES = Path(__file__).parents[1] / "fixtures" / "parser"


@pytest.mark.parametrize(
    "fixture_name", ["verified_excerpt", "plenary_excerpt", "subcommittee_excerpt"]
)
def test_reviewed_excerpt_meets_parser_quality_gate(fixture_name: str) -> None:
    source = (FIXTURES / f"{fixture_name}.txt").read_text(encoding="utf-8")
    raw_golden = json.loads((FIXTURES / f"{fixture_name}.golden.json").read_text(encoding="utf-8"))
    expected = [GoldenBoundary(**item) for item in raw_golden]
    report = evaluate_parse(parse_transcript(source, locator_prefix="fixture"), expected)
    assert report.f1 >= 0.95
    assert report.duplicate_sequences == 0
    assert report.empty_speeches == 0
    assert report.provenance_complete
    assert report.parse_failures == 0
