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

from .llm import LlmError, answer_delivery_metadata, synthesize

ServicesFactory = Callable[..., ServiceContext]
Synthesizer = Callable[[str, str, str, dict[str, Any]], tuple[str, str]]

_SOURCE_TITLE_DISPLAY_LIMIT = 180
_SOURCE_DETAIL_DISPLAY_LIMIT = 240


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
                    os.getenv("KBD_WORKSPACE_MAX_MINUTES_PER_REQUEST", "20")
                ),
            )
            research = KasmTools(services).explore_issue(query, limit=50)
            validation = research.get("bill_number_validation")
            if isinstance(validation, dict) and validation.get("exact_match") is not True:
                raise WorkspaceError(
                    "요청한 의안번호와 정확히 일치하는 공식 의안을 확인하지 못했습니다. "
                    "다른 법안으로 대체하지 않았으니 의안번호를 확인해 주세요.",
                    status_code=502,
                )
            if isinstance(validation, dict) and not isinstance(
                research.get("exact_bill_evidence_validation"), dict
            ):
                _restrict_exact_bill_evidence(research)
            answer, model = synthesizer(provider, llm_api_key.strip(), query, research)
    except WorkspaceError:
        raise
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
        "answer_delivery": answer_delivery_metadata(provider),
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
        "research_pagination": research.get("research_pagination") or {},
        "scope_inventory": research.get("scope_inventory") or {},
        "sources": _official_sources(research),
    }


def _official_sources(research: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(url: Any, title: Any, source_type: str, detail: Any = "") -> None:
        normalized_url = str(url or "").strip()
        if not _is_official_assembly_url(normalized_url) or normalized_url in seen:
            return
        seen.add(normalized_url)
        full_title = str(title or "국회 공식 원문")
        full_detail = str(detail or "")
        display_title = _truncate_for_display(full_title, _SOURCE_TITLE_DISPLAY_LIMIT)
        display_detail = _truncate_for_display(full_detail, _SOURCE_DETAIL_DISPLAY_LIMIT)
        sources.append(
            {
                "url": normalized_url,
                "title": full_title,
                "type": source_type,
                "detail": full_detail,
                "presentation": {
                    "title": display_title,
                    "detail": display_detail,
                    "title_truncated": display_title != full_title,
                    "detail_truncated": display_detail != full_detail,
                    "title_original_characters": len(full_title),
                    "detail_original_characters": len(full_detail),
                    "title_displayed_characters": len(display_title),
                    "detail_displayed_characters": len(display_detail),
                    "title_limit": _SOURCE_TITLE_DISPLAY_LIMIT,
                    "detail_limit": _SOURCE_DETAIL_DISPLAY_LIMIT,
                },
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
    raw_inventory = research.get("scope_inventory")
    inventory: dict[str, Any] = raw_inventory if isinstance(raw_inventory, dict) else {}
    for group_name, source_type in (
        ("bill_candidates", "의안 후보 지도"),
        ("meeting_candidates", "회의록 후보 지도"),
    ):
        raw_group = inventory.get(group_name)
        group: dict[str, Any] = raw_group if isinstance(raw_group, dict) else {}
        raw_items = group.get("items")
        items: list[Any] = raw_items if isinstance(raw_items, list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            add(
                item.get("official_url"),
                item.get("name") or item.get("title"),
                source_type,
                (
                    item.get("process_result")
                    or item.get("committee")
                    or (
                        "회의록 본문 확인 완료"
                        if item.get("full_text_loaded")
                        else "공식 후보 확인·본문 추가 확인 필요"
                    )
                ),
            )
    return sources


def _truncate_for_display(value: str, limit: int) -> str:
    """Bound only card presentation while retaining the full source metadata."""
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _restrict_exact_bill_evidence(research: dict[str, Any]) -> None:
    """Fail closed for speeches and threads as well as the exact bill record itself."""

    raw_validation = research.get("bill_number_validation")
    validation: dict[str, Any] = raw_validation if isinstance(raw_validation, dict) else {}
    requested = _string_values(validation.get("requested"))
    raw_bills = research.get("bills")
    bills: list[Any] = raw_bills if isinstance(raw_bills, list) else []
    exact_bills = [
        bill
        for bill in bills
        if isinstance(bill, dict) and str(bill.get("bill_no") or "") in requested
    ]
    allowed_bill_ids = {str(bill.get("id") or "") for bill in exact_bills}
    raw_links = research.get("links")
    links: list[Any] = raw_links if isinstance(raw_links, list) else []
    exact_links = [
        link
        for link in links
        if isinstance(link, dict) and str(link.get("bill_id") or "") in allowed_bill_ids
    ]
    linked_speech_ids = {
        str(link.get("speech_id") or "")
        for link in exact_links
        if str(link.get("speech_id") or "").strip()
    }
    raw_speeches = research.get("speeches")
    speeches: list[Any] = raw_speeches if isinstance(raw_speeches, list) else []
    exact_speeches = [
        speech
        for speech in speeches
        if isinstance(speech, dict) and str(speech.get("speech_id") or "") in linked_speech_ids
    ]
    raw_threads = research.get("discussion_threads")
    threads: list[Any] = raw_threads if isinstance(raw_threads, list) else []
    exact_threads: list[dict[str, Any]] = []
    for raw_thread in threads:
        if not isinstance(raw_thread, dict):
            continue
        matched = _string_values(raw_thread.get("matched_speech_ids"))
        allowed_matches = sorted(matched.intersection(linked_speech_ids))
        if not allowed_matches:
            continue
        thread = dict(raw_thread)
        thread["matched_speech_ids"] = allowed_matches
        thread["exact_bill_context"] = True
        exact_threads.append(thread)
    allowed_meeting_ids = {str(thread.get("meeting_id") or "") for thread in exact_threads}
    raw_timeline = research.get("timeline")
    timeline: list[Any] = raw_timeline if isinstance(raw_timeline, list) else []
    exact_timeline = [
        event
        for event in timeline
        if not isinstance(event, dict)
        or (
            str(event.get("bill_no") or "") in requested
            if event.get("bill_no")
            else event.get("event_type") != "debate"
            or str(event.get("meeting_id") or "") in allowed_meeting_ids
        )
    ]
    research["bills"] = exact_bills
    research["links"] = exact_links
    research["speeches"] = exact_speeches
    research["discussion_threads"] = exact_threads
    research["timeline"] = exact_timeline
    research["exact_bill_evidence_validation"] = {
        "requested_bill_numbers": sorted(requested),
        "unlinked_speeches_removed": len(speeches) - len(exact_speeches),
        "unlinked_threads_removed": len(threads) - len(exact_threads),
        "policy": "명시적 의안번호와 공식 연결이 증명된 발언·회의 맥락만 유지",
    }
    raw_quality = research.get("quality")
    if isinstance(raw_quality, dict):
        raw_quality["speech_matches"] = len(exact_speeches)
        raw_quality["discussion_threads"] = len(exact_threads)
        raw_quality["context_turns"] = sum(len(thread.get("turns", [])) for thread in exact_threads)
        if not exact_speeches:
            raw_warnings = raw_quality.get("warnings")
            warnings = raw_warnings if isinstance(raw_warnings, list) else []
            warning = (
                "해당 의안번호와 공식 연결이 증명된 회의 발언은 현재 자료에서 확인되지 않았습니다."
            )
            if warning not in warnings:
                warnings.append(warning)
            raw_quality["warnings"] = warnings


def _string_values(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item) for item in value if str(item).strip()}


def _is_official_assembly_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host == "assembly.go.kr" or host.endswith(".assembly.go.kr")
    )
