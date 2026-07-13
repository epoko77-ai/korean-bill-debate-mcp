"""Deterministic, fail-closed relevance scoring for research candidates.

The research pipeline receives records from several official endpoints.  Their
field names differ, and a meeting record can carry several agenda items.  This
module turns those records into one conservative relevance decision without
using an LLM or a semantic similarity score.

Structured scope is deliberately stronger than text similarity:

* a requested bill number must occur exactly on the record or one of its agendas;
* committee and date scopes are hard filters;
* generic research words never contribute to relevance; and
* a candidate without enough specific lexical evidence is rejected.

Semantic retrieval can rank the survivors later, but it must not override these
guards or create a bill relationship that the official metadata does not show.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from kasm.search.terminology import (
    LEGAL_TERMINOLOGY,
    TERMINOLOGY_VERSION,
    TermCategory,
    TermExpansion,
    TermRelation,
)

_BILL_NUMBER = re.compile(r"(?<!\d)\d{7}(?!\d)")
_STATUTE = re.compile(r"[가-힣]{2,30}(?:기본)?법(?=$|[^가-힣])")
_DATE = re.compile(r"(?P<year>20\d{2})[-./년 ](?P<month>\d{1,2})[-./월 ](?P<day>\d{1,2})")
_QUERY_WORD = re.compile(r"[0-9A-Za-z가-힣]+")
_HANGUL = re.compile(r"[가-힣]")
_DIGIT = re.compile(r"\d")

DEFAULT_MINIMUM_SCORE: Final = 10

# A match on these words describes the requested output, not the subject being
# investigated.  Korean particles are handled by ``_is_generic_term`` below.
_GENERIC_TERMS: Final = frozenset(
    {
        "관련",
        "결과",
        "검색",
        "개선",
        "국회",
        "내용",
        "대상",
        "대책",
        "대해",
        "대한",
        "문서",
        "방안",
        "발언",
        "법률",
        "법률안",
        "법안",
        "기본법",
        "보고서",
        "보장",
        "보호",
        "부터",
        "상태",
        "시간순",
        "올해",
        "원문",
        "의안",
        "입법",
        "자료",
        "전체",
        "쟁점",
        "정리",
        "지원",
        "조사",
        "조회",
        "최근",
        "처리",
        "현재",
        "확인",
        "확인해줘",
        "알려줘",
        "보여줘",
        "회의",
        "회의록",
        "확보",
        "논의",
        "검토보고서",
        "전문위원",
        "소위원회",
        "정부",
        "답변",
        "bill",
        "bills",
        "current",
        "debate",
        "debates",
        "legislation",
        "minutes",
        "recent",
        "report",
        "reports",
        "status",
    }
)
_KOREAN_PARTICLES: Final = (
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

_TITLE_FIELDS: Final = (
    "name",
    "title",
    "bill_name",
    "bill_title",
    "BILL_NAME",
    "BILL_NM",
)
_BODY_FIELDS: Final = (
    "summary",
    "description",
    "content",
    "text",
    "law_title",
    "statute",
    "status",
    "process_result",
)
_COMMITTEE_FIELDS: Final = (
    "committee",
    "committee_name",
    "committee_name_ko",
    "COMM_NAME",
    "CMIT_NM",
    "SB_CMIT_NM",
)
_DATE_FIELDS: Final = (
    "date",
    "meeting_date",
    "proposed_date",
    "proposal_date",
    "propose_date",
    "RGS_PROC_DT",
    "PROPOSE_DT",
)
_ID_FIELDS: Final = ("id", "candidate_id", "bill_id", "meeting_id", "official_url")


@dataclass(frozen=True, slots=True)
class RelevanceCriteria:
    """Structured relevance scope derived from a user's research request."""

    query: str
    bill_numbers: tuple[str, ...] = ()
    statute_terms: tuple[str, ...] = ()
    issue_terms: tuple[str, ...] = ()
    related_statute_terms: tuple[str, ...] = ()
    related_issue_terms: tuple[str, ...] = ()
    committees: tuple[str, ...] = ()
    date_from: date | None = None
    date_to: date | None = None
    minimum_score: int = DEFAULT_MINIMUM_SCORE
    terminology_version: str = TERMINOLOGY_VERSION
    terminology_expansions: tuple[TermExpansion, ...] = ()

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("query must not be empty")
        if any(not _is_bill_number(number) for number in self.bill_numbers):
            raise ValueError("bill numbers must contain exactly seven digits")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        if self.minimum_score < 1:
            raise ValueError("minimum_score must be positive")
        if self.terminology_version != TERMINOLOGY_VERSION:
            raise ValueError("relevance criteria uses an unsupported terminology version")

    @property
    def expansion_reasons(self) -> tuple[str, ...]:
        """Explain every equivalent or related term introduced by the registry."""

        return tuple(expansion.reason for expansion in self.terminology_expansions)

    @classmethod
    def from_query(
        cls,
        query: str,
        *,
        bill_numbers: Sequence[str] = (),
        statute_terms: Sequence[str] = (),
        issue_terms: Sequence[str] = (),
        committees: Sequence[str] = (),
        date_from: date | None = None,
        date_to: date | None = None,
        minimum_score: int = DEFAULT_MINIMUM_SCORE,
    ) -> RelevanceCriteria:
        """Build criteria while conservatively extracting known query concepts."""

        terminology = LEGAL_TERMINOLOGY.expand(query, include_related=True)
        normalized_query = _normalize_text(query)
        extracted_numbers = tuple(_BILL_NUMBER.findall(query))
        extracted_statutes = tuple(_STATUTE.findall(normalized_query))
        equivalent_statutes = tuple(
            expansion.term
            for expansion in terminology.expansions
            if expansion.relation is TermRelation.EQUIVALENT
            and expansion.category is TermCategory.STATUTE
        )
        equivalent_issues = tuple(
            expansion.term
            for expansion in terminology.expansions
            if expansion.relation is TermRelation.EQUIVALENT
            and expansion.category is TermCategory.ISSUE
        )
        related_statutes = tuple(
            expansion.term
            for expansion in terminology.expansions
            if expansion.relation is TermRelation.RELATED
            and expansion.category is TermCategory.STATUTE
        )
        related_issues = tuple(
            expansion.term
            for expansion in terminology.expansions
            if expansion.relation is TermRelation.RELATED
            and expansion.category is TermCategory.ISSUE
        )
        literal_issues = _literal_issue_terms(
            query,
            excluded=(
                *statute_terms,
                *extracted_statutes,
                *equivalent_statutes,
                *equivalent_issues,
                *related_statutes,
                *related_issues,
                *committees,
            ),
        )
        return cls(
            query=query,
            bill_numbers=_distinct((*bill_numbers, *extracted_numbers)),
            statute_terms=_meaningful_terms(
                (*statute_terms, *extracted_statutes, *equivalent_statutes)
            ),
            # The registry improves known legal synonyms, but it is not a list
            # of subjects this MCP is allowed to research.  Preserve vetted
            # Korean literals as first-class issue terms so an unfamiliar
            # topic (for example 딥페이크, 플랫폼 노동, or 기후 적응) is not
            # rejected merely because it is absent from the curated registry.
            issue_terms=_meaningful_terms(
                (*issue_terms, *equivalent_issues, *literal_issues)
            ),
            related_statute_terms=_meaningful_terms(related_statutes),
            related_issue_terms=_meaningful_terms(related_issues),
            committees=_distinct(committees),
            date_from=date_from,
            date_to=date_to,
            minimum_score=minimum_score,
            terminology_expansions=terminology.expansions,
        )


@dataclass(frozen=True, slots=True)
class RelevanceResult:
    """The auditable relevance decision for one original candidate mapping."""

    candidate: Mapping[str, object]
    candidate_id: str
    score: int
    relevant: bool
    match_reasons: tuple[str, ...]
    rejection_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _CandidateText:
    title: str
    agenda: str
    body: str


def evaluate_candidate(
    candidate: Mapping[str, object], criteria: RelevanceCriteria
) -> RelevanceResult:
    """Evaluate one candidate, applying structured filters before text scoring."""

    candidate_id = _candidate_id(candidate)
    bill_numbers = _candidate_bill_numbers(candidate)
    requested_numbers = set(criteria.bill_numbers)
    if requested_numbers and requested_numbers.isdisjoint(bill_numbers):
        return _rejected(candidate, candidate_id, "bill_no_mismatch")

    matched_committee = _matching_committee(candidate, criteria.committees)
    if criteria.committees and matched_committee is None:
        return _rejected(candidate, candidate_id, "committee_mismatch")

    candidate_dates = _candidate_dates(candidate)
    matched_date = _matching_date(candidate_dates, criteria.date_from, criteria.date_to)
    if (criteria.date_from or criteria.date_to) and matched_date is None:
        reason = "date_missing" if not candidate_dates else "date_out_of_range"
        return _rejected(candidate, candidate_id, reason)

    reasons: list[str] = []
    score = 0
    if requested_numbers:
        number = sorted(requested_numbers.intersection(bill_numbers))[0]
        score += 100
        reasons.append(f"bill_no_exact:{number}")

    if matched_committee is not None:
        reasons.append(f"committee_exact:{matched_committee}")
    if matched_date is not None and (criteria.date_from or criteria.date_to):
        reasons.append(f"date_in_range:{matched_date.isoformat()}")

    texts = _candidate_text(candidate)
    statutes = _meaningful_terms(criteria.statute_terms)
    issues = _meaningful_terms(criteria.issue_terms)
    related_statutes = _nonoverlapping_related(
        _meaningful_terms(criteria.related_statute_terms), (*statutes, *issues)
    )
    related_issues = _nonoverlapping_related(
        _meaningful_terms(criteria.related_issue_terms), (*statutes, *issues)
    )
    for kind, terms, weights in (
        # A direct reviewed concept must outrank any combination of merely
        # related concepts.  This prevents a document mentioning both a nearby
        # statute and a neighboring issue from displacing an exact user term.
        ("statute", statutes, {"title": 36, "agenda": 40, "body": 24}),
        ("issue", issues, {"title": 30, "agenda": 34, "body": 22}),
        (
            "related_statute",
            related_statutes,
            {"title": 12, "agenda": 14, "body": 6},
        ),
        (
            "related_issue",
            related_issues,
            {"title": 11, "agenda": 13, "body": 5},
        ),
    ):
        for term in terms:
            source = _best_source(term, texts, weights)
            if source is None:
                continue
            score += weights[source]
            reasons.append(f"{kind}:{term}@{source}")

    # Bill-number lookup is itself conclusive.  All other searches require at
    # least one non-generic term and must clear the configured threshold.
    meaningful = bool(statutes or issues or related_statutes or related_issues)
    if requested_numbers:
        relevant = score >= 100
    elif not meaningful and (matched_committee is not None or matched_date is not None):
        # A topic-free structured request such as "법사위의 올해 회의록" is
        # intentionally exhaustive inside its hard committee/date scope.
        score = criteria.minimum_score
        reasons.append("structured_scope_only")
        relevant = True
    elif not meaningful:
        return RelevanceResult(
            candidate=candidate,
            candidate_id=candidate_id,
            score=0,
            relevant=False,
            match_reasons=tuple(reasons),
            rejection_reasons=("no_meaningful_terms",),
        )
    else:
        relevant = score >= criteria.minimum_score

    rejection = () if relevant else ("below_minimum_score",)
    return RelevanceResult(
        candidate=candidate,
        candidate_id=candidate_id,
        score=score,
        relevant=relevant,
        match_reasons=tuple(reasons),
        rejection_reasons=rejection,
    )


def rank_candidates(
    candidates: Iterable[Mapping[str, object]], criteria: RelevanceCriteria
) -> tuple[RelevanceResult, ...]:
    """Return relevant candidates in a deterministic, evidence-first order."""

    results = (evaluate_candidate(candidate, criteria) for candidate in candidates)
    relevant = (result for result in results if result.relevant)
    return tuple(sorted(relevant, key=_result_sort_key))


def _result_sort_key(result: RelevanceResult) -> tuple[object, ...]:
    candidate_dates = _candidate_dates(result.candidate)
    newest = max(candidate_dates).toordinal() if candidate_dates else 0
    numbers = sorted(_candidate_bill_numbers(result.candidate))
    number = numbers[0] if numbers else ""
    title = _field_text(result.candidate, _TITLE_FIELDS)
    return (-result.score, -newest, number, result.candidate_id, title)


def _rejected(
    candidate: Mapping[str, object], candidate_id: str, reason: str
) -> RelevanceResult:
    return RelevanceResult(
        candidate=candidate,
        candidate_id=candidate_id,
        score=0,
        relevant=False,
        match_reasons=(),
        rejection_reasons=(reason,),
    )


def _candidate_text(candidate: Mapping[str, object]) -> _CandidateText:
    agenda_parts = [_string(candidate.get("agenda_text"))]
    raw_items = candidate.get("agenda_items")
    if isinstance(raw_items, Sequence) and not isinstance(raw_items, (str, bytes)):
        for item in raw_items:
            if isinstance(item, Mapping):
                agenda_parts.extend(
                    (
                        _string(item.get("bill_no")),
                        _string(item.get("BILL_NO")),
                        _string(item.get("title")),
                        _string(item.get("name")),
                    )
                )
            else:
                agenda_parts.append(_string(item))
    return _CandidateText(
        title=_normalize_text(_field_text(candidate, _TITLE_FIELDS)),
        agenda=_normalize_text(" ".join(part for part in agenda_parts if part)),
        body=_normalize_text(_field_text(candidate, _BODY_FIELDS)),
    )


def _candidate_bill_numbers(candidate: Mapping[str, object]) -> set[str]:
    values = {
        match
        for key in ("bill_no", "BILL_NO", "bill_number", "BILL_NUM")
        for match in _BILL_NUMBER.findall(_string(candidate.get(key)))
    }
    raw_items = candidate.get("agenda_items")
    if isinstance(raw_items, Sequence) and not isinstance(raw_items, (str, bytes)):
        for item in raw_items:
            if isinstance(item, Mapping):
                for key in ("bill_no", "BILL_NO", "bill_number"):
                    values.update(_BILL_NUMBER.findall(_string(item.get(key))))
            else:
                values.update(_BILL_NUMBER.findall(_string(item)))
    values.update(_BILL_NUMBER.findall(_string(candidate.get("agenda_text"))))
    return values


def _matching_committee(
    candidate: Mapping[str, object], requested: Sequence[str]
) -> str | None:
    if not requested:
        return None
    candidates = tuple(
        normalized
        for field in _COMMITTEE_FIELDS
        if (normalized := _normalize_committee(_string(candidate.get(field))))
    )
    scopes = tuple(_normalize_committee(value) for value in requested)
    for scope in scopes:
        for value in candidates:
            if scope == value or scope in value or value in scope:
                return scope
    return None


def _normalize_committee(value: str) -> str:
    canonical = LEGAL_TERMINOLOGY.canonicalize_committee(value) or ""
    return re.sub(r"[^0-9a-z가-힣]", "", canonical.casefold())


def _candidate_dates(candidate: Mapping[str, object]) -> tuple[date, ...]:
    values = {
        parsed
        for field in _DATE_FIELDS
        if (parsed := _parse_date(candidate.get(field))) is not None
    }
    return tuple(sorted(values))


def _matching_date(
    values: Sequence[date], lower: date | None, upper: date | None
) -> date | None:
    matching = [
        value
        for value in values
        if (lower is None or value >= lower) and (upper is None or value <= upper)
    ]
    return max(matching) if matching else None


def _parse_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _string(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        match = _DATE.search(text)
        if not match:
            return None
        try:
            return date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
        except ValueError:
            return None


def _best_source(
    term: str, texts: _CandidateText, weights: Mapping[str, int]
) -> str | None:
    matches = [
        source
        for source in ("title", "agenda", "body")
        if term in _match_key(getattr(texts, source))
    ]
    return max(matches, key=weights.__getitem__) if matches else None


def _meaningful_terms(values: Iterable[str]) -> tuple[str, ...]:
    normalized = (_normalize_term(value) for value in values)
    return _distinct(term for term in normalized if term and not _is_generic_term(term))


def _literal_issue_terms(
    query: str,
    *,
    excluded: Iterable[str] = (),
) -> tuple[str, ...]:
    """Extract auditable Korean subject literals without topic hard-coding.

    Output/request vocabulary, dates, bill numbers, and reviewed concepts are
    removed.  Adjacent two- and three-token phrases are emitted before their
    component tokens: exact multiword subjects therefore rank above broader
    partial matches while recall is still preserved for official wording that
    separates the words.
    """

    excluded_keys = {
        _match_key(_normalize_text(value)) for value in excluded if value.strip()
    }
    tokens: list[str] = []
    for raw in _QUERY_WORD.findall(query):
        token = _strip_particle(raw.casefold())
        key = _match_key(token)
        if (
            len(key) < 2
            or not _HANGUL.search(token)
            or _DIGIT.search(token)
            or key in excluded_keys
            or _is_generic_term(key)
        ):
            continue
        if key not in tokens:
            tokens.append(key)

    phrases: list[str] = []
    for width in (3, 2):
        for start in range(0, len(tokens) - width + 1):
            phrase = "".join(tokens[start : start + width])
            if phrase not in excluded_keys and phrase not in phrases:
                phrases.append(phrase)
    # The public query contract is already bounded to 500 characters.  Do not
    # silently discard later user concepts: partition planning either carries
    # every term or rejects an over-complex plan explicitly.
    return _distinct((*phrases, *tokens))


def _strip_particle(value: str) -> str:
    for particle in _KOREAN_PARTICLES:
        if value.endswith(particle) and len(value) >= len(particle) + 2:
            return value[: -len(particle)]
    return value


def _normalize_term(value: str) -> str:
    canonical = LEGAL_TERMINOLOGY.canonicalize(value)
    return _match_key(_normalize_text(canonical))


def _normalize_text(value: str) -> str:
    return LEGAL_TERMINOLOGY.normalize_equivalents(value)


def _match_key(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", value.casefold())


def _nonoverlapping_related(
    related: Sequence[str], exact: Sequence[str]
) -> tuple[str, ...]:
    """Avoid counting a broad and nested related term as independent hits."""

    return tuple(
        term
        for term in related
        if not any(term in exact_term or exact_term in term for exact_term in exact)
    )


def _is_generic_term(term: str) -> bool:
    if term in _GENERIC_TERMS or term.isdigit():
        return True
    for particle in _KOREAN_PARTICLES:
        if term.endswith(particle) and len(term) > len(particle):
            stem = term[: -len(particle)]
            if stem in _GENERIC_TERMS:
                return True
    return False


def _candidate_id(candidate: Mapping[str, object]) -> str:
    for field in _ID_FIELDS:
        value = _string(candidate.get(field)).strip()
        if value:
            return value
    numbers = sorted(_candidate_bill_numbers(candidate))
    if numbers:
        return f"bill:{numbers[0]}"
    title = _normalize_term(_field_text(candidate, _TITLE_FIELDS))
    dates = _candidate_dates(candidate)
    date_part = max(dates).isoformat() if dates else "undated"
    return f"candidate:{date_part}:{title or 'untitled'}"


def _field_text(candidate: Mapping[str, object], fields: Sequence[str]) -> str:
    return " ".join(
        value for field in fields if (value := _string(candidate.get(field)).strip())
    )


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _is_bill_number(value: str) -> bool:
    return bool(re.fullmatch(r"\d{7}", value))


def _distinct(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


__all__ = [
    "DEFAULT_MINIMUM_SCORE",
    "RelevanceCriteria",
    "RelevanceResult",
    "evaluate_candidate",
    "rank_candidates",
]
