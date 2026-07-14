"""Secure first-run setup for a user-keyed local MCP installation."""

from __future__ import annotations

import getpass
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from kasm.adapters.korea.bills import BILL_DATASET
from kasm.adapters.korea.client import AssemblyOpenApiClient

SERVER_NAME = "korean-bill-debate"
SERVER_SOURCE = (
    "git+https://github.com/epoko77-ai/korean-bill-debate-mcp.git@v1.0.0"
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
    key = _resolve_api_key(api_key)
    if validate:
        _validate_key(key)
    path = save_api_key(key, credentials_file)
    registration = register_client(client_name, credentials_file=path)
    return {
        "client": client_name,
        "credentials_file": str(path),
        "key_validated": validate,
        "registration": registration,
    }


def save_api_key(api_key: str, path: str | Path | None = None) -> Path:
    target = Path(
        path or Path.home() / ".config/korean-bill-debate-mcp/credentials.env"
    ).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"ASSEMBLY_OPEN_API_KEY={api_key}\n", encoding="utf-8")
    target.chmod(0o600)
    return target


def register_client(
    client_name: str, *, credentials_file: str | Path | None = None
) -> dict[str, Any]:
    credentials_path = _absolute_credentials_path(credentials_file)
    credential_environment = (
        f"KBD_CREDENTIALS_FILE={credentials_path}" if credentials_path else None
    )
    commands = {
        "claude-code": [
            "claude",
            "mcp",
            "add",
            "--scope",
            "user",
            SERVER_NAME,
            *(["-e", credential_environment] if credential_environment else []),
            "--",
            *SERVER_COMMAND,
        ],
        "codex": [
            "codex",
            "mcp",
            "add",
            SERVER_NAME,
            *(["--env", credential_environment] if credential_environment else []),
            "--",
            *SERVER_COMMAND,
        ],
        "gemini": [
            "gemini",
            "mcp",
            "add",
            "--scope",
            "user",
            *(["-e", credential_environment] if credential_environment else []),
            SERVER_NAME,
            "--",
            *SERVER_COMMAND,
        ],
    }
    if client_name in commands:
        command = commands[client_name]
        if not shutil.which(command[0]):
            return {"installed": False, "command": command, "reason": f"{command[0]} not found"}
        if client_name in {"codex", "gemini"}:
            existing = _preflight_existing_registration(
                client_name,
                command,
                credentials_path=credentials_path,
            )
            if existing is not None:
                return existing
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        message = (completed.stdout or completed.stderr).strip()
        combined_message = "\n".join(
            part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
        )
        if completed.returncode != 0 and "already exists" in combined_message.casefold():
            return _inspect_existing_registration(
                client_name,
                command,
                message,
                credentials_path=credentials_path,
            )
        return {
            "installed": completed.returncode == 0,
            "command": command,
            "message": message,
        }
    if client_name == "claude-desktop":
        path = _claude_desktop_config_path()
        _write_json_server(path, credentials_file=credentials_path)
        return {"installed": True, "config": str(path)}
    raise ValueError("client must be claude-code, codex, gemini, or claude-desktop")


def _resolve_api_key(api_key: str | None) -> str:
    """Resolve setup credentials without ever prompting a non-interactive process."""

    explicit = (api_key or "").strip()
    if explicit:
        return _validated_api_key(explicit)
    environment = os.getenv("ASSEMBLY_OPEN_API_KEY", "").strip()
    if environment:
        return _validated_api_key(environment)
    if not sys.stdin.isatty():
        raise ValueError(
            "열린국회 API 키가 필요합니다. ASSEMBLY_OPEN_API_KEY 환경변수를 "
            "설정하거나 --api-key로 전달하세요 "
            "(비대화형 실행에서는 키를 묻지 않습니다)"
        )
    prompted = getpass.getpass("열린국회 API 키: ").strip()
    if not prompted:
        raise ValueError("열린국회 API 키를 입력해야 합니다")
    return _validated_api_key(prompted)


def _validated_api_key(value: str) -> str:
    """Reject control characters before a key is written to an env file."""

    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("열린국회 API 키는 ASCII 문자열이어야 합니다") from exc
    if not 1 <= len(encoded) <= 256 or any(
        character < 33 or character > 126 for character in encoded
    ):
        raise ValueError(
            "열린국회 API 키는 공백이나 줄바꿈 없는 1~256자 문자열이어야 합니다"
        )
    return value


def _inspect_existing_claude_code_registration(
    add_command: list[str],
    add_message: str,
    *,
    credentials_path: str | None = None,
) -> dict[str, Any]:
    """Treat an existing Claude registration as success only when it is ours."""

    inspect_command = ["claude", "mcp", "get", SERVER_NAME]
    inspected = subprocess.run(
        inspect_command,
        check=False,
        capture_output=True,
        text=True,
    )
    inspection = (inspected.stdout or inspected.stderr).strip()
    fields = _parse_claude_registration(inspection)
    matches = (
        inspected.returncode == 0
        and fields.get("scope", "").casefold() == "user config"
        and fields.get("type", "").casefold() == "stdio"
        and fields.get("command") == SERVER_COMMAND[0]
        and _split_command_arguments(fields.get("args", "")) == SERVER_COMMAND[1:]
        and _credentials_environment_matches(fields, credentials_path)
    )
    if matches:
        return {
            "installed": True,
            "command": add_command,
            "message": add_message,
            "already_configured": True,
        }
    return {
        "installed": False,
        "command": add_command,
        "message": add_message,
        "reason": (
            f"{SERVER_NAME} already exists, but its Claude Code user registration "
            "does not match the expected command"
        ),
    }


def _parse_claude_registration(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    environment = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.casefold() == "environment:":
            environment = True
            continue
        if environment and "=" in stripped:
            name, value = stripped.split("=", 1)
            fields[f"env.{name.strip()}"] = value.strip()
            continue
        environment = False
        name, separator, value = stripped.partition(":")
        if separator:
            fields[name.casefold()] = value.strip()
    scope = fields.get("scope", "")
    if scope.casefold().startswith("user config"):
        fields["scope"] = "user config"
    return fields


def _inspect_existing_registration(
    client_name: str,
    add_command: list[str],
    add_message: str,
    *,
    credentials_path: str | None,
) -> dict[str, Any]:
    if client_name == "claude-code":
        return _inspect_existing_claude_code_registration(
            add_command,
            add_message,
            credentials_path=credentials_path,
        )
    if client_name == "codex":
        return _inspect_existing_codex_registration(
            add_command,
            add_message,
            credentials_path=credentials_path,
        )
    return _inspect_existing_gemini_registration(
        add_command,
        add_message,
        credentials_path=credentials_path,
    )


def _inspect_existing_codex_registration(
    add_command: list[str],
    add_message: str,
    *,
    credentials_path: str | None,
) -> dict[str, Any]:
    result = _preflight_existing_codex_registration(
        add_command,
        credentials_path=credentials_path,
        message=add_message,
    )
    if result is not None:
        return result
    return _existing_registration_result(
        matches=False,
        client_label="Codex",
        add_command=add_command,
        add_message=add_message,
    )


def _preflight_existing_codex_registration(
    add_command: list[str],
    *,
    credentials_path: str | None,
    message: str,
) -> dict[str, Any] | None:
    inspect_command = ["codex", "mcp", "get", "--json", SERVER_NAME]
    inspected = subprocess.run(
        inspect_command,
        check=False,
        capture_output=True,
        text=True,
    )
    if inspected.returncode != 0:
        return None
    matches = False
    try:
        payload = json.loads(inspected.stdout)
    except json.JSONDecodeError:
        payload = {}
    transport = payload.get("transport", {}) if isinstance(payload, dict) else {}
    if isinstance(transport, dict):
        env = transport.get("env", {})
        fields = {
            "env.KBD_CREDENTIALS_FILE": (
                env.get("KBD_CREDENTIALS_FILE", "") if isinstance(env, dict) else ""
            )
        }
        matches = (
            transport.get("type") == "stdio"
            and transport.get("command") == SERVER_COMMAND[0]
            and transport.get("args") == SERVER_COMMAND[1:]
            and _credentials_environment_matches(fields, credentials_path)
        )
    return _existing_registration_result(
        matches=matches,
        client_label="Codex",
        add_command=add_command,
        add_message=message,
    )


def _inspect_existing_gemini_registration(
    add_command: list[str],
    add_message: str,
    *,
    credentials_path: str | None,
) -> dict[str, Any]:
    """Inspect Gemini's user settings without replacing an existing server."""

    result = _preflight_existing_gemini_registration(
        add_command,
        credentials_path=credentials_path,
        message=add_message,
    )
    if result is not None:
        return result
    return _existing_registration_result(
        matches=False,
        client_label="Gemini CLI",
        add_command=add_command,
        add_message=add_message,
    )


def _preflight_existing_registration(
    client_name: str,
    add_command: list[str],
    *,
    credentials_path: str | None,
) -> dict[str, Any] | None:
    message = f"{SERVER_NAME} is already configured"
    if client_name == "codex":
        return _preflight_existing_codex_registration(
            add_command,
            credentials_path=credentials_path,
            message=message,
        )
    return _preflight_existing_gemini_registration(
        add_command,
        credentials_path=credentials_path,
        message=message,
    )


def _preflight_existing_gemini_registration(
    add_command: list[str],
    *,
    credentials_path: str | None,
    message: str,
) -> dict[str, Any] | None:
    matches = False
    path = _gemini_user_config_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _existing_registration_result(
            matches=False,
            client_label="Gemini CLI",
            add_command=add_command,
            add_message=message,
        )
    servers = payload.get("mcpServers", {}) if isinstance(payload, dict) else {}
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return None
    server = servers.get(SERVER_NAME, {})
    if isinstance(server, dict):
        env = server.get("env", {})
        fields = {
            "env.KBD_CREDENTIALS_FILE": (
                env.get("KBD_CREDENTIALS_FILE", "") if isinstance(env, dict) else ""
            )
        }
        matches = (
            server.get("command") == SERVER_COMMAND[0]
            and server.get("args") == SERVER_COMMAND[1:]
            and _credentials_environment_matches(fields, credentials_path)
        )
    return _existing_registration_result(
        matches=matches,
        client_label="Gemini CLI",
        add_command=add_command,
        add_message=message,
    )


def _existing_registration_result(
    *,
    matches: bool,
    client_label: str,
    add_command: list[str],
    add_message: str,
) -> dict[str, Any]:
    if matches:
        return {
            "installed": True,
            "command": add_command,
            "message": add_message,
            "already_configured": True,
        }
    return {
        "installed": False,
        "command": add_command,
        "message": add_message,
        "reason": (
            f"{SERVER_NAME} already exists, but its {client_label} registration "
            "does not match the expected command"
        ),
    }


def _credentials_environment_matches(
    fields: dict[str, str], credentials_path: str | None
) -> bool:
    if credentials_path is None:
        return True
    return fields.get("env.KBD_CREDENTIALS_FILE") == credentials_path


def _absolute_credentials_path(path: str | Path | None) -> str | None:
    if path is None:
        return None
    return str(Path(path).expanduser().resolve())


def _gemini_user_config_path() -> Path:
    home = Path(os.getenv("GEMINI_CLI_HOME") or Path.home())
    return home / ".gemini/settings.json"


def _split_command_arguments(arguments: str) -> list[str]:
    try:
        return shlex.split(arguments)
    except ValueError:
        return []


def _validate_key(key: str) -> None:
    client = AssemblyOpenApiClient(key, cache_ttl_seconds=0)
    client.fetch_page(BILL_DATASET, page_size=1, parameters={"AGE": 22}, refresh=True)


def _claude_desktop_config_path() -> Path:
    if os.name == "nt":
        return Path(os.environ["APPDATA"]) / "Claude/claude_desktop_config.json"
    return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"


def _write_json_server(
    path: Path, *, credentials_file: str | Path | None = None
) -> None:
    payload: dict[str, Any] = {}
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
    servers = payload.setdefault("mcpServers", {})
    server: dict[str, Any] = {
        "command": SERVER_COMMAND[0],
        "args": SERVER_COMMAND[1:],
    }
    credentials_path = _absolute_credentials_path(credentials_file)
    if credentials_path:
        server["env"] = {"KBD_CREDENTIALS_FILE": credentials_path}
    servers[SERVER_NAME] = server
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
