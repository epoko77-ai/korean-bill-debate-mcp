from __future__ import annotations

import asyncio
import runpy
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

_MATRIX = runpy.run_path(
    str(Path(__file__).parents[2] / "scripts" / "smoke_remote_production_matrix.py")
)
Scenario = _MATRIX["Scenario"]
ChildResult = _MATRIX["ChildResult"]
_mount_scenarios = cast(Callable[[], tuple[Any, ...]], _MATRIX["_mount_scenarios"])
_exact_scenario = cast(Callable[[], Any], _MATRIX["_exact_scenario"])
_broad_scenarios = cast(Callable[[str], tuple[Any, Any]], _MATRIX["_broad_scenarios"])
_mixed_scenarios = cast(Callable[[str], tuple[Any, ...]], _MATRIX["_mixed_scenarios"])
_child_environment = cast(Callable[..., dict[str, str]], _MATRIX["_child_environment"])
_contains_credential = cast(Callable[[str, str], bool], _MATRIX["_contains_credential"])
_merge_continued_payload = cast(Callable[..., dict[str, Any]], _MATRIX["_merge_continued_payload"])
_acceptance_failures = cast(
    Callable[[Any, dict[str, Any]], list[str]], _MATRIX["_acceptance_failures"]
)
_exercise = cast(Callable[[Any], Any], _MATRIX["_exercise"])

_RESEARCH_ID = "research_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


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
        "first_overview_phase": "metadata",
        "first_overview_inventory_complete": True,
        "first_overview_source_complete": False,
        "first_overview_pending_total_known": False,
        "first_overview_coverage_complete": False,
        "first_overview_catalog_truncated": False,
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


def test_child_environment_only_passes_an_explicit_valid_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _first, terminal = _broad_scenarios("2026-07-14")
    monkeypatch.setenv(
        "KBD_SMOKE_EXISTING_RESEARCH_ID",
        "research_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )

    fresh = _child_environment(
        terminal,
        base_url="https://deployment.example",
        api_key="assembly-user-key",
    )
    continued = _child_environment(
        terminal,
        base_url="https://deployment.example",
        api_key="assembly-user-key",
        existing_research_id=_RESEARCH_ID,
        prior_research_elapsed_seconds=73.25,
    )

    assert "KBD_SMOKE_EXISTING_RESEARCH_ID" not in fresh
    assert continued["KBD_SMOKE_EXISTING_RESEARCH_ID"] == _RESEARCH_ID
    assert continued["KBD_SMOKE_WAIT_SECONDS"] == "527"


@pytest.mark.parametrize(
    "invalid_research_id",
    (
        "research_short",
        "research_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "research_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa extra",
        "gAAAAAcredentialshapedvaluecredentialshapedvalue",
    ),
)
def test_child_environment_rejects_an_invalid_continuation_identity(
    invalid_research_id: str,
) -> None:
    _first, terminal = _broad_scenarios("2026-07-14")

    with pytest.raises(RuntimeError, match="continued research identity is invalid"):
        _child_environment(
            terminal,
            base_url="https://deployment.example",
            api_key="assembly-user-key",
            existing_research_id=invalid_research_id,
        )


def test_continued_payload_preserves_first_orientation_and_accumulates_elapsed_time() -> None:
    first_payload = _successful_research_payload()
    first_payload.update(
        {
            "research_id": _RESEARCH_ID,
            "research_elapsed_seconds": 110.25,
            "research_receipt_seconds": 0.75,
            "first_overview_seconds": 109.5,
            "first_overview_phase": "metadata",
        }
    )
    terminal_payload = _successful_research_payload()
    terminal_payload.update(
        {
            "research_id": _RESEARCH_ID,
            "research_elapsed_seconds": 450.5,
            "research_receipt_seconds": 0.0,
            "first_overview_seconds": 0.1,
            "first_overview_phase": "final",
        }
    )

    merged = _merge_continued_payload(
        terminal_payload,
        first_payload,
        continuation_wall_seconds=451.75,
    )

    assert merged["research_elapsed_seconds"] == 562.0
    assert merged["research_receipt_seconds"] == 0.75
    assert merged["first_overview_seconds"] == 109.5
    assert merged["first_overview_phase"] == "metadata"
    _first, terminal = _broad_scenarios("2026-07-14")
    assert _acceptance_failures(terminal, merged) == []


def test_continued_broad_threshold_includes_time_spent_reaching_first_overview() -> None:
    first_payload = _successful_research_payload()
    first_payload.update(
        {
            "research_id": _RESEARCH_ID,
            "research_elapsed_seconds": 110.25,
        }
    )
    terminal_payload = _successful_research_payload()
    terminal_payload.update(
        {
            "research_id": _RESEARCH_ID,
            "research_elapsed_seconds": 500.0,
        }
    )

    merged = _merge_continued_payload(terminal_payload, first_payload)
    _first, terminal = _broad_scenarios("2026-07-14")

    assert merged["research_elapsed_seconds"] == 610.25
    assert "broad terminal result exceeded 600 seconds" in _acceptance_failures(terminal, merged)


def test_broad_suite_reuses_the_first_child_research_instead_of_starting_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_payload = _successful_research_payload()
    first_payload.update(
        {
            "research_id": _RESEARCH_ID,
            "research_elapsed_seconds": 75.0,
        }
    )
    terminal_payload = _successful_research_payload()
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_run_group(
        scenarios: tuple[Any, ...],
        *,
        base_url: str,
        api_key: str,
        continuation_payload: dict[str, Any] | None = None,
    ) -> list[Any]:
        del base_url, api_key
        scenario = scenarios[0]
        calls.append((scenario.role, continuation_payload))
        payload = first_payload if scenario.role == "broad_first" else terminal_payload
        return [
            ChildResult(
                scenario=scenario,
                passed=True,
                wall_seconds=1.0,
                payload=payload,
                failures=(),
            )
        ]

    monkeypatch.setenv("ASSEMBLY_OPEN_API_KEY", "assembly-user-key")
    monkeypatch.setitem(_exercise.__globals__, "_run_group", fake_run_group)

    result = asyncio.run(
        _exercise(
            SimpleNamespace(
                suite="broad",
                base_url="https://deployment.example",
                broad_date_to="2026-07-14",
                allow_mixed_load=False,
            )
        )
    )

    assert result["passed"] is True
    assert calls == [("broad_first", None), ("broad_terminal", first_payload)]


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
    assert "broad first overview exceeded 120 seconds" in _acceptance_failures(terminal, payload)


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("first_overview_source_complete", True),
        ("first_overview_pending_total_known", True),
        ("first_overview_coverage_complete", True),
        ("first_overview_inventory_complete", None),
        ("first_overview_catalog_truncated", None),
    ),
)
def test_metadata_orientation_fail_closed_fields_are_release_gates(
    field: str,
    invalid: Any,
) -> None:
    payload = _successful_research_payload()
    payload[field] = invalid

    failures = _acceptance_failures(_exact_scenario(), payload)

    assert "metadata orientation violated fail-closed readiness semantics" in failures


def test_final_orientation_requires_known_terminal_accounting() -> None:
    payload = _successful_research_payload()
    payload.update(
        {
            "first_overview_phase": "final",
            "first_overview_inventory_complete": None,
            "first_overview_source_complete": False,
            "first_overview_pending_total_known": True,
            "first_overview_coverage_complete": False,
            "first_overview_catalog_truncated": None,
        }
    )
    assert _acceptance_failures(_exact_scenario(), payload) == []

    payload["first_overview_pending_total_known"] = False
    assert "final orientation readiness semantics are inconsistent" in _acceptance_failures(
        _exact_scenario(), payload
    )


def test_public_report_is_an_allow_list_and_omits_raw_child_payload() -> None:
    scenario = _exact_scenario()
    payload = _successful_research_payload()
    payload["unexpected_secret"] = "must-not-appear"
    payload["research_id"] = _RESEARCH_ID
    result = ChildResult(
        scenario=scenario,
        passed=True,
        wall_seconds=2.5,
        payload=payload,
        failures=(),
    ).report()
    assert "must-not-appear" not in str(result)
    assert "unexpected_secret" not in str(result)
    assert _RESEARCH_ID not in str(result)
    assert result["metrics"]["long_text_characters"] == 75_000
