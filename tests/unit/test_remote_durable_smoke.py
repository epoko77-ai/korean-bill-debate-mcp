from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

_SMOKE = runpy.run_path(
    str(Path(__file__).parents[2] / "scripts" / "smoke_remote_durable_oauth.py")
)
_extract_callback_result = cast(
    Callable[..., tuple[str, str | None]], _SMOKE["_extract_callback_result"]
)
_redirect_origin = cast(Callable[[str], str], _SMOKE["_redirect_origin"])
_safe_failure_message = cast(
    Callable[[Exception], str], _SMOKE["_safe_failure_message"]
)


def test_web_callback_is_intercepted_without_following_the_remote_location() -> None:
    redirect_uri = "https://claude.ai/api/mcp/auth_callback?connector=kbd"

    assert _redirect_origin(redirect_uri) == "https://claude.ai"
    assert _extract_callback_result(
        "https://claude.ai/api/mcp/auth_callback?connector=kbd&code=issued&state=opaque",
        redirect_uri=redirect_uri,
        expected_state="opaque",
    ) == ("issued", "opaque")


@pytest.mark.parametrize(
    ("location", "state"),
    [
        ("https://attacker.example/callback?code=issued&state=opaque", "opaque"),
        ("https://chatgpt.com/oauth/callback?code=issued&state=wrong", "opaque"),
        ("https://chatgpt.com/oauth/callback?error=denied&state=opaque", "opaque"),
        (
            "https://chatgpt.com/oauth/callback?code=first&code=second&state=opaque",
            "opaque",
        ),
    ],
)
def test_web_callback_rejects_wrong_target_error_state_or_duplicate_code(
    location: str,
    state: str,
) -> None:
    with pytest.raises(RuntimeError):
        _extract_callback_result(
            location,
            redirect_uri="https://chatgpt.com/oauth/callback",
            expected_state=state,
        )


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "javascript:alert(1)",
        "https://claude.ai/callback#fragment",
        "https://chatgpt.com/callback?code=reserved",
        "https://chatgpt.com/callback?state=reserved",
        "https://chatgpt.com/callback?error=reserved",
    ],
)
def test_smoke_rejects_unsafe_or_ambiguous_redirect_uri(redirect_uri: str) -> None:
    with pytest.raises(RuntimeError):
        _redirect_origin(redirect_uri)


def test_failure_output_redacts_credentials_and_callback_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASSEMBLY_OPEN_API_KEY", "assembly-secret-value")
    assert "assembly-secret-value" not in _safe_failure_message(
        RuntimeError("upstream echoed assembly-secret-value")
    )
    safe = _safe_failure_message(
        RuntimeError(
            "request https://chatgpt.com/callback?code=one-time-code-value "
            "used Bearer access-token-secret"
        )
    )
    assert "one-time-code-value" not in safe
    assert "access-token-secret" not in safe
