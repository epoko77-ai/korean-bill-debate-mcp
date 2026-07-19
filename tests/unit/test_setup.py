from __future__ import annotations

import json
import stat
import subprocess

import pytest

from kasm.adapters.korea.client import AssemblyOpenApiClient
from kasm.cli.main import main
from kasm.setup import SERVER_COMMAND, register_client, run_setup, save_api_key


def test_setup_stores_key_with_user_only_permissions(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ASSEMBLY_OPEN_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
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


def test_claude_desktop_registration_passes_custom_credentials_file(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("kasm.setup.Path.home", lambda: tmp_path)
    credentials = tmp_path / "private credentials.env"

    register_client("claude-desktop", credentials_file=credentials)

    path = tmp_path / "Library/Application Support/Claude/claude_desktop_config.json"
    payload = json.loads(path.read_text("utf-8"))
    assert payload["mcpServers"]["korean-bill-debate"]["env"] == {
        "KBD_CREDENTIALS_FILE": str(credentials.resolve())
    }


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
        if command[:3] == ["codex", "mcp", "get"]:
            return subprocess.CompletedProcess(command, 1, "", "not found")
        return subprocess.CompletedProcess(command, 0, "registered", "")

    monkeypatch.setattr("kasm.setup.subprocess.run", run)

    for client in ("claude-code", "codex", "gemini"):
        result = register_client(client)
        assert result["installed"] is True
        assert result["message"] == "registered"
        assert result["command"][-len(SERVER_COMMAND) :] == SERVER_COMMAND

    add_commands = [command for command in commands if command[2] == "add"]
    assert add_commands[0][:6] == [
        "claude",
        "mcp",
        "add",
        "--scope",
        "user",
        "korean-bill-debate",
    ]
    assert add_commands[1][:4] == ["codex", "mcp", "add", "korean-bill-debate"]
    assert add_commands[2][:7] == [
        "gemini",
        "mcp",
        "add",
        "--scope",
        "user",
        "korean-bill-debate",
        "--",
    ]


@pytest.mark.parametrize(
    ("client", "expected_prefix"),
    (
        (
            "claude-code",
            [
                "claude",
                "mcp",
                "add",
                "--scope",
                "user",
                "korean-bill-debate",
                "-e",
            ],
        ),
        ("codex", ["codex", "mcp", "add", "korean-bill-debate", "--env"]),
        ("gemini", ["gemini", "mcp", "add", "--scope", "user", "-e"]),
    ),
)
def test_command_line_registration_passes_custom_credentials_file(
    client, expected_prefix, tmp_path, monkeypatch
) -> None:
    commands: list[list[str]] = []
    credentials = tmp_path / "private credentials.env"
    monkeypatch.setattr("kasm.setup.shutil.which", lambda command: f"/bin/{command}")

    def run(command, **_kwargs):
        commands.append(command)
        if command[:3] == ["codex", "mcp", "get"]:
            return subprocess.CompletedProcess(command, 1, "", "not found")
        return subprocess.CompletedProcess(command, 0, "registered", "")

    monkeypatch.setattr("kasm.setup.subprocess.run", run)

    result = register_client(client, credentials_file=credentials)

    assert result["installed"] is True
    assert result["command"][: len(expected_prefix)] == expected_prefix
    assert f"KBD_CREDENTIALS_FILE={credentials.resolve()}" in result["command"]
    assert result["command"][-len(SERVER_COMMAND) :] == SERVER_COMMAND


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
    credentials = str((tmp_path / "credentials.env").resolve())
    assert output["registration"] == {
        "installed": False,
        "command": [
            "codex",
            "mcp",
            "add",
            "korean-bill-debate",
            "--env",
            f"KBD_CREDENTIALS_FILE={credentials}",
            "--",
            *SERVER_COMMAND,
        ],
        "message": "bad config",
    }


def test_setup_prefers_explicit_key_over_environment(tmp_path, monkeypatch) -> None:
    captured: list[str] = []
    monkeypatch.setenv("ASSEMBLY_OPEN_API_KEY", "environment-key")
    monkeypatch.setattr("kasm.setup._validate_key", captured.append)
    registered: list[tuple[str, str]] = []

    def register(client, *, credentials_file):
        registered.append((client, str(credentials_file)))
        return {"installed": True}

    monkeypatch.setattr("kasm.setup.register_client", register)

    result = run_setup(
        client_name="codex",
        api_key=" explicit-key ",
        credentials_file=tmp_path / "credentials.env",
    )

    assert captured == ["explicit-key"]
    assert (tmp_path / "credentials.env").read_text("utf-8") == (
        "ASSEMBLY_OPEN_API_KEY=explicit-key\n"
    )
    assert result["key_validated"] is True
    assert registered == [("codex", str(tmp_path / "credentials.env"))]


def test_setup_uses_environment_key_without_prompt(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ASSEMBLY_OPEN_API_KEY", "environment-key")
    monkeypatch.setattr(
        "kasm.setup.getpass.getpass",
        lambda _prompt: pytest.fail("environment key must avoid prompting"),
    )
    monkeypatch.setattr(
        "kasm.setup.register_client",
        lambda _client, **_kwargs: {"installed": True},
    )

    run_setup(
        client_name="codex",
        credentials_file=tmp_path / "credentials.env",
        validate=False,
    )

    assert (tmp_path / "credentials.env").read_text("utf-8") == (
        "ASSEMBLY_OPEN_API_KEY=environment-key\n"
    )


def test_setup_without_key_fails_fast_when_stdin_is_noninteractive(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("ASSEMBLY_OPEN_API_KEY", raising=False)
    monkeypatch.setattr("kasm.setup.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "kasm.setup.getpass.getpass",
        lambda _prompt: pytest.fail("non-interactive setup must not prompt"),
    )

    with pytest.raises(ValueError, match="ASSEMBLY_OPEN_API_KEY"):
        run_setup(
            client_name="codex",
            credentials_file=tmp_path / "credentials.env",
            validate=False,
        )


@pytest.mark.parametrize("value", ("line\nbreak", "space key", "한글키", "x" * 257))
def test_setup_rejects_unsafe_key_before_writing_credentials(
    value: str, tmp_path, monkeypatch
) -> None:
    target = tmp_path / "credentials.env"
    monkeypatch.setattr(
        "kasm.setup.register_client",
        lambda _client, **_kwargs: pytest.fail(
            "unsafe credentials must fail before registration"
        ),
    )

    with pytest.raises(ValueError, match="API 키"):
        run_setup(
            client_name="codex",
            api_key=value,
            credentials_file=target,
            validate=False,
        )

    assert not target.exists()


def test_cli_setup_without_key_returns_failure_in_noninteractive_shell(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("ASSEMBLY_OPEN_API_KEY", raising=False)
    monkeypatch.setattr("kasm.setup.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "kasm.setup.getpass.getpass",
        lambda _prompt: pytest.fail("non-interactive CLI must not prompt"),
    )

    exit_code = main(
        [
            "setup",
            "--client",
            "codex",
            "--no-validate",
            "--credentials-file",
            str(tmp_path / "credentials.env"),
        ]
    )

    assert exit_code == 2
    assert "ASSEMBLY_OPEN_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "credentials.env").exists()


def test_setup_prompts_only_in_interactive_shell(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ASSEMBLY_OPEN_API_KEY", raising=False)
    monkeypatch.setattr("kasm.setup.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("kasm.setup.getpass.getpass", lambda _prompt: " prompted-key ")
    monkeypatch.setattr(
        "kasm.setup.register_client",
        lambda _client, **_kwargs: {"installed": True},
    )

    run_setup(
        client_name="codex",
        credentials_file=tmp_path / "credentials.env",
        validate=False,
    )

    assert (tmp_path / "credentials.env").read_text("utf-8") == (
        "ASSEMBLY_OPEN_API_KEY=prompted-key\n"
    )


def test_setup_help_exposes_api_key_and_environment_fallback(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["setup", "--help"])

    assert raised.value.code == 0
    output = capsys.readouterr().out
    assert "--api-key" in output
    assert "ASSEMBLY_OPEN_API_KEY" in output


def test_claude_code_already_matching_registration_is_idempotent(monkeypatch) -> None:
    monkeypatch.setattr("kasm.setup.shutil.which", lambda command: f"/bin/{command}")
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[:3] == ["claude", "mcp", "add"]:
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "MCP server korean-bill-debate already exists in user config",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            "\n".join(
                (
                    "korean-bill-debate:",
                    "  Scope: User config (available in all your projects)",
                    "  Status: ✘ Failed to connect",
                    "  Type: stdio",
                    "  Command: uvx",
                    f"  Args: {' '.join(SERVER_COMMAND[1:])}",
                )
            ),
            "",
        )

    monkeypatch.setattr("kasm.setup.subprocess.run", run)

    result = register_client("claude-code")

    assert result["installed"] is True
    assert result["already_configured"] is True
    assert calls[1] == ["claude", "mcp", "get", "korean-bill-debate"]


def test_claude_code_does_not_accept_conflicting_existing_registration(monkeypatch) -> None:
    monkeypatch.setattr("kasm.setup.shutil.which", lambda command: f"/bin/{command}")

    def run(command, **_kwargs):
        if command[:3] == ["claude", "mcp", "add"]:
            return subprocess.CompletedProcess(command, 1, "", "already exists")
        return subprocess.CompletedProcess(
            command,
            0,
            "\n".join(
                (
                    "korean-bill-debate:",
                    "  Scope: User config",
                    "  Type: stdio",
                    "  Command: attacker-command",
                    "  Args: --steal-data",
                )
            ),
            "",
        )

    monkeypatch.setattr("kasm.setup.subprocess.run", run)

    result = register_client("claude-code")

    assert result["installed"] is False
    assert "does not match" in result["reason"]


def test_claude_code_existing_registration_must_match_credentials_file(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("kasm.setup.shutil.which", lambda command: f"/bin/{command}")
    expected = tmp_path / "credentials.env"

    def run(command, **_kwargs):
        if command[:3] == ["claude", "mcp", "add"]:
            return subprocess.CompletedProcess(command, 1, "", "already exists")
        return subprocess.CompletedProcess(
            command,
            0,
            "\n".join(
                (
                    "korean-bill-debate:",
                    "  Scope: User config",
                    "  Type: stdio",
                    "  Command: uvx",
                    f"  Args: {' '.join(SERVER_COMMAND[1:])}",
                    "  Environment:",
                    f"    KBD_CREDENTIALS_FILE={expected.resolve()}",
                )
            ),
            "",
        )

    monkeypatch.setattr("kasm.setup.subprocess.run", run)

    result = register_client("claude-code", credentials_file=expected)

    assert result["installed"] is True
    assert result["already_configured"] is True


def test_codex_already_matching_registration_is_idempotent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kasm.setup.shutil.which", lambda command: f"/bin/{command}")
    credentials = tmp_path / "credentials.env"
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[:3] == ["codex", "mcp", "add"]:
            return subprocess.CompletedProcess(command, 1, "", "already exists")
        payload = {
            "transport": {
                "type": "stdio",
                "command": SERVER_COMMAND[0],
                "args": SERVER_COMMAND[1:],
                "env": {"KBD_CREDENTIALS_FILE": str(credentials.resolve())},
            }
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr("kasm.setup.subprocess.run", run)

    result = register_client("codex", credentials_file=credentials)

    assert result["installed"] is True
    assert result["already_configured"] is True
    assert calls == [["codex", "mcp", "get", "--json", "korean-bill-debate"]]


def test_codex_does_not_accept_conflicting_existing_registration(monkeypatch) -> None:
    monkeypatch.setattr("kasm.setup.shutil.which", lambda command: f"/bin/{command}")
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[:3] == ["codex", "mcp", "add"]:
            return subprocess.CompletedProcess(command, 0, "overwritten", "")
        payload = {
            "transport": {
                "type": "stdio",
                "command": "attacker-command",
                "args": ["--steal-data"],
                "env": {},
            }
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr("kasm.setup.subprocess.run", run)

    result = register_client("codex")

    assert result["installed"] is False
    assert "does not match" in result["reason"]
    assert calls == [["codex", "mcp", "get", "--json", "korean-bill-debate"]]


def test_gemini_already_matching_user_registration_is_idempotent(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("kasm.setup.shutil.which", lambda command: f"/bin/{command}")
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path))
    credentials = tmp_path / "credentials.env"
    settings = tmp_path / ".gemini/settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "korean-bill-debate": {
                        "command": SERVER_COMMAND[0],
                        "args": SERVER_COMMAND[1:],
                        "env": {
                            "KBD_CREDENTIALS_FILE": str(credentials.resolve()),
                        },
                    }
                }
            }
        ),
        "utf-8",
    )
    monkeypatch.setattr(
        "kasm.setup.subprocess.run",
        lambda _command, **_kwargs: pytest.fail(
            "matching Gemini user settings must be accepted before add"
        ),
    )

    result = register_client("gemini", credentials_file=credentials)

    assert result["installed"] is True
    assert result["already_configured"] is True
    assert result["command"][:6] == [
        "gemini",
        "mcp",
        "add",
        "--scope",
        "user",
        "-e",
    ]


def test_gemini_does_not_accept_conflicting_existing_user_registration(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("kasm.setup.shutil.which", lambda command: f"/bin/{command}")
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path))
    settings = tmp_path / ".gemini/settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "korean-bill-debate": {
                        "command": "attacker-command",
                        "args": ["--steal-data"],
                    }
                }
            }
        ),
        "utf-8",
    )
    monkeypatch.setattr(
        "kasm.setup.subprocess.run",
        lambda _command, **_kwargs: pytest.fail(
            "conflicting Gemini user settings must not be overwritten"
        ),
    )

    result = register_client("gemini")

    assert result["installed"] is False
    assert "does not match" in result["reason"]
