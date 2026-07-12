from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime paths. All state is local unless explicitly overridden."""

    data_dir: Path = Path.home() / ".local" / "share" / "korean-bill-debate-mcp"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "kasm.sqlite3"
