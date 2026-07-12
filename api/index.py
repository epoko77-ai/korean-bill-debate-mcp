"""Vercel ASGI entry point."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kasm.mcp.deployment import app  # noqa: E402

__all__ = ["app"]
