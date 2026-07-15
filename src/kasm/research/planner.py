"""Deterministic planning of a user's legislative research scope.

The planner intentionally does not guess topics, committees, or dates with an
LLM.  It records only scope that is explicit in the request, plus the configured
window for the word ``recent``.  The original request remains the contract
query; the separately prepared query is the normalized retrieval input.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from kasm.research.assembly_terms import (
    DEFAULT_ASSEMBLY_TERM_BOUNDS,
    assembly_terms_intersecting,
)
from kasm.research.contracts import (
    DEFAULT_EVIDENCE_TYPES,
    EvidenceType,
    ResearchContract,
    ResearchIntent,
)
from kasm.research.proposers import extract_proposer_query_scope
from kasm.search.bilingual import PreparedQuery, prepare_query
from kasm.search.terminology import LEGAL_TERMINOLOGY, TermCategory

_ISO_DATE = re.compile(
    r"(?<!\d)(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})(?!\d)"
)
_KOREAN_DATE = re.compile(
    r"(?<!\d)(?P<year>\d{4})\s*년\s*(?P<month>\d{1,2})\s*월\s*"
    r"(?P<day>\d{1,2})\s*일"
)
_CURRENT = re.compile(r"현재까지|지금까지")
_RECENT = re.compile(r"최근")
_BILL_NUMBER = re.compile(r"(?<!\d)\d{7}(?!\d)")
_YEAR_MONTH = re.compile(
    r"(?<!\d)(?P<year>(?:19|20)\d{2})\s*년\s*(?P<month>1[0-2]|0?[1-9])\s*월"
)
_YEAR_ONLY = re.compile(
    r"(?<!\d)(?P<year>(?:19|20)\d{2})\s*년(?!\s*\d{1,2}\s*월)"
)
_KOREAN_INHERITED_DAY_RANGE = re.compile(
    r"(?<!\d)(?P<start_year>(?:19|20)\d{2})\s*년\s*"
    r"(?P<start_month>\d{1,2})\s*월\s*(?P<start_day>\d{1,2})\s*일\s*"
    r"(?:부터|에서)\s*"
    r"(?:(?P<end_year>(?:19|20)\d{2})\s*년\s*)?"
    r"(?P<end_month>\d{1,2})\s*월\s*(?P<end_day>\d{1,2})\s*일\s*까지"
)
_KOREAN_MONTH_RANGE = re.compile(
    r"(?<!\d)(?P<start_year>(?:19|20)\d{2})\s*년\s*"
    r"(?P<start_month>1[0-2]|0?[1-9])\s*월\s*(?:부터|에서)\s*"
    r"(?:(?P<end_year>(?:19|20)\d{2})\s*년\s*)?"
    r"(?P<end_month>1[0-2]|0?[1-9])\s*월\s*까지"
)
_KOREAN_YEAR_RANGE = re.compile(
    r"(?<!\d)(?P<start_year>(?:19|20)\d{2})\s*년\s*(?:부터|에서)\s*"
    r"(?P<end_year>(?:19|20)\d{2})\s*년\s*까지"
)
_RELATIVE_MONTHS = re.compile(r"(?:최근|지난)\s*(?P<count>\d{1,3})\s*개월")
_RELATIVE_YEARS = re.compile(r"(?:최근|지난)\s*(?P<count>\d{1,2})\s*년")
_THIS_YEAR = re.compile(r"올해|금년")
_LAST_YEAR = re.compile(r"작년|지난해")
_THIS_MONTH = re.compile(r"이번\s*달|이달")
_LAST_MONTH = re.compile(r"지난\s*달|지난달")
_ASSEMBLY_TERM = re.compile(
    r"(?<!\d)(?:제\s*)?(?P<term>[1-9]|1\d|2[0-2])\s*대(?:\s*국회)?"
)
_CONSTITUENT_ASSEMBLY = re.compile(r"제헌(?:\s*국회)?")
_ASSEMBLY_TERM_RANGE = re.compile(
    r"(?<!\d)(?:제\s*)?(?P<start>[1-9]|1\d|2[0-2])\s*대?\s*"
    r"(?:부터|에서|~|～|〜|\-|–|—)\s*"
    r"(?:제\s*)?(?P<end>[1-9]|1\d|2[0-2])\s*대(?:\s*국회)?(?:\s*까지)?"
)
_ASSEMBLY_RANGE_CONNECTOR = re.compile(r"(?:부터|에서|~|～|〜|\-|–|—)")

# Current 22nd Assembly names are first.  Former names are retained because a
# query can explicitly request an earlier period.  Matching is literal: topic
# words such as "검찰" never silently infer a committee.
OFFICIAL_COMMITTEE_NAMES = (
    "국회운영위원회",
    "법제사법위원회",
    "정무위원회",
    "재정경제기획위원회",
    "기획재정위원회",
    "교육위원회",
    "과학기술정보방송통신위원회",
    "외교통일위원회",
    "국방위원회",
    "행정안전위원회",
    "문화체육관광위원회",
    "농림축산식품해양수산위원회",
    "산업통상자원중소벤처기업위원회",
    "보건복지위원회",
    "기후에너지환경노동위원회",
    "환경노동위원회",
    "국토교통위원회",
    "정보위원회",
    "성평등가족위원회",
    "여성가족위원회",
    "예산결산특별위원회",
    "윤리특별위원회",
)

_INTENT_PATTERNS: tuple[tuple[ResearchIntent, tuple[re.Pattern[str], ...]], ...] = (
    (
        ResearchIntent.DISCOVER,
        (
            re.compile(r"관련\s*(?:법안|입법)|어떤\s*(?:법안|입법)|법안\s*목록"),
            re.compile(r"찾아(?:줘|주세요)?|검색해?(?:줘|주세요)?"),
            re.compile(r"\b(?:discover|find|list)\b", re.IGNORECASE),
        ),
    ),
    (
        ResearchIntent.TRACK_STATUS,
        (
            re.compile(r"상태|진행\s*상황|어디까지"),
            re.compile(r"계류|가결|부결|폐기|통과(?:됐|되었|했)"),
            re.compile(
                r"\bcurrent status\b|\btrack(?:ing)?\s+(?:the\s+)?status\b|"
                r"\bwhere\s+(?:does|is)\b.*\bstand",
                re.IGNORECASE,
            ),
        ),
    ),
    (
        ResearchIntent.TIMELINE,
        (
            re.compile(r"시계열|시간순|연대기|논의\s*흐름|처리\s*경과"),
            re.compile(r"\btimeline\b|\bchronolog(?:y|ical|ically)\b", re.IGNORECASE),
        ),
    ),
    (
        ResearchIntent.EXPLAIN_ISSUES,
        (
            re.compile(r"왜|이유|배경|쟁점|막혔|막힌|문제점|무엇을\s*지적"),
            re.compile(r"\bwhy\b|\breasons?\b|\bissues?\b", re.IGNORECASE),
        ),
    ),
    (
        ResearchIntent.COMPARE_POSITIONS,
        (
            re.compile(r"찬반|찬성|반대|입장|견해|밀고|막았"),
            re.compile(
                r"\barguments? for and against\b|\boppos(?:e|ed|ing|ition)\b|"
                r"\bcompare\b.*\bpositions?\b",
                re.IGNORECASE,
            ),
        ),
    ),
    (
        ResearchIntent.QUOTE_EVIDENCE,
        (
            re.compile(
                r"발언|인용|원문|회의록|속기록|질의\s*[·ㆍ‧・]?\s*답변|"
                r"정부(?:는|가|에서는|의)?\s*(?:어떻게|뭐라고|무엇이라고)?\s*답|"
                r"어떻게\s*답했|뭐라고\s*(?:했|말했)"
            ),
            re.compile(
                r"\bquote\b|\bverbatim\b|\bminutes\b|\bgovernment responses?\b|"
                r"\bwhat\b.*\b(?:say|said)\b",
                re.IGNORECASE,
            ),
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class IntentEvidence:
    """Deterministic query spans supporting one interpreted analysis mode."""

    intent: ResearchIntent
    matched_phrases: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "matched_phrases": list(self.matched_phrases),
        }


@dataclass(frozen=True, slots=True)
class InterpretedScope:
    """Human- and machine-readable account of how a request was interpreted."""

    original_query: str
    search_query: str
    query_language: str
    translation_mode: str
    date_from: date | None
    date_to: date | None
    date_interpretation: str
    recent_months: int
    assembly_term: int
    assembly_terms: tuple[int, ...]
    assembly_term_explicit: bool
    bill_numbers: tuple[str, ...]
    committees: tuple[str, ...]
    representative_proposer_names: tuple[str, ...]
    co_proposer_names: tuple[str, ...]
    proposer_names: tuple[str, ...]
    evidence_types: tuple[EvidenceType, ...]
    intents: tuple[ResearchIntent, ...]
    intent_evidence: tuple[IntentEvidence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_query": self.original_query,
            "search_query": self.search_query,
            "query_language": self.query_language,
            "translation_mode": self.translation_mode,
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "date_interpretation": self.date_interpretation,
            "recent_months": self.recent_months,
            "assembly_term": self.assembly_term,
            "assembly_terms": list(self.assembly_terms),
            "assembly_term_explicit": self.assembly_term_explicit,
            "bill_numbers": list(self.bill_numbers),
            "committees": list(self.committees),
            "representative_proposer_names": list(
                self.representative_proposer_names
            ),
            "co_proposer_names": list(self.co_proposer_names),
            "proposer_names": list(self.proposer_names),
            "evidence_types": [item.value for item in self.evidence_types],
            "intents": [item.value for item in self.intents],
            "intent_evidence": [item.to_dict() for item in self.intent_evidence],
        }


@dataclass(frozen=True, slots=True)
class ResearchPlan:
    """A validated contract together with its deterministic retrieval query."""

    contract: ResearchContract
    prepared_query: PreparedQuery
    interpreted_scope: InterpretedScope

    @property
    def search_query(self) -> str:
        return self.prepared_query.search_query

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract.canonical_payload(),
            "interpreted_scope": self.interpreted_scope.to_dict(),
        }


class ResearchContractPlanner:
    """Build complete research contracts without probabilistic interpretation."""

    def __init__(self, *, recent_months: int = 6, assembly_term: int = 22) -> None:
        if recent_months < 1:
            raise ValueError("recent_months must be positive")
        if assembly_term < 1:
            raise ValueError("assembly_term must be positive")
        self.recent_months = recent_months
        self.assembly_term = assembly_term

    def plan(
        self,
        query: str,
        *,
        as_of: datetime | None = None,
        korean_query: str | None = None,
        assembly_term: int | None = None,
        committees: Sequence[str] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        evidence_types: Sequence[EvidenceType | str] | None = None,
        ordering: str = "chronological",
        completeness: str = "comprehensive",
    ) -> ResearchPlan:
        observed_at = as_of or datetime.now(UTC)
        if observed_at.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")

        prepared = prepare_query(query, korean_query)
        dates = _explicit_dates(prepared.original)
        interpreted_from, interpreted_to, date_interpretation = _date_scope(
            prepared.original,
            dates,
            observed_at.date(),
            self.recent_months,
        )
        if date_from is not None or date_to is not None:
            interpreted_from = date_from
            interpreted_to = (
                date_to
                if date_to is not None
                else observed_at.date() if date_from is not None else None
            )
            date_interpretation = (
                "structured_start_to_current"
                if date_from is not None and date_to is None
                else "structured_override"
            )
        if interpreted_from and interpreted_to and interpreted_from > interpreted_to:
            raise ValueError("date_from must be on or before date_to")
        bill_numbers = _ordered_unique(_BILL_NUMBER.findall(prepared.original))
        proposer_scope = extract_proposer_query_scope(prepared.original)
        interpreted_committees = _explicit_committees(
            prepared.original,
            prepared.search_query,
        )
        if committees is not None:
            interpreted_committees = _ordered_unique(
                tuple(value.strip() for value in committees if value.strip())
            )
        query_terms = _explicit_assembly_terms(prepared.original)
        explicit_terms = (
            (assembly_term,)
            if assembly_term is not None
            else query_terms
        )
        term_is_explicit = bool(explicit_terms)
        interpreted_term = explicit_terms[-1] if explicit_terms else self.assembly_term
        if interpreted_term < 1:
            raise ValueError("assembly_term must be positive")
        bill_terms = tuple(sorted({int(number[:2]) for number in bill_numbers}))
        unsupported_bill_terms = tuple(
            term for term in bill_terms if term not in DEFAULT_ASSEMBLY_TERM_BOUNDS
        )
        if unsupported_bill_terms:
            raise ValueError("bill number belongs to an unsupported Assembly term")
        if bill_terms:
            if explicit_terms and not set(bill_terms).issubset(explicit_terms):
                raise ValueError("bill number conflicts with the explicit Assembly term")
            if interpreted_from is not None or interpreted_to is not None:
                effective_to = min(interpreted_to or observed_at.date(), observed_at.date())
                if any(
                    (interpreted_from or bounds[0]) > min(bounds[1], effective_to)
                    or effective_to < bounds[0]
                    for term in bill_terms
                    for bounds in (DEFAULT_ASSEMBLY_TERM_BOUNDS[term],)
                ):
                    raise ValueError("bill number conflicts with the requested date range")
            # The first two digits of an official seven-digit bill number bind
            # its Assembly term. This exact identity outranks the configured
            # current-term default and prevents a valid historical bill from
            # being queried against the wrong AGE partition.
            if interpreted_from is None and interpreted_to is None:
                interpreted_terms = explicit_terms or _validated_supported_terms(
                    bill_terms
                )
                term_is_explicit = True
            else:
                scoped_terms = (
                    explicit_terms
                    if explicit_terms
                    else _assembly_terms_for_scope(
                        configured_term=interpreted_term,
                        explicit=False,
                        date_from=interpreted_from,
                        date_to=interpreted_to,
                        as_of=observed_at.date(),
                    )
                )
                interpreted_terms = _validated_supported_terms(
                    (*scoped_terms, *bill_terms)
                )
            interpreted_term = interpreted_terms[-1]
        else:
            interpreted_terms = explicit_terms or _assembly_terms_for_scope(
                configured_term=interpreted_term,
                explicit=False,
                date_from=interpreted_from,
                date_to=interpreted_to,
                as_of=observed_at.date(),
            )
        # ``assembly_term`` remains the backwards-compatible primary term.  For
        # an unconstrained historical range it is the newest term in the actual
        # collection scope; ``assembly_terms`` is authoritative and lossless.
        interpreted_term = interpreted_terms[-1]
        intents, intent_evidence = _research_intents(prepared.original)
        requested_evidence = (
            tuple(EvidenceType(item) for item in evidence_types)
            if evidence_types is not None
            else DEFAULT_EVIDENCE_TYPES
        )

        contract = ResearchContract(
            # Keep the user's complete request as the semantic scope.  In particular,
            # prepare_query intentionally reduces some English bill queries to a bill
            # number, which is useful for retrieval but would be lossy as a contract.
            query=prepared.original,
            as_of=observed_at,
            date_from=interpreted_from,
            date_to=interpreted_to,
            assembly_term=interpreted_term,
            assembly_terms=interpreted_terms,
            committees=interpreted_committees,
            bill_numbers=bill_numbers,
            representative_proposer_names=(
                proposer_scope.representative_proposer_names
            ),
            co_proposer_names=proposer_scope.co_proposer_names,
            proposer_names=proposer_scope.proposer_names,
            evidence_types=requested_evidence,
            intents=intents,
            ordering=ordering,
            completeness=completeness,
        )
        interpreted = InterpretedScope(
            original_query=prepared.original,
            search_query=prepared.search_query,
            query_language=prepared.language,
            translation_mode=prepared.translation_mode,
            date_from=interpreted_from,
            date_to=interpreted_to,
            date_interpretation=date_interpretation,
            recent_months=self.recent_months,
            assembly_term=interpreted_term,
            assembly_terms=interpreted_terms,
            assembly_term_explicit=term_is_explicit,
            bill_numbers=bill_numbers,
            committees=interpreted_committees,
            representative_proposer_names=(
                proposer_scope.representative_proposer_names
            ),
            co_proposer_names=proposer_scope.co_proposer_names,
            proposer_names=proposer_scope.proposer_names,
            evidence_types=requested_evidence,
            intents=intents,
            intent_evidence=intent_evidence,
        )
        return ResearchPlan(contract, prepared, interpreted)


def _assembly_terms_for_scope(
    *,
    configured_term: int,
    explicit: bool,
    date_from: date | None,
    date_to: date | None,
    as_of: date,
) -> tuple[int, ...]:
    """Return every Assembly term intersecting an unconstrained date scope.

    A term supplied through structured input or written as ``제21대`` is a
    hard constraint even when the accompanying calendar dates extend beyond
    it.  Otherwise a bounded date request is allowed to cross term boundaries;
    the partition layer will clip only the outer unsupported/as-of boundaries.
    """

    if explicit or date_from is None:
        return (configured_term,)
    effective_to = min(date_to or as_of, as_of)
    overlapping = tuple(
        item.number
        for item in assembly_terms_intersecting(date_from, effective_to)
    )
    if overlapping:
        return overlapping
    raise ValueError("requested date range does not overlap a supported Assembly term")


def _validated_supported_terms(values: Sequence[int]) -> tuple[int, ...]:
    """Return each requested official term once without inventing intervening scope."""

    selected = tuple(sorted(set(values)))
    if not selected:
        raise ValueError("Assembly term scope must not be empty")
    if any(term not in DEFAULT_ASSEMBLY_TERM_BOUNDS for term in selected):
        raise ValueError("Assembly term scope contains an unsupported term")
    return selected


def plan_research(
    query: str,
    *,
    as_of: datetime | None = None,
    korean_query: str | None = None,
    recent_months: int = 6,
    assembly_term: int | None = None,
    committees: Sequence[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    evidence_types: Sequence[EvidenceType | str] | None = None,
    ordering: str = "chronological",
    completeness: str = "comprehensive",
) -> ResearchPlan:
    """Convenience entry point for one deterministic planning operation."""

    return ResearchContractPlanner(
        recent_months=recent_months,
        assembly_term=22 if assembly_term is None else assembly_term,
    ).plan(
        query,
        as_of=as_of,
        korean_query=korean_query,
        assembly_term=assembly_term,
        committees=committees,
        date_from=date_from,
        date_to=date_to,
        evidence_types=evidence_types,
        ordering=ordering,
        completeness=completeness,
    )


def _explicit_dates(query: str) -> tuple[date, ...]:
    matches: list[tuple[int, date]] = []
    for pattern in (_ISO_DATE, _KOREAN_DATE):
        for match in pattern.finditer(query):
            try:
                parsed = date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
            except ValueError as exc:
                raise ValueError(f"invalid explicit date: {match.group(0)}") from exc
            matches.append((match.start(), parsed))
    matches.sort(key=lambda item: item[0])
    return tuple(value for _, value in matches)


def _date_scope(
    query: str,
    explicit_dates: tuple[date, ...],
    current_date: date,
    recent_months: int,
) -> tuple[date | None, date | None, str]:
    if len(explicit_dates) > 2:
        raise ValueError("a research scope can contain at most two explicit dates")
    if len(explicit_dates) == 2:
        return explicit_dates[0], explicit_dates[1], "explicit_range"
    korean_range = _korean_period_range(query)
    if korean_range is not None:
        return korean_range
    if len(explicit_dates) == 1 and _CURRENT.search(query):
        return explicit_dates[0], current_date, "explicit_start_to_current"
    if len(explicit_dates) == 1:
        return explicit_dates[0], explicit_dates[0], "explicit_date"
    natural_scope = _natural_date_scope(query, current_date)
    if natural_scope is not None:
        return natural_scope
    if _RECENT.search(query) or _CURRENT.search(query):
        interpretation = (
            "recent_default" if _RECENT.search(query) else "current_with_recent_default"
        )
        return (
            _subtract_calendar_months(current_date, recent_months),
            current_date,
            interpretation,
        )
    return None, None, "unbounded"


def _korean_period_range(query: str) -> tuple[date, date, str] | None:
    """Parse common Korean ranges whose right boundary inherits its year.

    Full dates with a year on both sides are already handled by
    :func:`_explicit_dates`.  These patterns cover the equally common shortened
    forms, such as ``2026년 1월 1일부터 3월 31일까지`` and
    ``2026년 1월부터 3월까지``, without treating only the first period as the
    requested scope.
    """

    day_range = _KOREAN_INHERITED_DAY_RANGE.search(query)
    if day_range:
        start_year = int(day_range.group("start_year"))
        end_year_text = day_range.group("end_year")
        end_year = int(end_year_text) if end_year_text else start_year
        start = _validated_date(
            start_year,
            int(day_range.group("start_month")),
            int(day_range.group("start_day")),
            day_range.group(0),
        )
        end = _validated_date(
            end_year,
            int(day_range.group("end_month")),
            int(day_range.group("end_day")),
            day_range.group(0),
        )
        _validate_natural_range(start, end)
        return start, end, "explicit_korean_day_range"

    month_range = _KOREAN_MONTH_RANGE.search(query)
    if month_range:
        start_year = int(month_range.group("start_year"))
        end_year_text = month_range.group("end_year")
        end_year = int(end_year_text) if end_year_text else start_year
        start_month = int(month_range.group("start_month"))
        end_month = int(month_range.group("end_month"))
        start = date(start_year, start_month, 1)
        end = date(
            end_year,
            end_month,
            calendar.monthrange(end_year, end_month)[1],
        )
        _validate_natural_range(start, end)
        return start, end, "explicit_calendar_month_range"

    year_range = _KOREAN_YEAR_RANGE.search(query)
    if year_range:
        start_year = int(year_range.group("start_year"))
        end_year = int(year_range.group("end_year"))
        start = date(start_year, 1, 1)
        end = date(end_year, 12, 31)
        _validate_natural_range(start, end)
        return start, end, "explicit_calendar_year_range"
    return None


def _validated_date(year: int, month: int, day: int, source: str) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValueError(f"invalid explicit date range: {source}") from exc


def _validate_natural_range(date_from: date, date_to: date) -> None:
    if date_from > date_to:
        raise ValueError("date_from must be on or before date_to")


def _natural_date_scope(
    query: str,
    current_date: date,
) -> tuple[date, date, str] | None:
    relative_months = _RELATIVE_MONTHS.search(query)
    if relative_months:
        count = int(relative_months.group("count"))
        if not 1 <= count <= 120:
            raise ValueError("relative month range must be between 1 and 120 months")
        return (
            _subtract_calendar_months(current_date, count),
            current_date,
            f"relative_{count}_months_to_current",
        )
    relative_years = _RELATIVE_YEARS.search(query)
    if relative_years:
        count = int(relative_years.group("count"))
        if not 1 <= count <= 10:
            raise ValueError("relative year range must be between 1 and 10 years")
        return (
            _subtract_calendar_months(current_date, count * 12),
            current_date,
            f"relative_{count}_years_to_current",
        )
    if _THIS_YEAR.search(query):
        return date(current_date.year, 1, 1), current_date, "current_year_to_date"
    if _LAST_YEAR.search(query):
        year = current_date.year - 1
        return date(year, 1, 1), date(year, 12, 31), "previous_calendar_year"
    if _THIS_MONTH.search(query):
        return date(current_date.year, current_date.month, 1), current_date, "current_month_to_date"
    if _LAST_MONTH.search(query):
        previous_end = date(current_date.year, current_date.month, 1) - timedelta(days=1)
        return (
            date(previous_end.year, previous_end.month, 1),
            previous_end,
            "previous_calendar_month",
        )
    year_month = _YEAR_MONTH.search(query)
    if year_month:
        year = int(year_month.group("year"))
        month = int(year_month.group("month"))
        start = date(year, month, 1)
        if _CURRENT.search(query):
            return start, current_date, "explicit_month_start_to_current"
        end = date(year, month, calendar.monthrange(year, month)[1])
        if start <= current_date <= end:
            end = current_date
        return start, end, "explicit_calendar_month"
    year_only = _YEAR_ONLY.search(query)
    if year_only:
        year = int(year_only.group("year"))
        start = date(year, 1, 1)
        if _CURRENT.search(query) or year == current_date.year:
            return start, current_date, "explicit_year_to_current"
        return start, date(year, 12, 31), "explicit_calendar_year"
    return None


def _subtract_calendar_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _explicit_committees(original: str, search_query: str) -> tuple[str, ...]:
    located: list[tuple[int, str]] = []
    seen: set[str] = set()
    for source_offset, text in ((0, original), (len(original) + 1, search_query)):
        expansion = LEGAL_TERMINOLOGY.expand(text, include_related=False)
        for item in expansion.expansions:
            if item.category is TermCategory.COMMITTEE and item.term not in seen:
                located.append((source_offset, item.term))
                seen.add(item.term)
        for committee in OFFICIAL_COMMITTEE_NAMES:
            position = text.find(committee)
            if position >= 0 and committee not in seen:
                located.append((source_offset + position, committee))
                seen.add(committee)
    located.sort(key=lambda item: item[0])
    return tuple(committee for _, committee in located)


def _explicit_assembly_terms(query: str) -> tuple[int, ...]:
    """Return literal terms, expanding only an explicit written range.

    ``제18대와 제22대`` is a two-term comparison and therefore does not
    silently search the intervening terms.  ``18대부터 22대까지`` is an
    explicit range and expands to every term from 18 through 22.
    """

    numeric_range = _ASSEMBLY_TERM_RANGE.search(query)
    if numeric_range is not None:
        start = int(numeric_range.group("start"))
        end = int(numeric_range.group("end"))
        if start > end:
            raise ValueError("Assembly term range must be in chronological order")
        return _validated_supported_terms(tuple(range(start, end + 1)))

    located: list[tuple[int, int, int]] = [
        (match.start(), match.end(), int(match.group("term")))
        for match in _ASSEMBLY_TERM.finditer(query)
    ]
    located.extend(
        (match.start(), match.end(), 1)
        for match in _CONSTITUENT_ASSEMBLY.finditer(query)
    )
    located.sort()
    terms = tuple(dict.fromkeys(term for _start, _end, term in located))
    if len(terms) <= 1:
        return terms

    # This also supports ``제헌국회부터 제5대까지`` where the first endpoint
    # has no numeric term to be consumed by the compact numeric-range regex.
    scope_text = query[located[0][1] : located[-1][0]]
    if _ASSEMBLY_RANGE_CONNECTOR.search(scope_text):
        if terms[0] > terms[-1]:
            raise ValueError("Assembly term range must be in chronological order")
        return _validated_supported_terms(tuple(range(terms[0], terms[-1] + 1)))
    return _validated_supported_terms(terms)


def _ordered_unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _research_intents(
    query: str,
) -> tuple[tuple[ResearchIntent, ...], tuple[IntentEvidence, ...]]:
    evidence: list[IntentEvidence] = []
    for intent, patterns in _INTENT_PATTERNS:
        located: list[tuple[int, str]] = []
        for pattern in patterns:
            located.extend((match.start(), match.group(0)) for match in pattern.finditer(query))
        if located:
            located.sort(key=lambda item: item[0])
            matched = _ordered_unique(tuple(phrase for _, phrase in located))
            evidence.append(IntentEvidence(intent, matched))

    if not evidence:
        fallback = IntentEvidence(
            ResearchIntent.DISCOVER,
            ("default: no explicit analysis mode",),
        )
        return (ResearchIntent.DISCOVER,), (fallback,)
    return tuple(item.intent for item in evidence), tuple(evidence)
