"""Utilities for tracking source integrity."""

from __future__ import annotations

import hashlib


def source_hash(content: str | bytes) -> str:
    """Return a portable SHA-256 digest for source material."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(data).hexdigest()


sha256 = source_hash
