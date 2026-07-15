"""Run the post-deploy connector-protocol, correctness, and bounded-load matrix.

This orchestrator calls ``smoke_remote_durable_oauth.py`` in isolated child processes. It never
passes an LLM credential to a child, never invokes the workspace synthesis endpoint, and only emits
an allow-listed metrics report. Mixed load is capped at eight clients and requires an explicit
``--allow-mixed-load`` flag. The OAuth scenarios validate live server compatibility with platform
Origins and HTTPS callbacks; they do not impersonate a logged-in Claude.ai or ChatGPT browser UI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).with_name("smoke_remote_durable_oauth.py")
_CLAUDE_ORIGIN = "https://claude.ai"
_CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
_CHATGPT_ORIGIN = "https://chatgpt.com"
_CHATGPT_REPRESENTATIVE_CALLBACK = "https://chatgpt.com/kbd-mcp-protocol-callback"
_EXACT_QUERY = (
    "2219564번 의안의 처리상태, 회의록, 전문위원 검토보고서를 공식 원문 기준으로 조사해줘"
)
_BROAD_QUERY = "2026년 7월 인공지능 관련 법안과 위원회 논의를 공식 원문 기준으로 조사해줘"
_SAFE_PARENT_ENV = (
    "ALL_PROXY",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "NO_PROXY",
    "PATH",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "TMPDIR",
)
_ACCEPTANCE_THRESHOLDS = {
    "oauth_approval_seconds": 5,
    "research_receipt_seconds": 15,
    "exact_first_overview_seconds": 35,
    "exact_terminal_seconds": 180,
    "broad_first_overview_seconds": 120,
    "broad_terminal_seconds": 600,
    "mixed_exact_first_overview_seconds": 60,
    "mixed_exact_terminal_seconds": 300,
    "mixed_broad_first_overview_seconds": 180,
    "mixed_broad_terminal_seconds": 600,
    "mixed_clients": 8,
    "critical_http_failures": 0,
    "duplicate_identities": 0,
}
_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bgAAAAA[A-Za-z0-9_-]{12,}"),
    re.compile(r"/mcp/t/[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)(?:code|access_token|refresh_token)=([^&\s]{8,})"),
)


@dataclass(frozen=True)
class Scenario:
    name: str
    role: str
    platform: str
    origin: str
    callback_uri: str
    wait_seconds: int = 0
    connection_only: bool = False
    stop_at_overview: bool = False
    exhaustive: bool = False
    query: str = _EXACT_QUERY
    expected_bill: str = "2219564"
    date_from: str = ""
    date_to: str = ""

    @property
    def process_timeout_seconds(self) -> int:
        return max(180, self.wait_seconds + 180)


@dataclass(frozen=True)
class ChildResult:
    scenario: Scenario
    passed: bool
    wall_seconds: float
    payload: dict[str, Any] | None
    failures: tuple[str, ...]
    error: str | None = None

    def report(self) -> dict[str, Any]:
        payload = self.payload or {}
        oauth = payload.get("oauth") if isinstance(payload.get("oauth"), dict) else {}
        http = payload.get("http") if isinstance(payload.get("http"), dict) else {}
        return {
            "name": self.scenario.name,
            "role": self.scenario.role,
            "platform": self.scenario.platform,
            "passed": self.passed,
            "wall_seconds": round(self.wall_seconds, 3),
            "failures": list(self.failures),
            "error": self.error,
            "metrics": {
                "authorization_seconds": oauth.get("authorization_seconds"),
                "tool_count": payload.get("tool_count"),
                "research_receipt_seconds": payload.get("research_receipt_seconds"),
                "first_overview_seconds": payload.get("first_overview_seconds"),
                "first_overview_phase": payload.get("first_overview_phase"),
                "first_overview_inventory_complete": payload.get(
                    "first_overview_inventory_complete"
                ),
                "first_overview_source_complete": payload.get("first_overview_source_complete"),
                "first_overview_pending_total_known": payload.get(
                    "first_overview_pending_total_known"
                ),
                "first_overview_coverage_complete": payload.get("first_overview_coverage_complete"),
                "first_overview_catalog_truncated": payload.get("first_overview_catalog_truncated"),
                "accepted_total": payload.get("first_overview_accepted_total"),
                "metadata_or_final_catalog_pages": payload.get("first_overview_catalog_pages"),
                "terminal_status": payload.get("terminal_status"),
                "research_elapsed_seconds": payload.get("research_elapsed_seconds"),
                "final_catalog_total": payload.get("final_catalog_total"),
                "final_catalog_pages": payload.get("final_catalog_pages"),
                "evidence_total": payload.get("evidence_inventory_total")
                or payload.get("evidence_count"),
                "evidence_pages": payload.get("evidence_inventory_pages"),
                "long_text_characters": payload.get("long_text_characters"),
                "long_text_calls": payload.get("long_text_calls"),
                "first_overview_duplicates": payload.get("first_overview_duplicate_count"),
                "final_catalog_duplicates": payload.get("final_catalog_duplicate_count"),
                "evidence_duplicates": payload.get("evidence_duplicate_count"),
                "slowest_status_seconds": payload.get("slowest_status_seconds"),
            },
            "http": {
                "request_count": http.get("request_count"),
                "status_counts": http.get("status_counts"),
                "failure_status_counts": http.get("failure_status_counts"),
                "critical_failure_count": http.get("critical_failure_count"),
                "slowest_seconds": http.get("slowest_seconds"),
            },
        }


def _mount_scenarios() -> tuple[Scenario, ...]:
    # OpenAI does not publish one immutable ChatGPT callback URI. Operators can
    # inject the exact URI observed during a real connector registration; the
    # default deliberately identifies itself as a representative HTTPS path.
    chatgpt_callback = (
        os.getenv("KBD_CHATGPT_REDIRECT_URI", "").strip() or _CHATGPT_REPRESENTATIVE_CALLBACK
    )
    return (
        Scenario(
            name="claude_protocol_compatibility",
            role="protocol",
            platform="claude.ai",
            origin=_CLAUDE_ORIGIN,
            callback_uri=_CLAUDE_CALLBACK,
            connection_only=True,
        ),
        Scenario(
            name="chatgpt_protocol_compatibility",
            role="protocol",
            platform="chatgpt.com",
            origin=_CHATGPT_ORIGIN,
            callback_uri=chatgpt_callback,
            connection_only=True,
        ),
    )


def _exact_scenario() -> Scenario:
    return Scenario(
        name="exact_2219564_exhaustive",
        role="exact",
        platform="chatgpt.com",
        origin=_CHATGPT_ORIGIN,
        callback_uri=(
            os.getenv("KBD_CHATGPT_REDIRECT_URI", "").strip() or _CHATGPT_REPRESENTATIVE_CALLBACK
        ),
        wait_seconds=180,
        exhaustive=True,
    )


def _broad_scenarios(date_to: str) -> tuple[Scenario, Scenario]:
    common = {
        "query": _BROAD_QUERY,
        "expected_bill": "",
        "date_from": "2026-07-01",
        "date_to": date_to,
    }
    return (
        Scenario(
            name="broad_ai_july_first_overview",
            role="broad_first",
            platform="chatgpt.com",
            origin=_CHATGPT_ORIGIN,
            callback_uri=(
                os.getenv("KBD_CHATGPT_REDIRECT_URI", "").strip()
                or _CHATGPT_REPRESENTATIVE_CALLBACK
            ),
            wait_seconds=120,
            stop_at_overview=True,
            **common,
        ),
        Scenario(
            name="broad_ai_july_terminal",
            role="broad_terminal",
            platform="claude.ai",
            origin=_CLAUDE_ORIGIN,
            callback_uri=_CLAUDE_CALLBACK,
            wait_seconds=600,
            **common,
        ),
    )


def _mixed_scenarios(date_to: str) -> tuple[Scenario, ...]:
    broad_common = {
        "role": "mixed_broad",
        "wait_seconds": 600,
        "query": _BROAD_QUERY,
        "expected_bill": "",
        "date_from": "2026-07-01",
        "date_to": date_to,
    }
    broad = (
        Scenario(
            name="mixed_broad_1_claude",
            platform="claude.ai",
            origin=_CLAUDE_ORIGIN,
            callback_uri=_CLAUDE_CALLBACK,
            **broad_common,
        ),
        Scenario(
            name="mixed_broad_2_chatgpt",
            platform="chatgpt.com",
            origin=_CHATGPT_ORIGIN,
            callback_uri=(
                os.getenv("KBD_CHATGPT_REDIRECT_URI", "").strip()
                or _CHATGPT_REPRESENTATIVE_CALLBACK
            ),
            **broad_common,
        ),
    )
    exact = tuple(
        Scenario(
            name=f"mixed_exact_{position}_{platform}",
            role="mixed_exact",
            platform=f"{platform}.com" if platform == "chatgpt" else "claude.ai",
            origin=_CHATGPT_ORIGIN if platform == "chatgpt" else _CLAUDE_ORIGIN,
            callback_uri=(
                (
                    os.getenv("KBD_CHATGPT_REDIRECT_URI", "").strip()
                    or _CHATGPT_REPRESENTATIVE_CALLBACK
                )
                if platform == "chatgpt"
                else _CLAUDE_CALLBACK
            ),
            wait_seconds=300,
        )
        for position, platform in enumerate(
            ("claude", "chatgpt", "claude", "chatgpt", "claude", "chatgpt"),
            start=1,
        )
    )
    scenarios = (*broad, *exact)
    if len(scenarios) != _ACCEPTANCE_THRESHOLDS["mixed_clients"]:
        raise AssertionError("mixed production smoke must remain capped at eight clients")
    return scenarios


def _child_environment(
    scenario: Scenario,
    *,
    base_url: str,
    api_key: str,
) -> dict[str, str]:
    """Build an allow-listed environment that cannot expose paid LLM credentials."""

    child = {
        name: value for name in _SAFE_PARENT_ENV if (value := os.environ.get(name)) is not None
    }
    child.update(
        {
            "ASSEMBLY_OPEN_API_KEY": api_key,
            "KBD_REMOTE_BASE_URL": base_url,
            "KBD_SMOKE_ORIGIN": scenario.origin,
            "KBD_SMOKE_REDIRECT_URI": scenario.callback_uri,
            "KBD_SMOKE_REQUIRE_WEB_CALLBACK": "1",
            "KBD_SMOKE_WAIT_SECONDS": str(scenario.wait_seconds),
            "KBD_SMOKE_QUERY": scenario.query,
            "KBD_SMOKE_EXPECT_BILL_NUMBER": scenario.expected_bill,
            "KBD_SMOKE_ASSEMBLY_TERM": "22",
            "KBD_SMOKE_DATE_FROM": scenario.date_from,
            "KBD_SMOKE_DATE_TO": scenario.date_to,
            "KBD_SMOKE_CLIENT_NAME": f"KBD post-deploy {scenario.name}",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
    )
    if scenario.connection_only:
        child["KBD_SMOKE_CONNECTION_ONLY"] = "1"
    if scenario.stop_at_overview:
        child["KBD_SMOKE_STOP_AT_OVERVIEW"] = "1"
    if scenario.exhaustive:
        child["KBD_SMOKE_EXHAUSTIVE"] = "1"
    return child


def _contains_credential(output: str, api_key: str) -> bool:
    if api_key and api_key in output:
        return True
    return any(pattern.search(output) for pattern in _CREDENTIAL_PATTERNS)


def _safe_error(stderr: str, api_key: str) -> str:
    if _contains_credential(stderr, api_key):
        return "child failed; credential-shaped output was suppressed"
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return "child exited without a JSON result"
    value = lines[-1]
    value = re.sub(r"(https?://[^?\s]+)\?[^\s]+", r"\1?[redacted]", value)
    value = re.sub(
        r"(?i)(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token)"
        r"\s*[:=]\s*\S+",
        r"\1=[redacted]",
        value,
    )
    return value[:500]


def _number(payload: dict[str, Any], name: str) -> float | None:
    value = payload.get(name)
    return float(value) if isinstance(value, int | float) else None


def _acceptance_failures(scenario: Scenario, payload: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    oauth = payload.get("oauth") if isinstance(payload.get("oauth"), dict) else {}
    http = payload.get("http") if isinstance(payload.get("http"), dict) else {}
    if payload.get("passed") is not True:
        failures.append("child did not report passed=true")
    if payload.get("tool_count") != 13 or payload.get("all_tools_read_only") is not True:
        failures.append("MCP surface is not exactly 13 read-only tools")
    if (
        oauth.get("dynamic_registration") is not True
        or oauth.get("pkce") is not True
        or oauth.get("offline_refresh") is not True
        or oauth.get("web_callback") is not True
    ):
        failures.append("DCR/PKCE/refresh/HTTPS callback contract is incomplete")
    authorization_seconds = _number(oauth, "authorization_seconds")
    if (
        authorization_seconds is None
        or authorization_seconds > _ACCEPTANCE_THRESHOLDS["oauth_approval_seconds"]
    ):
        failures.append("OAuth approval exceeded 5 seconds")
    if int(http.get("critical_failure_count") or 0):
        failures.append("HTTP 429 or 5xx was observed")
    if scenario.role == "protocol":
        if payload.get("connection_only") is not True:
            failures.append("protocol compatibility smoke unexpectedly started research")
        return failures

    receipt_seconds = _number(payload, "research_receipt_seconds")
    if (
        receipt_seconds is None
        or receipt_seconds > _ACCEPTANCE_THRESHOLDS["research_receipt_seconds"]
    ):
        failures.append("research receipt exceeded 15 seconds")
    if payload.get("first_overview_verified") is not True:
        failures.append("first candidate orientation was not verified")
    if payload.get("first_overview_duplicate_count") != 0:
        failures.append("first candidate map duplicate count was not verified as zero")
    first_seconds = _number(payload, "first_overview_seconds")
    accepted_total = int(payload.get("first_overview_accepted_total") or 0)
    if accepted_total < 1:
        failures.append("candidate map contained no accepted entities")
    first_phase = payload.get("first_overview_phase")
    if first_phase == "metadata":
        if (
            payload.get("first_overview_source_complete") is not False
            or payload.get("first_overview_pending_total_known") is not False
            or payload.get("first_overview_coverage_complete") is not False
            or type(payload.get("first_overview_inventory_complete")) is not bool
            or type(payload.get("first_overview_catalog_truncated")) is not bool
        ):
            failures.append("metadata orientation violated fail-closed readiness semantics")
    elif first_phase == "final":
        if payload.get("first_overview_pending_total_known") is not True or (
            payload.get("first_overview_source_complete")
            is not payload.get("first_overview_coverage_complete")
        ):
            failures.append("final orientation readiness semantics are inconsistent")
    else:
        failures.append("first candidate orientation phase is invalid")

    if scenario.role == "broad_first":
        if (
            first_seconds is None
            or first_seconds > _ACCEPTANCE_THRESHOLDS["broad_first_overview_seconds"]
        ):
            failures.append("broad first overview exceeded 120 seconds")
        return failures

    if payload.get("terminal_status") not in {"complete", "partial"}:
        failures.append("research did not reach a valid terminal state")
    if payload.get("final_overview_verified") is not True:
        failures.append("terminal final overview was not verified")
    elapsed = _number(payload, "research_elapsed_seconds")
    if scenario.role == "exact":
        if (
            first_seconds is None
            or first_seconds > _ACCEPTANCE_THRESHOLDS["exact_first_overview_seconds"]
        ):
            failures.append("exact first overview exceeded 35 seconds")
        if elapsed is None or elapsed > _ACCEPTANCE_THRESHOLDS["exact_terminal_seconds"]:
            failures.append("exact terminal result exceeded 180 seconds")
        if payload.get("exact_bill_verified") is not True:
            failures.append("exact bill 2219564 identity was not preserved")
        if payload.get("exhaustive_verified") is not True:
            failures.append("exact result was not exhaustively traversed")
        if int(payload.get("final_catalog_total") or 0) < 1:
            failures.append("exact final catalog was empty")
        if int(payload.get("evidence_inventory_total") or 0) < 1:
            failures.append("exact evidence inventory was empty")
        if int(payload.get("long_text_characters") or 0) < 1:
            failures.append("long official text was not reconstructed and hash-verified")
        if payload.get("final_catalog_duplicate_count") != 0:
            failures.append("exact final catalog duplicate count was not verified as zero")
        if payload.get("evidence_duplicate_count") != 0:
            failures.append("exact evidence duplicate count was not verified as zero")
    elif scenario.role == "broad_terminal":
        if (
            first_seconds is None
            or first_seconds > _ACCEPTANCE_THRESHOLDS["broad_first_overview_seconds"]
        ):
            failures.append("broad first overview exceeded 120 seconds")
        if elapsed is None or elapsed > _ACCEPTANCE_THRESHOLDS["broad_terminal_seconds"]:
            failures.append("broad terminal result exceeded 600 seconds")
        if int(payload.get("evidence_count") or 0) < 1:
            failures.append("broad terminal evidence was empty")
    elif scenario.role == "mixed_exact":
        if (
            first_seconds is None
            or first_seconds > _ACCEPTANCE_THRESHOLDS["mixed_exact_first_overview_seconds"]
        ):
            failures.append("mixed exact first overview exceeded 60 seconds")
        if elapsed is None or elapsed > _ACCEPTANCE_THRESHOLDS["mixed_exact_terminal_seconds"]:
            failures.append("mixed exact terminal result exceeded 300 seconds")
        if payload.get("exact_bill_verified") is not True:
            failures.append("mixed exact bill identity was not preserved")
    elif scenario.role == "mixed_broad":
        if (
            first_seconds is None
            or first_seconds > _ACCEPTANCE_THRESHOLDS["mixed_broad_first_overview_seconds"]
        ):
            failures.append("mixed broad first overview exceeded 180 seconds")
        if elapsed is None or elapsed > _ACCEPTANCE_THRESHOLDS["mixed_broad_terminal_seconds"]:
            failures.append("mixed broad terminal result exceeded 600 seconds")
        if int(payload.get("evidence_count") or 0) < 1:
            failures.append("mixed broad terminal evidence was empty")
    return failures


async def _run_child(
    scenario: Scenario,
    *,
    base_url: str,
    api_key: str,
) -> ChildResult:
    environment = _child_environment(scenario, base_url=base_url, api_key=api_key)
    started = time.perf_counter()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(_SCRIPT),
        cwd=str(_SCRIPT.parent.parent),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=scenario.process_timeout_seconds,
        )
    except TimeoutError:
        process.kill()
        await process.communicate()
        return ChildResult(
            scenario=scenario,
            passed=False,
            wall_seconds=time.perf_counter() - started,
            payload=None,
            failures=("child process exceeded its bounded deadline",),
            error="timeout",
        )
    wall_seconds = time.perf_counter() - started
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    if _contains_credential(stdout, api_key) or _contains_credential(stderr, api_key):
        return ChildResult(
            scenario=scenario,
            passed=False,
            wall_seconds=wall_seconds,
            payload=None,
            failures=("child output contained credential-shaped data and was suppressed",),
            error="redaction guard",
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return ChildResult(
            scenario=scenario,
            passed=False,
            wall_seconds=wall_seconds,
            payload=None,
            failures=("child output was not one JSON object",),
            error="invalid JSON",
        )
    if not isinstance(payload, dict):
        return ChildResult(
            scenario=scenario,
            passed=False,
            wall_seconds=wall_seconds,
            payload=None,
            failures=("child JSON result was not an object",),
            error="invalid JSON shape",
        )
    if process.returncode != 0:
        reported_error = payload.get("error")
        return ChildResult(
            scenario=scenario,
            passed=False,
            wall_seconds=wall_seconds,
            payload=payload,
            failures=(f"child exited with status {process.returncode}",),
            error=(
                str(reported_error)[:500]
                if isinstance(reported_error, str)
                else _safe_error(stderr, api_key)
            ),
        )
    failures = tuple(_acceptance_failures(scenario, payload))
    return ChildResult(
        scenario=scenario,
        passed=not failures,
        wall_seconds=wall_seconds,
        payload=payload,
        failures=failures,
    )


async def _run_group(
    scenarios: tuple[Scenario, ...],
    *,
    base_url: str,
    api_key: str,
) -> list[ChildResult]:
    return list(
        await asyncio.gather(
            *(_run_child(scenario, base_url=base_url, api_key=api_key) for scenario in scenarios)
        )
    )


def _summary(results: list[ChildResult]) -> dict[str, Any]:
    reports = [result.report() for result in results]
    critical_http = sum(
        int(report["http"].get("critical_failure_count") or 0) for report in reports
    )
    return {
        "scenario_count": len(results),
        "automated_verification_scope": (
            "live OAuth and MCP protocol compatibility; not logged-in platform UI automation"
        ),
        "passed_count": sum(result.passed for result in results),
        "failed_count": sum(not result.passed for result in results),
        "critical_http_failure_count": critical_http,
        "acceptance_thresholds": _ACCEPTANCE_THRESHOLDS,
        "max_wall_seconds": round(max((result.wall_seconds for result in results), default=0.0), 3),
        "results": reports,
        "passed": bool(results) and all(result.passed for result in results),
    }


async def _exercise(args: argparse.Namespace) -> dict[str, Any]:
    api_key = os.getenv("ASSEMBLY_OPEN_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ASSEMBLY_OPEN_API_KEY is required")
    base_url = str(args.base_url).rstrip("/")
    if not base_url.startswith("https://"):
        raise RuntimeError("production matrix base URL must use HTTPS")
    if args.suite in {"mixed", "all"} and not args.allow_mixed_load:
        raise RuntimeError("mixed load requires --allow-mixed-load")

    results: list[ChildResult] = []
    if args.suite in {"mount", "protocol", "all"}:
        protocol_results = await _run_group(_mount_scenarios(), base_url=base_url, api_key=api_key)
        results.extend(protocol_results)
        if args.suite == "all" and not all(result.passed for result in protocol_results):
            return {"base_url": base_url, "suite": args.suite, **_summary(results)}

    if args.suite in {"exact", "all"}:
        results.extend(await _run_group((_exact_scenario(),), base_url=base_url, api_key=api_key))

    if args.suite in {"broad", "all"}:
        broad_first, broad_terminal = _broad_scenarios(str(args.broad_date_to))
        first_results = await _run_group((broad_first,), base_url=base_url, api_key=api_key)
        results.extend(first_results)
        if first_results[0].passed:
            results.extend(await _run_group((broad_terminal,), base_url=base_url, api_key=api_key))

    if args.suite in {"mixed", "all"}:
        prerequisites = [
            result
            for result in results
            if result.scenario.role in {"protocol", "exact", "broad_first", "broad_terminal"}
        ]
        if args.suite == "mixed" or all(result.passed for result in prerequisites):
            results.extend(
                await _run_group(
                    _mixed_scenarios(str(args.broad_date_to)),
                    base_url=base_url,
                    api_key=api_key,
                )
            )

    return {"base_url": base_url, "suite": args.suite, **_summary(results)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=("protocol", "mount", "exact", "broad", "mixed", "all"),
        required=True,
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("KBD_REMOTE_BASE_URL", "https://korean-bill-debate-mcp.vercel.app"),
    )
    parser.add_argument("--broad-date-to", default="2026-07-14")
    parser.add_argument(
        "--allow-mixed-load",
        action="store_true",
        help="explicitly permit the capped broad2+exact6 production load phase",
    )
    return parser


def main() -> int:
    try:
        result = asyncio.run(_exercise(_parser().parse_args()))
    except Exception as exc:  # noqa: BLE001 - CLI emits a deliberately sanitized failure
        result = {
            "passed": False,
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
