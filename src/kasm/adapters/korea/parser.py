"""Rule-based, source-preserving parser for Korean Assembly transcripts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from .normalizer import normalize_name, normalize_organization, normalize_role, normalize_text

PARSER_VERSION = "korea-rules-v2"

# Real transcripts most commonly use ○ or ◯, with optional spaces and a colon.
_BULLET_MARKER = re.compile(
    r"(?m)^[ \t]*[○◯●]\s*(?P<label>[^\n:：]{1,60}?)(?:\s*[:：]\s*|[ \t]{2,})(?P<inline>[^\n]*)"
)
_COLON_MARKER = re.compile(
    r"(?m)^[ \t]*(?P<label>[가-힣A-Za-z][^\n:：]{1,50}?)\s*[:：]\s*(?P<inline>[^\n]*)"
)
_AGENDA = re.compile(r"^[ \t]*(?P<number>\d+)\.\s*(?P<title>.+)$", re.MULTILINE)
_PROCEEDING = re.compile(
    r"^[（(](?P<kind>정회|속개|산회|박수|웃음|자료 제출|서면 답변|발언 취소|발언 정정)[^)）]*[)）]$"
)

_ROLE_SUFFIXES = (
    "소위원장",
    "위원장",
    "간사",
    "위원",
    "의원",
    "장관",
    "차관",
    "처장",
    "처장권한대행",
    "청장",
    "은행장",
    "원장",
    "실장",
    "국장",
    "의사국장",
    "사무처장",
    "과장",
    "수석전문위원",
    "전문위원",
    "의장",
    "부의장",
    "국무총리",
    "진술인",
)
_ROLE_PATTERN = "|".join(sorted(_ROLE_SUFFIXES, key=len, reverse=True))
_ROLE_FIRST = re.compile(rf"^(?P<role>{_ROLE_PATTERN})\s*(?P<name>[가-힣·]{{2,12}})$")
_NAME_FIRST = re.compile(rf"^(?P<name>[가-힣·]{{2,12}})\s*(?P<role>{_ROLE_PATTERN})$")
_ORG_ROLE_NAME = re.compile(
    rf"^(?P<org>.+?)\s+(?P<role>{_ROLE_PATTERN})\s+(?P<name>[가-힣·]{{2,12}})$"
)
_COMPACT_ORG_ROLE_NAME = re.compile(
    rf"^(?P<org>.+?)(?P<role>{_ROLE_PATTERN})\s*(?P<name>[가-힣·]{{2,4}})$"
)
_NOISY_NAME_ROLE = re.compile(
    rf"^(?P<name>[가-힣·]{{2,4}})(?P<role>{_ROLE_PATTERN})[가-힣?!]{{1,12}}$"
)
_NOISY_ROLE_NAME = re.compile(
    rf"^(?P<role>{_ROLE_PATTERN})(?P<name>[가-힣·]{{2,4}})[가-힣?!]{{1,12}}$"
)


@dataclass(frozen=True, slots=True)
class ParsedSpeech:
    sequence: int
    speaker_name: str
    text: str
    speaker_role: str | None = None
    organization: str | None = None
    agenda: str | None = None
    source_locator: str | None = None
    source_start: int | None = None
    source_end: int | None = None
    speech_type: str = "speech"
    parser_version: str = PARSER_VERSION


@dataclass(frozen=True, slots=True)
class ParseFailure:
    reason: str
    source_locator: str
    excerpt: str


@dataclass(slots=True)
class ParseResult:
    speeches: list[ParsedSpeech] = field(default_factory=list)
    failures: list[ParseFailure] = field(default_factory=list)


def split_speaker_label(label: str) -> tuple[str, str | None, str | None]:
    """Extract (name, role, organization), conservatively and deterministically."""

    label = normalize_text(label).strip("()（）")
    # PDF extraction sometimes appends the beginning of the spoken text to a
    # compact role-first marker (위원장김정호맙...). Preserve ordinary 2–4
    # syllable names, but trim only unmistakable residue patterns.
    if not re.search(r"\s", label):
        for role in _ROLE_SUFFIXES:
            if not label.startswith(role):
                continue
            tail = label[len(role) :]
            if len(tail) > 4 or (len(tail) == 4 and tail[-1] in "맙녕갑압후럼"):
                return normalize_name(tail[:3]), role, None
    match = _ORG_ROLE_NAME.match(label)
    if match:
        return (
            normalize_name(match["name"]),
            normalize_role(match["role"]),
            normalize_organization(match["org"]),
        )
    match = _ROLE_FIRST.match(label)
    if match:
        return normalize_name(match["name"]), normalize_role(match["role"]), None
    match = _NAME_FIRST.match(label)
    if match:
        return normalize_name(match["name"]), normalize_role(match["role"]), None
    match = _COMPACT_ORG_ROLE_NAME.match(label)
    if match:
        return (
            normalize_name(match["name"]),
            normalize_role(match["role"]),
            normalize_organization(match["org"]),
        )
    match = _NOISY_NAME_ROLE.match(label)
    if match:
        return normalize_name(match["name"]), normalize_role(match["role"]), None
    match = _NOISY_ROLE_NAME.match(label)
    if match:
        return normalize_name(match["name"]), normalize_role(match["role"]), None
    # Some sources concatenate the role and name (e.g. 위원장홍길동).
    for role in _ROLE_SUFFIXES:
        if label.startswith(role) and re.fullmatch(r"[가-힣·]{2,12}", label[len(role) :]):
            return normalize_name(label[len(role) :]), role, None
        if label.endswith(role) and re.fullmatch(r"[가-힣·]{2,12}", label[: -len(role)]):
            return normalize_name(label[: -len(role)]), role, None
    compact = normalize_name(label)
    for role in _ROLE_SUFFIXES:
        if compact.startswith(role) and len(compact) > len(role) + 3:
            return compact[len(role) : len(role) + 3], role, None
        position = compact.find(role)
        if 2 <= position <= 4 and len(compact) > position + len(role):
            return compact[:position], role, None
    return compact, None, None


class KoreaTranscriptParser:
    """Parse speaker turns without discarding unparseable source regions."""

    def parse(
        self,
        source: str,
        *,
        locator_prefix: str = "offset",
        metadata: Mapping[str, object] | None = None,
    ) -> ParseResult:
        """Parse source; metadata may supply a stable ``source_locator`` prefix."""

        if metadata and locator_prefix == "offset":
            locator_prefix = str(
                metadata.get("source_locator")
                or metadata.get("source_url")
                or metadata.get("meeting_id")
                or locator_prefix
            )
        result = ParseResult()
        matches = self._markers(source)
        if not matches:
            excerpt = normalize_text(source)[:160]
            if excerpt:
                result.failures.append(
                    ParseFailure(
                        "no speaker markers found", f"{locator_prefix}:0-{len(source)}", excerpt
                    )
                )
            return result

        if source[: matches[0].start()].strip():
            prefix = normalize_text(source[: matches[0].start()])
            # Headers and agenda blocks are expected. Retain only genuinely
            # speech-like text as a warning.
            if not _AGENDA.search(prefix) and len(prefix) > 80:
                result.failures.append(
                    ParseFailure(
                        "unassigned text before first speaker",
                        f"{locator_prefix}:0-{matches[0].start()}",
                        prefix[:160],
                    )
                )

        # Agenda tables are often repeated at the start of a PDF and many bills
        # may be considered together. Treating the last heading before every
        # speaker as their agenda creates false bill links. Advance agenda state
        # only across speaker boundaries and represent multi-item blocks honestly.
        current_agenda: str | None = None
        previous_boundary = 0
        for index, marker in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
            inline = marker.group("inline") or ""
            body_start = marker.start("inline")
            body = normalize_text(inline + source[marker.end() : end])
            name, role, organization = split_speaker_label(marker.group("label"))
            locator = f"{locator_prefix}:{marker.start()}-{end}"
            if not name or not body:
                result.failures.append(
                    ParseFailure(
                        "empty speaker or speech text",
                        locator,
                        normalize_text(source[marker.start() : end])[:160],
                    )
                )
                continue
            agenda_matches = list(_AGENDA.finditer(source, previous_boundary, marker.start()))
            if len(agenda_matches) == 1:
                current_agenda = _agenda_title(agenda_matches[0])
            elif len(agenda_matches) > 1:
                first = agenda_matches[0].group("number")
                last = agenda_matches[-1].group("number")
                current_agenda = f"복수 의사일정 제{first}항~제{last}항 일괄 심사"
            proceeding = _PROCEEDING.match(body)
            result.speeches.append(
                ParsedSpeech(
                    sequence=len(result.speeches) + 1,
                    speaker_name=name,
                    speaker_role=role,
                    organization=organization,
                    text=body,
                    agenda=current_agenda,
                    source_locator=locator,
                    source_start=body_start,
                    source_end=end,
                    speech_type="proceeding" if proceeding else "speech",
                )
            )
            previous_boundary = marker.end()
        return result

    @staticmethod
    def _markers(source: str) -> list[re.Match[str]]:
        bullet = list(_BULLET_MARKER.finditer(source))
        # Colon markers are fallback-only: mixing them tends to mistake times
        # and headings for speakers.
        return bullet or list(_COLON_MARKER.finditer(source))


def parse_transcript(
    source: str,
    *,
    locator_prefix: str = "offset",
    metadata: Mapping[str, object] | None = None,
) -> ParseResult:
    return KoreaTranscriptParser().parse(source, locator_prefix=locator_prefix, metadata=metadata)


def _agenda_title(match: re.Match[str]) -> str:
    title = normalize_text(match.group("title"))
    title = re.sub(r"\s*·{3,}\s*\d+\s*$", "", title)
    return f"{match.group('number')}. {title}"
