"""Turn a research contract into complete, deterministic official-source partitions.

The Open Assembly meeting APIs accept a calendar-month ``CONF_DATE`` filter.
Using that native granularity keeps pagination snapshots bounded while preserving
every day at the first and last boundary.  The dedicated subcommittee endpoint
does not expose a dependable date filter, so it is fetched once per Assembly
term and date-filtered after collection.

Bill discovery and bill status are deliberately two different phases.  Topic
terms are valid for the main bill endpoint, while the status endpoint must be
queried with each exact ``BILL_NO`` discovered in phase one.  Review-report
discovery similarly depends on the official external bill id returned by the
bill metadata endpoint; every discovered official PDF is then a required fetch.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Any, Final

from kasm.adapters.korea.bills import BILL_DATASET, BILL_STATUS_DATASET
from kasm.adapters.korea.client import OPEN_ASSEMBLY_BASE_URL
from kasm.adapters.korea.documents import BILL_INFO_URL
from kasm.adapters.korea.sources import DATASET_BY_SOURCE, MeetingSource
from kasm.research.assembly_terms import DEFAULT_ASSEMBLY_TERM_BOUNDS
from kasm.research.collector import MetadataKind, MetadataPartition
from kasm.research.contracts import EvidenceType
from kasm.research.planner import ResearchPlan
from kasm.search.lexical import query_terms
from kasm.search.terminology import LEGAL_TERMINOLOGY, TermRelation


class OfficialSourceKind(StrEnum):
    """Every official endpoint family needed by the connected evidence graph."""

    BILL_METADATA = "bill_metadata"
    BILL_STATUS = "bill_status"
    PLENARY_MINUTES = "plenary_minutes"
    COMMITTEE_MINUTES = "committee_minutes"
    SUBCOMMITTEE_MINUTES = "subcommittee_minutes"
    REVIEW_REPORT_INDEX = "review_report_index"
    REVIEW_REPORT_PDF = "review_report_pdf"


class SearchTermRole(StrEnum):
    """Why a term belongs to the auditable official-source relevance scope."""

    OFFICIAL_EQUIVALENT = "official_equivalent"
    OFFICIAL_RELATED = "official_related"
    USER_LITERAL = "user_literal"


@dataclass(frozen=True, slots=True)
class OfficialSource:
    kind: OfficialSourceKind
    endpoint: str
    method: str
    dataset: str | None
    collection_rule: str
    deferred: bool = False


OFFICIAL_RESEARCH_SOURCES: Final = (
    OfficialSource(
        OfficialSourceKind.BILL_METADATA,
        f"{OPEN_ASSEMBLY_BASE_URL}/{BILL_DATASET}",
        "GET",
        BILL_DATASET,
        "fetch_exact_bill_or_complete_assembly_term_then_resolve_all_candidates",
    ),
    OfficialSource(
        OfficialSourceKind.BILL_STATUS,
        f"{OPEN_ASSEMBLY_BASE_URL}/{BILL_STATUS_DATASET}",
        "GET",
        BILL_STATUS_DATASET,
        "fetch_all_for_each_exact_bill_number",
        deferred=True,
    ),
    OfficialSource(
        OfficialSourceKind.PLENARY_MINUTES,
        f"{OPEN_ASSEMBLY_BASE_URL}/{DATASET_BY_SOURCE[MeetingSource.PLENARY]}",
        "GET",
        DATASET_BY_SOURCE[MeetingSource.PLENARY],
        "fetch_all_for_each_calendar_month",
    ),
    OfficialSource(
        OfficialSourceKind.COMMITTEE_MINUTES,
        f"{OPEN_ASSEMBLY_BASE_URL}/{DATASET_BY_SOURCE[MeetingSource.COMMITTEE]}",
        "GET",
        DATASET_BY_SOURCE[MeetingSource.COMMITTEE],
        "fetch_all_for_each_calendar_month_and_explicit_committee",
    ),
    OfficialSource(
        OfficialSourceKind.SUBCOMMITTEE_MINUTES,
        f"{OPEN_ASSEMBLY_BASE_URL}/{DATASET_BY_SOURCE[MeetingSource.SUBCOMMITTEE]}",
        "GET",
        DATASET_BY_SOURCE[MeetingSource.SUBCOMMITTEE],
        "fetch_all_once_per_assembly_term_then_filter_dates_locally",
    ),
    OfficialSource(
        OfficialSourceKind.REVIEW_REPORT_INDEX,
        BILL_INFO_URL,
        "POST",
        None,
        "check_once_for_each_relevant_bill_with_external_bill_id",
        deferred=True,
    ),
    OfficialSource(
        OfficialSourceKind.REVIEW_REPORT_PDF,
        "https://likms.assembly.go.kr/{discovered_official_review_report_pdf}",
        "GET",
        None,
        "fetch_every_official_pdf_link_discovered_by_review_report_index",
        deferred=True,
    ),
)

_SOURCE_BY_KIND: Final = {source.kind: source for source in OFFICIAL_RESEARCH_SOURCES}

_MEETING_EVIDENCE: Final = frozenset(
    {
        EvidenceType.AGENDAS,
        EvidenceType.SUBCOMMITTEE_MINUTES,
        EvidenceType.SPEECHES,
        EvidenceType.SPEECH_CONTEXT,
        EvidenceType.GOVERNMENT_RESPONSES,
    }
)
_ALL_MINUTES_EVIDENCE: Final = frozenset(
    {
        EvidenceType.AGENDAS,
        EvidenceType.SPEECHES,
        EvidenceType.SPEECH_CONTEXT,
        EvidenceType.GOVERNMENT_RESPONSES,
    }
)
_BILL_EVIDENCE: Final = frozenset(
    {
        EvidenceType.BILLS,
        EvidenceType.BILL_TEXT,
        EvidenceType.BILL_STATUS,
        EvidenceType.REVIEW_REPORTS,
    }
)
_GENERIC_QUERY_TERMS: Final = frozenset(
    {
        "관련",
        "높은",
        "결과",
        "검색",
        "공식",
        "국회",
        "기준",
        "내용",
        "대상",
        "대해",
        "대한",
        "발의된",
        "문서",
        "발언",
        "법률",
        "법률안",
        "법안",
        "상임위원회",
        "보고서",
        "부터",
        "상태",
        "시간순",
        "올해",
        "이에",
        "원문",
        "의안",
        "의안번호",
        "입법",
        "자료",
        "쟁점",
        "중요",
        "중요도",
        "정리",
        "정리하고",
        "정리해줘",
        "조사",
        "조사해",
        "조사해줘",
        "조사해주세요",
        "조회",
        "주세요",
        "최근",
        "정도",
        "처리",
        "현재",
        "확인",
        "확인해줘",
        "알려줘",
        "보여줘",
        "회의",
        "회의록",
        "위원회",
        "논의",
        "검토보고서",
        "전문위원",
        "소위원회",
        "정부",
        "답변",
    }
)
_QUERY_PARTICLES: Final = (
    "으로부터",
    "에서부터",
    "에게서",
    "까지의",
    "부터의",
    "에서는",
    "으로",
    "에서",
    "부터",
    "까지",
    "에게",
    "한테",
    "처럼",
    "보다",
    "과의",
    "와의",
    "에는",
    "에도",
    "에만",
    "이라는",
    "라고",
    "의",
    "을",
    "를",
    "은",
    "는",
    "이",
    "가",
    "에",
    "도",
    "만",
    "과",
    "와",
    "로",
)
_HANGUL = re.compile(r"[가-힣]")
_DIGIT = re.compile(r"\d")


@dataclass(frozen=True, slots=True)
class MonthBucket:
    """One lossless calendar-month slice of the effective requested interval."""

    value: str
    date_from: date
    date_to: date

    def __post_init__(self) -> None:
        if not re.fullmatch(r"(?:19|20)\d{2}-(?:0[1-9]|1[0-2])", self.value):
            raise ValueError("month bucket value must use YYYY-MM")
        if self.date_from > self.date_to:
            raise ValueError("month bucket date_from must not follow date_to")
        if self.date_from.strftime("%Y-%m") != self.value:
            raise ValueError("month bucket start must belong to its value")
        if self.date_to.strftime("%Y-%m") != self.value:
            raise ValueError("month bucket end must belong to its value")


@dataclass(frozen=True, slots=True)
class AssemblyTermRange:
    """The exact calendar slice collected for one National Assembly term."""

    assembly_term: int
    date_from: date
    date_to: date
    months: tuple[MonthBucket, ...]

    def __post_init__(self) -> None:
        if self.assembly_term < 1:
            raise ValueError("assembly term range requires a positive term")
        if self.date_from > self.date_to:
            raise ValueError("assembly term range start must not follow its end")
        _validate_month_coverage(self.months, self.date_from, self.date_to)


@dataclass(frozen=True, slots=True)
class PartitionSearchTerm:
    value: str
    role: SearchTermRole
    category: str | None
    reason: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("partition search terms must not be empty")
        if re.fullmatch(r"\d{7}", self.value):
            raise ValueError("exact bill numbers must not be represented as search terms")


@dataclass(frozen=True, slots=True)
class PlannedMetadataPartition:
    """A fetchable metadata partition plus the scope it represents."""

    source: OfficialSourceKind
    partition: MetadataPartition
    exact_bill_no: str | None = None
    search_term: PartitionSearchTerm | None = None
    month: MonthBucket | None = None
    local_date_from: date | None = None
    local_date_to: date | None = None
    local_committees: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeferredSourceRequirement:
    """A source whose exact requests depend on phase-one official metadata."""

    source: OfficialSourceKind
    trigger: str
    required_fields: tuple[str, ...]
    expected_count: int | None
    expected_count_rule: str


@dataclass(frozen=True, slots=True)
class PartitionCoverageExpectation:
    assembly_term_count: int
    month_bucket_count: int
    source_endpoint_count: int
    official_search_term_count: int
    official_search_term_batch_size: int
    official_search_term_batch_count: int
    official_search_terms_preserved: bool
    metadata_partition_count: int
    bill_metadata_partition_count: int
    bill_status_partition_count: int
    meeting_partition_count: int
    deferred_requirement_count: int
    exact_review_bill_count: int
    dynamic_status_count: bool
    dynamic_review_count: bool
    scope_fully_represented: bool

    def to_dict(self) -> dict[str, int | bool]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResearchPartitionPlan:
    """Complete static partitions and explicit deferred official-source work."""

    exact_bill_numbers: tuple[str, ...]
    search_terms: tuple[PartitionSearchTerm, ...]
    search_term_batch_size: int
    committees: tuple[str, ...]
    assembly_terms: tuple[int, ...]
    term_ranges: tuple[AssemblyTermRange, ...]
    requested_date_from: date | None
    requested_date_to: date | None
    effective_date_from: date
    effective_date_to: date
    range_policy: str
    range_adjustments: tuple[str, ...]
    months: tuple[MonthBucket, ...]
    official_sources: tuple[OfficialSource, ...]
    planned_partitions: tuple[PlannedMetadataPartition, ...]
    deferred_requirements: tuple[DeferredSourceRequirement, ...]
    coverage: PartitionCoverageExpectation

    def __post_init__(self) -> None:
        if self.search_term_batch_size < 1:
            raise ValueError("search term batch size must be positive")

    @property
    def metadata_partitions(self) -> tuple[MetadataPartition, ...]:
        return tuple(item.partition for item in self.planned_partitions)

    @property
    def search_term_batches(self) -> tuple[tuple[PartitionSearchTerm, ...], ...]:
        """Return every relevance term once in deterministic bounded batches."""

        return _batch_search_terms(self.search_terms, self.search_term_batch_size)

    def to_dict(self) -> dict[str, Any]:
        return {
            "exact_bill_numbers": list(self.exact_bill_numbers),
            "search_terms": [asdict(term) for term in self.search_terms],
            "search_term_batch_size": self.search_term_batch_size,
            "search_term_batches": [
                [asdict(term) for term in batch] for batch in self.search_term_batches
            ],
            "committees": list(self.committees),
            "assembly_terms": list(self.assembly_terms),
            "term_ranges": [
                {
                    "assembly_term": item.assembly_term,
                    "date_from": item.date_from.isoformat(),
                    "date_to": item.date_to.isoformat(),
                    "months": [month.value for month in item.months],
                }
                for item in self.term_ranges
            ],
            "requested_date_from": (
                self.requested_date_from.isoformat() if self.requested_date_from else None
            ),
            "requested_date_to": (
                self.requested_date_to.isoformat() if self.requested_date_to else None
            ),
            "effective_date_from": self.effective_date_from.isoformat(),
            "effective_date_to": self.effective_date_to.isoformat(),
            "range_policy": self.range_policy,
            "range_adjustments": list(self.range_adjustments),
            "months": [
                {
                    "value": month.value,
                    "date_from": month.date_from.isoformat(),
                    "date_to": month.date_to.isoformat(),
                }
                for month in self.months
            ],
            "official_sources": [
                {**asdict(source), "kind": source.kind.value}
                for source in self.official_sources
            ],
            "metadata_partitions": [
                {
                    "partition_id": item.partition.partition_id,
                    "source": item.source.value,
                    "kind": item.partition.kind.value,
                    "dataset": item.partition.dataset,
                    "parameters": item.partition.parameters_dict(),
                    "exact_bill_no": item.exact_bill_no,
                    "search_term": item.search_term.value if item.search_term else None,
                    "month": item.month.value if item.month else None,
                    "local_date_from": (
                        item.local_date_from.isoformat() if item.local_date_from else None
                    ),
                    "local_date_to": (
                        item.local_date_to.isoformat() if item.local_date_to else None
                    ),
                    "local_committees": list(item.local_committees),
                }
                for item in self.planned_partitions
            ],
            "deferred_requirements": [
                {**asdict(item), "source": item.source.value}
                for item in self.deferred_requirements
            ],
            "coverage": self.coverage.to_dict(),
        }


class ResearchPartitionPlanner:
    """Compile a deterministic research plan into official-source partitions."""

    def __init__(
        self,
        *,
        page_size: int = 100,
        max_search_terms: int = 32,
        assembly_term_bounds: Mapping[int, tuple[date, date]] | None = None,
    ) -> None:
        if not 1 <= page_size <= 1000:
            raise ValueError("page_size must be between 1 and 1000")
        if max_search_terms < 1:
            raise ValueError("max_search_terms must be positive")
        self.page_size = page_size
        # ``max_search_terms`` is retained as a backwards-compatible argument,
        # but it is a deterministic audit-batch size rather than a destructive
        # cap. Topic discovery always fetches the complete Assembly-term bill
        # universe and evaluates every preserved relevance term locally.
        self.search_term_batch_size = max_search_terms
        self.assembly_term_bounds = dict(
            assembly_term_bounds or DEFAULT_ASSEMBLY_TERM_BOUNDS
        )

    def plan(self, research_plan: ResearchPlan) -> ResearchPartitionPlan:
        contract = research_plan.contract
        term_ranges, policy, adjustments = _effective_term_ranges(
            contract.assembly_terms,
            contract.date_from,
            contract.date_to,
            as_of=contract.as_of.date(),
            assembly_term_bounds=self.assembly_term_bounds,
            explicit_term=research_plan.interpreted_scope.assembly_term_explicit,
        )
        effective_from = term_ranges[0].date_from
        effective_to = term_ranges[-1].date_to
        months = tuple(
            month for term_range in term_ranges for month in term_range.months
        )
        exact_numbers = tuple(dict.fromkeys(contract.bill_numbers))
        search_terms = _partition_search_terms(research_plan)
        search_term_batches = _batch_search_terms(
            search_terms,
            self.search_term_batch_size,
        )

        requested_evidence = set(contract.evidence_types)
        needs_bills = bool(requested_evidence.intersection(_BILL_EVIDENCE))
        needs_status = EvidenceType.BILL_STATUS in requested_evidence
        needs_meetings = bool(requested_evidence.intersection(_MEETING_EVIDENCE))
        needs_all_minutes = bool(requested_evidence.intersection(_ALL_MINUTES_EVIDENCE))
        needs_reviews = EvidenceType.REVIEW_REPORTS in requested_evidence
        if needs_bills and not exact_numbers and not search_terms:
            raise ValueError(
                "no Korean official-source search term could be derived; "
                "provide a Korean query explicitly"
            )

        planned: list[PlannedMetadataPartition] = []
        if needs_bills:
            for assembly_term in contract.assembly_terms:
                planned.extend(
                    self._bill_partitions(
                        assembly_term,
                        exact_numbers,
                        search_terms,
                        representative_proposer_names=(
                            contract.representative_proposer_names
                            if not contract.co_proposer_names
                            and not contract.proposer_names
                            else ()
                        ),
                        include_status=needs_status,
                    )
                )
        if needs_meetings:
            if exact_numbers:
                for term_range in term_ranges:
                    planned.extend(
                        self._exact_bill_meeting_partitions(
                            term_range.assembly_term,
                            exact_numbers,
                            contract.committees,
                            term_range.date_from,
                            term_range.date_to,
                            include_plenary=needs_all_minutes,
                        )
                    )
            else:
                for term_range in term_ranges:
                    planned.extend(
                        self._meeting_partitions(
                            term_range.assembly_term,
                            contract.committees,
                            term_range.months,
                            term_range.date_from,
                            term_range.date_to,
                            include_plenary=needs_all_minutes,
                        )
                    )
        planned = _deduplicate_partitions(planned)

        source_kinds: list[OfficialSourceKind] = []
        if needs_bills:
            source_kinds.append(OfficialSourceKind.BILL_METADATA)
        if needs_status:
            source_kinds.append(OfficialSourceKind.BILL_STATUS)
        if needs_meetings:
            if needs_all_minutes:
                source_kinds.append(OfficialSourceKind.PLENARY_MINUTES)
            source_kinds.append(OfficialSourceKind.COMMITTEE_MINUTES)
            if not exact_numbers:
                source_kinds.append(OfficialSourceKind.SUBCOMMITTEE_MINUTES)
        if needs_reviews:
            source_kinds.extend(
                (
                    OfficialSourceKind.REVIEW_REPORT_INDEX,
                    OfficialSourceKind.REVIEW_REPORT_PDF,
                )
            )
        source_kinds = list(dict.fromkeys(source_kinds))
        sources = tuple(_SOURCE_BY_KIND[kind] for kind in source_kinds)
        deferred = _deferred_requirements(
            exact_numbers,
            needs_status=needs_status,
            needs_reviews=needs_reviews,
        )

        bill_count = sum(
            item.source is OfficialSourceKind.BILL_METADATA for item in planned
        )
        status_count = sum(
            item.source is OfficialSourceKind.BILL_STATUS for item in planned
        )
        meeting_count = sum(
            item.partition.kind is MetadataKind.MEETING for item in planned
        )
        coverage = PartitionCoverageExpectation(
            assembly_term_count=len(contract.assembly_terms),
            month_bucket_count=len(months),
            source_endpoint_count=len(sources),
            official_search_term_count=len(search_terms),
            official_search_term_batch_size=self.search_term_batch_size,
            official_search_term_batch_count=len(search_term_batches),
            official_search_terms_preserved=(
                tuple(term for batch in search_term_batches for term in batch)
                == search_terms
            ),
            metadata_partition_count=len(planned),
            bill_metadata_partition_count=bill_count,
            bill_status_partition_count=status_count,
            meeting_partition_count=meeting_count,
            deferred_requirement_count=len(deferred),
            exact_review_bill_count=len(exact_numbers) if needs_reviews else 0,
            dynamic_status_count=bool(needs_status and not exact_numbers),
            dynamic_review_count=bool(needs_reviews and not exact_numbers),
            scope_fully_represented=not adjustments,
        )
        return ResearchPartitionPlan(
            exact_bill_numbers=exact_numbers,
            search_terms=search_terms,
            search_term_batch_size=self.search_term_batch_size,
            committees=contract.committees,
            assembly_terms=contract.assembly_terms,
            term_ranges=term_ranges,
            requested_date_from=contract.date_from,
            requested_date_to=contract.date_to,
            effective_date_from=effective_from,
            effective_date_to=effective_to,
            range_policy=policy,
            range_adjustments=adjustments,
            months=months,
            official_sources=sources,
            planned_partitions=tuple(planned),
            deferred_requirements=deferred,
            coverage=coverage,
        )

    def _bill_partitions(
        self,
        assembly_term: int,
        exact_numbers: tuple[str, ...],
        search_terms: tuple[PartitionSearchTerm, ...],
        *,
        representative_proposer_names: tuple[str, ...],
        include_status: bool,
    ) -> list[PlannedMetadataPartition]:
        result: list[PlannedMetadataPartition] = []
        if exact_numbers:
            for bill_no in exact_numbers:
                if int(bill_no[:2]) != assembly_term:
                    continue
                result.append(
                    _planned_partition(
                        OfficialSourceKind.BILL_METADATA,
                        MetadataKind.BILL,
                        BILL_DATASET,
                        {"AGE": assembly_term, "BILL_NO": bill_no},
                        self.page_size,
                        exact_bill_no=bill_no,
                    )
                )
                if include_status:
                    result.append(
                        _planned_partition(
                            OfficialSourceKind.BILL_STATUS,
                            MetadataKind.BILL,
                            BILL_STATUS_DATASET,
                            {"AGE": assembly_term, "BILL_NO": bill_no},
                            self.page_size,
                            exact_bill_no=bill_no,
                        )
                    )
            return result

        # The official PROPOSER parameter is a supported representative-name
        # lookup and is independently checked against RST_PROPOSER after fetch.
        # It makes the common representative-only search fast without weakening
        # identity.  PUBL_PROPOSER/RST_PROPOSER request parameters are ignored by
        # the upstream service, so co-proposer and role-agnostic searches must
        # continue to collect the complete Assembly-term bill universe.
        if search_terms:
            if representative_proposer_names:
                for name in representative_proposer_names:
                    result.append(
                        _planned_partition(
                            OfficialSourceKind.BILL_METADATA,
                            MetadataKind.BILL,
                            BILL_DATASET,
                            {"AGE": assembly_term, "PROPOSER": name},
                            self.page_size,
                        )
                    )
            else:
                # Topic searches collect the complete bill universe for the
                # Assembly term once, then evaluate every row locally. Relying
                # on BILL_NAME expansions would silently lose unforeseen terms.
                result.append(
                    _planned_partition(
                        OfficialSourceKind.BILL_METADATA,
                        MetadataKind.BILL,
                        BILL_DATASET,
                        {"AGE": assembly_term},
                        self.page_size,
                    )
                )
        # BILL_STATUS intentionally has no BILL_NAME partitions.  That endpoint
        # ignores topic-name filtering; exact candidate numbers are materialized
        # from the deferred requirement after bill discovery and relevance gates.
        return result

    def _exact_bill_meeting_partitions(
        self,
        assembly_term: int,
        exact_numbers: tuple[str, ...],
        committees: tuple[str, ...],
        date_from: date,
        date_to: date,
        *,
        include_plenary: bool,
    ) -> list[PlannedMetadataPartition]:
        """Fetch only agenda rows that carry an explicit requested bill number.

        Both minutes endpoints require ``CONF_DATE`` but accept a calendar year,
        and their ``SUB_NAME`` filter matches the exact seven-digit number in the
        official agenda label. One query per source/year is therefore complete
        for an explicit identifier without scanning every month. Committee rows
        also contain subcommittee agendas, so the unfilterable full-term
        subcommittee listing is deliberately excluded from this fast path.
        Every returned row still passes the resolver's independent bill-number
        gate; an upstream partial/fuzzy match can never substitute another bill.
        """

        numbers = tuple(
            number for number in exact_numbers if int(number[:2]) == assembly_term
        )
        if not numbers:
            return []
        result: list[PlannedMetadataPartition] = []
        committee_scopes: tuple[str | None, ...] = committees or (None,)
        for year in range(date_from.year, date_to.year + 1):
            for bill_no in numbers:
                base: dict[str, str | int] = {
                    "DAE_NUM": assembly_term,
                    "CONF_DATE": str(year),
                    "SUB_NAME": bill_no,
                }
                if include_plenary:
                    result.append(
                        _planned_partition(
                            OfficialSourceKind.PLENARY_MINUTES,
                            MetadataKind.MEETING,
                            DATASET_BY_SOURCE[MeetingSource.PLENARY],
                            base,
                            self.page_size,
                            exact_bill_no=bill_no,
                            local_date_from=date_from,
                            local_date_to=date_to,
                            local_committees=committees,
                        )
                    )
                for committee in committee_scopes:
                    parameters = dict(base)
                    if committee:
                        parameters["COMM_NAME"] = committee
                    result.append(
                        _planned_partition(
                            OfficialSourceKind.COMMITTEE_MINUTES,
                            MetadataKind.MEETING,
                            DATASET_BY_SOURCE[MeetingSource.COMMITTEE],
                            parameters,
                            self.page_size,
                            exact_bill_no=bill_no,
                            local_date_from=date_from,
                            local_date_to=date_to,
                            local_committees=(committee,) if committee else (),
                        )
                    )
        return result

    def _meeting_partitions(
        self,
        assembly_term: int,
        committees: tuple[str, ...],
        months: tuple[MonthBucket, ...],
        date_from: date,
        date_to: date,
        *,
        include_plenary: bool,
    ) -> list[PlannedMetadataPartition]:
        result: list[PlannedMetadataPartition] = []
        committee_scopes: tuple[str | None, ...] = committees or (None,)
        for month in months:
            if include_plenary:
                result.append(
                    _planned_partition(
                        OfficialSourceKind.PLENARY_MINUTES,
                        MetadataKind.MEETING,
                        DATASET_BY_SOURCE[MeetingSource.PLENARY],
                        {"DAE_NUM": assembly_term, "CONF_DATE": month.value},
                        self.page_size,
                        month=month,
                        local_date_from=date_from,
                        local_date_to=date_to,
                        local_committees=committees,
                    )
                )
            for committee in committee_scopes:
                parameters: dict[str, str | int] = {
                    "DAE_NUM": assembly_term,
                    "CONF_DATE": month.value,
                }
                if committee:
                    parameters["COMM_NAME"] = committee
                result.append(
                    _planned_partition(
                        OfficialSourceKind.COMMITTEE_MINUTES,
                        MetadataKind.MEETING,
                        DATASET_BY_SOURCE[MeetingSource.COMMITTEE],
                        parameters,
                        self.page_size,
                        month=month,
                        local_date_from=date_from,
                        local_date_to=date_to,
                        local_committees=(committee,) if committee else (),
                    )
                )
        # The dedicated source is intentionally one full-term fetch.  Its public
        # contract has no reliable month filter; complete pagination followed by
        # the explicit local date/committee filters is the fail-closed strategy.
        result.append(
            _planned_partition(
                OfficialSourceKind.SUBCOMMITTEE_MINUTES,
                MetadataKind.MEETING,
                DATASET_BY_SOURCE[MeetingSource.SUBCOMMITTEE],
                {"ERACO": f"제{assembly_term}대"},
                self.page_size,
                local_date_from=date_from,
                local_date_to=date_to,
                local_committees=committees,
            )
        )
        return result


def plan_partitions(
    research_plan: ResearchPlan,
    *,
    page_size: int = 100,
    max_search_terms: int = 32,
    assembly_term_bounds: Mapping[int, tuple[date, date]] | None = None,
) -> ResearchPartitionPlan:
    """Convenience entry point for deterministic official-source partitioning."""

    return ResearchPartitionPlanner(
        page_size=page_size,
        max_search_terms=max_search_terms,
        assembly_term_bounds=assembly_term_bounds,
    ).plan(research_plan)


def _effective_term_ranges(
    assembly_terms: tuple[int, ...],
    requested_from: date | None,
    requested_to: date | None,
    *,
    as_of: date,
    assembly_term_bounds: Mapping[int, tuple[date, date]],
    explicit_term: bool,
) -> tuple[tuple[AssemblyTermRange, ...], str, tuple[str, ...]]:
    if not assembly_terms:
        raise ValueError("at least one Assembly term is required")
    bounds: list[tuple[int, date, date]] = []
    for assembly_term in assembly_terms:
        try:
            term_start, term_end = assembly_term_bounds[assembly_term]
        except KeyError as exc:
            raise ValueError(
                f"assembly term {assembly_term} requires explicit date bounds"
            ) from exc
        if term_start > term_end:
            raise ValueError("assembly term bounds are invalid")
        bounds.append((assembly_term, term_start, term_end))
    for bound_left, bound_right in zip(bounds, bounds[1:], strict=False):
        if bound_left[2] >= bound_right[1]:
            raise ValueError("assembly term scope contains an overlap")

    scope_start = bounds[0][1]
    scope_end = min(as_of, bounds[-1][2])
    effective_from = requested_from or scope_start
    effective_to = requested_to or scope_end
    adjustments: list[str] = []
    if effective_from < scope_start:
        effective_from = scope_start
        adjustments.append(
            "date_from_clipped_to_assembly_term_start"
            if explicit_term
            else "date_from_clipped_to_supported_assembly_terms_start"
        )
    if effective_to > scope_end:
        effective_to = scope_end
        adjustment = (
            "date_to_clipped_to_as_of"
            if as_of <= bounds[-1][2]
            else (
                "date_to_clipped_to_assembly_term_end"
                if explicit_term
                else "date_to_clipped_to_supported_assembly_terms_end"
            )
        )
        adjustments.append(adjustment)
    if effective_from > effective_to:
        label = "configured Assembly term" if explicit_term else "supported Assembly terms"
        raise ValueError(f"requested date range does not overlap the {label}")

    term_ranges = tuple(
        AssemblyTermRange(
            assembly_term,
            max(effective_from, term_start),
            min(effective_to, term_end),
            _month_buckets(
                max(effective_from, term_start),
                min(effective_to, term_end),
            ),
        )
        for assembly_term, term_start, term_end in bounds
        if max(effective_from, term_start) <= min(effective_to, term_end)
    )
    if not term_ranges:
        raise ValueError("requested date range has no fetchable Assembly term slice")
    for range_left, range_right in zip(term_ranges, term_ranges[1:], strict=False):
        if range_left.date_to >= range_right.date_from:
            raise ValueError("effective Assembly term ranges contain an overlap")

    if requested_from is None and requested_to is None:
        policy = (
            "current_assembly_term_to_as_of"
            if as_of <= bounds[-1][2]
            else "complete_configured_assembly_term"
        )
    else:
        policy = (
            "explicit_range_intersected_with_assembly_term_and_as_of"
            if explicit_term
            else "explicit_range_intersected_with_assembly_terms_and_as_of"
        )
    return term_ranges, policy, tuple(adjustments)


def _month_buckets(date_from: date, date_to: date) -> tuple[MonthBucket, ...]:
    cursor = date(date_from.year, date_from.month, 1)
    buckets: list[MonthBucket] = []
    while cursor <= date_to:
        month_end = date(
            cursor.year,
            cursor.month,
            calendar.monthrange(cursor.year, cursor.month)[1],
        )
        buckets.append(
            MonthBucket(
                cursor.strftime("%Y-%m"),
                max(date_from, cursor),
                min(date_to, month_end),
            )
        )
        cursor = month_end + timedelta(days=1)
    _validate_month_coverage(buckets, date_from, date_to)
    return tuple(buckets)


def _validate_month_coverage(
    buckets: Iterable[MonthBucket], date_from: date, date_to: date
) -> None:
    values = tuple(buckets)
    if not values or values[0].date_from != date_from or values[-1].date_to != date_to:
        raise ValueError("month partitions do not cover the requested boundaries")
    for left, right in zip(values, values[1:], strict=False):
        if left.date_to + timedelta(days=1) != right.date_from:
            raise ValueError("month partitions contain a gap or overlap")


def _partition_search_terms(
    research_plan: ResearchPlan,
) -> tuple[PartitionSearchTerm, ...]:
    expansion = LEGAL_TERMINOLOGY.expand(
        research_plan.search_query,
        include_related=True,
    )
    terms: list[PartitionSearchTerm] = []
    for item in expansion.expansions:
        terms.append(
            PartitionSearchTerm(
                value=item.term,
                role=(
                    SearchTermRole.OFFICIAL_EQUIVALENT
                    if item.relation is TermRelation.EQUIVALENT
                    else SearchTermRole.OFFICIAL_RELATED
                ),
                category=item.category.value,
                reason=item.reason,
            )
        )

    official_keys = {_term_key(item.value) for item in terms}
    committees = tuple(_term_key(item) for item in research_plan.contract.committees)
    for term in query_terms(research_plan.search_query):
        normalized = _strip_query_particle(term.strip())
        key = _term_key(normalized)
        if (
            len(key) < 2
            or not _HANGUL.search(normalized)
            or _DIGIT.search(normalized)
            or key in _GENERIC_QUERY_TERMS
            or key in official_keys
            or any(key == committee or key.startswith(committee) for committee in committees)
        ):
            continue
        terms.append(
            PartitionSearchTerm(
                normalized,
                SearchTermRole.USER_LITERAL,
                None,
                "literal_korean_query_term",
            )
        )
        official_keys.add(key)
    return _deduplicate_search_terms(terms)


def _deduplicate_search_terms(
    terms: Iterable[PartitionSearchTerm],
) -> tuple[PartitionSearchTerm, ...]:
    priority = {
        SearchTermRole.OFFICIAL_EQUIVALENT: 0,
        SearchTermRole.USER_LITERAL: 1,
        SearchTermRole.OFFICIAL_RELATED: 2,
    }
    selected: dict[str, PartitionSearchTerm] = {}
    for term in terms:
        key = _term_key(term.value)
        current = selected.get(key)
        if current is None or priority[term.role] < priority[current.role]:
            selected[key] = term
    return tuple(
        sorted(
            selected.values(),
            key=lambda item: (priority[item.role], item.value, item.reason),
        )
    )


def _batch_search_terms(
    terms: tuple[PartitionSearchTerm, ...],
    batch_size: int,
) -> tuple[tuple[PartitionSearchTerm, ...], ...]:
    """Group all ordered terms without dropping, reordering, or duplicating any."""

    if batch_size < 1:
        raise ValueError("search term batch size must be positive")
    return tuple(
        terms[offset : offset + batch_size]
        for offset in range(0, len(terms), batch_size)
    )


def _planned_partition(
    source: OfficialSourceKind,
    kind: MetadataKind,
    dataset: str,
    parameters: Mapping[str, str | int],
    page_size: int,
    *,
    exact_bill_no: str | None = None,
    search_term: PartitionSearchTerm | None = None,
    month: MonthBucket | None = None,
    local_date_from: date | None = None,
    local_date_to: date | None = None,
    local_committees: tuple[str, ...] = (),
) -> PlannedMetadataPartition:
    canonical = json.dumps(
        {"source": source.value, "parameters": parameters},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    suffix = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    partition = MetadataPartition.create(
        f"{source.value}:{suffix}",
        kind,
        dataset,
        parameters=parameters,
        page_size=page_size,
    )
    return PlannedMetadataPartition(
        source=source,
        partition=partition,
        exact_bill_no=exact_bill_no,
        search_term=search_term,
        month=month,
        local_date_from=local_date_from,
        local_date_to=local_date_to,
        local_committees=local_committees,
    )


def _deduplicate_partitions(
    partitions: Iterable[PlannedMetadataPartition],
) -> list[PlannedMetadataPartition]:
    unique: dict[
        tuple[str, str, tuple[tuple[str, str | int], ...]],
        PlannedMetadataPartition,
    ] = {}
    for item in partitions:
        key = (
            item.source.value,
            item.partition.dataset,
            item.partition.parameters,
        )
        unique.setdefault(key, item)
    return sorted(
        unique.values(),
        key=lambda item: (
            item.source.value,
            item.partition.dataset,
            item.partition.parameters,
            item.partition.partition_id,
        ),
    )


def _deferred_requirements(
    exact_numbers: tuple[str, ...],
    *,
    needs_status: bool,
    needs_reviews: bool,
) -> tuple[DeferredSourceRequirement, ...]:
    requirements: list[DeferredSourceRequirement] = []
    if needs_status and not exact_numbers:
        requirements.append(
            DeferredSourceRequirement(
                OfficialSourceKind.BILL_STATUS,
                "after_bill_metadata_relevance_filter",
                ("BILL_NO",),
                None,
                "one_exact_BILL_NO_partition_per_relevant_unique_bill",
            )
        )
    if needs_reviews:
        requirements.append(
            DeferredSourceRequirement(
                OfficialSourceKind.REVIEW_REPORT_INDEX,
                "after_bill_metadata_relevance_filter",
                ("BILL_NO", "BILL_ID_or_DETAIL_LINK.billId"),
                len(exact_numbers) if exact_numbers else None,
                "one_index_check_per_relevant_unique_bill",
            )
        )
        requirements.append(
            DeferredSourceRequirement(
                OfficialSourceKind.REVIEW_REPORT_PDF,
                "after_review_report_index",
                ("official_url",),
                None,
                "every_distinct_official_review_report_pdf_link",
            )
        )
    return tuple(requirements)


def _term_key(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", value.casefold())


def _strip_query_particle(value: str) -> str:
    for particle in _QUERY_PARTICLES:
        if value.endswith(particle) and len(value) >= len(particle) + 2:
            return value[: -len(particle)]
    return value


__all__ = [
    "DEFAULT_ASSEMBLY_TERM_BOUNDS",
    "OFFICIAL_RESEARCH_SOURCES",
    "DeferredSourceRequirement",
    "MonthBucket",
    "OfficialSource",
    "OfficialSourceKind",
    "PartitionCoverageExpectation",
    "PartitionSearchTerm",
    "PlannedMetadataPartition",
    "ResearchPartitionPlan",
    "ResearchPartitionPlanner",
    "SearchTermRole",
    "plan_partitions",
]
