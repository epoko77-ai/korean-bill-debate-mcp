"""Orchestration for the no-account, request-scoped research workspace."""

from __future__ import annotations

import os
import tempfile
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kasm.live import create_live_services
from kasm.mcp.tools import KasmTools, ServiceContext

from .llm import LlmError, synthesize

ServicesFactory = Callable[..., ServiceContext]
Synthesizer = Callable[[str, str, str, dict[str, Any]], tuple[str, str]]


class WorkspaceError(RuntimeError):
    """Safe error returned to the browser without credential material."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def run_workspace_research(
    *,
    question: str,
    assembly_api_key: str,
    llm_provider: str,
    llm_api_key: str,
    services_factory: ServicesFactory = create_live_services,
    synthesizer: Synthesizer = synthesize,
) -> dict[str, Any]:
    """Research and synthesize once without persisting either credential."""
    query = question.strip()
    assembly_key = assembly_api_key.strip()
    provider = llm_provider.strip().lower()
    if not query or len(query) > 500:
        raise WorkspaceError("질문은 1자 이상 500자 이하로 입력해 주세요.")
    if not assembly_key or len(assembly_key) > 256:
        raise WorkspaceError("열린국회 API 키를 확인해 주세요.")
    if provider not in {"openai", "anthropic"}:
        raise WorkspaceError("OpenAI 또는 Anthropic을 선택해 주세요.")
    if not llm_api_key.strip() or len(llm_api_key.strip()) > 2048:
        raise WorkspaceError("LLM API 키를 확인해 주세요.")

    started = time.monotonic()
    temp_root = Path(os.getenv("KBD_WORKSPACE_TEMP_DIR", "/tmp"))
    try:
        with tempfile.TemporaryDirectory(prefix="kbd-workspace-", dir=temp_root) as data_dir:
            services = services_factory(
                api_key=assembly_key,
                data_dir=data_dir,
                max_minutes_per_request=int(
                    os.getenv("KBD_WORKSPACE_MAX_MINUTES_PER_REQUEST", "1")
                ),
            )
            research = KasmTools(services).explore_issue(query, limit=12)
            answer, model = synthesizer(provider, llm_api_key.strip(), query, research)
    except LlmError as exc:
        raise WorkspaceError(str(exc), status_code=502) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspaceError(
            "공식 국회 자료를 가져오지 못했습니다. "
            "열린국회 API 키와 잠시 후 재시도를 확인해 주세요.",
            status_code=502,
        ) from exc

    return {
        "answer": answer,
        "provider": provider,
        "model": model,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "evidence": _evidence_summary(research),
    }


def _evidence_summary(research: dict[str, Any]) -> dict[str, Any]:
    raw_bills = research.get("bills")
    raw_speeches = research.get("speeches")
    raw_threads = research.get("discussion_threads")
    bills: list[Any] = raw_bills if isinstance(raw_bills, list) else []
    speeches: list[Any] = raw_speeches if isinstance(raw_speeches, list) else []
    threads: list[Any] = raw_threads if isinstance(raw_threads, list) else []
    return {
        "bill_count": len(bills),
        "speech_count": len(speeches),
        "thread_count": len(threads),
        "quality": research.get("quality") or {},
        "live_refresh": research.get("live_refresh") or {},
        "sources": _official_sources(research),
    }


def _official_sources(research: dict[str, Any]) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(url: Any, title: Any, source_type: str, detail: Any = "") -> None:
        normalized_url = str(url or "").strip()
        if not _is_official_assembly_url(normalized_url) or normalized_url in seen:
            return
        seen.add(normalized_url)
        sources.append(
            {
                "url": normalized_url,
                "title": str(title or "국회 공식 원문")[:180],
                "type": source_type,
                "detail": str(detail or "")[:240],
            }
        )

    for bill in research.get("bills", []):
        if not isinstance(bill, dict):
            continue
        add(
            bill.get("official_url") or bill.get("source_url"),
            bill.get("name") or bill.get("bill_name"),
            "의안",
            bill.get("status"),
        )
        for document in bill.get("documents", []):
            if isinstance(document, dict):
                add(
                    document.get("official_url"),
                    document.get("title"),
                    "전문위원 검토보고서",
                )
    for speech in research.get("speeches", []):
        if not isinstance(speech, dict):
            continue
        raw_citation = speech.get("citation")
        citation: dict[str, Any] = raw_citation if isinstance(raw_citation, dict) else {}
        add(
            citation.get("official_url") or speech.get("official_source"),
            citation.get("meeting") or speech.get("meeting"),
            "회의록",
            f"{speech.get('speaker') or ''} · {citation.get('source_locator') or ''}",
        )
    return sources[:40]


def _is_official_assembly_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host == "assembly.go.kr" or host.endswith(".assembly.go.kr")
    )
