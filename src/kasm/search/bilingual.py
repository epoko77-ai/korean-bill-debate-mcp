"""Bilingual query preparation for Korean-first official Assembly sources."""

from __future__ import annotations

import re
from dataclasses import dataclass

_HANGUL = re.compile(r"[가-힣]")
_LATIN = re.compile(r"[A-Za-z]")
_YEAR = re.compile(r"\b(20\d{2})\b")
_BILL_NO = re.compile(r"\b\d{7,}\b")
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
    (r"\blegislation and judiciary committee\b", "법제사법위원회"),
    (
        r"\bscience,? ict,? broadcasting and communications committee\b",
        "과학기술정보방송통신위원회",
    ),
    (r"\bculture,? sports and tourism committee\b", "문화체육관광위원회"),
    (r"\bnational policy committee\b", "정무위원회"),
    (r"\bclimate,? energy,? environment and labo[u]?r committee\b", "기후에너지환경노동위원회"),
    (
        r"\bagriculture,? food,? rural affairs,? oceans and fisheries committee\b",
        "농림축산식품해양수산위원회",
    ),
    (r"\bfinance and economy planning committee\b", "재정경제기획위원회"),
    (r"\bcriminal procedure act\b", "형사소송법"),
    (r"\bhousing lease protection act\b", "주택임대차보호법"),
    (r"\bai basic act\b", "인공지능 기본법"),
    (r"\bartificial intelligence(?: industry| ecosystem)?\b", "인공지능"),
    (r"\b(?:ai|artificial intelligence) data cent(?:er|re)s?\b", "인공지능 데이터센터"),
    (r"\bai\b", "인공지능"),
    (r"\bsupplementary investigation (?:authority|power|rights?)\b", "보완수사권"),
    (r"\bsupplementary investigation\b", "보완수사"),
    (r"\bprosecut(?:or|ors|ion|orial)\b", "검찰"),
    (r"\bplatform (?:labor|labour|workers?)\b", "플랫폼 노동"),
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
    (r"\bsovereign ai\b|\bdomestic foundation models?\b", "소버린 AI"),
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

_COMMITTEE_TRANSLATIONS = tuple(
    (pattern, translated)
    for pattern, translated in _ENGLISH_SEARCH_HINTS
    if translated.endswith("위원회")
)


@dataclass(frozen=True, slots=True)
class PreparedQuery:
    original: str
    search_query: str
    language: str
    translation_mode: str

    def metadata(self) -> dict[str, str]:
        return {
            "query": self.original,
            "query_language": self.language,
            "search_query_ko": self.search_query,
            "query_translation": self.translation_mode,
            "source_language": "ko",
        }


def prepare_query(query: str, korean_query: str | None = None) -> PreparedQuery:
    """Use an explicit Korean search query or derive conservative Korean search hints."""
    original = query.strip()
    language = "en" if _LATIN.search(original) and not _HANGUL.search(original) else "ko"
    if korean_query and korean_query.strip():
        explicit = korean_query.strip()
        if len(explicit) > 500:
            raise ValueError("korean_query must not exceed 500 characters")
        return PreparedQuery(original, explicit, language, "client_supplied")
    if language != "en":
        return PreparedQuery(original, original, language, "none")
    translated = english_search_query(original)
    if translated:
        return PreparedQuery(original, translated, language, "built_in_glossary")
    return PreparedQuery(original, original, language, "untranslated")


def english_search_query(query: str) -> str:
    """Return deduplicated Korean concepts found in an English legislative query."""
    folded = query.casefold()
    bill_no = _BILL_NO.search(folded)
    if bill_no:
        return bill_no.group()
    terms: list[str] = []
    year = _YEAR.search(folded)
    month = next((number for name, number in _MONTHS.items() if name in folded), None)
    if year and month:
        terms.append(f"{year.group(1)}년 {month}월")
    elif year:
        terms.append(f"{year.group(1)}년")
    for pattern, translated in _ENGLISH_SEARCH_HINTS:
        overlaps_existing = any(
            translated in existing or existing in translated for existing in terms
        )
        if re.search(pattern, folded) and not overlaps_existing:
            terms.append(translated)
    return " ".join(terms)


def korean_committee(value: str | None) -> str | None:
    """Translate a supported English committee name used as a structured filter."""
    if value is None or _HANGUL.search(value):
        return value
    folded = value.casefold().strip()
    for pattern, translated in _COMMITTEE_TRANSLATIONS:
        if re.search(pattern, folded):
            return translated
    return value
