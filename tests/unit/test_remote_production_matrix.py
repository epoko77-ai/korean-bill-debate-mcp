from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

_MATRIX = runpy.run_path(
    str(Path(__file__).parents[2] / "scripts" / "smoke_remote_production_matrix.py")
)
Scenario = _MATRIX["Scenario"]
ChildResult = _MATRIX["ChildResult"]
_mount_scenarios = cast(Callable[[], tuple[Any, ...]], _MATRIX["_mount_scenarios"])
_exact_scenario = cast(Callable[[], Any], _MATRIX["_exact_scenario"])
_broad_scenarios = cast(
    Callable[[str], tuple[Any, Any]], _MATRIX["_broad_scenarios"]
)
_mixed_scenarios = cast(
    Callable[[str], tuple[Any, ...]], _MATRIX["_mixed_scenarios"]
)
_child_environment = cast(Callable[..., dict[str, str]], _MATRIX["_child_environment"])
_contains_credential = cast(
    Callable[[str, str], bool], _MATRIX["_contains_credential"]
)
_acceptance_failures = cast(
    Callable[[Any, dict[str, Any]], list[str]], _MATRIX["_acceptance_failures"]
)


def _successful_research_payload() -> dict[str, Any]:
    return {
        "passed": True,
        "tool_count": 13,
        "all_tools_read_only": True,
        "oauth": {
            "dynamic_registration": True,
            "pkce": True,
            "offline_refresh": True,
            "web_callback": True,
            "authorization_seconds": 0.25,
        },
        "http": {"critical_failure_count": 0},
        "research_receipt_seconds": 0.4,
        "first_overview_verified": True,
        "first_overview_seconds": 4.0,
        "first_overview_accepted_total": 2,
        "terminal_status": "complete",
        "final_overview_verified": True,
        "research_elapsed_seconds": 20.0,
        "exact_bill_verified": True,
        "exhaustive_verified": True,
        "final_catalog_total": 2,
        "evidence_inventory_total": 10,
        "evidence_count": 10,
        "long_text_characters": 75_000,
        "first_overview_duplicate_count": 0,
        "final_catalog_duplicate_count": 0,
        "evidence_duplicate_count": 0,
    }


def test_matrix_labels_live_protocol_checks_without_claiming_ui_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KBD_CHATGPT_REDIRECT_URI", raising=False)
    claude, chatgpt = _mount_scenarios()
    assert claude.name == "claude_protocol_compatibility"
    assert chatgpt.name == "chatgpt_protocol_compatibility"
    assert {claude.role, chatgpt.role} == {"protocol"}
    assert claude.origin == "https://claude.ai"
    assert claude.callback_uri == "https://claude.ai/api/mcp/auth_callback"
    assert chatgpt.origin == "https://chatgpt.com"
    assert chatgpt.callback_uri == "https://chatgpt.com/kbd-mcp-protocol-callback"
    assert all(item.connection_only for item in (claude, chatgpt))


def test_matrix_accepts_the_exact_observed_chatgpt_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback = "https://chatgpt.com/connector/observed-callback?surface=web"
    monkeypatch.setenv("KBD_CHATGPT_REDIRECT_URI", callback)

    _claude, chatgpt = _mount_scenarios()

    assert chatgpt.callback_uri == callback


def test_mixed_matrix_is_exactly_broad_two_plus_exact_six() -> None:
    scenarios = _mixed_scenarios("2026-07-14")
    assert len(scenarios) == 8
    assert sum(item.role == "mixed_broad" for item in scenarios) == 2
    assert sum(item.role == "mixed_exact" for item in scenarios) == 6
    assert {item.origin for item in scenarios} == {
        "https://claude.ai",
        "https://chatgpt.com",
    }


def test_child_environment_drops_every_paid_llm_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "paid-openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "paid-anthropic-secret")
    monkeypatch.setenv("GOOGLE_API_KEY", "paid-google-secret")
    child = _child_environment(
        _exact_scenario(),
        base_url="https://deployment.example",
        api_key="assembly-user-key",
    )
    assert child["ASSEMBLY_OPEN_API_KEY"] == "assembly-user-key"
    assert "OPENAI_API_KEY" not in child
    assert "ANTHROPIC_API_KEY" not in child
    assert "GOOGLE_API_KEY" not in child
    assert not any("LLM" in name for name in child)


@pytest.mark.parametrize(
    "output",
    [
        "Authorization: Bearer very-secret-token-value",
        "https://example.test/mcp/t/gAAAAABsecretsecretsecretsecret",
        "https://chatgpt.com/callback?code=one-time-secret-code",
        "sk-ant-api03-secretsecretsecret",
    ],
)
def test_output_guard_recognizes_credential_shaped_values(output: str) -> None:
    assert _contains_credential(output, "assembly-user-key")


def test_exact_acceptance_requires_exhaustive_catalog_inventory_and_long_text() -> None:
    payload = _successful_research_payload()
    assert _acceptance_failures(_exact_scenario(), payload) == []

    payload["long_text_characters"] = 0
    payload["first_overview_seconds"] = 36
    failures = _acceptance_failures(_exact_scenario(), payload)
    assert "exact first overview exceeded 35 seconds" in failures
    assert "long official text was not reconstructed and hash-verified" in failures


def test_broad_thresholds_distinguish_first_overview_and_terminal() -> None:
    first, terminal = _broad_scenarios("2026-07-14")
    payload = _successful_research_payload()
    payload["exact_bill_verified"] = False
    payload["exhaustive_verified"] = False
    assert _acceptance_failures(first, payload) == []
    assert _acceptance_failures(terminal, payload) == []

    payload["first_overview_seconds"] = 121
    assert "broad first overview exceeded 120 seconds" in _acceptance_failures(
        terminal, payload
    )


def test_public_report_is_an_allow_list_and_omits_raw_child_payload() -> None:
    scenario = _exact_scenario()
    payload = _successful_research_payload()
    payload["unexpected_secret"] = "must-not-appear"
    result = ChildResult(
        scenario=scenario,
        passed=True,
        wall_seconds=2.5,
        payload=payload,
        failures=(),
    ).report()
    assert "must-not-appear" not in str(result)
    assert "unexpected_secret" not in str(result)
    assert result["metrics"]["long_text_characters"] == 75_000
