"""Canonical JSON primitives for public corpus content.

Unlike orchestration artifacts, full official text can legitimately contain
credential-shaped example strings.  Corpus metadata validates URLs and all
controlled fields explicitly, while this serializer preserves arbitrary
official prose without applying heuristic secret scanning to that prose.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any


def canonical_json(payload: Any) -> bytes:
    normalized = _normalize(payload, active=set(), depth=0)
    try:
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("corpus payload is not canonical JSON") from exc


def canonical_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def decode_canonical_json(raw: bytes) -> Any:
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("stored corpus object is not valid JSON") from None
    if canonical_json(payload) != raw:
        raise ValueError("stored corpus object is not canonical JSON")
    return payload


def _normalize(
    value: Any,
    *,
    active: set[int],
    depth: int,
) -> Any:
    if depth > 100:
        raise ValueError("corpus JSON exceeds the maximum nesting depth")
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("corpus JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError("corpus JSON must not contain cycles")
        active.add(identity)
        try:
            result: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("corpus JSON object keys must be strings")
                result[key] = _normalize(item, active=active, depth=depth + 1)
            return result
        finally:
            active.remove(identity)
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        identity = id(value)
        if identity in active:
            raise ValueError("corpus JSON must not contain cycles")
        active.add(identity)
        try:
            return [
                _normalize(item, active=active, depth=depth + 1)
                for item in value
            ]
        finally:
            active.remove(identity)
    raise ValueError("corpus payload contains a non-JSON value")
