from __future__ import annotations

import json
import stat
import subprocess

from kasm.adapters.korea.client import AssemblyOpenApiClient
from kasm.cli.main import main
from kasm.setup import SERVER_COMMAND, register_client, save_api_key


def test_setup_stores_key_with_user_only_permissions(tmp_path, monkeypatch) -> None:
    credentials = tmp_path / "credentials.env"
    save_api_key("private-key", credentials)
    monkeypatch.setenv("KBD_CREDENTIALS_FILE", str(credentials))
    client = AssemblyOpenApiClient()
    assert client.api_key == "private-key"
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600


def test_claude_desktop_registration_preserves_existing_servers(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kasm.setup.Path.home", lambda: tmp_path)
    path = tmp_path / "Library/Application Support/Claude/claude_desktop_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"existing": {"command": "x"}}}), "utf-8")
    result = register_client("claude-desktop")
    payload = json.loads(path.read_text("utf-8"))
    assert result["installed"] is True
    assert payload["mcpServers"]["existing"]["command"] == "x"
    assert payload["mcpServers"]["korean-bill-debate"]["command"] == "uvx"


def test_cli_setup_returns_failure_when_client_executable_is_missing(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr("kasm.setup.shutil.which", lambda _command: None)

    exit_code = main(
        [
            "setup",
            "--client",
            "gemini",
            "--api-key",
            "fixture-key",
            "--no-validate",
            "--credentials-file",
            str(tmp_path / "credentials.env"),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert output["registration"]["installed"] is False
    assert output["registration"]["reason"] == "gemini not found"


def test_command_line_clients_register_the_pinned_server(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr("kasm.setup.shutil.which", lambda command: f"/bin/{command}")

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "registered", "")

    monkeypatch.setattr("kasm.setup.subprocess.run", run)

    for client in ("claude-code", "codex", "gemini"):
        result = register_client(client)
        assert result["installed"] is True
        assert result["message"] == "registered"
        assert result["command"][-len(SERVER_COMMAND) :] == SERVER_COMMAND

    assert commands[0][:6] == ["claude", "mcp", "add", "--scope", "user", "korean-bill-debate"]
    assert commands[1][:4] == ["codex", "mcp", "add", "korean-bill-debate"]
    assert commands[2][:4] == ["gemini", "mcp", "add", "korean-bill-debate"]


def test_cli_setup_returns_failure_when_registration_command_fails(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr("kasm.setup.shutil.which", lambda command: f"/bin/{command}")
    monkeypatch.setattr(
        "kasm.setup.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", "bad config"),
    )

    exit_code = main(
        [
            "setup",
            "--client",
            "codex",
            "--api-key",
            "fixture-key",
            "--no-validate",
            "--credentials-file",
            str(tmp_path / "credentials.env"),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert output["registration"] == {
        "installed": False,
        "command": ["codex", "mcp", "add", "korean-bill-debate", "--", *SERVER_COMMAND],
        "message": "bad config",
    }
