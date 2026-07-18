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
from enum import StrEnum
from functools import lru_cache
from typing import Final

from kasm.research.proposers import extract_proposer_query_scope, valid_member_name
from kasm.search.terminology import (
    LEGAL_TERMINOLOGY,
    TERMINOLOGY_VERSION,
    TermCategory,
    TermExpansion,
    TermRelation,
)

_BILL_NUMBER = re.compile(r"(?<!\d)\d{7}(?!\d)")
_STATUTE = re.compile(r"[가-힣]{2,30}(?:기본)?법(?=$|[^가-힣])")
_DATE = re.compile(r"(?P<year>(?:19|20)\d{2})[-./년 ](?P<month>\d{1,2})[-./월 ](?P<day>\d{1,2})")
_PROPOSAL_DATE_QUERY = re.compile(
    r"(?:(?:19|20)\d{2}\s*년|올해|금년)"
    r"[^.!?\n]{0,16}(?:대표\s*|공동\s*)?발의"
    r"|(?:발의일(?:자)?|발의\s*(?:연도|년도|시점))"
    r"[^.!?\n]{0,16}(?:(?:19|20)\d{2}\s*년|올해|금년)"
)
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
        "공식",
        "국회",
        "기준",
        "내용",
        "대상",
        "대책",
        "대해",
        "대한",
        "문서",
        "방안",
        "발언",
        "발의",
        "발의자",
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
        "의원",
        "대표",
        "대표발의",
        "대표발의자",
        "공동",
        "공동발의",
        "공동발의자",
        "쟁점",
        "정리",
        "지원",
        "조사",
        "조사해",
        "조사해줘",
        "조사해주세요",
        "조회",
        "주세요",
        "최근",
        "처리",
        "현재",
        "확인",
        "확인해줘",
        "알려줘",
        "보여줘",
        "회의",
        "회의록",
        "위원회",
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
_REPRESENTATIVE_PROPOSER_FIELDS: Final = (
    "RST_PROPOSER",
    "representative_proposer",
    "representative_proposers",
)
_CO_PROPOSER_FIELDS: Final = (
    "PUBL_PROPOSER",
    "co_proposer",
    "co_proposers",
)
_DISPLAY_PROPOSER_FIELDS: Final = ("PROPOSER", "proposer")
_PROPOSER_CODE_FIELD: Final = {
    "RST_PROPOSER": "RST_MONA_CD",
    "PUBL_PROPOSER": "PUBL_MONA_CD",
    "representative_proposer": "representative_proposer_code",
    "representative_proposers": "representative_proposer_codes",
    "co_proposer": "co_proposer_code",
    "co_proposers": "co_proposer_codes",
}
_PROPOSER_SPLIT: Final = re.compile(r"\s*[,·ㆍ;|/]\s*")
_DISPLAY_REPRESENTATIVE: Final = re.compile(
    r"^\s*(?P<name>[가-힣]{2,5})\s*의원(?:\s*등\s*\d+\s*인)?\s*$"
)
_PROPOSER_INSTRUCTION_TERM: Final = re.compile(
    r"^(?:대표|공동)?발의(?:자|자가|자는|자의|자로|한|한.*|했.*)?$"
)
_COMMITTEE_FIELDS: Final = (
    "committee",
    "committee_name",
    "committee_name_ko",
    "COMM_NAME",
    "CMIT_NM",
    "SB_CMIT_NM",
)
_PROPOSAL_DATE_FIELDS: Final = (
    "proposed_date",
    "proposal_date",
    "propose_date",
    "PROPOSE_DT",
    "PROPOSE_DATE",
)
_DATE_FIELDS: Final = (
    "date",
    "meeting_date",
    "RGS_PROC_DT",
    *_PROPOSAL_DATE_FIELDS,
)
_ID_FIELDS: Final = ("id", "candidate_id", "bill_id", "meeting_id", "official_url")


class BillDateBasis(StrEnum):
    """Date field used to apply a bill's requested time range."""

    ANY = "any"
    PROPOSAL = "proposal"


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
    representative_proposer_names: tuple[str, ...] = ()
    co_proposer_names: tuple[str, ...] = ()
    proposer_names: tuple[str, ...] = ()
    date_from: date | None = None
    date_to: date | None = None
    bill_date_basis: BillDateBasis = BillDateBasis.ANY
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
        if not isinstance(self.bill_date_basis, BillDateBasis):
            raise ValueError("bill_date_basis must be a BillDateBasis")
        if self.minimum_score < 1:
            raise ValueError("minimum_score must be positive")
        for names in (
            self.representative_proposer_names,
            self.co_proposer_names,
            self.proposer_names,
        ):
            if len(names) != len(set(names)) or any(not valid_member_name(name) for name in names):
                raise ValueError("proposer names must be unique Korean full names")
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
        representative_proposer_names: Sequence[str] = (),
        co_proposer_names: Sequence[str] = (),
        proposer_names: Sequence[str] = (),
        date_from: date | None = None,
        date_to: date | None = None,
        bill_date_basis: BillDateBasis | None = None,
        minimum_score: int = DEFAULT_MINIMUM_SCORE,
    ) -> RelevanceCriteria:
        """Build criteria while conservatively extracting known query concepts."""

        terminology = LEGAL_TERMINOLOGY.expand(query, include_related=True)
        extracted_proposers = extract_proposer_query_scope(query)
        representative_names = _distinct(
            (
                *representative_proposer_names,
                *extracted_proposers.representative_proposer_names,
            )
        )
        co_names = _distinct((*co_proposer_names, *extracted_proposers.co_proposer_names))
        role_specific = {*representative_names, *co_names}
        any_names = _distinct(
            name
            for name in (*proposer_names, *extracted_proposers.proposer_names)
            if name not in role_specific
        )
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
                *representative_names,
                *co_names,
                *any_names,
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
            issue_terms=_meaningful_terms((*issue_terms, *equivalent_issues, *literal_issues)),
            related_statute_terms=_meaningful_terms(related_statutes),
            related_issue_terms=_meaningful_terms(related_issues),
            committees=_distinct(committees),
            representative_proposer_names=representative_names,
            co_proposer_names=co_names,
            proposer_names=any_names,
            date_from=date_from,
            date_to=date_to,
            bill_date_basis=(
                bill_date_basis if bill_date_basis is not None else infer_bill_date_basis(query)
            ),
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


@dataclass(frozen=True, slots=True)
class _PreparedCriteria:
    bill_numbers: frozenset[str]
    statutes: tuple[str, ...]
    issues: tuple[str, ...]
    related_statutes: tuple[str, ...]
    related_issues: tuple[str, ...]
    representative_proposer_names: tuple[str, ...]
    co_proposer_names: tuple[str, ...]
    proposer_names: tuple[str, ...]

    @property
    def meaningful(self) -> bool:
        return bool(self.statutes or self.issues or self.related_statutes or self.related_issues)

    @property
    def has_proposer_scope(self) -> bool:
        return bool(
            self.representative_proposer_names or self.co_proposer_names or self.proposer_names
        )


@dataclass(frozen=True, slots=True)
class _OfficialProposers:
    representatives: tuple[tuple[str, str, str | None], ...]
    co_proposers: tuple[tuple[str, str, str | None], ...]


def evaluate_candidate(
    candidate: Mapping[str, object], criteria: RelevanceCriteria
) -> RelevanceResult:
    """Evaluate one candidate, applying structured filters before text scoring."""

    candidate_id = _candidate_id(candidate)
    prepared = _prepare_criteria(criteria)
    requested_numbers = prepared.bill_numbers
    bill_numbers = _candidate_bill_numbers(candidate) if requested_numbers else set()
    if requested_numbers and requested_numbers.isdisjoint(bill_numbers):
        return _rejected(candidate, candidate_id, "bill_no_mismatch")

    matched_committee = _matching_committee(candidate, criteria.committees)
    if criteria.committees and matched_committee is None:
        return _rejected(candidate, candidate_id, "committee_mismatch")

    bill_candidate = _is_bill_candidate(candidate)
    has_date_scope = bool(criteria.date_from or criteria.date_to)
    proposal_date_scope = bool(
        has_date_scope and bill_candidate and criteria.bill_date_basis is BillDateBasis.PROPOSAL
    )
    candidate_dates = (
        _candidate_dates(
            candidate,
            fields=(_PROPOSAL_DATE_FIELDS if proposal_date_scope else _DATE_FIELDS),
        )
        if has_date_scope
        else ()
    )
    matched_date = (
        _matching_date(candidate_dates, criteria.date_from, criteria.date_to)
        if has_date_scope
        else None
    )
    if has_date_scope and matched_date is None:
        reason_prefix = "proposal_date" if proposal_date_scope else "date"
        reason = (
            f"{reason_prefix}_missing" if not candidate_dates else f"{reason_prefix}_out_of_range"
        )
        return _rejected(candidate, candidate_id, reason)

    reasons: list[str] = []
    score = 0
    if requested_numbers:
        number = sorted(requested_numbers.intersection(bill_numbers))[0]
        score += 100
        reasons.append(f"bill_no_exact:{number}")

    if matched_committee is not None:
        reasons.append(f"committee_exact:{matched_committee}")
    if matched_date is not None and has_date_scope:
        reason_prefix = "proposal_date" if proposal_date_scope else "date"
        reasons.append(f"{reason_prefix}_in_range:{matched_date.isoformat()}")

    proposer_match_count = 0
    if prepared.has_proposer_scope and bill_candidate:
        proposer_reasons, proposer_rejection = _match_proposer_scope(
            candidate,
            prepared,
        )
        if proposer_rejection is not None:
            return _rejected(candidate, candidate_id, proposer_rejection)
        proposer_match_count = len(proposer_reasons)
        score += proposer_match_count * 100
        reasons.extend(proposer_reasons)

    texts = _candidate_text(candidate)
    topic_score = 0
    for kind, terms, weights in (
        # A direct reviewed concept must outrank any combination of merely
        # related concepts.  This prevents a document mentioning both a nearby
        # statute and a neighboring issue from displacing an exact user term.
        (
            "statute",
            prepared.statutes,
            {"title": 36, "agenda": 40, "body": 24},
        ),
        ("issue", prepared.issues, {"title": 30, "agenda": 34, "body": 22}),
        (
            "related_statute",
            prepared.related_statutes,
            {"title": 12, "agenda": 14, "body": 6},
        ),
        (
            "related_issue",
            prepared.related_issues,
            {"title": 11, "agenda": 13, "body": 5},
        ),
    ):
        for term in terms:
            source = _best_source(term, texts, weights)
            if source is None:
                continue
            score += weights[source]
            topic_score += weights[source]
            reasons.append(f"{kind}:{term}@{source}")

    # Bill-number lookup is itself conclusive.  All other searches require at
    # least one non-generic term and must clear the configured threshold.
    if prepared.has_proposer_scope and bill_candidate:
        # A proposer name is an identity filter, not another fuzzy keyword.
        # When the user also supplies a subject, both must independently pass.
        relevant = bool(
            proposer_match_count
            and (not prepared.meaningful or topic_score >= criteria.minimum_score)
        )
    elif requested_numbers:
        relevant = score >= 100
    elif not prepared.meaningful and (matched_committee is not None or matched_date is not None):
        # A topic-free structured request such as "법사위의 올해 회의록" is
        # intentionally exhaustive inside its hard committee/date scope.
        score = criteria.minimum_score
        reasons.append("structured_scope_only")
        relevant = True
    elif not prepared.meaningful:
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

    rejection = (
        ()
        if relevant
        else (
            "proposer_topic_mismatch"
            if prepared.has_proposer_scope and bill_candidate and prepared.meaningful
            else "below_minimum_score",
        )
    )
    return RelevanceResult(
        candidate=candidate,
        candidate_id=candidate_id,
        score=score,
        relevant=relevant,
        match_reasons=tuple(reasons),
        rejection_reasons=rejection,
    )


@lru_cache(maxsize=256)
def _prepare_criteria(criteria: RelevanceCriteria) -> _PreparedCriteria:
    """Normalize immutable query terms once for every candidate universe."""

    statutes = _meaningful_terms(criteria.statute_terms)
    issues = _meaningful_terms(criteria.issue_terms)
    return _PreparedCriteria(
        bill_numbers=frozenset(criteria.bill_numbers),
        statutes=statutes,
        issues=issues,
        related_statutes=_nonoverlapping_related(
            _meaningful_terms(criteria.related_statute_terms),
            (*statutes, *issues),
        ),
        related_issues=_nonoverlapping_related(
            _meaningful_terms(criteria.related_issue_terms),
            (*statutes, *issues),
        ),
        representative_proposer_names=criteria.representative_proposer_names,
        co_proposer_names=criteria.co_proposer_names,
        proposer_names=criteria.proposer_names,
    )


def rank_candidates(
    candidates: Iterable[Mapping[str, object]], criteria: RelevanceCriteria
) -> tuple[RelevanceResult, ...]:
    """Return relevant candidates in a deterministic, evidence-first order."""

    return rank_relevance_results(
        evaluate_candidate(candidate, criteria) for candidate in candidates
    )


def rank_relevance_results(
    results: Iterable[RelevanceResult],
) -> tuple[RelevanceResult, ...]:
    """Rank already-evaluated relevant results without scoring them again."""

    return tuple(
        sorted(
            (result for result in results if result.relevant),
            key=_result_sort_key,
        )
    )


def _result_sort_key(result: RelevanceResult) -> tuple[object, ...]:
    candidate_dates = _candidate_dates(result.candidate)
    newest = max(candidate_dates).toordinal() if candidate_dates else 0
    numbers = sorted(_candidate_bill_numbers(result.candidate))
    number = numbers[0] if numbers else ""
    title = _field_text(result.candidate, _TITLE_FIELDS)
    return (-result.score, -newest, number, result.candidate_id, title)


def _rejected(candidate: Mapping[str, object], candidate_id: str, reason: str) -> RelevanceResult:
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


def _is_bill_candidate(candidate: Mapping[str, object]) -> bool:
    return any(
        _string(candidate.get(field)).strip()
        for field in (
            "BILL_NO",
            "bill_no",
            "BILL_ID",
            "bill_id",
            *_REPRESENTATIVE_PROPOSER_FIELDS,
            *_CO_PROPOSER_FIELDS,
            *_DISPLAY_PROPOSER_FIELDS,
        )
    )


def _match_proposer_scope(
    candidate: Mapping[str, object],
    criteria: _PreparedCriteria,
) -> tuple[tuple[str, ...], str | None]:
    official = _official_proposers(candidate)
    representatives = {name: (field, code) for name, field, code in official.representatives}
    co_proposers = {name: (field, code) for name, field, code in official.co_proposers}
    all_proposers = {
        name: ("co_proposer", field, code) for name, field, code in official.co_proposers
    }
    all_proposers.update(
        {name: ("representative", field, code) for name, field, code in official.representatives}
    )
    reasons: list[str] = []

    matched_representatives = tuple(
        (name, representatives[name])
        for name in criteria.representative_proposer_names
        if name in representatives
    )
    if criteria.representative_proposer_names and not matched_representatives:
        requested = "|".join(criteria.representative_proposer_names)
        return (), f"representative_proposer_mismatch:{requested}"
    for name, representative_match in matched_representatives:
        field, code = representative_match
        reasons.append(_proposer_reason("representative", name, field, code))

    matched_co_proposers = tuple(
        (name, co_proposers[name]) for name in criteria.co_proposer_names if name in co_proposers
    )
    if criteria.co_proposer_names and not matched_co_proposers:
        requested = "|".join(criteria.co_proposer_names)
        return (), f"co_proposer_mismatch:{requested}"
    for name, co_proposer_match in matched_co_proposers:
        field, code = co_proposer_match
        reasons.append(_proposer_reason("co_proposer", name, field, code))

    matched_proposers = tuple(
        (name, all_proposers[name]) for name in criteria.proposer_names if name in all_proposers
    )
    if criteria.proposer_names and not matched_proposers:
        requested = "|".join(criteria.proposer_names)
        return (), f"proposer_mismatch:{requested}"
    for name, proposer_match in matched_proposers:
        role, field, code = proposer_match
        reasons.append(_proposer_reason(role, name, field, code))
    return tuple(reasons), None


def _official_proposers(candidate: Mapping[str, object]) -> _OfficialProposers:
    representatives = _proposer_entries(
        candidate,
        _REPRESENTATIVE_PROPOSER_FIELDS,
    )
    if not representatives:
        # ``PROPOSER`` is an official compact label such as "김성원의원 등
        # 10인".  It can safely recover only the displayed representative;
        # it cannot prove the identities behind "등 N인".
        representatives = _display_representative_entries(candidate)
    co_proposers = _proposer_entries(candidate, _CO_PROPOSER_FIELDS)
    return _OfficialProposers(representatives, co_proposers)


def _proposer_entries(
    candidate: Mapping[str, object],
    fields: Sequence[str],
) -> tuple[tuple[str, str, str | None], ...]:
    entries: list[tuple[str, str, str | None]] = []
    for field in fields:
        names = _proposer_names(candidate.get(field))
        code_field = _PROPOSER_CODE_FIELD.get(field)
        codes = _proposer_codes(candidate.get(code_field)) if code_field else ()
        aligned_codes: tuple[str | None, ...] = (
            tuple(codes) if len(codes) == len(names) else (None,) * len(names)
        )
        entries.extend((name, field, code) for name, code in zip(names, aligned_codes, strict=True))
    return tuple(dict.fromkeys(entries))


def _display_representative_entries(
    candidate: Mapping[str, object],
) -> tuple[tuple[str, str, str | None], ...]:
    for field in _DISPLAY_PROPOSER_FIELDS:
        value = _string(candidate.get(field))
        match = _DISPLAY_REPRESENTATIVE.fullmatch(value)
        if match and valid_member_name(match.group("name")):
            return ((match.group("name"), field, None),)
    return ()


def _proposer_names(value: object) -> tuple[str, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return _distinct(name for item in value for name in _proposer_names(item))
    text = _string(value).strip()
    if not text:
        return ()
    names: list[str] = []
    for raw in _PROPOSER_SPLIT.split(text):
        candidate = re.sub(r"^(?:국회)?의원\s*", "", raw.strip())
        candidate = re.sub(r"\s*의원$", "", candidate).strip()
        if valid_member_name(candidate):
            names.append(candidate)
    return _distinct(names)


def _proposer_codes(value: object) -> tuple[str, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        raw_values = (_string(item).strip() for item in value)
    else:
        raw_values = (item.strip() for item in _PROPOSER_SPLIT.split(_string(value)))
    return tuple(dict.fromkeys(item for item in raw_values if re.fullmatch(r"[0-9A-Za-z]+", item)))


def _proposer_reason(
    role: str,
    name: str,
    field: str,
    member_code: str | None,
) -> str:
    reason = f"proposer_exact:{role}:{name}@{field}"
    code_field = _PROPOSER_CODE_FIELD.get(field)
    if member_code is not None and code_field is not None:
        reason += f"[{code_field}={member_code}]"
    return reason


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


def _matching_committee(candidate: Mapping[str, object], requested: Sequence[str]) -> str | None:
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


def _candidate_dates(
    candidate: Mapping[str, object],
    *,
    fields: Sequence[str] = _DATE_FIELDS,
) -> tuple[date, ...]:
    values = {
        parsed for field in fields if (parsed := _parse_date(candidate.get(field))) is not None
    }
    return tuple(sorted(values))


def infer_bill_date_basis(query: str) -> BillDateBasis:
    """Bind an explicitly proposal-scoped year to proposal metadata only."""

    return BillDateBasis.PROPOSAL if _PROPOSAL_DATE_QUERY.search(query) else BillDateBasis.ANY


def _matching_date(values: Sequence[date], lower: date | None, upper: date | None) -> date | None:
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


def _best_source(term: str, texts: _CandidateText, weights: Mapping[str, int]) -> str | None:
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

    excluded_keys = {_match_key(_normalize_text(value)) for value in excluded if value.strip()}
    tokens: list[str] = []
    for raw in _QUERY_WORD.findall(query):
        raw_key = _match_key(raw.casefold())
        if _is_generic_term(raw_key):
            continue
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


def _nonoverlapping_related(related: Sequence[str], exact: Sequence[str]) -> tuple[str, ...]:
    """Avoid counting a broad and nested related term as independent hits."""

    return tuple(
        term
        for term in related
        if not any(term in exact_term or exact_term in term for exact_term in exact)
    )


def _is_generic_term(term: str) -> bool:
    if term in _GENERIC_TERMS or term.isdigit() or _PROPOSER_INSTRUCTION_TERM.fullmatch(term):
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
    return " ".join(value for field in fields if (value := _string(candidate.get(field)).strip()))


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _is_bill_number(value: str) -> bool:
    return bool(re.fullmatch(r"\d{7}", value))


def _distinct(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


__all__ = [
    "BillDateBasis",
    "DEFAULT_MINIMUM_SCORE",
    "RelevanceCriteria",
    "RelevanceResult",
    "evaluate_candidate",
    "infer_bill_date_basis",
    "rank_candidates",
    "rank_relevance_results",
]
