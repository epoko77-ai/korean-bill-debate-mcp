from __future__ import annotations

import re
import tomllib
from pathlib import Path

from kasm import __version__

ROOT = Path(__file__).resolve().parents[2]


def test_release_version_is_consistent_across_runtime_metadata_and_lock() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked_project = next(
        package
        for package in lock["package"]
        if package["name"] == "korean-bill-debate-mcp"
    )

    assert project["project"]["version"] == __version__
    assert locked_project["version"] == __version__


def test_documented_release_pins_match_runtime_version() -> None:
    for relative_path in (
        "README.md",
        "README.en.md",
        "docs/deployment.md",
        "docs/mcp-clients.md",
        "src/kasm/setup.py",
    ):
        text = (ROOT / relative_path).read_text()
        source_pins = re.findall(
            r"korean-bill-debate-mcp(?:\.git)?@v([0-9]+\.[0-9]+\.[0-9]+)",
            text,
        )
        assert source_pins, f"missing release source pin in {relative_path}"
        assert set(source_pins) == {__version__}
