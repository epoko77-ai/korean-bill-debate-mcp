from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from kasm.corpus.build_pipeline import (
    CorpusBuildCheckpoint,
    CorpusBuildError,
    CorpusBuildRunner,
    publish_complete_revision,
    read_checkpoint,
    write_activation,
    write_checkpoint,
)
from kasm.corpus.inventory import CorpusInventoryItem, CorpusInventoryManifest
from kasm.corpus.models import CorpusEvidenceKind
from kasm.corpus.repository import CorpusRepository
from kasm.corpus.storage import FilesystemCorpusObjectStore
from kasm.research.contracts import EvidenceType
from kasm.research.document_worker import (
    DocumentWorkResult,
    OfficialDocumentWorker,
    TransientDocumentError,
)
from kasm.research.documents import (
    FilesystemOfficialDocumentStore,
    OfficialDocumentKind,
    ParsedOfficialDocument,
    TextSegment,
)
from kasm.research.engine import DocumentWorkItem

NOW = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)
BILL_URL = (
    "https://likms.assembly.go.kr/bill/bi/bill/detail/downloadDtlZip.do?"
    "billId=PRC_TEST_22&billNo=2212345&billKindCd=법률안&dwFileGbn=B"
)
REVIEW_URL = "https://likms.assembly.go.kr/filegate/review.pdf?id=review-2212345"
MINUTES_URL = (
    "https://record.assembly.go.kr/assembly/viewer/minutes/download/pdf.do?id=54338"
)


def _inventory() -> CorpusInventoryManifest:
    rows = (
        _inventory_item(
            CorpusEvidenceKind.BILL_ORIGINAL,
            "bill:2212345:original",
            OfficialDocumentKind.BILL_TEXT,
            BILL_URL,
            (EvidenceType.BILL_TEXT,),
            ("2212345",),
        ),
        _inventory_item(
            CorpusEvidenceKind.REVIEW_REPORT,
            "review:url-sha256:" + hashlib.sha256(REVIEW_URL.encode()).hexdigest(),
            OfficialDocumentKind.REVIEW_REPORT,
            REVIEW_URL,
            (EvidenceType.REVIEW_REPORTS,),
            ("2212345",),
        ),
        _inventory_item(
            CorpusEvidenceKind.MINUTES,
            "minutes:54338",
            OfficialDocumentKind.MINUTES,
            MINUTES_URL,
            (EvidenceType.SPEECHES, EvidenceType.SPEECH_CONTEXT),
            ("2212345",),
        ),
    )
    return CorpusInventoryManifest.create(
        inventory_as_of=NOW,
        assembly_terms=(22,),
        source_snapshot_hash="d" * 64,
        items=rows,
        gaps=(),
        expected_counts={(22, kind): 1 for kind in CorpusEvidenceKind},
    )


def _inventory_item(
    evidence_kind: CorpusEvidenceKind,
    identifier: str,
    document_kind: OfficialDocumentKind,
    url: str,
    evidence_types: tuple[EvidenceType, ...],
    bills: tuple[str, ...],
) -> CorpusInventoryItem:
    return CorpusInventoryItem(
        22,
        evidence_kind,
        identifier,
        DocumentWorkItem.create(
            document_kind,
            url,
            evidence_types=evidence_types,
            related_bill_numbers=bills,
        ),
        title=f"{evidence_kind.value} 공식 원문",
        document_date=date(2026, 7, 1),
        committee="법제사법위원회",
    )


class FakeWorker:
    def __init__(self, *, fail_once_url: str | None = None, text_suffix: str = "v1") -> None:
        self.fail_once_url = fail_once_url
        self.failed = False
        self.text_suffix = text_suffix
        self.calls: list[str] = []

    def process(
        self,
        kind: OfficialDocumentKind,
        official_url: str,
        *,
        refresh: bool,
    ) -> DocumentWorkResult:
        del refresh
        self.calls.append(official_url)
        if official_url == self.fail_once_url and not self.failed:
            self.failed = True
            raise TransientDocumentError("temporary", code="network_error")
        source_hash = hashlib.sha256((official_url + self.text_suffix).encode()).hexdigest()
        document = ParsedOfficialDocument(
            kind,
            official_url,
            source_hash,
            "pypdf-test-v1",
            NOW + timedelta(seconds=10),
            (
                TextSegment("p.1", f"인공지능 안전과 보완수사권 {self.text_suffix}"),
                TextSegment("p.2", "전문위원 검토와 정부 답변 전체"),
            ),
        )
        return DocumentWorkResult(
            kind,
            official_url,
            document.parser_version,
            100,
            2,
            len(document.full_text),
            source_hash,
            document.text_hash,
            False,
            f"official/raw/{source_hash}",
            document.object_key,
            document,
        )


class Response:
    def __init__(self, content: bytes, *, final_url: str | None = None) -> None:
        self.content = content
        self.position = 0
        self.status = 200
        self.headers: dict[str, str] = {}
        self.final_url = final_url

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if self.position >= len(self.content):
            return b""
        end = len(self.content) if size < 0 else self.position + size
        value = self.content[self.position : end]
        self.position += len(value)
        return value

    def geturl(self) -> str | None:
        return self.final_url


def _bill_archive() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("2212345_인공지능안전법_의안원문.pdf", b"%PDF-bill fixture")
    return output.getvalue()


def _runner(
    repository: CorpusRepository,
    worker: FakeWorker,
    checkpoint_path: Path,
) -> CorpusBuildRunner:
    return CorpusBuildRunner(
        repository,
        worker,  # type: ignore[arg-type]
        checkpoint_writer=lambda value: write_checkpoint(checkpoint_path, value),
        clock=lambda: NOW + timedelta(minutes=10),
    )


def test_real_document_worker_fixture_parses_all_axes_then_searches(
    tmp_path: Path,
) -> None:
    identity_html = (
        b'<input id="billId" value="PRC_TEST_22">'
        b'<input id="billNo" value="2212345">'
    )
    archive = _bill_archive()

    def opener(request, *, timeout: float) -> Response:
        del timeout
        url = request.full_url
        if "billDetail.do" in url:
            return Response(identity_html)
        if "downloadDtlZip.do" in url and request.get_method() == "POST":
            return Response(archive)
        if url == REVIEW_URL:
            return Response(b"%PDF-review fixture")
        if url == MINUTES_URL:
            return Response(b"%PDF-minutes fixture")
        raise AssertionError("unexpected fixture URL")

    def extract_pages(raw: bytes) -> tuple[str, ...]:
        if b"bill fixture" in raw:
            return ("인공지능 안전 의안원문 전체",)
        if b"review fixture" in raw:
            return ("전문위원은 위험 분류를 검토했다", "정부는 시행령으로 답변했다")
        return ("법안심사소위원회에서 인공지능 안전을 논의했다",)

    repository = CorpusRepository(FilesystemCorpusObjectStore(tmp_path / "corpus"))
    checkpoint_path = tmp_path / "fixture-checkpoint.json"
    checkpoint = CorpusBuildCheckpoint.create(
        _inventory(),
        parser_version="pypdf-test-v1",
        created_at=NOW,
    )
    worker = OfficialDocumentWorker(
        FilesystemOfficialDocumentStore(tmp_path / "official-documents"),
        parser_version="pypdf-test-v1",
        opener=opener,
        page_extractor=extract_pages,
        clock=lambda: NOW + timedelta(seconds=5),
    )
    complete = CorpusBuildRunner(
        repository,
        worker,
        checkpoint_writer=lambda value: write_checkpoint(checkpoint_path, value),
        clock=lambda: NOW + timedelta(seconds=10),
    ).run(checkpoint, attempts_per_item=1)
    revision = publish_complete_revision(
        repository,
        complete,
        created_at=NOW + timedelta(minutes=1),
    )

    assert complete.complete is True
    assert revision.complete is True
    assert len(repository.search_all(revision.revision_id, "인공지능 안전")) == 2
    assert len(repository.search_all(revision.revision_id, "전문위원 위험 분류")) == 1
    assert len(tuple((tmp_path / "official-documents" / "official" / "raw").glob("*"))) >= 3


def test_inventory_parse_resume_publish_search_and_complete_only_activation(
    tmp_path: Path,
) -> None:
    repository = CorpusRepository(FilesystemCorpusObjectStore(tmp_path / "corpus"))
    checkpoint_path = tmp_path / "state" / "checkpoint.json"
    checkpoint = CorpusBuildCheckpoint.create(
        _inventory(),
        parser_version="pypdf-test-v1",
        created_at=NOW,
    )
    write_checkpoint(checkpoint_path, checkpoint)

    first_worker = FakeWorker(fail_once_url=REVIEW_URL)
    partial = _runner(repository, first_worker, checkpoint_path).run(
        checkpoint,
        attempts_per_item=1,
    )
    assert partial.complete is False
    assert partial.summary()["documents_succeeded"] == 2
    assert partial.summary()["documents_retryable_failed"] == 1
    assert partial.summary()["documents_unattempted"] == 0
    with pytest.raises(CorpusBuildError, match="publication was refused"):
        publish_complete_revision(repository, partial, created_at=NOW + timedelta(minutes=1))
    assert not tuple((tmp_path / "corpus" / "revisions").glob("*/manifest.json"))

    restored = read_checkpoint(checkpoint_path)
    resumed_worker = FakeWorker()
    complete = _runner(repository, resumed_worker, checkpoint_path).run(
        restored,
        attempts_per_item=1,
    )
    assert resumed_worker.calls == [REVIEW_URL]
    assert complete.complete is True
    assert all(entry.complete for entry in complete.accounting)

    revision = publish_complete_revision(
        repository,
        complete,
        created_at=NOW + timedelta(minutes=1),
    )
    assert revision.complete is True
    assert len(revision.documents) == 3
    assert len(repository.search_all(revision.revision_id, "인공지능 안전")) == 3
    activation = tmp_path / "activation.json"
    write_activation(activation, revision, inventory_id=complete.inventory.inventory_id)
    payload = json.loads(activation.read_text(encoding="utf-8"))
    assert payload == {
        "complete": True,
        "environment_variable": "KBD_RESEARCH_CORPUS_REVISION",
        "inventory_as_of": NOW.isoformat(),
        "inventory_id": complete.inventory.inventory_id,
        "revision_id": revision.revision_id,
        "schema_version": 1,
    }


def test_complete_incremental_parent_revision_replaces_changed_full_text(
    tmp_path: Path,
) -> None:
    repository = CorpusRepository(FilesystemCorpusObjectStore(tmp_path / "corpus"))
    parent_path = tmp_path / "parent.json"
    parent_state = CorpusBuildCheckpoint.create(
        _inventory(),
        parser_version="pypdf-test-v1",
        created_at=NOW,
    )
    parent_state = _runner(repository, FakeWorker(text_suffix="v1"), parent_path).run(
        parent_state,
        attempts_per_item=1,
    )
    parent = publish_complete_revision(
        repository,
        parent_state,
        created_at=NOW + timedelta(minutes=1),
    )

    child_path = tmp_path / "child.json"
    child_state = CorpusBuildCheckpoint.create(
        _inventory(),
        parser_version="pypdf-test-v1",
        parent_revision_id=parent.revision_id,
        created_at=NOW + timedelta(minutes=2),
    )
    child_state = _runner(repository, FakeWorker(text_suffix="v2"), child_path).run(
        child_state,
        attempts_per_item=1,
    )
    child = publish_complete_revision(
        repository,
        child_state,
        created_at=NOW + timedelta(minutes=3),
    )

    assert child.complete is True
    assert child.parent_revision_id == parent.revision_id
    assert repository.search_all(parent.revision_id, "v1")
    assert repository.search_all(parent.revision_id, "v2") == ()
    assert repository.search_all(child.revision_id, "v2")
    assert repository.search_all(child.revision_id, "v1") == ()


def test_activation_refuses_an_incomplete_repository_revision(tmp_path: Path) -> None:
    repository = CorpusRepository(FilesystemCorpusObjectStore(tmp_path / "corpus"))
    builder = repository.begin_revision(
        assembly_terms=(22,),
        evidence_kinds=(CorpusEvidenceKind.MINUTES,),
    )
    builder.set_expected_count(22, CorpusEvidenceKind.MINUTES, 0)
    incomplete = builder.publish(inventory_as_of=NOW, created_at=NOW)
    assert incomplete.complete is False

    activation = tmp_path / "must-not-exist.json"
    with pytest.raises(CorpusBuildError, match="cannot be activated"):
        write_activation(activation, incomplete, inventory_id="a" * 64)
    assert not activation.exists()
