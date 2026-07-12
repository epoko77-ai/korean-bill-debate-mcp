"""Conservative normalization for Korean Assembly transcripts.

Normalization intentionally does not rewrite spelling or punctuation: the returned
text remains suitable for citation against the official transcript.
"""

from __future__ import annotations

import re
import unicodedata

_HORIZONTAL_SPACE = re.compile(r"[^\S\r\n]+")
_BLANK_LINES = re.compile(r"\n(?:[ \t]*\n){2,}")
_ROLE_SPACE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    """Normalize Unicode and whitespace while preserving paragraph boundaries."""

    value = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    value = _HORIZONTAL_SPACE.sub(" ", value)
    value = "\n".join(line.strip() for line in value.splitlines())
    return _BLANK_LINES.sub("\n\n", value).strip()


def normalize_name(value: str) -> str:
    """Remove marker residue and accidental spacing from a person's name."""

    value = unicodedata.normalize("NFC", value).strip(" \t:：○◯●◆◇")
    return _ROLE_SPACE.sub("", value)


def normalize_role(value: str | None) -> str | None:
    """Return a stable display form for a role, without guessing identities."""

    if not value:
        return None
    result = _ROLE_SPACE.sub(" ", unicodedata.normalize("NFC", value)).strip(" \t:：()（）")
    return result or None


def normalize_organization(value: str | None) -> str | None:
    if not value:
        return None
    result = _ROLE_SPACE.sub(" ", unicodedata.normalize("NFC", value)).strip(" \t:：()（）")
    return result or None
