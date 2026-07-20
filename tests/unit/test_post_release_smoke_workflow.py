from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/post-release-smoke.yml"


def test_post_release_smoke_has_bounded_release_and_manual_triggers() -> None:
    text = WORKFLOW.read_text("utf-8")

    assert "  release:\n    types: [published]\n" in text
    assert "  workflow_dispatch:\n" in text
    assert "  contents: read\n" in text
    assert "  group: post-release-production-oauth-smoke\n" in text
    assert "  cancel-in-progress: false\n" in text
    assert "    timeout-minutes: 15\n" in text
    assert "    environment: Production\n" in text


def test_post_release_smoke_checks_out_the_release_and_waits_for_its_version() -> None:
    text = WORKFLOW.read_text("utf-8")

    assert "ref: ${{ github.event.release.tag_name || github.ref }}" in text
    assert "expected=$(uv run python -c 'from kasm import __version__" in text
    assert '"$KBD_REMOTE_BASE_URL/healthz"' in text
    assert 'if [ "$deployed" = "$expected" ]; then' in text
    assert 'while [ "$attempt" -le 20 ]; do' in text


def test_post_release_smoke_passes_only_the_required_user_key() -> None:
    text = WORKFLOW.read_text("utf-8")

    assert "ASSEMBLY_OPEN_API_KEY: ${{ secrets.ASSEMBLY_OPEN_API_KEY }}" in text
    assert 'test -n "$ASSEMBLY_OPEN_API_KEY"' in text
    mount = text.index("smoke_remote_production_matrix.py --suite mount")
    exact = text.index("smoke_remote_production_matrix.py --suite exact")
    assert mount < exact
    for suite in ("broad", "mixed", "all"):
        assert f"smoke_remote_production_matrix.py --suite {suite}" not in text
    assert "--allow-mixed-load" not in text
    assert "upload-artifact" not in text
    for paid_secret in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        assert paid_secret not in text
