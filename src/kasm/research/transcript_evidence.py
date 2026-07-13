"""Page-aware transcript extraction for evidence-linked research.

The legacy ingestion path parses one concatenated text file and therefore
cannot prove which PDF page contains a speech.  Research results parse every
preserved page separately, carry agenda context forward, then assign one
continuous sequence across the complete minutes document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from kasm.adapters.korea.normalizer import normalize_text
from kasm.adapters.korea.parser import (
    PARSER_VERSION,
    ParsedSpeech,
    ParseFailure,
    parse_transcript,
)
from kasm.core.ids import speech_id
from kasm.core.models import Meeting, Speech, SpeechRelation
from kasm.core.relations import infer_question_answer_relations

from .documents import OfficialDocumentKind, ParsedOfficialDocument

_AGENDA_BOUNDARY = re.compile(r"(?m)^\s*(?:제\s*)?\d+\s*[.)]\s*\S")
_TRANSCRIPT_STITCH_VERSION = "page-stitch-v1"


@dataclass(frozen=True, slots=True)
class TranscriptEvidence:
    """Every parsed speech and every unparsed source region from one PDF."""

    meeting_id: str
    document_url: str
    document_source_hash: str
    speeches: tuple[Speech, ...]
    relations: tuple[SpeechRelation, ...]
    failures: tuple[ParseFailure, ...]
    page_count: int
    source_characters: int
    speech_characters: int

    @property
    def complete_parse(self) -> bool:
        return not self.failures


def extract_transcript_evidence(
    meeting: Meeting,
    document: ParsedOfficialDocument,
) -> TranscriptEvidence:
    """Parse all pages without losing text, locators, or cross-page ordering."""

    if document.kind is not OfficialDocumentKind.MINUTES:
        raise ValueError("transcript evidence requires an official minutes document")
    if meeting.source_url != document.official_url:
        raise ValueError("minutes document URL does not match its meeting")

    parsed: list[ParsedSpeech] = []
    failures: list[ParseFailure] = []
    current_agenda: str | None = None
    for segment in document.segments:
        page = parse_transcript(segment.text, locator_prefix=segment.locator)
        leading_end = _first_speech_start(page.speeches, segment.locator)
        if leading_end is None:
            leading_end = len(segment.text)
        continuation = _continuation_text(segment.text[:leading_end])
        stitched = bool(parsed and continuation and not _AGENDA_BOUNDARY.search(continuation))
        if stitched:
            parsed[-1] = _append_continuation(
                parsed[-1],
                continuation,
                f"{segment.locator}:0-{leading_end}",
                leading_end,
            )
        failures.extend(
            failure
            for failure in page.failures
            if not (
                stitched
                and failure.source_locator == f"{segment.locator}:0-{leading_end}"
                and failure.reason
                in {"no speaker markers found", "unassigned text before first speaker"}
            )
        )
        for item in page.speeches:
            if item.agenda:
                current_agenda = item.agenda
            parsed.append(
                ParsedSpeech(
                    sequence=len(parsed) + 1,
                    speaker_name=item.speaker_name,
                    text=item.text,
                    speaker_role=item.speaker_role,
                    organization=item.organization,
                    agenda=item.agenda or current_agenda,
                    source_locator=item.source_locator,
                    source_start=item.source_start,
                    source_end=item.source_end,
                    speech_type=item.speech_type,
                    parser_version=item.parser_version,
                )
            )

    speeches = tuple(
        Speech(
            id=speech_id(meeting.id, item.sequence),
            meeting_id=meeting.id,
            sequence=item.sequence,
            speaker_id=None,
            speaker_name=item.speaker_name,
            speaker_role=item.speaker_role,
            organization=item.organization,
            text=item.text,
            agenda=item.agenda,
            previous_speech_id=(
                speech_id(meeting.id, item.sequence - 1) if item.sequence > 1 else None
            ),
            next_speech_id=(
                speech_id(meeting.id, item.sequence + 1) if item.sequence < len(parsed) else None
            ),
            source_locator=item.source_locator,
            source_hash=document.source_hash,
            parser_version=(
                f"{PARSER_VERSION}+{document.parser_version}+{_TRANSCRIPT_STITCH_VERSION}"
            ),
        )
        for item in parsed
    )
    relations = tuple(infer_question_answer_relations(speeches))
    return TranscriptEvidence(
        meeting_id=meeting.id,
        document_url=document.official_url,
        document_source_hash=document.source_hash,
        speeches=speeches,
        relations=relations,
        failures=tuple(failures),
        page_count=len(document.segments),
        source_characters=len(document.full_text),
        speech_characters=sum(len(item.text) for item in speeches),
    )


def _first_speech_start(
    speeches: list[ParsedSpeech],
    page_locator: str,
) -> int | None:
    """Return the first marker offset encoded by the page parser."""

    if not speeches or speeches[0].source_locator is None:
        return None
    match = re.fullmatch(
        rf"{re.escape(page_locator)}:(?P<start>\d+)-\d+",
        speeches[0].source_locator,
    )
    return int(match["start"]) if match else None


def _continuation_text(value: str) -> str:
    """Keep the complete normalized page prefix when it continues a turn."""

    return normalize_text(value)


def _append_continuation(
    speech: ParsedSpeech,
    continuation: str,
    locator: str,
    source_end: int,
) -> ParsedSpeech:
    """Stitch an unmarked next-page prefix into the preceding speech."""

    source_locator = speech.source_locator
    source_locator = locator if source_locator is None else f"{source_locator}|{locator}"
    return replace(
        speech,
        text=normalize_text(f"{speech.text}\n{continuation}"),
        source_locator=source_locator,
        source_end=source_end,
    )


__all__ = ["TranscriptEvidence", "extract_transcript_evidence"]
