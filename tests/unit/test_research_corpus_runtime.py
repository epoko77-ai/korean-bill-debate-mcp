from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta

from kasm.corpus import (
    CorpusDocument,
    CorpusDocumentIdentity,
    CorpusEvidenceKind,
    CorpusObjectConflictError,
    CorpusRepository,
    CorpusRevisionManifest,
)
from kasm.research.contracts import EvidenceType
from kasm.research.corpus_bridge import ExactCorpusWorkDescriptor
from kasm.research.corpus_runtime import RevisionCorpusRecallProvider
from kasm.research.documents import OfficialDocumentKind
from kasm.research.engine import CorpusRecallStatus, DocumentWorkItem
from kasm.research.planner import ResearchContractPlanner, ResearchPlan
from kasm.research.relevance import RelevanceCriteria

NOW = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)
BILL_NUMBER = "2219564"
BILL_URL = (
    "https://likms.assembly.go.kr/bill/bi/bill/detail/downloadDtlZip.do?"
    "billNo=2219564"
)
MINUTES_URL = (
    "https://record.assembly.go.kr/assembly/viewer/minutes/download/pdf.do?id=54338"
)
REVIEW_URL = "https://likms.assembly.go.kr/filegate/review.pdf?id=2219564"


class MemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_immutable(self, key: str, content: bytes) -> None:
        existing = self.objects.get(key)
        if existing is not None and existing != content:
            raise CorpusObjectConflictError("conflict")
        self.objects.setdefault(key, content)

    def get(self, key: str) -> bytes | None:
        return self.objects.get(key)


def _evidence_kind(kind: OfficialDocumentKind) -> CorpusEvidenceKind:
    return {
        OfficialDocumentKind.BILL_TEXT: CorpusEvidenceKind.BILL_ORIGINAL,
        OfficialDocumentKind.REVIEW_REPORT: CorpusEvidenceKind.REVIEW_REPORT,
        OfficialDocumentKind.MINUTES: CorpusEvidenceKind.MINUTES,
    }[kind]


def _work(
    kind: OfficialDocumentKind,
    url: str,
    *,
    bills: tuple[str, ...] = (BILL_NUMBER,),
) -> DocumentWorkItem:
    evidence_types = {
        OfficialDocumentKind.BILL_TEXT: (EvidenceType.BILL_TEXT,),
        OfficialDocumentKind.REVIEW_REPORT: (EvidenceType.REVIEW_REPORTS,),
        OfficialDocumentKind.MINUTES: (
            EvidenceType.SUBCOMMITTEE_MINUTES,
            EvidenceType.SPEECHES,
        ),
    }[kind]
    return DocumentWorkItem.create(
        kind,
        url,
        evidence_types=evidence_types,
        related_bill_numbers=bills,
    )


def _descriptor(
    kind: OfficialDocumentKind,
    url: str,
    identifier: str,
    *,
    bills: tuple[str, ...] = (BILL_NUMBER,),
    document_date: date = date(2026, 7, 1),
    committee: str = "법제사법위원회",
) -> ExactCorpusWorkDescriptor:
    return ExactCorpusWorkDescriptor(
        work_item=_work(kind, url, bills=bills),
        assembly_term=22,
        official_identifier=identifier,
        title="공식 문서",
        document_date=document_date,
        committee=committee,
    )


def _document(
    descriptor: ExactCorpusWorkDescriptor,
    text: str,
) -> CorpusDocument:
    item = descriptor.work_item
    return CorpusDocument(
        identity=CorpusDocumentIdentity(
            descriptor.assembly_term or 0,
            _evidence_kind(item.kind),
            descriptor.official_identifier or "",
        ),
        official_url=item.official_url,
        source_hash=hashlib.sha256((item.official_url + text).encode()).hexdigest(),
        parser_version="pypdf-layout-v1",
        text=text,
        observed_at=NOW,
        title=descriptor.title,
        document_date=descriptor.document_date,
        related_bill_numbers=item.related_bill_numbers,
        committee=descriptor.committee,
    )


def _corpus(
    *,
    complete: bool = True,
) -> tuple[
    CorpusRepository,
    CorpusRevisionManifest,
    tuple[ExactCorpusWorkDescriptor, ...],
]:
    descriptors = (
        _descriptor(
            OfficialDocumentKind.BILL_TEXT,
            BILL_URL,
            "bill:2219564:original",
        ),
        _descriptor(
            OfficialDocumentKind.MINUTES,
            MINUTES_URL,
            "minutes:54338",
        ),
        _descriptor(
            OfficialDocumentKind.REVIEW_REPORT,
            REVIEW_URL,
            "review:2219564:1",
        ),
    )
    documents = (
        _document(descriptors[0], "인공지능 기본법과 AI 안전 의무를 규정한다."),
        _document(descriptors[1], "위원은 AI 안전 의무를 질의하였다."),
        _document(descriptors[2], "형사소송법의 일반 절차를 검토한다."),
    )
    repository = CorpusRepository(MemoryObjectStore())
    builder = repository.begin_revision(
        assembly_terms=(22,),
        evidence_kinds=tuple(CorpusEvidenceKind),
    )
    builder.upsert_documents(documents)
    for kind in CorpusEvidenceKind:
        count = sum(
            document.identity.evidence_kind is kind for document in documents
        )
        builder.set_expected_count(22, kind, count + (1 if not complete else 0))
    manifest = builder.publish(created_at=NOW, inventory_as_of=NOW)
    return repository, manifest, descriptors


def _provider(
    repository: CorpusRepository,
    manifest: CorpusRevisionManifest,
) -> RevisionCorpusRecallProvider:
    return RevisionCorpusRecallProvider(
        repository,
        revision_id=manifest.revision_id,
    )


def _criteria(plan: ResearchPlan) -> RelevanceCriteria:
    return RelevanceCriteria.from_query(
        plan.contract.query,
        bill_numbers=plan.contract.bill_numbers,
        committees=plan.contract.committees,
    )


def test_complete_revision_maps_every_exact_hit_without_related_topic_noise() -> None:
    repository, manifest, descriptors = _corpus()
    provider = _provider(repository, manifest)
    plan = ResearchContractPlanner().plan(
        "최근 AI 입법을 회의록과 검토보고서까지 조사해줘",
        as_of=NOW,
    )

    state = provider.recall(plan, _criteria(plan))

    assert state.status is CorpusRecallStatus.VERIFIED
    assert state.revision_id == manifest.revision_id
    assert state.candidate_count == state.mapped_count == 2
    assert state.exact_bill_numbers == (BILL_NUMBER,)
    assert state.exact_meeting_urls == (MINUTES_URL,)
    assert state.required_work_ids == tuple(
        sorted((descriptors[0].work_item.work_id, descriptors[1].work_item.work_id))
    )
    # The review report contains only the registry's related statute.  It must
    # not become an exact AI hit and force an unrelated candidate into scope.
    assert descriptors[2].work_item.work_id not in state.required_work_ids


def test_multiword_surface_requires_all_tokens_after_one_pass_any_search() -> None:
    repository, manifest, _descriptors = _corpus()
    unrelated = _descriptor(
        OfficialDocumentKind.REVIEW_REPORT,
        "https://likms.assembly.go.kr/filegate/review.pdf?id=generic-basic-act",
        "review:generic-basic-act",
        bills=("2219999",),
    )
    builder = repository.begin_revision(parent_revision_id=manifest.revision_id)
    builder.upsert_document(_document(unrelated, "다른 분야의 기본법 검토"))
    builder.set_expected_count(22, CorpusEvidenceKind.REVIEW_REPORT, 2)
    revised = builder.publish(created_at=NOW, inventory_as_of=NOW)
    provider = _provider(repository, revised)
    plan = ResearchContractPlanner().plan("인공지능 기본법", as_of=NOW)

    state = provider.recall(plan, _criteria(plan))

    assert state.status is CorpusRecallStatus.VERIFIED
    assert state.exact_bill_numbers == (BILL_NUMBER,)
    assert state.candidate_count == 1
    assert unrelated.work_item.work_id not in state.required_work_ids


def test_related_terms_use_the_same_threshold_as_metadata_resolution() -> None:
    repository, manifest, _descriptors = _corpus()
    related = _descriptor(
        OfficialDocumentKind.REVIEW_REPORT,
        "https://likms.assembly.go.kr/filegate/review.pdf?id=related-combination",
        "review:related-combination",
        bills=("2219999",),
    )
    builder = repository.begin_revision(parent_revision_id=manifest.revision_id)
    builder.upsert_document(
        _document(related, "형사소송법과 보완수사요구권을 함께 검토한다.")
    )
    builder.set_expected_count(22, CorpusEvidenceKind.REVIEW_REPORT, 2)
    revised = builder.publish(created_at=NOW, inventory_as_of=NOW)
    provider = _provider(repository, revised)
    plan = ResearchContractPlanner().plan("보완수사권", as_of=NOW)

    state = provider.recall(plan, _criteria(plan))

    assert state.status is CorpusRecallStatus.VERIFIED
    assert state.candidate_count == 1
    assert state.exact_bill_numbers == ("2219999",)
    assert state.required_work_ids == (related.work_item.work_id,)


def test_committee_scope_words_never_become_topic_terms() -> None:
    repository, manifest, _descriptors = _corpus()
    scope_only = _descriptor(
        OfficialDocumentKind.REVIEW_REPORT,
        "https://likms.assembly.go.kr/filegate/review.pdf?id=committee-only",
        "review:committee-only",
        bills=("2219998",),
    )
    builder = repository.begin_revision(parent_revision_id=manifest.revision_id)
    builder.upsert_document(_document(scope_only, "법제사법위원회 검토 자료"))
    builder.set_expected_count(22, CorpusEvidenceKind.REVIEW_REPORT, 2)
    revised = builder.publish(created_at=NOW, inventory_as_of=NOW)
    provider = _provider(repository, revised)
    plan = ResearchContractPlanner().plan(
        "법제사법위원회 최근 AI 입법",
        as_of=NOW,
    )

    state = provider.recall(plan, _criteria(plan))

    assert state.status is CorpusRecallStatus.VERIFIED
    assert state.candidate_count == 2
    assert "2219998" not in state.exact_bill_numbers


def test_out_of_range_minutes_are_excluded_without_invalidating_recall() -> None:
    repository, manifest, _descriptors = _corpus()
    old_minutes = _descriptor(
        OfficialDocumentKind.MINUTES,
        "https://record.assembly.go.kr/minutes/old-ai.pdf",
        "minutes:old-ai",
        bills=("2219997",),
        document_date=date(2025, 1, 1),
    )
    builder = repository.begin_revision(parent_revision_id=manifest.revision_id)
    builder.upsert_document(_document(old_minutes, "AI 안전 의무를 논의하였다."))
    builder.set_expected_count(22, CorpusEvidenceKind.MINUTES, 2)
    revised = builder.publish(created_at=NOW, inventory_as_of=NOW)
    provider = _provider(repository, revised)
    plan = ResearchContractPlanner().plan(
        "2026-07-01부터 2026-07-14까지 AI 입법",
        as_of=NOW,
    )

    state = provider.recall(plan, _criteria(plan))

    assert state.status is CorpusRecallStatus.VERIFIED
    assert state.candidate_count == 2
    assert state.exact_meeting_urls == (MINUTES_URL,)
    assert "2219997" not in state.exact_bill_numbers


def test_missing_scope_metadata_fails_closed_for_a_topical_hit() -> None:
    repository, manifest, _descriptors = _corpus()
    missing_committee = _descriptor(
        OfficialDocumentKind.REVIEW_REPORT,
        "https://likms.assembly.go.kr/filegate/review.pdf?id=no-committee",
        "review:no-committee",
        bills=("2219996",),
        committee="",
    )
    builder = repository.begin_revision(parent_revision_id=manifest.revision_id)
    builder.upsert_document(_document(missing_committee, "AI 안전 의무 검토"))
    builder.set_expected_count(22, CorpusEvidenceKind.REVIEW_REPORT, 2)
    revised = builder.publish(created_at=NOW, inventory_as_of=NOW)
    provider = _provider(repository, revised)
    plan = ResearchContractPlanner().plan(
        "법제사법위원회 AI 입법",
        as_of=NOW,
    )

    state = provider.recall(plan, _criteria(plan))

    assert state.status is CorpusRecallStatus.INCOMPLETE
    assert state.gap_reasons == ("corpus_candidate_committee_missing:1",)
    assert state.exact_bill_numbers == ()


def test_bill_original_identifier_cannot_be_rebound_to_another_bill() -> None:
    descriptor = _descriptor(
        OfficialDocumentKind.BILL_TEXT,
        BILL_URL,
        "bill:2219564:original",
        bills=("2219995",),
    )
    repository = CorpusRepository(MemoryObjectStore())
    builder = repository.begin_revision(
        assembly_terms=(22,),
        evidence_kinds=tuple(CorpusEvidenceKind),
    )
    builder.upsert_document(_document(descriptor, "AI 안전 의무"))
    for kind in CorpusEvidenceKind:
        builder.set_expected_count(
            22,
            kind,
            1 if kind is CorpusEvidenceKind.BILL_ORIGINAL else 0,
        )
    manifest = builder.publish(created_at=NOW, inventory_as_of=NOW)
    provider = _provider(repository, manifest)
    plan = ResearchContractPlanner().plan("AI 입법", as_of=NOW)

    state = provider.recall(plan, _criteria(plan))

    assert state.status is CorpusRecallStatus.INCOMPLETE
    assert "corpus_candidate_bill_identifier_mismatch:1" in state.gap_reasons
    assert "corpus_candidate_candidate_unmapped:1" in state.gap_reasons
    assert state.exact_bill_numbers == ()


def test_incomplete_revision_never_returns_widening_identities() -> None:
    repository, manifest, _descriptors = _corpus(complete=False)
    plan = ResearchContractPlanner().plan("AI 입법", as_of=NOW)
    state = _provider(repository, manifest).recall(
        plan,
        _criteria(plan),
    )

    assert manifest.complete is False
    assert state.status is CorpusRecallStatus.INCOMPLETE
    assert state.gap_reasons == ("corpus_revision_incomplete",)
    assert state.exact_bill_numbers == ()
    assert state.exact_meeting_urls == ()


def test_revision_inventory_cutoff_must_cover_request_observation_time() -> None:
    repository, manifest, _descriptors = _corpus()
    plan = ResearchContractPlanner().plan("AI 입법", as_of=NOW + timedelta(seconds=1))
    state = _provider(repository, manifest).recall(
        plan,
        _criteria(plan),
    )

    assert state.status is CorpusRecallStatus.INCOMPLETE
    assert state.gap_reasons == ("corpus_revision_stale",)
    assert state.exact_bill_numbers == ()


def test_recall_binding_changes_with_revision_and_algorithm() -> None:
    repository, manifest, _descriptors = _corpus()
    provider = _provider(repository, manifest)

    assert provider.binding_id != provider.revision_id
    assert len(provider.binding_id) == 64
