"""Secure first-run setup for a user-keyed local MCP installation."""

from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from kasm.adapters.korea.bills import BILL_DATASET
from kasm.adapters.korea.client import AssemblyOpenApiClient

SERVER_NAME = "korean-bill-debate"
SERVER_SOURCE = (
    "git+https://github.com/epoko77-ai/korean-bill-debate-mcp.git@v0.7.1"
)
SERVER_COMMAND = ["uvx", "--from", SERVER_SOURCE, "kbd", "mcp"]


def run_setup(
    *,
    client_name: str,
    api_key: str | None = None,
    credentials_file: str | Path | None = None,
    validate: bool = True,
) -> dict[str, Any]:
    """Store the key with user-only permissions and register a supported MCP client."""
    key = api_key or getpass.getpass("열린국회 API 키: ").strip()
    if not key:
        raise ValueError("열린국회 API 키를 입력해야 합니다")
    if validate:
        _validate_key(key)
    path = save_api_key(key, credentials_file)
    registration = register_client(client_name)
    return {
        "client": client_name,
        "credentials_file": str(path),
        "key_validated": validate,
        "registration": registration,
    }


def save_api_key(api_key: str, path: str | Path | None = None) -> Path:
    target = Path(path or Path.home() / ".config/korean-bill-debate-mcp/credentials.env")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"ASSEMBLY_OPEN_API_KEY={api_key}\n", encoding="utf-8")
    target.chmod(0o600)
    return target


def register_client(client_name: str) -> dict[str, Any]:
    commands = {
        "claude-code": [
            "claude",
            "mcp",
            "add",
            "--scope",
            "user",
            SERVER_NAME,
            "--",
            *SERVER_COMMAND,
        ],
        "codex": ["codex", "mcp", "add", SERVER_NAME, "--", *SERVER_COMMAND],
        "gemini": ["gemini", "mcp", "add", SERVER_NAME, "--", *SERVER_COMMAND],
    }
    if client_name in commands:
        command = commands[client_name]
        if not shutil.which(command[0]):
            return {"installed": False, "command": command, "reason": f"{command[0]} not found"}
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        return {
            "installed": completed.returncode == 0,
            "command": command,
            "message": (completed.stdout or completed.stderr).strip(),
        }
    if client_name == "claude-desktop":
        path = _claude_desktop_config_path()
        _write_json_server(path)
        return {"installed": True, "config": str(path)}
    raise ValueError("client must be claude-code, codex, gemini, or claude-desktop")


def _validate_key(key: str) -> None:
    client = AssemblyOpenApiClient(key, cache_ttl_seconds=0)
    client.fetch_page(BILL_DATASET, page_size=1, parameters={"AGE": 22}, refresh=True)


def _claude_desktop_config_path() -> Path:
    if os.name == "nt":
        return Path(os.environ["APPDATA"]) / "Claude/claude_desktop_config.json"
    return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"


def _write_json_server(path: Path) -> None:
    payload: dict[str, Any] = {}
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
    servers = payload.setdefault("mcpServers", {})
    servers[SERVER_NAME] = {"command": SERVER_COMMAND[0], "args": SERVER_COMMAND[1:]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
