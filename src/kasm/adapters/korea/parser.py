"""Rule-based, source-preserving parser for Korean Assembly transcripts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from .normalizer import normalize_name, normalize_organization, normalize_role, normalize_text

# Increment whenever attribution-affecting parsing rules change.  Cache reuse
# is keyed by this value; keeping v5 after the role-layout fixes caused hosted
# requests to reuse stale speakers and miss otherwise recoverable turns.
PARSER_VERSION = "korea-rules-v7"

# Real transcripts most commonly use ○ or ◯, with optional spaces and a colon.
_BULLET_MARKER = re.compile(
    r"(?m)^[ \t]*[○◯●]\s*(?P<label>[^\n:：]{1,60}?)(?:\s*[:：]\s*|[ \t]{2,})(?P<inline>[^\n]*)"
)
_COLON_MARKER = re.compile(
    r"(?m)^[ \t]*(?P<label>[가-힣A-Za-z][^\n:：]{1,50}?)\s*[:：]\s*(?P<inline>[^\n]*)"
)
_AGENDA = re.compile(r"^[ \t]*(?P<number>\d+)\.\s*(?P<title>.+)$", re.MULTILINE)
_TRAILING_BILL_AGENDA = re.compile(
    r"(?m)^[ \t]*\d+\.\s*[^\n]{2,180}?법률안[^\n]{0,120}?"
    r"(?:의안번호\s*\d{5,})"
)
_PROCEEDING = re.compile(
    r"^[（(](?P<kind>정회|속개|산회|박수|웃음|자료 제출|서면 답변|발언 취소|발언 정정)[^)）]*[)）]$"
)

_ROLE_SUFFIXES = (
    "소위원장",
    "위원장대리",
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
_INLINE_REPEATED_ROLE_SPEAKER = re.compile(
    rf"^(?P<role>{_ROLE_PATTERN})\s*(?P<name>[가-힣·]{{2,4}})"
    rf"(?P<body>.*?(?:{_ROLE_PATTERN})\s*(?P=name)(?:\s|위원|의원|입니다).*)$",
    re.DOTALL,
)
_INLINE_LEADING_NAME = re.compile(
    r"^[ \t]*(?P<name>[가-힣·]{2,4})(?=[ \t]{2,})"
)
_INLINE_LEADING_TOKEN = re.compile(r"^[ \t]*(?P<token>[가-힣·]{2,12})(?:[ \t]+|$)")
_INLINE_LEADING_ROLE = re.compile(
    r"^[ \t]*(?P<role>위원|의원)(?=[ \t]+)"
)
_ORGANIZATION_ROLE_PREFIX = re.compile(
    r"(?:부|처|청|원|실|국|위원회|복지|재정|국방|법제|행정|안전|농림|국토|산업|"
    r"문화|교육|환경|노동|여성|가족|과학|기술|정보|외교|통일)$"
)
_INLINE_NON_NAMES = frozenset(
    {
        "정부",
        "위원",
        "의사",
        "자료",
        "오늘",
        "먼저",
        "그러면",
        "수고",
        "존경",
        "우리",
        "제가",
        "저는",
        "이상",
        "다음",
        "잠깐",
        "회의를",
        "질의를",
        "답변을",
        "말씀을",
        "설명을",
        "보고를",
        "의결을",
        "이것은",
        "이게",
        "정부측",
        "선임",
    }
)
_KOREAN_SURNAME_INITIALS = frozenset(
    "김이박최정강조윤장임한오서신권황안송전홍유고문양손배백허남심노하곽성차주우구민진지엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용예봉사부가복태목형피두감동호빈범좌견"
)
_INLINE_ROLE_ADDRESS = re.compile(r"^(?:위원|의원)(?:님|들|\s+여러분)")

# Minutes PDFs sometimes render section headings with the same bullet glyph used
# for speaker turns.  These labels are document structure, not people.  Keep the
# list deliberately exact: a role-less Korean personal name (for example 조정식)
# must continue to parse, while a role-only marker is too ambiguous to attribute
# safely and is quarantined as a parse failure below.
_STRUCTURAL_LABELS = frozenset(
    {
        "소위",
        "소위원회",
        "의안",
        "안건",
        "보고",
        "심사경과",
        "심사경과보고",
    }
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
    compact_label = re.sub(r"\s+", "", label)
    if compact_label in _STRUCTURAL_LABELS or compact_label in _ROLE_SUFFIXES:
        return "", None, None
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
            trailing = source[marker.end() : end]
            agenda_boundary = _TRAILING_BILL_AGENDA.search(trailing)
            body_end = (
                marker.end() + agenda_boundary.start()
                if agenda_boundary is not None
                else end
            )
            body = normalize_text(inline + source[marker.end() : body_end])
            raw_label = marker.group("label")
            name, role, organization = split_speaker_label(raw_label)
            if _is_wide_gap_structural_marker(raw_label, inline, parsed_name=name):
                name, role, organization = "", None, None
            recovered_prefix: str | None = None
            wide_gap = _recover_wide_gap_speaker(
                raw_label,
                inline,
                parsed_name=name,
                parsed_role=role,
                parsed_organization=organization,
            )
            if wide_gap is not None:
                name, role, organization, recovered_prefix = wide_gap
            elif name and role is None and _INLINE_ROLE_ADDRESS.match(inline) is None:
                inline_role = _INLINE_LEADING_ROLE.match(inline)
                if inline_role is not None:
                    role = normalize_role(inline_role["role"])
                    recovered_prefix = inline_role.group()
            if not name:
                recovered = _INLINE_REPEATED_ROLE_SPEAKER.match(normalize_text(inline))
                if recovered is not None:
                    name = normalize_name(recovered["name"])
                    role = normalize_role(recovered["role"])
            if recovered_prefix:
                body = re.sub(
                    rf"^{re.escape(recovered_prefix)}\s*",
                    "",
                    body,
                    count=1,
                )
            locator = f"{locator_prefix}:{marker.start()}-{end}"
            if not name:
                result.failures.append(
                    ParseFailure(
                        "non-speaker or ambiguous marker label",
                        locator,
                        normalize_text(source[marker.start() : end])[:160],
                    )
                )
                continue
            if not body:
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
                    source_end=body_end,
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


def _recover_wide_gap_speaker(
    label: str,
    inline: str,
    *,
    parsed_name: str,
    parsed_role: str | None,
    parsed_organization: str | None,
) -> tuple[str, str | None, str | None, str] | None:
    """Recover production pypdf markers that split ``role  name`` widely.

    Recovery is deliberately limited to labels that are exactly a known role
    or clearly look like an organization followed by a known role.  Ordinary
    ``personal-name + role`` labels therefore keep their parsed identity.
    """

    name_match = _INLINE_LEADING_NAME.match(inline)
    if name_match is None:
        return None
    inline_name = normalize_name(name_match["name"])
    if (
        inline_name in _INLINE_NON_NAMES
        or not inline_name
        or inline_name[0] not in _KOREAN_SURNAME_INITIALS
    ):
        return None

    compact_label = re.sub(r"\s+", "", normalize_text(label).strip("()（）"))
    matched_role = next(
        (
            role
            for role in sorted(_ROLE_SUFFIXES, key=len, reverse=True)
            if compact_label.endswith(role)
        ),
        None,
    )
    if matched_role is None:
        return None
    prefix = compact_label[: -len(matched_role)]
    prefix_without_ordinal = re.sub(r"제\d+$", "", prefix)
    exact_role_label = not prefix
    organization_role_label = bool(
        prefix
        and (
            len(parsed_name) > 4
            or re.search(r"제\d+$", prefix)
            or _ORGANIZATION_ROLE_PREFIX.search(prefix_without_ordinal)
        )
    )
    if not exact_role_label and not organization_role_label:
        return None

    recovered_organization = parsed_organization
    if prefix_without_ordinal:
        recovered_organization = normalize_organization(prefix_without_ordinal)
    return (
        inline_name,
        normalize_role(matched_role),
        recovered_organization,
        name_match["name"],
    )


def _is_wide_gap_structural_marker(
    label: str,
    inline: str,
    *,
    parsed_name: str,
) -> bool:
    """Reject layout headings such as ``안건조정위원장  선임``.

    The ordinary label parser can split a long compact heading at the short
    ``원장`` suffix and manufacture a person.  Limit this guard to long or
    absent parsed names so a genuine ``김윤 위원  선임...`` turn is preserved.
    """

    if parsed_name and len(parsed_name) <= 4:
        return False
    token_match = _INLINE_LEADING_TOKEN.match(inline)
    if token_match is None or normalize_name(token_match["token"]) not in _INLINE_NON_NAMES:
        return False
    compact_label = re.sub(r"\s+", "", normalize_text(label).strip("()（）"))
    return any(compact_label.endswith(role) for role in _ROLE_SUFFIXES)


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
