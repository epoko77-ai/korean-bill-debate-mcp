from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol

import pytest

from kasm.corpus import (
    CorpusDocument,
    CorpusDocumentIdentity,
    CorpusEvidenceKind,
    CorpusIngestionFailure,
    CorpusObjectConflictError,
    CorpusObjectIntegrityError,
    CorpusObjectStore,
    CorpusRepository,
    CorpusRepositoryIntegrityError,
    CorpusRevisionManifest,
    FilesystemCorpusObjectStore,
    IncompleteCorpusRevisionError,
    LexicalMatchMode,
)
from kasm.corpus.models import revision_manifest_key, shard_id_for_term

NOW = datetime(2026, 7, 13, 15, 0, tzinfo=UTC)


class MemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_keys: list[str] = []
        self.get_keys: list[str] = []

    def put_immutable(self, key: str, content: bytes) -> None:
        existing = self.objects.get(key)
        if existing is not None and existing != content:
            raise CorpusObjectConflictError("conflict")
        if existing is None:
            self.objects[key] = content
            self.put_keys.append(key)

    def get(self, key: str) -> bytes | None:
        self.get_keys.append(key)
        return self.objects.get(key)


def _document(
    identifier: str,
    text: str,
    *,
    term: int = 22,
    kind: CorpusEvidenceKind = CorpusEvidenceKind.BILL_ORIGINAL,
    parser_version: str = "pypdf-6+kbd-1",
    observed_at: datetime = NOW,
) -> CorpusDocument:
    return CorpusDocument(
        identity=CorpusDocumentIdentity(term, kind, identifier),
        official_url=(
            "https://likms.assembly.go.kr/bill/billDetail.do?"
            f"billId={identifier}&ageFrom={term}&ageTo={term}"
        ),
        source_hash=hashlib.sha256(("source:" + identifier + text).encode()).hexdigest(),
        parser_version=parser_version,
        text=text,
        observed_at=observed_at,
        title=f"{identifier} 공식 문서",
        document_date=date(2026, 7, 1),
    )


def _publish_complete(
    repository: CorpusRepository,
    documents: tuple[CorpusDocument, ...],
    *,
    kind: CorpusEvidenceKind = CorpusEvidenceKind.BILL_ORIGINAL,
) -> CorpusRevisionManifest:
    kinds = tuple(CorpusEvidenceKind)
    builder = repository.begin_revision(
        assembly_terms=(22,),
        evidence_kinds=kinds,
    )
    builder.upsert_documents(documents)
    for selected_kind in kinds:
        builder.set_expected_count(
            22,
            selected_kind,
            len(documents) if selected_kind is kind else 0,
        )
    return builder.publish(created_at=NOW, inventory_as_of=NOW)


def test_manifest_covers_every_term_kind_and_records_official_provenance() -> None:
    repository = CorpusRepository(MemoryObjectStore())
    kinds = tuple(CorpusEvidenceKind)
    documents = tuple(
        _document(
            f"official-{term}-{kind.value}",
            f"{term}대 {kind.value} 인공지능 기본법 전체 본문",
            term=term,
            kind=kind,
        )
        for term in (21, 22)
        for kind in kinds
    )
    builder = repository.begin_revision(
        assembly_terms=(21, 22),
        evidence_kinds=kinds,
    )
    builder.upsert_documents(documents)
    for term in (21, 22):
        for kind in kinds:
            builder.set_expected_count(term, kind, 1)

    manifest = builder.publish(created_at=NOW, inventory_as_of=NOW)

    assert manifest.complete is True
    assert manifest.inventory_as_of == NOW
    assert len(manifest.coverage) == 6
    assert all(entry.complete for entry in manifest.coverage)
    assert manifest.lexical_index.document_count == 6
    assert manifest.lexical_index.document_set_hash
    for reference in manifest.documents:
        assert reference.identity.official_identifier
        assert reference.official_url.startswith("https://likms.assembly.go.kr/")
        assert len(reference.source_hash) == 64
        assert reference.parser_version == "pypdf-6+kbd-1"
        assert len(reference.text_hash) == 64
        assert len(reference.object_hash) == 64

    restored = repository.require_revision(manifest.revision_id)
    assert restored == manifest


@pytest.mark.parametrize("mode", ["unknown", "failed", "unaccounted"])
def test_completeness_fails_closed_for_every_coverage_gap(mode: str) -> None:
    repository = CorpusRepository(MemoryObjectStore())
    document = _document("bill-1", "플랫폼 노동자 보호")
    builder = repository.begin_revision(
        assembly_terms=(22,),
        evidence_kinds=(CorpusEvidenceKind.BILL_ORIGINAL,),
    )
    if mode != "failed":
        builder.upsert_document(document)
    if mode == "failed":
        builder.set_expected_count(22, CorpusEvidenceKind.BILL_ORIGINAL, 1)
        builder.record_failure(
            22,
            CorpusEvidenceKind.BILL_ORIGINAL,
            CorpusIngestionFailure(
                failure_key="bill-1",
                reason_code="download_failed",
                official_identifier="bill-1",
            ),
        )
    elif mode == "unaccounted":
        builder.set_expected_count(22, CorpusEvidenceKind.BILL_ORIGINAL, 2)
    manifest = builder.publish(created_at=NOW, inventory_as_of=NOW)

    coverage = manifest.coverage[0]
    assert manifest.complete is False
    if mode == "unknown":
        assert coverage.expected_count is None
        assert coverage.unaccounted_count is None
    elif mode == "failed":
        assert coverage.failed_count == 1
        assert coverage.to_dict()["failed_count"] == 1
    else:
        assert coverage.unaccounted_count == 1

    with pytest.raises(IncompleteCorpusRevisionError):
        repository.search_all(manifest.revision_id, "플랫폼 노동")
    # Inspection is explicit and cannot be confused with comprehensive use.
    repository.search_all(
        manifest.revision_id,
        "플랫폼 노동",
        require_complete=False,
    )


def test_revision_missing_an_official_full_text_axis_is_never_complete() -> None:
    repository = CorpusRepository(MemoryObjectStore())
    document = _document("bill-1", "인공지능 안전")
    builder = repository.begin_revision(
        assembly_terms=(22,),
        evidence_kinds=(CorpusEvidenceKind.BILL_ORIGINAL,),
    )
    builder.upsert_document(document)
    builder.set_expected_count(22, CorpusEvidenceKind.BILL_ORIGINAL, 1)

    manifest = builder.publish(created_at=NOW, inventory_as_of=NOW)

    assert manifest.coverage[0].complete is True
    assert manifest.complete is False
    with pytest.raises(IncompleteCorpusRevisionError):
        repository.search_all(manifest.revision_id, "인공지능")


def test_incremental_upsert_reuses_unaffected_shards_and_preserves_parent() -> None:
    store = MemoryObjectStore()
    repository = CorpusRepository(store)
    old_term = "기후위기"
    new_term = "재생에너지"
    stable_term = "보완수사권"
    assert len(
        {shard_id_for_term(old_term), shard_id_for_term(new_term), shard_id_for_term(stable_term)}
    ) == 3
    parent = _publish_complete(
        repository,
        (
            _document("bill-changing", old_term),
            _document("bill-stable", stable_term),
        ),
    )
    parent_stable_shard = {
        shard.shard_id: shard
        for shard in parent.lexical_index.shards
    }[shard_id_for_term(stable_term)]

    builder = repository.begin_revision(parent_revision_id=parent.revision_id)
    replacement = _document(
        "bill-changing",
        new_term,
        parser_version="pypdf-6+kbd-2",
        observed_at=NOW + timedelta(hours=1),
    )
    builder.upsert_document(replacement)
    child = builder.publish(
        created_at=NOW + timedelta(hours=1),
        inventory_as_of=NOW + timedelta(hours=1),
    )

    assert child.parent_revision_id == parent.revision_id
    assert child.complete is True
    child_stable_shard = {
        shard.shard_id: shard
        for shard in child.lexical_index.shards
    }[shard_id_for_term(stable_term)]
    assert child_stable_shard == parent_stable_shard
    assert [
        item.identity.official_identifier
        for item in repository.search_all(parent.revision_id, old_term)
    ] == ["bill-changing"]
    assert repository.search_all(child.revision_id, old_term) == ()
    assert [
        item.identity.official_identifier
        for item in repository.search_all(child.revision_id, new_term)
    ] == ["bill-changing"]
    assert repository.get_document(parent.documents[0]).parser_version in {
        "pypdf-6+kbd-1",
        "pypdf-6+kbd-2",
    }
    child_ref = next(
        item
        for item in child.documents
        if item.identity.official_identifier == "bill-changing"
    )
    assert repository.get_document(child_ref) == replacement


def test_search_returns_all_candidates_without_hidden_top_n_and_accounts_pages() -> None:
    repository = CorpusRepository(MemoryObjectStore())
    total = 1_205
    documents = tuple(
        _document(f"bill-{number:04d}", f"공통주제 세부내용 {number}")
        for number in range(total)
    )
    manifest = _publish_complete(repository, documents)

    all_candidates = repository.search_all(manifest.revision_id, "공통주제")

    assert len(all_candidates) == total
    seen: list[str] = []
    cursor: str | None = None
    previous_accounted = 0
    while True:
        page = repository.search_page(
            manifest.revision_id,
            "공통주제",
            page_size=137,
            cursor=cursor,
        )
        assert page.total_matching_candidates == total
        assert page.offset == previous_accounted
        seen.extend(item.identity_id for item in page.candidates)
        previous_accounted = page.accounted_count
        assert page.remaining_count == total - previous_accounted
        if page.exhausted:
            assert page.next_cursor is None
            break
        assert page.next_cursor is not None
        cursor = page.next_cursor

    assert previous_accounted == total
    assert len(seen) == len(set(seen)) == total
    assert seen == [item.identity_id for item in all_candidates]


def test_search_is_deterministic_supports_modes_and_scope_filters() -> None:
    repository = CorpusRepository(MemoryObjectStore())
    builder = repository.begin_revision(
        assembly_terms=(21, 22),
        evidence_kinds=(
            CorpusEvidenceKind.BILL_ORIGINAL,
            CorpusEvidenceKind.MINUTES,
            CorpusEvidenceKind.REVIEW_REPORT,
        ),
    )
    documents = (
        _document("z", "인공지능 안전", term=22),
        _document("a", "인공지능 안전 안전", term=21),
        _document(
            "m",
            "인공지능 산업",
            term=22,
            kind=CorpusEvidenceKind.MINUTES,
        ),
    )
    builder.upsert_documents(reversed(documents))
    for term in (21, 22):
        for kind in CorpusEvidenceKind:
            count = sum(
                document.identity.assembly_term == term
                and document.identity.evidence_kind is kind
                for document in documents
            )
            builder.set_expected_count(term, kind, count)
    manifest = builder.publish(created_at=NOW, inventory_as_of=NOW)

    any_matches = repository.search_all(manifest.revision_id, "인공지능 안전")
    all_matches = repository.search_all(
        manifest.revision_id,
        "인공지능 안전",
        match_mode=LexicalMatchMode.ALL,
    )

    assert [item.identity.official_identifier for item in any_matches] == ["a", "z", "m"]
    assert [item.identity.official_identifier for item in all_matches] == ["a", "z"]
    assert [
        item.identity.official_identifier
        for item in repository.search_all(
            manifest.revision_id,
            "인공지능",
            assembly_terms=(22,),
            evidence_kinds=(CorpusEvidenceKind.MINUTES,),
        )
    ] == ["m"]


def test_particle_normalization_matches_korean_inflection() -> None:
    repository = CorpusRepository(MemoryObjectStore())
    manifest = _publish_complete(
        repository,
        (_document("bill-1", "전문위원은 보완수사권을 검토하였다."),),
    )

    matches = repository.search_all(manifest.revision_id, "보완수사권에 대하여")

    assert [item.identity.official_identifier for item in matches] == ["bill-1"]
    assert "보완수사권" in matches[0].matched_terms


def test_cursor_is_bound_to_query_revision_filters_and_page_size() -> None:
    repository = CorpusRepository(MemoryObjectStore())
    manifest = _publish_complete(
        repository,
        tuple(_document(f"bill-{number}", "인공지능") for number in range(3)),
    )
    first = repository.search_page(
        manifest.revision_id,
        "인공지능",
        page_size=1,
    )
    assert first.next_cursor is not None

    with pytest.raises(ValueError, match="does not match"):
        repository.search_page(
            manifest.revision_id,
            "기후위기",
            page_size=1,
            cursor=first.next_cursor,
        )
    with pytest.raises(ValueError, match="does not match"):
        repository.search_page(
            manifest.revision_id,
            "인공지능",
            page_size=2,
            cursor=first.next_cursor,
        )
    tampered = first.next_cursor[:-1] + (
        "A" if first.next_cursor[-1] != "A" else "B"
    )
    with pytest.raises(ValueError, match="invalid corpus search cursor"):
        repository.search_page(
            manifest.revision_id,
            "인공지능",
            page_size=1,
            cursor=tampered,
        )


def test_document_and_manifest_tampering_are_detected() -> None:
    store = MemoryObjectStore()
    repository = CorpusRepository(store)
    manifest = _publish_complete(
        repository,
        (_document("bill-1", "장애인 이동권"),),
    )
    reference = manifest.documents[0]
    payload = json.loads(store.objects[reference.object_key])
    payload["text"] = "해양사고"
    store.objects[reference.object_key] = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    with pytest.raises(CorpusRepositoryIntegrityError, match="hash"):
        repository.get_document(reference)

    key = revision_manifest_key(manifest.revision_id)
    revision_payload = json.loads(store.objects[key])
    revision_payload["complete"] = False
    store.objects[key] = json.dumps(
        revision_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    with pytest.raises(CorpusRepositoryIntegrityError, match="manifest"):
        repository.get_revision(manifest.revision_id)


def test_official_url_rejects_credentials_but_official_text_is_lossless() -> None:
    secret_shaped_official_text = (
        "공식 보고서 예시 문자열 sk-ant-api03-" + "x" * 100 + " 끝"
    )
    document = _document("bill-1", secret_shaped_official_text)
    repository = CorpusRepository(MemoryObjectStore())
    reference = repository.put_document(document)

    assert repository.get_document(reference).text == secret_shaped_official_text
    with pytest.raises(ValueError, match="credential"):
        CorpusDocument(
            identity=document.identity,
            official_url="https://open.assembly.go.kr/openapi?KEY=real-secret",
            source_hash=document.source_hash,
            parser_version=document.parser_version,
            text=document.text,
            observed_at=NOW,
        )
    with pytest.raises(ValueError, match="National Assembly HTTPS"):
        CorpusDocument(
            identity=document.identity,
            official_url="https://example.com/not-official.pdf",
            source_hash=document.source_hash,
            parser_version=document.parser_version,
            text=document.text,
            observed_at=NOW,
        )


def test_success_and_failure_cannot_claim_the_same_official_identity() -> None:
    repository = CorpusRepository(MemoryObjectStore())
    builder = repository.begin_revision(
        assembly_terms=(22,),
        evidence_kinds=(CorpusEvidenceKind.REVIEW_REPORT,),
    )
    builder.upsert_document(
        _document(
            "review-1",
            "전문위원 검토보고서",
            kind=CorpusEvidenceKind.REVIEW_REPORT,
        )
    )
    builder.set_expected_count(22, CorpusEvidenceKind.REVIEW_REPORT, 2)
    builder.record_failure(
        22,
        CorpusEvidenceKind.REVIEW_REPORT,
        CorpusIngestionFailure(
            failure_key="review-1",
            reason_code="parse_failed",
            official_identifier="review-1",
        ),
    )

    with pytest.raises(ValueError, match="succeed and fail"):
        builder.publish(created_at=NOW, inventory_as_of=NOW)


def test_filesystem_store_is_private_atomic_idempotent_and_path_safe(
    tmp_path: Path,
) -> None:
    store: CorpusObjectStore = FilesystemCorpusObjectStore(tmp_path / "corpus")
    store.put_immutable("objects/example/value.bin", b"first")
    store.put_immutable("objects/example/value.bin", b"first")

    path = tmp_path / "corpus/objects/example/value.bin"
    assert store.get("objects/example/value.bin") == b"first"
    assert os.stat(path).st_mode & 0o777 == 0o600
    with pytest.raises(CorpusObjectConflictError):
        store.put_immutable("objects/example/value.bin", b"second")
    with pytest.raises(ValueError, match="key"):
        store.get("../outside")

    target = tmp_path / "outside"
    target.write_bytes(b"outside")
    link = tmp_path / "corpus/objects/example/link.bin"
    link.symlink_to(target)
    with pytest.raises(CorpusObjectIntegrityError, match="outside|symbolic"):
        store.get("objects/example/link.bin")


def test_object_store_protocol_does_not_require_listing_or_mutable_pointers() -> None:
    class MinimalBlobLike(Protocol):
        def put_immutable(self, key: str, content: bytes) -> None: ...

        def get(self, key: str) -> bytes | None: ...

    blob_like: MinimalBlobLike = MemoryObjectStore()
    repository = CorpusRepository(blob_like)

    manifest = _publish_complete(
        repository,
        (_document("bill-1", "감염병 대응"),),
    )

    assert repository.require_revision(manifest.revision_id) == manifest


def test_bulk_initial_revision_indexes_every_document_without_a_result_limit() -> None:
    repository = CorpusRepository(MemoryObjectStore())
    documents = tuple(
        _document(
            f"bill-bulk-{index:03d}",
            f"공통정책어 고유정책어{index:03d} 전체 원문",
        )
        for index in range(128)
    )

    manifest = _publish_complete(repository, documents)

    assert manifest.complete is True
    assert len(repository.search_all(manifest.revision_id, "공통정책어")) == 128
    unique = repository.search_all(manifest.revision_id, "고유정책어127")
    assert len(unique) == 1
    assert unique[0].identity.official_identifier == "bill-bulk-127"
