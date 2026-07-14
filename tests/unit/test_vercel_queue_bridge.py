from __future__ import annotations

import json
import tomllib
from pathlib import Path


def test_vercel_queue_trigger_preserves_python_function_and_rewrite() -> None:
    root = Path(__file__).resolve().parents[2]
    config = json.loads((root / "vercel.json").read_text())

    assert config["regions"] == ["icn1"]
    assert config["functions"]["api/index.py"] == {
        "maxDuration": 300,
        "includeFiles": "src/**",
    }
    assert config["functions"]["api/research_dispatch.py"] == {
        "maxDuration": 300,
        "includeFiles": "src/**",
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
    assert config["rewrites"] == [
        {
            "source": "/_internal/research/dispatch",
            "destination": "/api/research_dispatch",
        },
        {"source": "/(.*)", "destination": "/api/index"}
    ]


def test_queue_bridge_never_reads_failure_body_or_logs_task() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "api/queues/kbd-research.ts").read_text()

    assert 'handleCallback<unknown>' in source
    assert 'handleNodeCallback<unknown>' in source
    assert "export default nodeQueueRoute" in source
    assert 'currentDeploymentOrigin(request)' in source
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
