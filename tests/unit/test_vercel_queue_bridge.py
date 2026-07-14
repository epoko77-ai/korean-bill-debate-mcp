from __future__ import annotations

import json
import tomllib
from pathlib import Path


def test_vercel_queue_trigger_preserves_python_function_and_rewrite() -> None:
    root = Path(__file__).resolve().parents[2]
    config = json.loads((root / "vercel.json").read_text())

    assert (root / "serverless/kbd-research-shared.mjs").is_file()
    assert not (root / "api/queues/kbd-research-shared.mjs").exists()

    # The project was once detected as the monolithic `python` framework,
    # which collapsed both Python entrypoints into one large MCP Lambda.  The
    # generic Functions build must preserve index and worker as independent
    # cold-start and CPU boundaries.
    assert config["framework"] is None
    assert config["outputDirectory"] == "public"
    assert config["regions"] == ["icn1"]
    excluded = (
        "{.git/**,.github/**,.venv/**,.uv-cache/**,.mypy_cache/**,.pytest_cache/**,"
        ".ruff_cache/**,.vercel/**,node_modules/**,assets/**,benchmarks/**,build/**,"
        "dist/**,docs/**,scripts/**,tests/**}"
    )
    assert config["functions"]["api/index.py"] == {
        "maxDuration": 300,
        "includeFiles": "src/**",
        "excludeFiles": excluded,
    }
    assert config["functions"]["api/research_dispatch.py"] == {
        "maxDuration": 300,
        "includeFiles": "src/**",
        "excludeFiles": excluded,
    }
    queue_function = config["functions"]["api/queues/kbd-research.ts"]
    assert queue_function["maxDuration"] == 300
    assert queue_function["experimentalTriggers"] == [
        {
            "type": "queue/v2beta",
            "topic": "kbd-research",
            "retryAfterSeconds": 15,
            "initialDelaySeconds": 0,
            "maxConcurrency": 8,
        }
    ]
    assert config["functions"]["api/queues/kbd-research-recovery.ts"] == {
        "maxDuration": 300,
    }
    assert config["crons"] == [
        {
            "path": "/_internal/research/recover",
            "schedule": "* * * * *",
        }
    ]
    assert config["rewrites"] == [
        {
            "source": "/_internal/research/recover",
            "destination": "/api/queues/kbd-research-recovery",
        },
        {
            "source": "/_internal/research/dispatch",
            "destination": "/api/research_dispatch",
        },
        {"source": "/(.*)", "destination": "/api/index"}
    ]


def test_queue_bridge_never_reads_failure_body_or_logs_task() -> None:
    root = Path(__file__).resolve().parents[2]
    entry = (root / "api/queues/kbd-research.ts").read_text()
    shared = (root / "serverless/kbd-research-shared.mjs").read_text()
    source = entry + shared

    assert 'handleCallback<unknown>' in entry
    assert 'handleNodeCallback<unknown>' in entry
    assert "export default nodeQueueRoute" in entry
    assert 'currentDeploymentOrigin(request)' in entry
    assert '../../serverless/kbd-research-shared.mjs' in entry
    assert 'new URL(INTERNAL_PATH, deploymentOrigin)' in source
    assert 'response.body?.cancel()' in source
    assert 'const error = `research dispatch failed (${response.status})`' in source
    assert "metadata.deliveryCount" in source
    assert '"x-kbd-delivery-count"' in source
    assert "MAX_PERMANENT_DELIVERY_ATTEMPTS = 3" in source
    assert "MAX_NORMAL_DELIVERY_ATTEMPTS = 10" in source
    assert '"x-kbd-terminal-failure"' in source
    assert '"task_retry_budget_exhausted"' in source
    assert '"x-kbd-dispatch-error-class"' in source
    assert '"permanent-task"' in source
    assert "return { acknowledge: true }" in source
    assert "error instanceof PermanentDispatchError" in source
    assert '"x-vercel-oidc-token"' in source
    assert '"x-vercel-trusted-oidc-idp-token"' in source
    assert "DISPATCH_TIMEOUT_MS = 270_000" in source
    assert "TERMINAL_FAILURE_TIMEOUT_MS = 25_000" in source
    assert "response.headers.get(ERROR_CLASS_HEADER)" in source
    assert "response.text()" not in source
    assert "console." not in source


def test_vercel_upload_excludes_local_credentials_and_build_state() -> None:
    root = Path(__file__).resolve().parents[2]
    patterns = set((root / ".vercelignore").read_text().splitlines())

    assert {".env", ".env.*", ".vercel", "node_modules", "build", "dist"} <= patterns
    assert "!.env.example" in patterns


def test_vercel_python_sdk_is_a_base_runtime_dependency() -> None:
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    dependencies = project["project"]["dependencies"]

    assert any(value.startswith("vercel>=0.6") for value in dependencies)
