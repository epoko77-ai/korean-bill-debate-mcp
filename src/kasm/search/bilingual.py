"""Bilingual query preparation for Korean-first official Assembly sources."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .terminology import LEGAL_TERMINOLOGY, TERMINOLOGY_VERSION

_HANGUL = re.compile(r"[가-힣]")
_LATIN = re.compile(r"[A-Za-z]")
_YEAR = re.compile(r"\b(20\d{2})\b")
_BILL_NO = re.compile(r"\b\d{7,}\b")
_MAX_QUERY_CHARS = 500
_MAX_SEARCH_QUERY_CHARS = 1000
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

# Specific phrases come before broad concepts. These translations are search hints, not
# machine-translated evidence; quoted records remain in their official Korean source language.
_ENGLISH_SEARCH_HINTS = (
    (r"\bprosecut(?:or|ors|ion|orial)\b", "검찰"),
    (r"\bclimate crisis\b", "기후위기"),
    (r"\benergy transition\b", "에너지전환"),
    (r"\bfair trade\b", "공정거래"),
    (r"\btax investigation\b", "세무조사"),
    (r"\bprice inflation\b|\bconsumer prices?\b", "물가"),
    (r"\bagricultur(?:e|al)\b", "농업"),
    (r"\bfisher(?:y|ies)\b", "수산"),
    (r"\btourism\b", "관광"),
    (r"\bbroadcast(?:ing)? reform\b", "방송 개혁"),
    (r"\bdigital inclusion\b", "디지털 포용"),
    (r"\babolish(?:ed|ing|ment)?\b", "폐지"),
    (r"\bcurrent status\b|\bwhere (?:does|is) .* stand\b", "처리상태"),
    (r"\bsubcommittee (?:minutes|records?|debates?)\b", "소위원회 회의록"),
    (
        r"\bexpert review reports?\b|\bcommittee review reports?\b|\breview reports?\b",
        "전문위원 검토보고서",
    ),
    (r"\bgovernment (?:answer|response|position)s?\b", "정부 답변"),
    (r"\blawmakers?\b|\blegislators?\b|\bmembers? of (?:the )?assembly\b", "의원"),
    (r"\bremarks?\b|\bstatements?\b|\bspeeches?\b", "발언"),
    (r"\barguments? for and against\b|\bopposing views?\b", "찬반 의견"),
    (r"\bminutes\b", "회의록"),
    (r"\bcommittees?\b", "위원회"),
    (r"\bbills?\b|\blegislation\b", "법안"),
)


@dataclass(frozen=True, slots=True)
class PreparedQuery:
    original: str
    search_query: str
    language: str
    translation_mode: str
    terminology_version: str = TERMINOLOGY_VERSION
    expansion_reasons: tuple[str, ...] = ()

    def metadata(self) -> dict[str, object]:
        return {
            "query": self.original,
            "query_language": self.language,
            "search_query_ko": self.search_query,
            "query_translation": self.translation_mode,
            "source_language": "ko",
            "terminology_version": self.terminology_version,
            "query_expansion_reasons": list(self.expansion_reasons),
        }


def prepare_query(query: str, korean_query: str | None = None) -> PreparedQuery:
    """Use an explicit Korean search query or derive conservative Korean search hints."""
    original = query.strip()
    if len(original) > _MAX_QUERY_CHARS:
        raise ValueError(f"query must not exceed {_MAX_QUERY_CHARS} characters")
    has_latin = bool(_LATIN.search(original))
    has_hangul = bool(_HANGUL.search(original))
    language = "en" if has_latin and not has_hangul else "ko"
    if korean_query and korean_query.strip():
        explicit = korean_query.strip()
        if len(explicit) > _MAX_QUERY_CHARS:
            raise ValueError(
                f"korean_query must not exceed {_MAX_QUERY_CHARS} characters"
            )
        return PreparedQuery(original, explicit, language, "client_supplied")
    if has_hangul and has_latin:
        expanded, reasons = _mixed_search_query(original)
        if expanded != original:
            return PreparedQuery(
                original,
                expanded,
                language,
                "built_in_glossary",
                expansion_reasons=reasons,
            )
        return PreparedQuery(original, original, language, "none")
    if language != "en":
        return PreparedQuery(original, original, language, "none")
    translated, reasons = _english_search_query(original)
    if translated:
        return PreparedQuery(
            original,
            translated,
            language,
            "built_in_glossary",
            expansion_reasons=reasons,
        )
    return PreparedQuery(original, original, language, "untranslated")


def mixed_search_query(query: str) -> str:
    """Preserve a Korean query while adding deterministic Korean hints for Latin terms.

    Korean scope and date expressions often carry meaning that must not be discarded.  Unlike
    the English-only translation path, mixed queries therefore retain the complete original and
    append only glossary concepts not already present.  The glossary order makes the expansion
    stable, and the hard output bound prevents accidental query growth.
    """
    return _mixed_search_query(query)[0]


def _mixed_search_query(query: str) -> tuple[str, tuple[str, ...]]:
    # Python's Unicode ``\b`` sees both Latin letters and Hangul as word characters.  Insert a
    # matching-only boundary so common Korean particles (``AI는``, ``bills와``) do not hide a
    # glossary term.  The returned query still preserves the user's original spelling exactly.
    folded = re.sub(r"(?<=[a-z])(?=[가-힣])|(?<=[가-힣])(?=[a-z])", " ", query.casefold())
    terminology = LEGAL_TERMINOLOGY.expand(query, include_related=False)
    hints: list[str] = []
    reasons: list[str] = []
    for expansion in terminology.expansions:
        if _append_hint(query, hints, expansion.term):
            reasons.append(expansion.reason)

    matches = [
        (pattern, translated, tuple(re.finditer(pattern, folded)))
        for pattern, translated in _ENGLISH_SEARCH_HINTS
    ]
    covered_spans = [
        match.span()
        for _, translated, pattern_matches in matches
        if _concept_overlaps(translated, query)
        for match in (*pattern_matches, *_concept_matches(translated, folded))
    ]
    for _, translated, pattern_matches in matches:
        if not pattern_matches:
            continue
        if _concept_overlaps(translated, query) or any(
            _concept_overlaps(translated, existing) for existing in hints
        ):
            continue
        if all(
            any(
                match.start() >= covered_start and match.end() <= covered_end
                for covered_start, covered_end in covered_spans
            )
            for match in pattern_matches
        ):
            continue
        if _append_hint(query, hints, translated):
            reasons.append(f"english_glossary:{translated}")
    return " ".join((query, *hints)), tuple(reasons)


def _append_hint(query: str, hints: list[str], term: str) -> bool:
    if _concept_overlaps(term, query) or any(
        _concept_overlaps(term, existing) for existing in hints
    ):
        return False
    candidate = " ".join((query, *hints, term))
    if len(candidate) > _MAX_SEARCH_QUERY_CHARS:
        return False
    hints.append(term)
    return True


def _concept_overlaps(concept: str, text: str) -> bool:
    """Return whether two search concepts contain one another, ignoring separators."""
    normalized_concept = re.sub(r"[^0-9a-z가-힣]+", "", concept.casefold())
    normalized_text = re.sub(r"[^0-9a-z가-힣]+", "", text.casefold())
    return bool(
        normalized_concept
        and normalized_text
        and (
            normalized_concept in normalized_text
            or normalized_text in normalized_concept
        )
    )


def _concept_matches(concept: str, text: str) -> tuple[re.Match[str], ...]:
    """Find an already-present concept despite spacing or punctuation differences."""
    parts = re.findall(r"[0-9a-z가-힣]+", concept.casefold())
    if not parts:
        return ()
    pattern = r"[\W_]*".join(re.escape(part) for part in parts)
    return tuple(re.finditer(pattern, text))


def english_search_query(query: str) -> str:
    """Return deduplicated Korean concepts found in an English legislative query."""
    return _english_search_query(query)[0]


def _english_search_query(query: str) -> tuple[str, tuple[str, ...]]:
    folded = query.casefold()
    bill_no = _BILL_NO.search(folded)
    if bill_no:
        return bill_no.group(), ("exact_bill_number",)
    terms: list[str] = []
    reasons: list[str] = []
    year = _YEAR.search(folded)
    month = next((number for name, number in _MONTHS.items() if name in folded), None)
    if year and month:
        terms.append(f"{year.group(1)}년 {month}월")
    elif year:
        terms.append(f"{year.group(1)}년")
    terminology = LEGAL_TERMINOLOGY.expand(query, include_related=False)
    for expansion in terminology.expansions:
        if _append_term(terms, expansion.term):
            reasons.append(expansion.reason)
    for pattern, translated in _ENGLISH_SEARCH_HINTS:
        if re.search(pattern, folded) and _append_term(terms, translated):
            reasons.append(f"english_glossary:{translated}")
    translated = " ".join(terms)
    if len(translated) > _MAX_SEARCH_QUERY_CHARS:
        raise ValueError("expanded search query exceeds the safe length limit")
    return translated, tuple(reasons)


def _append_term(terms: list[str], term: str) -> bool:
    if any(_concept_overlaps(term, existing) for existing in terms):
        return False
    terms.append(term)
    return True


def korean_committee(value: str | None) -> str | None:
    """Translate a supported English committee name used as a structured filter."""
    if value is None or _HANGUL.search(value):
        return value
    return LEGAL_TERMINOLOGY.canonicalize_committee(value)
