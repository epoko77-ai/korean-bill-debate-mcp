"""Stable, human-readable identifiers."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date

_UNSAFE = re.compile(r"[^\w.-]+", re.UNICODE)


def slug(value: str) -> str:
    """Return a deterministic identifier component, preserving Korean text."""
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    result = _UNSAFE.sub("-", normalized).strip("-.")
    if not result:
        raise ValueError("identifier component must contain letters or digits")
    return result


def meeting_id(
    assembly_term: int,
    meeting_type: str,
    meeting_date: date,
    meeting_identifier: str,
) -> str:
    if assembly_term <= 0:
        raise ValueError("assembly_term must be positive")
    return (
        f"kna:{assembly_term}:{slug(meeting_type)}:"
        f"{meeting_date.isoformat()}:{slug(meeting_identifier)}"
    )


def speech_id(meeting: str, sequence: int) -> str:
    if not meeting or not meeting.startswith("kna:"):
        raise ValueError("meeting must be a KNA meeting id")
    if sequence < 0:
        raise ValueError("sequence must be non-negative")
    return f"{meeting}:speech-{sequence:04d}"


def agenda_id(meeting: str, sequence: int, title: str, bill_no: str | None = None) -> str:
    if not meeting or not meeting.startswith("kna:"):
        raise ValueError("meeting must be a KNA meeting id")
    if sequence < 0 or not title.strip():
        raise ValueError("agenda requires a non-negative sequence and title")
    fingerprint = hashlib.sha256(
        f"{sequence}\0{bill_no or ''}\0{' '.join(title.split())}".encode()
    ).hexdigest()[:16]
    return f"{meeting}:agenda-{sequence:04d}-{fingerprint}"


# Explicit names are convenient for callers and backwards-compatible aliases are cheap.
generate_meeting_id = meeting_id
generate_speech_id = speech_id
generate_agenda_id = agenda_id
