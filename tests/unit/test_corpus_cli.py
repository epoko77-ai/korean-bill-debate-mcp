from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from kasm.corpus.build_pipeline import CorpusBuildCheckpoint, write_checkpoint
from kasm.corpus.corpus_cli import build_parser, main
from kasm.corpus.inventory import CorpusInventoryItem, CorpusInventoryManifest
from kasm.corpus.models import CorpusEvidenceKind
from kasm.research.contracts import EvidenceType
from kasm.research.documents import OfficialDocumentKind
from kasm.research.engine import DocumentWorkItem


def _inventory() -> CorpusInventoryManifest:
    url = (
        "https://record.assembly.go.kr/assembly/viewer/minutes/"
        "download/pdf.do?id=54338"
    )
    item = CorpusInventoryItem(
        22,
        CorpusEvidenceKind.MINUTES,
        "minutes:54338",
        DocumentWorkItem.create(
            OfficialDocumentKind.MINUTES,
            url,
            evidence_types=(EvidenceType.SPEECHES,),
        ),
        title="공식 회의록",
    )
    return CorpusInventoryManifest.create(
        inventory_as_of=datetime.now(UTC),
        assembly_terms=(22,),
        source_snapshot_hash="a" * 64,
        items=(item,),
        gaps=(),
        expected_counts={
            (22, CorpusEvidenceKind.BILL_ORIGINAL): 0,
            (22, CorpusEvidenceKind.REVIEW_REPORT): 0,
            (22, CorpusEvidenceKind.MINUTES): 1,
        },
    )


def test_operator_cli_exposes_inventory_build_publish_and_status() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    for command in ("inventory", "build", "publish", "run", "status"):
        assert command in help_text


def test_status_reports_incomplete_without_exposing_a_revision_id(
    tmp_path: Path,
    capsys,
) -> None:
    checkpoint = CorpusBuildCheckpoint.create(
        _inventory(),
        parser_version="pypdf-test-v1",
    )
    path = tmp_path / "checkpoint.json"
    write_checkpoint(path, checkpoint)

    result = main(("status", "--checkpoint", str(path)))

    assert result == 3
    output = capsys.readouterr().out
    assert '"complete":false' in output
    assert "revision_id" not in output
