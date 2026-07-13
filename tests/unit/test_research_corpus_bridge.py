from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from kasm.corpus import (
    CorpusDocumentIdentity,
    CorpusEvidenceKind,
    CorpusSearchCandidate,
)
from kasm.research.contracts import EvidenceType
from kasm.research.corpus_bridge import (
    CorpusBridgeGapCode,
    ExactCorpusWorkDescriptor,
    corpus_document_from_parsed,
    corpus_failure_from_outcome,
    map_candidates_to_work,
)
from kasm.research.document_worker import DocumentWorkResult
from kasm.research.documents import (
    OfficialDocumentKind,
    ParsedOfficialDocument,
    TextSegment,
)
from kasm.research.engine import (
    DocumentOutcome,
    DocumentOutcomeStatus,
    DocumentWorkItem,
)

NOW = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)
MINUTES_URL = (
    "https://record.assembly.go.kr/assembly/viewer/minutes/download/pdf.do?id=54338"
)
REVIEW_URL = "https://likms.assembly.go.kr/filegate/review.pdf?id=review-2219564"
BILL_TEXT_URL = (
    "https://likms.assembly.go.kr/bill/bi/bill/detail/downloadDtlZip.do?"
    "billId=PRC_K2R6V0N6&billNo=2219564&billKindCd=법률안&dwFileGbn=B"
)


def _item(
    kind: OfficialDocumentKind,
    url: str,
    *,
    bills: tuple[str, ...] = (),
) -> DocumentWorkItem:
    evidence = {
        OfficialDocumentKind.MINUTES: (EvidenceType.SPEECHES,),
        OfficialDocumentKind.REVIEW_REPORT: (EvidenceType.REVIEW_REPORTS,),
        OfficialDocumentKind.BILL_TEXT: (EvidenceType.BILL_TEXT,),
    }[kind]
    return DocumentWorkItem.create(
        kind,
        url,
        evidence_types=evidence,
        related_bill_numbers=bills,
    )


def _descriptor(
    kind: OfficialDocumentKind,
    url: str,
    official_identifier: str | None,
    *,
    term: int | None = 22,
    bills: tuple[str, ...] | None = None,
    title: str = "공식 문서",
) -> ExactCorpusWorkDescriptor:
    related = (
        bills
        if bills is not None
        else (() if kind is OfficialDocumentKind.MINUTES else ("2219564",))
    )
    return ExactCorpusWorkDescriptor(
        work_item=_item(kind, url, bills=related),
        assembly_term=term,
        official_identifier=official_identifier,
        title=title,
        document_date=date(2026, 7, 1),
    )


def _parsed(
    kind: OfficialDocumentKind,
    url: str,
    *,
    text: str = "공식 원문 전체",
) -> ParsedOfficialDocument:
    return ParsedOfficialDocument(
        kind=kind,
        official_url=url,
        source_hash={
            OfficialDocumentKind.MINUTES: "a" * 64,
            OfficialDocumentKind.REVIEW_REPORT: "b" * 64,
            OfficialDocumentKind.BILL_TEXT: "c" * 64,
        }[kind],
        parser_version="pypdf-6+kbd-2",
        parsed_at=NOW,
        segments=(
            TextSegment("p.1", text + " 1쪽"),
            TextSegment("p.2", text + " 2쪽"),
        ),
    )


def _work_result(document: ParsedOfficialDocument) -> DocumentWorkResult:
    return DocumentWorkResult(
        kind=document.kind,
        official_url=document.official_url,
        parser_version=document.parser_version,
        byte_count=max(1, len(document.full_text.encode())),
        page_count=len(document.segments),
        character_count=len(document.full_text),
        source_hash=document.source_hash,
        text_hash=document.text_hash,
        cache_hit=False,
        raw_object_key=f"official/raw/{document.source_hash}",
        parsed_object_key=document.object_key,
        document=document,
    )


@pytest.mark.parametrize(
    ("kind", "url", "identifier", "expected_kind"),
    [
        (
            OfficialDocumentKind.MINUTES,
            MINUTES_URL,
            "minutes:54338",
            CorpusEvidenceKind.MINUTES,
        ),
        (
            OfficialDocumentKind.REVIEW_REPORT,
            REVIEW_URL,
            "review:2219564:1",
            CorpusEvidenceKind.REVIEW_REPORT,
        ),
        (
            OfficialDocumentKind.BILL_TEXT,
            BILL_TEXT_URL,
            "bill:2219564:original",
            CorpusEvidenceKind.BILL_ORIGINAL,
        ),
    ],
)
def test_successful_parsed_documents_map_all_official_kinds_exactly(
    kind: OfficialDocumentKind,
    url: str,
    identifier: str,
    expected_kind: CorpusEvidenceKind,
) -> None:
    descriptor = _descriptor(kind, url, identifier)
    parsed = _parsed(kind, url)

    result = corpus_document_from_parsed(descriptor, parsed)

    assert result.succeeded is True
    assert result.gap is None
    assert result.document is not None
    assert result.document.identity == CorpusDocumentIdentity(
        22,
        expected_kind,
        identifier,
    )
    assert result.document.official_url == url
    assert result.document.source_hash == parsed.source_hash
    assert result.document.parser_version == parsed.parser_version
    assert result.document.text == parsed.full_text
    assert result.document.text == "공식 원문 전체 1쪽\n\n공식 원문 전체 2쪽"
    assert result.document.observed_at == parsed.parsed_at


@pytest.mark.parametrize(
    ("term", "identifier", "expected_code"),
    [
        (None, "minutes:54338", CorpusBridgeGapCode.ASSEMBLY_TERM_MISSING),
        (0, "minutes:54338", CorpusBridgeGapCode.ASSEMBLY_TERM_INVALID),
        (None, None, CorpusBridgeGapCode.ASSEMBLY_TERM_MISSING),
        (22, None, CorpusBridgeGapCode.OFFICIAL_IDENTIFIER_MISSING),
        (22, " ", CorpusBridgeGapCode.OFFICIAL_IDENTIFIER_INVALID),
    ],
)
def test_missing_or_invalid_exact_identity_returns_typed_gap(
    term: int | None,
    identifier: str | None,
    expected_code: CorpusBridgeGapCode,
) -> None:
    descriptor = _descriptor(
        OfficialDocumentKind.MINUTES,
        MINUTES_URL,
        identifier,
        term=term,
    )

    result = corpus_document_from_parsed(
        descriptor,
        _parsed(OfficialDocumentKind.MINUTES, MINUTES_URL),
    )

    assert result.document is None
    assert result.gap is not None
    assert result.gap.code is expected_code


def test_work_id_and_bill_term_are_reverified_instead_of_trusted() -> None:
    valid = _descriptor(
        OfficialDocumentKind.BILL_TEXT,
        BILL_TEXT_URL,
        "bill:2219564:original",
    )
    malformed_item = DocumentWorkItem(
        work_id="caller-invented-work-id",
        kind=valid.work_item.kind,
        official_url=valid.work_item.official_url,
        evidence_types=valid.work_item.evidence_types,
        related_bill_numbers=valid.work_item.related_bill_numbers,
    )
    malformed = ExactCorpusWorkDescriptor(
        malformed_item,
        22,
        "bill:2219564:original",
    )
    wrong_term = _descriptor(
        OfficialDocumentKind.REVIEW_REPORT,
        REVIEW_URL,
        "review:2112345:1",
        term=22,
        bills=("2112345",),
    )

    malformed_result = corpus_document_from_parsed(
        malformed,
        _parsed(OfficialDocumentKind.BILL_TEXT, BILL_TEXT_URL),
    )
    wrong_term_result = corpus_document_from_parsed(
        wrong_term,
        _parsed(OfficialDocumentKind.REVIEW_REPORT, REVIEW_URL),
    )

    assert malformed_result.gap is not None
    assert malformed_result.gap.code is CorpusBridgeGapCode.WORK_IDENTITY_MISMATCH
    assert wrong_term_result.gap is not None
    assert wrong_term_result.gap.code is CorpusBridgeGapCode.BILL_TERM_MISMATCH


@pytest.mark.parametrize(
    ("bills", "expected_code"),
    [
        ((), CorpusBridgeGapCode.BILL_IDENTITY_MISSING),
        (("2219564", "2219565"), CorpusBridgeGapCode.BILL_IDENTITY_AMBIGUOUS),
    ],
)
def test_bill_documents_require_one_exact_related_bill(
    bills: tuple[str, ...],
    expected_code: CorpusBridgeGapCode,
) -> None:
    descriptor = _descriptor(
        OfficialDocumentKind.BILL_TEXT,
        BILL_TEXT_URL,
        "bill:2219564:original",
        bills=bills,
    )

    result = corpus_document_from_parsed(
        descriptor,
        _parsed(OfficialDocumentKind.BILL_TEXT, BILL_TEXT_URL),
    )

    assert result.gap is not None
    assert result.gap.code is expected_code


def test_one_review_report_can_keep_multiple_exact_same_term_bill_relations() -> None:
    descriptor = _descriptor(
        OfficialDocumentKind.REVIEW_REPORT,
        REVIEW_URL,
        "review:joint-report-1",
        bills=("2219564", "2219565"),
    )

    result = corpus_document_from_parsed(
        descriptor,
        _parsed(OfficialDocumentKind.REVIEW_REPORT, REVIEW_URL),
    )

    assert result.succeeded is True
    assert result.document is not None
    assert result.document.identity.official_identifier == "review:joint-report-1"


def test_parsed_kind_and_url_must_match_the_exact_work_item() -> None:
    descriptor = _descriptor(
        OfficialDocumentKind.MINUTES,
        MINUTES_URL,
        "minutes:54338",
    )

    wrong_kind = corpus_document_from_parsed(
        descriptor,
        _parsed(OfficialDocumentKind.REVIEW_REPORT, MINUTES_URL),
    )
    wrong_url = corpus_document_from_parsed(
        descriptor,
        _parsed(
            OfficialDocumentKind.MINUTES,
            "https://record.assembly.go.kr/assembly/viewer/minutes/download/pdf.do?id=99999",
        ),
    )

    assert wrong_kind.gap is not None
    assert wrong_kind.gap.code is CorpusBridgeGapCode.DOCUMENT_KIND_MISMATCH
    assert wrong_url.gap is not None
    assert wrong_url.gap.code is CorpusBridgeGapCode.DOCUMENT_URL_MISMATCH


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (DocumentOutcomeStatus.RETRYABLE_FAILURE, True),
        (DocumentOutcomeStatus.FAILED, False),
    ],
)
def test_failed_outcome_maps_to_exact_scope_without_free_text_message(
    status: DocumentOutcomeStatus,
    retryable: bool,
) -> None:
    descriptor = _descriptor(
        OfficialDocumentKind.REVIEW_REPORT,
        REVIEW_URL,
        "review:2219564:1",
    )
    outcome = DocumentOutcome(
        descriptor.work_item.work_id,
        status,
        error_code="network_error",
        error_message="Bearer secret-that-must-not-be-persisted",
    )

    result = corpus_failure_from_outcome(descriptor, outcome)

    assert result.succeeded is True
    assert result.gap is None
    assert result.failure is not None
    assert result.failure.assembly_term == 22
    assert result.failure.evidence_kind is CorpusEvidenceKind.REVIEW_REPORT
    assert result.failure.retryable is retryable
    assert result.failure.work_id == descriptor.work_item.work_id
    assert result.failure.failure.failure_key == descriptor.work_item.work_id
    assert result.failure.failure.reason_code == "network_error"
    assert result.failure.failure.official_identifier == "review:2219564:1"
    assert "secret" not in str(result.failure.failure.to_dict()).casefold()


def test_inconsistent_or_unbound_failed_outcomes_return_typed_gaps() -> None:
    descriptor = _descriptor(
        OfficialDocumentKind.MINUTES,
        MINUTES_URL,
        "minutes:54338",
    )
    parsed = _parsed(OfficialDocumentKind.MINUTES, MINUTES_URL)
    cases = (
        (
            DocumentOutcome(
                "another-work-id",
                DocumentOutcomeStatus.FAILED,
                error_code="network_error",
            ),
            CorpusBridgeGapCode.OUTCOME_WORK_MISMATCH,
        ),
        (
            DocumentOutcome(
                descriptor.work_item.work_id,
                DocumentOutcomeStatus.SUCCEEDED,
                result=_work_result(parsed),
            ),
            CorpusBridgeGapCode.OUTCOME_NOT_FAILED,
        ),
        (
            DocumentOutcome(
                descriptor.work_item.work_id,
                DocumentOutcomeStatus.FAILED,
                result=_work_result(parsed),
                error_code="network_error",
            ),
            CorpusBridgeGapCode.FAILED_OUTCOME_HAS_RESULT,
        ),
        (
            DocumentOutcome(
                descriptor.work_item.work_id,
                DocumentOutcomeStatus.FAILED,
                error_code="INVALID ERROR CODE",
            ),
            CorpusBridgeGapCode.OUTCOME_ERROR_CODE_INVALID,
        ),
    )

    for outcome, expected_code in cases:
        result = corpus_failure_from_outcome(descriptor, outcome)
        assert result.failure is None
        assert result.gap is not None
        assert result.gap.code is expected_code


def _candidate(
    descriptor: ExactCorpusWorkDescriptor,
    *,
    official_url: str | None = None,
    title: str | None = None,
) -> CorpusSearchCandidate:
    assert descriptor.assembly_term is not None
    assert descriptor.official_identifier is not None
    kind = {
        OfficialDocumentKind.MINUTES: CorpusEvidenceKind.MINUTES,
        OfficialDocumentKind.REVIEW_REPORT: CorpusEvidenceKind.REVIEW_REPORT,
        OfficialDocumentKind.BILL_TEXT: CorpusEvidenceKind.BILL_ORIGINAL,
    }[descriptor.work_item.kind]
    return CorpusSearchCandidate(
        identity=CorpusDocumentIdentity(
            descriptor.assembly_term,
            kind,
            descriptor.official_identifier,
        ),
        official_url=official_url or descriptor.work_item.official_url,
        title=title if title is not None else descriptor.title,
        document_date="2026-07-01",
        matched_terms=("인공지능",),
        occurrence_count=1,
    )


def test_candidate_mapping_accounts_for_more_than_one_thousand_exact_identities() -> None:
    descriptors = tuple(
        _descriptor(
            OfficialDocumentKind.MINUTES,
            (
                "https://record.assembly.go.kr/assembly/viewer/minutes/"
                f"download/pdf.do?id={number}"
            ),
            f"minutes:{number}",
            title="동일한 회의록 제목",
        )
        for number in range(60_000, 61_105)
    )
    candidates = tuple(_candidate(descriptor) for descriptor in descriptors)

    result = map_candidates_to_work(candidates, tuple(reversed(descriptors)))

    assert result.candidate_count == 1_105
    assert result.matched_count == 1_105
    assert result.unmapped_count == 0
    assert result.complete is True
    assert result.gaps == ()
    assert [match.candidate.identity for match in result.matches] == [
        candidate.identity for candidate in candidates
    ]


def test_candidate_mapping_never_uses_matching_title_as_identity() -> None:
    descriptor = _descriptor(
        OfficialDocumentKind.MINUTES,
        MINUTES_URL,
        "minutes:54338",
        title="최근 AI 입법 회의록",
    )
    candidate = CorpusSearchCandidate(
        identity=CorpusDocumentIdentity(
            22,
            CorpusEvidenceKind.MINUTES,
            "minutes:99999",
        ),
        official_url=descriptor.work_item.official_url,
        title=descriptor.title,
        document_date="2026-07-01",
        matched_terms=("인공지능",),
        occurrence_count=3,
    )

    result = map_candidates_to_work((candidate,), (descriptor,))

    assert result.complete is False
    assert result.matches == ()
    assert result.unmapped_count == 1
    assert [gap.code for gap in result.gaps] == [
        CorpusBridgeGapCode.CANDIDATE_UNMAPPED
    ]


def test_candidate_mapping_rejects_url_mismatch_ambiguity_and_duplicates() -> None:
    descriptor = _descriptor(
        OfficialDocumentKind.MINUTES,
        MINUTES_URL,
        "minutes:54338",
    )
    other_url_descriptor = _descriptor(
        OfficialDocumentKind.MINUTES,
        "https://record.assembly.go.kr/assembly/viewer/minutes/download/pdf.do?id=54339",
        "minutes:54338",
    )
    different_url_candidate = _candidate(
        descriptor,
        official_url=(
            "https://record.assembly.go.kr/assembly/viewer/minutes/download/pdf.do?id=77777"
        ),
    )

    mismatch = map_candidates_to_work((different_url_candidate,), (descriptor,))
    ambiguous = map_candidates_to_work(
        (_candidate(descriptor),),
        (descriptor, other_url_descriptor),
    )
    duplicate = map_candidates_to_work(
        (_candidate(descriptor), _candidate(descriptor)),
        (descriptor,),
    )

    assert [gap.code for gap in mismatch.gaps] == [
        CorpusBridgeGapCode.CANDIDATE_URL_MISMATCH
    ]
    assert [gap.code for gap in ambiguous.gaps] == [
        CorpusBridgeGapCode.CANDIDATE_AMBIGUOUS
    ]
    assert duplicate.matched_count == 1
    assert duplicate.unmapped_count == 1
    assert [gap.code for gap in duplicate.gaps] == [
        CorpusBridgeGapCode.CANDIDATE_DUPLICATE
    ]
