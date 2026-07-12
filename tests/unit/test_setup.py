from __future__ import annotations

import json
import stat

from kasm.adapters.korea.client import AssemblyOpenApiClient
from kasm.setup import register_client, save_api_key


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
