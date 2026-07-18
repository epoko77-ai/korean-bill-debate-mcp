"""Transport-independent implementations of the public MCP tools.

The functions in this module deliberately do not depend on the MCP SDK.  This
makes the product API usable from the CLI and straightforward to unit test.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast

from kasm.core.quality import issue_quality
from kasm.research.request_scope import (
    exhaustive_requested,
    focused_result_request,
    requested_result_count,
)
from kasm.search.bilingual import korean_committee, prepare_query

_BILL_NUMBER = re.compile(r"(?<!\d)(\d{7})(?!\d)")


class SearchService(Protocol):
    def search(self, query: str, **filters: Any) -> Any: ...


class SpeechRepository(Protocol):
    def get(self, speech_id: str) -> Any: ...


class ResearchBackend(Protocol):
    """Durable research workflow injected by hosted deployments.

    Implementations must return JSON-compatible values (or domain objects
    exposing ``public_payload``/``to_dict``).  Starting research is expected to
    enqueue durable work and return promptly; it must not perform document
    downloads or PDF parsing in the connector request. Pages and evidence IDs
    must be bound to the supplied research ID; document lookup must never act as
    an arbitrary URL fetcher.
    """

    def start_research(
        self,
        query: str,
        *,
        korean_query: str | None = None,
        assembly_term: int | None = None,
        committees: tuple[str, ...] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> Any: ...

    def get_research_status(self, research_id: str) -> Any: ...

    def get_research_overview(
        self,
        research_id: str,
        *,
        offset: int = 0,
        page_size: int = 20,
        view_source_hash: str | None = None,
    ) -> Any: ...

    def get_research_page(
        self,
        research_id: str,
        *,
        cursor: str | None = None,
        page_size: int = 20,
    ) -> Any: ...

    def get_evidence_document(
        self,
        research_id: str,
        evidence_id: str,
        *,
        cursor: str | None = None,
        max_characters: int = 20_000,
        scope: str = "selected",
    ) -> Any: ...


@dataclass(slots=True)
class ServiceContext:
    """Dependencies required by the MCP tools.

    ``catalog`` may be the same object as ``repository``.  Keeping it separate
    allows a search index and metadata store to evolve independently.
    """

    search: SearchService
    repository: SpeechRepository
    catalog: Any | None = None
    research: ResearchBackend | None = None


def to_jsonable(value: Any) -> Any:
    """Convert core model values to JSON-compatible values."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if is_dataclass(value):
        return to_jsonable(asdict(value))  # type: ignore[arg-type]
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return to_jsonable(vars(value))
    return str(value)


def _invoke(target: Any, names: tuple[str, ...], /, *args: Any, **kwargs: Any) -> Any:
    for name in names:
        method = getattr(target, name, None)
        if method is not None:
            try:
                return method(*args, **kwargs)
            except TypeError:
                # Repositories commonly expose filters as a single object.
                if kwargs and not args:
                    return method(kwargs)
                raise
    joined = " or ".join(names)
    raise RuntimeError(f"Configured service does not implement {joined}")


def extract_bill_numbers(query: str) -> list[str]:
    """Return unique seven-digit Assembly bill numbers mentioned in natural language."""
    return list(dict.fromkeys(_BILL_NUMBER.findall(query)))


class KasmTools:
    """Public speech and bill tools, independent of any transport."""

    def __init__(self, services: ServiceContext):
        self.services = services

    def search_speeches(
        self,
        query: str,
        assembly_term: int | None = None,
        committee: str | None = None,
        speaker: str | None = None,
        speaker_role: str | None = None,
        organization: str | None = None,
        meeting_type: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 10,
        include_context: bool = True,
        korean_query: str | None = None,
        minutes_offset: int = 0,
    ) -> dict[str, Any]:
        """Search official speeches in Korean or English.

        Source records are Korean. For an English request, optionally provide concise Korean search
        terms in korean_query; otherwise the built-in legislative glossary supplies common terms.
        Preserve official citations and answer the user in the language they requested. ``limit``
        is a ranked quick-selection size, never proof that the requested source scope is complete;
        use the durable research workflow for a complete candidate map and explicit traversal.
        """
        if not query.strip():
            raise ValueError("query must not be empty")
        if len(query) > 500:
            raise ValueError("query must not exceed 500 characters")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if minutes_offset < 0:
            raise ValueError("minutes_offset must be non-negative")
        filters = {
            "assembly_term": assembly_term,
            "committee": korean_committee(committee),
            "speaker": speaker,
            "speaker_role": speaker_role,
            "organization": organization,
            "meeting_type": meeting_type,
            "date_from": date_from,
            "date_to": date_to,
            "limit": limit,
            "include_context": include_context,
            "minutes_offset": minutes_offset,
        }
        prepared = prepare_query(query, korean_query)
        result = self.services.search.search(
            prepared.search_query,
            **{key: value for key, value in filters.items() if value is not None},
        )
        payload = to_jsonable(result)
        if isinstance(payload, Mapping) and "results" in payload:
            response = {**prepared.metadata(), **dict(payload)}
        else:
            response = {**prepared.metadata(), "results": payload or []}
        results = response.get("results")
        response["selection"] = {
            "mode": "ranked_core",
            "requested_limit": limit,
            "returned_count": len(results) if isinstance(results, list) else None,
            "complete_scope_known": False,
            "comprehensive_answer_allowed": False,
            "instruction": (
                "This quick search is not a completeness claim. Use start_research when the "
                "entire requested scope must be mapped and exhausted."
            ),
        }
        refresh = getattr(self.services.search, "last_refresh", None)
        if isinstance(refresh, dict):
            response["live_refresh"] = to_jsonable(refresh)
            response["research_pagination"] = _research_pagination(refresh)
        return response

    def get_speech(self, speech_id: str) -> dict[str, Any]:
        result = _invoke(self.services.repository, ("get_speech", "get"), speech_id)
        if result is None:
            raise LookupError(f"Speech not found: {speech_id}")
        return cast(dict[str, Any], to_jsonable(result))

    def get_speech_context(
        self, speech_id: str, before: int = 2, after: int = 2
    ) -> dict[str, Any] | list[Any]:
        if before < 0 or after < 0:
            raise ValueError("before and after must be non-negative")
        result = _invoke(
            self.services.repository,
            ("get_speech_context", "get_context", "context"),
            speech_id,
            before=before,
            after=after,
        )
        return cast(dict[str, Any] | list[Any], to_jsonable(result))

    def list_committees(
        self, assembly_term: int | None = None, query: str | None = None
    ) -> list[Any]:
        catalog = self.services.catalog or self.services.repository
        prepared_query = prepare_query(query).search_query if query else None
        filters = {"assembly_term": assembly_term, "query": prepared_query}
        result = _invoke(
            catalog,
            ("list_committees",),
            **{key: value for key, value in filters.items() if value is not None},
        )
        return cast(list[Any], to_jsonable(result))

    def list_meetings(
        self,
        committee: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        meeting_type: str | None = None,
    ) -> list[Any]:
        """Query official meeting metadata for the requested scope and list cached candidates."""
        catalog = self.services.catalog or self.services.repository
        filters = {
            "committee": korean_committee(committee),
            "date_from": date_from,
            "date_to": date_to,
            "meeting_type": meeting_type,
        }
        result = _invoke(
            catalog,
            ("list_meetings",),
            **{key: value for key, value in filters.items() if value is not None},
        )
        return cast(list[Any], to_jsonable(result))

    def search_bills(
        self,
        query: str,
        assembly_term: int | None = None,
        committee: str | None = None,
        status: str | None = None,
        limit: int = 10,
        korean_query: str | None = None,
        include_documents: bool = False,
    ) -> dict[str, Any]:
        """Search live bills from a Korean or English request.

        Use korean_query for Korean bill-title or policy keywords when the English subject contains
        a proper noun not covered by the built-in glossary. The fast default returns bill candidates
        and status. Set include_documents=true only for a small targeted search, or call
        get_bill_status for each relevant bill to retrieve its expert review report without making
        one broad request time out. ``limit`` is only the ranked quick-selection size; this tool
        does not claim that every bill in a broad issue scope has been enumerated.
        """
        if not query.strip():
            raise ValueError("query must not be empty")
        if status not in {None, "pending", "processed"}:
            raise ValueError("status must be pending or processed")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        catalog = self.services.catalog or self.services.repository
        filters = {
            "assembly_term": assembly_term,
            "committee": korean_committee(committee),
            "status": status,
            "limit": limit,
            "include_documents": include_documents,
        }
        prepared = prepare_query(query, korean_query)
        result = _invoke(
            catalog,
            ("search_bills",),
            prepared.search_query,
            **{k: v for k, v in filters.items() if v is not None},
        )
        results = to_jsonable(result) or []
        return {
            **prepared.metadata(),
            "results": results,
            "selection": {
                "mode": "ranked_core",
                "requested_limit": limit,
                "returned_count": len(results) if isinstance(results, list) else None,
                "complete_scope_known": False,
                "comprehensive_answer_allowed": False,
                "instruction": (
                    "This quick search is not a completeness claim. Use start_research when "
                    "the entire requested scope must be mapped and exhausted."
                ),
            },
        }

    def get_bill_status(self, bill_id_or_no: str) -> dict[str, Any]:
        """Refresh one bill's status and attach its official expert review report when available."""
        if not bill_id_or_no.strip():
            raise ValueError("bill_id_or_no must not be empty")
        catalog = self.services.catalog or self.services.repository
        result = _invoke(catalog, ("get_bill_status",), bill_id_or_no)
        if result is None:
            raise LookupError(f"Bill not found: {bill_id_or_no}")
        return cast(dict[str, Any], to_jsonable(result))

    def start_research(
        self,
        query: str,
        assembly_term: int | None = None,
        committees: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        korean_query: str | None = None,
    ) -> dict[str, Any]:
        """Start exhaustive legislative research and return immediately with a research_id.

        전건·전수·빠짐없이·역대 또는 여러 국회 대수를 포괄하는 조사에만 이 도구를
        한 번 호출하세요. 상위 N건, ``5개 정도``, 일반 요약 요청에는 이 도구를 쓰지 말고
        ``explore_issue(limit=N)``를 사용하세요. 백그라운드에서 관련 의안,
        처리 상태, 안건, 소위원회 회의록, 전문위원 검토보고서, 의원 발언과 정부 답변을
        빠짐없이 확인합니다. 반환된 ``next_action``에 따라 ``get_research_status``를
        호출하고, 완료 후 ``get_research_page``의 안정적인 커서를 끝까지 따라가세요.

        제1대(제헌국회)부터 제22대까지 대수·날짜 범위를 해석합니다. 범위가 없으면 제22대를
        기본값으로 사용합니다. ``김OO 대표발의``, ``김OO 공동발의``, ``김OO 발의``는 공식
        역할 필드에서 이름 전체를 정확히 확인하고, 선택된 법안의 의안번호가 있는 회의만
        연결합니다. 성공한 0건은 ``source_availability``에 데이터셋별로 표시하며, 실패나
        미완료를 자료 없음으로 표현하지 마세요.

        Use this only for explicitly exhaustive Korean or English research. For top-N or ordinary
        summaries, use ``explore_issue`` with the requested limit. This queues durable work and
        returns a receipt; it never arbitrarily reduces candidates to a top-N sample. Do not make a
        comprehensive claim from the receipt or before both coverage and evidence pagination are
        complete. ``korean_query`` is only an optional Korean search hint for an English request;
        the original natural-language query and its intent remain authoritative. Explicit Korean
        Assembly-term/date scopes can cover terms 1 through 22; an omitted scope defaults to the
        current 22nd Assembly. Korean proposer-role phrases are exact identity filters, not fuzzy
        topic terms.
        """
        if not query.strip():
            raise ValueError("query must not be empty")
        if len(query) > 500:
            raise ValueError("query must not exceed 500 characters")
        if korean_query is not None and len(korean_query) > 500:
            raise ValueError("korean_query must not exceed 500 characters")
        if assembly_term is not None and assembly_term < 1:
            raise ValueError("assembly_term must be positive")
        parsed_from = _optional_iso_date(date_from, "date_from")
        parsed_to = _optional_iso_date(date_to, "date_to")
        if parsed_from and parsed_to and parsed_from > parsed_to:
            raise ValueError("date_from must be on or before date_to")
        # Tool selection by an MCP client is advisory, so enforce the routing
        # rule here as well. A top-N request accidentally sent to start_research
        # must not become a ten-minute exhaustive job.
        if focused_result_request(query) and not committees:
            bounded = self.explore_issue(
                query,
                limit=requested_result_count(query) or 20,
                korean_query=korean_query,
                date_from=date_from,
                date_to=date_to,
                assembly_term=assembly_term,
                exhaustive=False,
            )
            compatibility_value = bounded.get("compatibility")
            compatibility = (
                dict(compatibility_value)
                if isinstance(compatibility_value, Mapping)
                else {}
            )
            compatibility.update(
                {
                    "entrypoint": "start_research",
                    "rerouted_to": "explore_issue",
                    "reason": "explicit_bounded_result_count",
                }
            )
            bounded["compatibility"] = compatibility
            return bounded
        backend = self._research_backend()
        normalized_committees = (
            None
            if committees is None
            else tuple(
                dict.fromkeys(
                    translated
                    for value in committees
                    if value.strip()
                    for translated in (korean_committee(value.strip()) or value.strip(),)
                )
            )
        )
        value = backend.start_research(
            query.strip(),
            korean_query=korean_query.strip() if korean_query and korean_query.strip() else None,
            assembly_term=assembly_term,
            committees=normalized_committees,
            date_from=date_from,
            date_to=date_to,
        )
        payload = _public_backend_payload(value)
        research_id = _research_id(payload)
        payload["research_id"] = research_id
        payload["comprehensive_answer_allowed"] = False
        payload["next_action"] = _next_action(
            "get_research_status",
            {"research_id": research_id},
            ko="조사가 끝날 때까지 이 연구 ID의 상태를 확인하세요.",
            en="Poll this research ID until its status is complete, partial, failed, or expired.",
            retry_after_seconds=payload.get("retry_after_seconds", 1),
        )
        return payload

    def get_research_status(self, research_id: str) -> dict[str, Any]:
        """Check durable research progress without restarting or duplicating the investigation.

        ``queued`` 또는 ``running``이면 반환된 ``next_action``으로 같은 연구 ID를 다시
        확인하세요. ``complete`` 또는 ``partial``이면 새 조사를 시작하지 말고
        ``get_research_overview``를 호출하세요. ``partial``과 불완전한 coverage는 반드시
        사용자에게 누락 사유를 밝혀야 하며 종합 조사가 끝났다고 표현하면 안 됩니다.

        Poll the same research_id. Never call start_research again merely because work is still
        running, and never infer completeness from progress or a non-empty result. When
        ``overview_available=true`` means a bounded candidate map can be read with
        ``get_research_overview``. Status polling deliberately does not inline that map: repeated
        polls must stay small and must not duplicate the same evidence into the model context.
        """
        _validate_identifier(research_id, "research_id")
        backend = self._research_backend()
        payload = _public_backend_payload(backend.get_research_status(research_id))
        payload.setdefault("research_id", research_id)
        status = str(payload.get("status") or "unknown").lower()
        payload["comprehensive_answer_allowed"] = False
        overview_available = bool(payload.get("overview_available"))
        # Keep polling payloads bounded. The overview is immutable and has its
        # own paged tool; inlining it here duplicated the same (formerly up to
        # 100-item) map on every one-second status poll.
        payload["overview_inlined"] = False
        if status in {"complete", "partial"}:
            payload["next_action"] = _next_action(
                "get_research_overview",
                {"research_id": research_id, "page_size": 20},
                ko=(
                    "핵심 자료와 법안·회의·문서 전체 지도를 먼저 읽으세요. partial이면 "
                    "coverage의 누락 사유를 반드시 밝히세요."
                ),
                en=(
                    "Read the core-first map of every bill, meeting, and document. If status "
                    "is partial, disclose every coverage gap."
                ),
            )
        elif status in {"failed", "expired"}:
            payload["next_action"] = _next_action(
                None,
                {},
                ko="오류 또는 만료 정보를 사용자에게 알리고 같은 결과를 완전하다고 쓰지 마세요.",
                en="Report the failure or expiry; do not present this research as complete.",
            )
        elif overview_available:
            payload["next_action"] = _next_action(
                "get_research_overview",
                {"research_id": research_id, "page_size": 20},
                ko=(
                    "원문 조사가 진행되는 동안 먼저 확인된 후보 지도를 읽으세요. "
                    "metadata_inventory_complete가 false이면 아직 전체 후보 목록도 아니며, "
                    "이 단계에서는 실질적 결론을 내리면 안 됩니다."
                ),
                en=(
                    "Read the observed candidate map while source documents are still being "
                    "verified. If metadata_inventory_complete is false, even candidate discovery "
                    "is still incomplete; do not draw a substantive conclusion yet."
                ),
            )
        else:
            retry_after = payload.get("retry_after_seconds", 2)
            payload["next_action"] = _next_action(
                "get_research_status",
                {"research_id": research_id},
                ko="새 조사를 만들지 말고 잠시 뒤 같은 연구 ID를 다시 확인하세요.",
                en="Wait briefly, then poll the same research ID without starting a duplicate.",
                retry_after_seconds=retry_after,
            )
        return payload

    def get_research_overview(
        self,
        research_id: str,
        offset: int = 0,
        page_size: int = 20,
        view_source_hash: str | None = None,
    ) -> dict[str, Any]:
        """Return the core-first map before opening selected or exhaustive source text.

        빠른 응답은 자료를 버리는 것이 아닙니다. 이 도구는 핵심 근거와 함께 관련 법안,
        회의, 의안원문, 전문위원 검토보고서의 전체 목록·건수·기간을 먼저 보여 줍니다.
        metadata 단계에서는 ``catalog.next_offset``, final 단계에서는
        ``catalog.page.next_offset``이 있으면 같은 page_size로 끝까지 읽으세요. metadata
        단계의 결과는 명시적인 잠정 후보 지도일 뿐이며 결론으로 사용하면 안 됩니다.

        Once ``metadata_inventory_complete=true``, this is the complete metadata orientation
        layer rather than a top-N result. Before then it is an observed first-page preview only.
        Follow ``next_action`` exactly: metadata pagination includes ``view_source_hash`` so every
        offset remains pinned to one immutable map even if final results become ready. Use
        get_research_page for the full evidence inventory and scope="all" only for an exhaustive
        source-text read.
        """

        _validate_identifier(research_id, "research_id")
        if offset < 0:
            raise ValueError("offset must not be negative")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        if view_source_hash is not None and (
            len(view_source_hash) != 64
            or any(character not in "0123456789abcdef" for character in view_source_hash)
        ):
            raise ValueError("view_source_hash must be a lowercase SHA-256 digest")
        overview_arguments: dict[str, Any] = {
            "offset": offset,
            "page_size": page_size,
        }
        if view_source_hash is not None:
            overview_arguments["view_source_hash"] = view_source_hash
        payload = _public_backend_payload(
            self._research_backend().get_research_overview(
                research_id,
                **overview_arguments,
            )
        )
        payload.setdefault("research_id", research_id)
        payload["comprehensive_answer_allowed"] = False
        payload["answer_source_requirements"] = {
            "per_bill_official_bill_url_required": True,
            "per_discussion_claim_official_deliberation_url_required": True,
            "bill_url_field": "catalog.groups[].official_bill_url",
            "deliberation_url_field": (
                "catalog.groups[].official_deliberation_urls"
            ),
            "missing_discussion_policy": (
                "If no official deliberation URL is present for a selected bill, say that "
                "no committee/subcommittee discussion was found in the checked scope; do not "
                "invent or generalize a discussion."
            ),
        }
        catalog_value = payload.get("catalog")
        catalog = catalog_value if isinstance(catalog_value, Mapping) else {}
        page_value = catalog.get("page")
        # The metadata candidate catalog is itself a page, while the final
        # entity catalog nests its page accounting beside grouped entries.
        # Treating only the final shape as pageable silently skipped accepted
        # metadata candidates beyond the first page on broad questions.
        page = page_value if isinstance(page_value, Mapping) else catalog
        next_offset = page.get("next_offset")
        if next_offset is not None:
            next_arguments: dict[str, Any] = {
                "research_id": research_id,
                "offset": int(next_offset),
                "page_size": page_size,
            }
            if payload.get("phase") == "metadata":
                source_hash = str(payload.get("source_hash") or "")
                if len(source_hash) != 64:
                    raise RuntimeError("metadata overview lacks a stable source hash")
                next_arguments["view_source_hash"] = source_hash
            payload["next_action"] = _next_action(
                "get_research_overview",
                next_arguments,
                ko="같은 page_size로 법안·회의·문서 지도의 다음 페이지를 읽으세요.",
                en="Read the next bill, meeting, and document catalog page.",
            )
            return payload

        if payload.get("phase") == "final" and payload.get("result_state") == "no_matching_records":
            coverage_value = payload.get("coverage")
            coverage = coverage_value if isinstance(coverage_value, Mapping) else {}
            verified_empty = bool(payload.get("complete")) and bool(coverage.get("complete"))
            if verified_empty:
                payload["comprehensive_answer_allowed"] = True
                payload["next_action"] = _next_action(
                    None,
                    {},
                    ko=(
                        "공식 출처 조회가 완결됐고 이번에 조회한 열린국회 데이터셋에서는 "
                        "요청 조건에 일치하는 자료가 확인되지 않았습니다. "
                        "source_availability의 출처·국회 대수별 상태와 함께 사용자에게 "
                        "데이터셋 범위 내 자료 없음으로 답하세요. 역사적으로 관련 기록이 전혀 "
                        "없었다고 확대 해석하지 마세요. 추가 근거 호출은 필요하지 않습니다."
                    ),
                    en=(
                        "Official-source coverage is complete and no matching records were found "
                        "in the Open Assembly datasets checked. Report that dataset-scoped result "
                        "with the per-source and Assembly-term states in source_availability; do "
                        "not generalize it to the nonexistence of historical records. No evidence "
                        "call is needed."
                    ),
                )
                return payload
            payload["result_state"] = "inconclusive"

        if bool(payload.get("provisional")) and not bool(
            payload.get("substantive_conclusion_available")
        ):
            payload["next_action"] = _next_action(
                "get_research_status",
                {"research_id": research_id},
                ko=(
                    "현재 확인된 후보 지도는 확보했습니다. 같은 연구 ID를 계속 확인해 "
                    "전체 후보·공식 원문 조사를 이어가세요."
                ),
                en=(
                    "The currently observed candidate map is ready. Poll the same research ID "
                    "for complete candidate discovery and official-source verification."
                ),
                retry_after_seconds=2,
            )
            return payload

        core_ids_value = payload.get("core_full_text_required_ids")
        core_ids = (
            [str(value) for value in core_ids_value if str(value).strip()]
            if isinstance(core_ids_value, list)
            else []
        )
        if core_ids:
            payload["next_action"] = _next_action(
                "get_evidence_document",
                {
                    "research_id": research_id,
                    "evidence_id": core_ids[0],
                    "scope": "core",
                },
                ko="핵심으로 선정된 긴 공식 원문을 우선순위 순서대로 확인하세요.",
                en="Read the routed long core sources in priority order.",
            )
        else:
            payload["next_action"] = _next_action(
                "get_research_page",
                {"research_id": research_id, "page_size": 20},
                ko=(
                    "핵심 자료는 개요에 완전하게 포함됐습니다. 필요하면 전체 근거 목록을 "
                    "열어 선택적으로 더 확인하세요."
                ),
                en=(
                    "The short core is complete inline. Open the full evidence inventory only "
                    "as needed."
                ),
                optional=True,
            )
        return payload

    def get_research_page(
        self,
        research_id: str,
        cursor: str | None = None,
        page_size: int = 20,
        exhaustive: bool = False,
    ) -> dict[str, Any]:
        """Return one stable evidence index page for a completed or partial research job.

        짧은 근거만 ``text``로 완전히 포함됩니다. 긴 근거는 절반짜리 미리보기를 만들지
        않고 ``text_inline_complete=false``와 정확한 길이·해시를 반환하므로, 해당 ID를
        ``get_evidence_document``로 끝까지 읽으세요. ``next_cursor``가 있으면 같은
        page_size로 모든 근거 ID를 먼저 수집하세요. 임의 top-N이나 근거 유형 생략은
        허용되지 않습니다.

        Short evidence may be complete inline. Long evidence is never silently shortened: it is
        indexed by exact size/hash with ``text_inline_complete=false`` and must be exhausted via
        get_evidence_document. Follow every result cursor; a non-empty page proves neither result
        pagination nor requested-scope completeness.
        """
        _validate_identifier(research_id, "research_id")
        if cursor is not None and (not cursor.strip() or len(cursor) > 8192):
            raise ValueError("cursor must contain between 1 and 8192 characters")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        payload = _public_backend_payload(
            self._research_backend().get_research_page(
                research_id,
                cursor=cursor,
                page_size=page_size,
            )
        )
        payload.setdefault("research_id", research_id)
        page_value = payload.get("page")
        coverage_value = payload.get("coverage")
        page: Mapping[str, Any] = page_value if isinstance(page_value, Mapping) else {}
        coverage: Mapping[str, Any] = coverage_value if isinstance(coverage_value, Mapping) else {}
        next_cursor = page.get("next_cursor")
        page_complete = bool(page.get("complete"))
        coverage_complete = bool(coverage.get("complete"))
        full_text_required_total = int(payload.get("full_text_required_total") or 0)
        first_full_text_id = str(payload.get("first_full_text_required_id") or "").strip()
        payload["comprehensive_answer_allowed"] = (
            exhaustive and page_complete and coverage_complete and full_text_required_total == 0
        )
        matched_total = page.get("matched_total")
        if (
            type(matched_total) is int
            and matched_total == 0
            and page_complete
            and coverage_complete
        ):
            payload.update(
                {
                    "result_state": "no_matching_records",
                    "result_message_ko": (
                        "이번 조사에서 조회한 열린국회 공식 데이터셋에서는 요청 조건에 "
                        "일치하는 자료를 확인하지 못했습니다."
                    ),
                    "result_message_en": (
                        "No matching records were found in the Open Assembly datasets checked "
                        "for this research scope."
                    ),
                    "comprehensive_answer_allowed": True,
                    "next_action": _next_action(
                        None,
                        {},
                        ko=(
                            "조회한 열린국회 데이터셋에서 확인된 자료 0건임을 사용자에게 "
                            "알리되, 역사적 기록 전체의 부재로 확대 해석하지 마세요."
                        ),
                        en=(
                            "Report zero records in the checked Open Assembly datasets without "
                            "generalizing to all historical records."
                        ),
                    ),
                }
            )
            return payload
        if next_cursor:
            payload["next_action"] = _next_action(
                "get_research_page",
                {
                    "research_id": research_id,
                    "cursor": str(next_cursor),
                    "page_size": page_size,
                    "exhaustive": exhaustive,
                },
                ko="같은 page_size와 반환된 커서로 다음 근거 페이지를 읽으세요.",
                en="Fetch the next evidence page with this cursor and the same page_size.",
            )
        elif not page_complete:
            payload["next_action"] = _next_action(
                "get_research_status",
                {"research_id": research_id},
                ko="페이지가 아직 완결되지 않았습니다. 연구 상태를 다시 확인하세요.",
                en="The page is not complete and has no cursor; check research status again.",
            )
        elif exhaustive and first_full_text_id:
            payload["next_action"] = _next_action(
                "get_evidence_document",
                {
                    "research_id": research_id,
                    "evidence_id": first_full_text_id,
                    "scope": "all",
                },
                ko=(
                    "전체 결과에서 첫 번째 긴 근거부터 시작해 next_evidence_id가 끝날 때까지 "
                    "모든 원문 구간을 순서대로 읽으세요."
                ),
                en=(
                    "Start with the first long evidence item across the complete result set, then "
                    "follow next_evidence_id until every exact range has been read."
                ),
            )
        elif exhaustive and not coverage_complete:
            payload["next_action"] = _next_action(
                None,
                {},
                ko="coverage의 모든 누락 사유를 밝히고 부분 조사 결과로만 답하세요.",
                en="Disclose every coverage gap and present only a partial research result.",
            )
        else:
            payload["next_action"] = _next_action(
                "get_evidence_document",
                {"research_id": research_id, "evidence_id": "<evidence.id>"},
                ko=(
                    "전체 목록을 확인했습니다. 사용자가 원하는 근거 ID를 선택해 원문을 "
                    "열거나, 전건 조사가 필요하면 exhaustive=true로 다시 순회하세요."
                ),
                en=(
                    "Select any needed evidence ID for full text, or repeat with exhaustive=true "
                    "when every source must be read."
                ),
                optional=True,
            )
        return payload

    def get_evidence_document(
        self,
        research_id: str,
        evidence_id: str,
        cursor: str | None = None,
        max_characters: int = 20_000,
        scope: str = "selected",
    ) -> dict[str, Any]:
        """Return one lossless page of official text and its verifiable source locators.

        ``get_research_page``가 반환한 evidence ID를 사용하세요. 구현체는 공식 URL,
        원문 해시, 페이지/구간 locator와 정확한 원문 조각을 반환합니다. ``complete``가
        false이면 반환된 ``next_cursor``와 같은 ``max_characters``로 계속 호출하여 전체
        원문을 끝까지 읽으세요. 각 조각은 요약되거나 버려지지 않으며 이어 붙이면 보존된
        전체 원문과 정확히 일치합니다. 인용할 때 해당 locator와 공식 URL을 보존하세요.

        Use an evidence ID from a research page. Each response is an exact, stable character range,
        not an LLM summary or preview. When complete is false, follow next_cursor with the same
        max_characters until every range has been read. Concatenating the text fields reconstructs
        the full stored document exactly. Preserve its official URL, content hash, and locator.
        """
        _validate_identifier(research_id, "research_id")
        _validate_identifier(evidence_id, "evidence_id", maximum=1000)
        if cursor is not None and (not cursor.strip() or len(cursor) > 8192):
            raise ValueError("cursor must contain between 1 and 8192 characters")
        if not 1 <= max_characters <= 50_000:
            raise ValueError("max_characters must be between 1 and 50000")
        if scope not in {"selected", "core", "all"}:
            raise ValueError("scope must be selected, core, or all")
        payload = _public_backend_payload(
            self._research_backend().get_evidence_document(
                research_id,
                evidence_id,
                cursor=cursor,
                max_characters=max_characters,
                scope=scope,
            )
        )
        payload.setdefault("research_id", research_id)
        payload.setdefault("evidence_id", evidence_id)
        next_cursor = payload.get("next_cursor")
        complete = bool(payload.get("complete"))
        next_evidence_id = str(payload.get("next_evidence_id") or "").strip()
        research_evidence_complete = bool(payload.get("research_evidence_complete"))
        core_evidence_complete = bool(payload.get("core_evidence_complete"))
        research_coverage_complete = bool(payload.get("research_coverage_complete"))
        payload["comprehensive_answer_allowed"] = (
            complete and research_evidence_complete and research_coverage_complete
        )
        if next_cursor:
            payload["next_action"] = _next_action(
                "get_evidence_document",
                {
                    "research_id": research_id,
                    "evidence_id": evidence_id,
                    "cursor": str(next_cursor),
                    "max_characters": max_characters,
                    "scope": scope,
                },
                ko="같은 max_characters와 반환된 커서로 공식 원문의 다음 구간을 읽으세요.",
                en=(
                    "Fetch the next exact document range with this cursor and the same "
                    "max_characters."
                ),
            )
        elif not complete:
            payload["next_action"] = _next_action(
                None,
                {},
                ko="원문 페이지가 불완전하지만 다음 커서가 없습니다. 이 결과를 인용하지 마세요.",
                en="The document page is incomplete without a cursor; do not cite this result.",
            )
        elif next_evidence_id:
            payload["next_action"] = _next_action(
                "get_evidence_document",
                {
                    "research_id": research_id,
                    "evidence_id": next_evidence_id,
                    "max_characters": max_characters,
                    "scope": scope,
                },
                ko="다음 긴 근거 ID의 전체 원문 구간을 이어서 읽으세요.",
                en="Continue with every exact range of the next long evidence item.",
            )
        elif scope == "core" and core_evidence_complete:
            payload["next_action"] = _next_action(
                "get_research_page",
                {
                    "research_id": research_id,
                    # Keep the model-facing connector response bounded. Each
                    # evidence entry may inline up to 4,000 complete characters,
                    # so the ordinary 20-item page has an 80,000-character
                    # source-text ceiling while explicit callers may still
                    # choose any supported page size and follow its cursor.
                    "page_size": 20,
                    "exhaustive": True,
                },
                ko=(
                    "핵심 공식 원문 확인을 마쳤습니다. 현재 확인 범위를 밝혀 우선 답변하고, "
                    "사용자가 전건 조사를 요청했다면 전체 근거 목록을 처음부터 exhaustive=true로 "
                    "순회한 뒤 모든 긴 원문을 이어서 읽으세요."
                ),
                en=(
                    "Core source review is complete. Give a scoped answer now; when the user "
                    "requested every record, traverse the entire inventory from the beginning "
                    "with exhaustive=true and then read every routed long source."
                ),
                optional=True,
            )
        elif scope == "selected":
            payload["next_action"] = _next_action(
                None,
                {},
                ko="선택한 공식 원문 확인을 마쳤습니다.",
                en="The selected official source is complete.",
            )
        else:
            payload["next_action"] = _next_action(
                None,
                {},
                ko=(
                    "긴 근거 원문을 모두 확인했습니다. coverage가 complete일 때만 종합 답변을 "
                    "작성하고 공식 URL과 locator를 인용하세요."
                ),
                en=(
                    "All long evidence text has been exhausted. Synthesize only when coverage is "
                    "complete, preserving every official URL and locator."
                ),
            )
        return payload

    def explore_issue(
        self,
        query: str,
        limit: int = 20,
        korean_query: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        minutes_offset: int = 0,
        assembly_term: int | None = None,
        committees: list[str] | None = None,
        exhaustive: bool = False,
    ) -> dict[str, Any]:
        """Return a bounded live answer across bills, status, committees and discussions.

        상위 N건, ``5개 정도``, 중요 법안 요약처럼 범위가 제한된 일반 질문의 기본
        도구입니다. ``limit``는 사용자가 요구한 결과 수를 그대로 반영하세요. 전건·전수·
        빠짐없이·역대 조사는 ``exhaustive=true``로 호출하거나 ``start_research``를
        사용하세요.

        Use this as the primary tool for bounded Korean or English questions asking what happened,
        who argued what, or how a policy and bill evolved. Results include evidence-ranked speeches,
        ordered multi-turn discussion threads, bill and review-report links, official provenance,
        and live-check metadata. Long research is deliberately paged so no evidence is silently
        truncated and no single connector request times out. When research_pagination.complete is
        false, call this tool again with the returned next_minutes_offset and the same query/date
        scope. Continue until complete before claiming the requested scope was comprehensively
        checked. For each relevant bill, call get_bill_status to retrieve the expert review report.
        It queries official Open Assembly APIs before searching the private local cache and reports
        bounded-refresh diagnostics. Synthesize the answer from actual turns; do not infer a stance
        that is not supported by a quoted speech. Put each quote's citation.official_url next
        to the claim so the user can open and verify the original minutes immediately. Source
        records are Korean; answer English users in English and identify translated quotations.
        For unfamiliar English subjects, supply concise Korean keywords in korean_query.

        With a durable backend, only ``exhaustive=true``, an explicit exhaustive phrase, or a
        structured scope unsupported by the bounded path is routed to ``start_research``. Ordinary
        requests use the live Open Assembly APIs and honor ``limit``. Bounded results must not be
        described as an exhaustive inventory; expose their pagination and source coverage as-is.
        """
        if not query.strip():
            raise ValueError("query must not be empty")
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if minutes_offset < 0:
            raise ValueError("minutes_offset must be non-negative")
        durable_required = bool(
            self.services.research is not None
            and (exhaustive or exhaustive_requested(query) or committees)
        )
        if durable_required:
            receipt = self.start_research(
                query,
                assembly_term=assembly_term,
                committees=committees,
                date_from=date_from,
                date_to=date_to,
                korean_query=korean_query,
            )
            receipt["compatibility"] = {
                "entrypoint": "explore_issue",
                "workflow": "durable_research",
                "reason": (
                    "structured_committee_scope"
                    if committees
                    else "explicit_exhaustive_request"
                ),
                "limit_does_not_truncate_research": True,
                "minutes_offset_ignored": minutes_offset,
            }
            return receipt
        catalog = self.services.catalog or self.services.repository
        prepared = prepare_query(query, korean_query)
        options: dict[str, Any] = {"limit": limit}
        if date_from is not None:
            options["date_from"] = date_from
        if date_to is not None:
            options["date_to"] = date_to
        if minutes_offset:
            options["minutes_offset"] = minutes_offset
        if assembly_term is not None:
            options["assembly_term"] = assembly_term
        try:
            payload = cast(
                dict[str, Any],
                to_jsonable(
                    _invoke(catalog, ("explore_issue",), prepared.search_query, **options)
                ),
            )
        except ValueError as exc:
            # The bounded compatibility path intentionally covers one Assembly
            # term. Route an explicitly cross-term request to the durable engine
            # before any result is presented as complete.
            if self.services.research is None or "supports one Assembly term" not in str(exc):
                raise
            receipt = self.start_research(
                query,
                assembly_term=assembly_term,
                committees=committees,
                date_from=date_from,
                date_to=date_to,
                korean_query=korean_query,
            )
            receipt["compatibility"] = {
                "entrypoint": "explore_issue",
                "workflow": "durable_research",
                "reason": "multi_term_scope",
                "limit_does_not_truncate_research": True,
                "minutes_offset_ignored": minutes_offset,
            }
            return receipt
        requested_bill_numbers = extract_bill_numbers(prepared.search_query)
        if requested_bill_numbers:
            payload = self._enforce_exact_bill_numbers(payload, requested_bill_numbers)
        payload.update(prepared.metadata())
        payload["requested_limit"] = limit
        payload["research_mode"] = "bounded_live"
        payload["comprehensive_answer_allowed"] = False
        payload["compatibility"] = {
            "entrypoint": "explore_issue",
            "workflow": "bounded_live",
            "requested_limit": limit,
            "exhaustive": False,
        }
        payload["answer_source_requirements"] = {
            "per_bill_official_url_required": True,
            "per_discussion_claim_official_url_required": True,
            "bill_url_fields": [
                "bills[].official_url",
                "scope_inventory.bill_candidates.items[].official_url",
            ],
            "discussion_url_fields": [
                "speeches[].citation.official_url",
                "discussion_threads[].turns[].citation.official_url",
                "scope_inventory.meeting_candidates.items[].official_url",
            ],
            "missing_source_policy": (
                "Do not present an uncited bill or discussion as verified. Explicitly state "
                "when no official committee/subcommittee record was found in the checked scope."
            ),
        }
        return payload

    def _research_backend(self) -> ResearchBackend:
        backend = self.services.research
        if backend is None:
            raise RuntimeError("A durable research backend is not configured")
        return backend

    def _enforce_exact_bill_numbers(
        self, payload: dict[str, Any], requested_bill_numbers: list[str]
    ) -> dict[str, Any]:
        """Never let fuzzy issue search substitute another bill for an explicit number."""
        catalog = self.services.catalog or self.services.repository
        exact_bills: list[dict[str, Any]] = []
        for bill_no in requested_bill_numbers:
            result = to_jsonable(_invoke(catalog, ("get_bill_status",), bill_no))
            if isinstance(result, dict) and str(result.get("bill_no") or "") == bill_no:
                exact_bills.append(result)
        allowed_bill_ids = {str(bill.get("id") or "") for bill in exact_bills}
        raw_links = payload.get("links")
        links = raw_links if isinstance(raw_links, list) else []
        payload["bills"] = exact_bills
        exact_links = [
            link
            for link in links
            if isinstance(link, dict) and str(link.get("bill_id") or "") in allowed_bill_ids
        ]
        payload["links"] = exact_links
        allowed_speech_ids = {
            str(link.get("speech_id") or "")
            for link in exact_links
            if str(link.get("speech_id") or "")
        }
        raw_speeches = payload.get("speeches")
        speeches = raw_speeches if isinstance(raw_speeches, list) else []
        exact_speeches = [
            speech
            for speech in speeches
            if isinstance(speech, dict) and str(speech.get("speech_id") or "") in allowed_speech_ids
        ]
        payload["speeches"] = exact_speeches
        raw_threads = payload.get("discussion_threads")
        threads = raw_threads if isinstance(raw_threads, list) else []
        exact_threads: list[dict[str, Any]] = []
        for thread in threads:
            if not isinstance(thread, dict):
                continue
            raw_matched_ids = thread.get("matched_speech_ids")
            matched_ids = raw_matched_ids if isinstance(raw_matched_ids, list) else []
            allowed_matches = sorted(
                str(speech_id) for speech_id in matched_ids if str(speech_id) in allowed_speech_ids
            )
            if not allowed_matches:
                continue
            exact_thread = dict(thread)
            exact_thread["matched_speech_ids"] = allowed_matches
            exact_thread["exact_bill_context"] = True
            exact_threads.append(exact_thread)
        payload["discussion_threads"] = exact_threads
        allowed_meeting_ids = {
            str(thread.get("meeting_id") or "")
            for thread in exact_threads
            if str(thread.get("meeting_id") or "")
        }
        raw_timeline = payload.get("timeline")
        timeline = raw_timeline if isinstance(raw_timeline, list) else []
        payload["timeline"] = [
            event
            for event in timeline
            if isinstance(event, dict)
            and (
                (event.get("bill_no") and str(event.get("bill_no")) in requested_bill_numbers)
                or (
                    event.get("event_type") == "debate"
                    and str(event.get("meeting_id") or "") in allowed_meeting_ids
                )
            )
        ]
        inventory = payload.get("scope_inventory")
        if isinstance(inventory, dict):
            bill_candidates = inventory.get("bill_candidates")
            if isinstance(bill_candidates, dict):
                raw_items = bill_candidates.get("items")
                items = raw_items if isinstance(raw_items, list) else []
                exact_items = [
                    item
                    for item in items
                    if isinstance(item, dict)
                    and str(item.get("bill_no") or "") in requested_bill_numbers
                ]
                bill_candidates.update({"items": exact_items, "total": len(exact_items)})
            speech_candidates = inventory.get("speech_candidates")
            if isinstance(speech_candidates, dict):
                raw_items = speech_candidates.get("items")
                items = raw_items if isinstance(raw_items, list) else []
                exact_items = [
                    item
                    for item in items
                    if isinstance(item, dict)
                    and str(item.get("speech_id") or "") in allowed_speech_ids
                ]
                speech_candidates.update({"items": exact_items, "total": len(exact_items)})
            link_inventory = inventory.get("links")
            if isinstance(link_inventory, dict):
                link_inventory.update({"items": exact_links, "total": len(exact_links)})
            selected = inventory.get("selected_for_synthesis")
            if isinstance(selected, dict):
                selected.update(
                    {
                        "bill_count": len(exact_bills),
                        "speech_count": len(exact_speeches),
                        "discussion_thread_count": len(exact_threads),
                        "bill_selection_complete": len(exact_bills)
                        == _inventory_total(inventory.get("bill_candidates")),
                        "speech_selection_complete": len(exact_speeches)
                        == _inventory_total(inventory.get("speech_candidates")),
                    }
                )
        payload["bill_number_validation"] = {
            "requested": requested_bill_numbers,
            "matched": [str(bill["bill_no"]) for bill in exact_bills],
            "exact_match": len(exact_bills) == len(requested_bill_numbers),
            "speech_relationship_policy": "official_speech_bill_links_only",
            "linked_speech_count": len(allowed_speech_ids),
        }
        payload["exact_bill_evidence_validation"] = {
            "requested_bill_numbers": sorted(requested_bill_numbers),
            "unlinked_speeches_removed": len(speeches) - len(exact_speeches),
            "unlinked_threads_removed": len(threads) - len(exact_threads),
            "policy": "명시적 의안번호와 공식 연결이 증명된 발언·회의 맥락만 유지",
        }
        payload["quality"] = issue_quality(payload)
        if not exact_bills:
            payload["quality"]["warnings"].append(
                "요청한 의안번호와 정확히 일치하는 공식 의안을 확인하지 못했습니다."
            )
        return payload


def _inventory_total(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    total = value.get("total")
    return int(total) if isinstance(total, int) and total >= 0 else 0


def _research_pagination(refresh: dict[str, Any]) -> dict[str, Any]:
    has_more = bool(refresh.get("has_more"))
    failures = int(refresh.get("minutes_failures") or 0)
    next_offset = refresh.get("next_minutes_offset")
    raw_scope = refresh.get("temporal_scope")
    temporal_scope = dict(raw_scope) if isinstance(raw_scope, Mapping) else {}
    raw_months = temporal_scope.get("queried_months") or refresh.get("months_queried")
    queried_months = (
        sorted(str(value) for value in raw_months)
        if isinstance(raw_months, (list, tuple, set))
        else []
    )
    temporal_scope.update(
        {
            "mode": str(temporal_scope.get("mode") or "unspecified"),
            "explicit": temporal_scope.get("explicit") is True,
            "requested_date_from": temporal_scope.get("requested_date_from"),
            "requested_date_to": temporal_scope.get("requested_date_to"),
            "requested_months": temporal_scope.get("requested_months") or [],
            "queried_months": queried_months,
            "window_start_month": queried_months[0] if queried_months else None,
            "window_end_month": queried_months[-1] if queried_months else None,
            "window_month_count": len(queried_months),
        }
    )
    window_complete = not has_more and failures == 0
    overall_complete = window_complete and temporal_scope["explicit"] is True
    return {
        "complete": overall_complete,
        "overall_complete": overall_complete,
        "window_complete": window_complete,
        "partial": not overall_complete,
        "window_partial": not window_complete,
        "temporal_scope": temporal_scope,
        "next_minutes_offset": next_offset,
        "failed_count": failures,
        "failed_official_urls": refresh.get("failed_official_urls") or [],
        "instruction": (
            f"Call the same tool again with minutes_offset={next_offset}."
            if has_more
            else (
                "The meeting window is partial; disclose every failed official URL."
                if failures
                else (
                    "The explicit temporal scope has been checked."
                    if overall_complete
                    else (
                        "The configured meeting window has been checked, but its temporal "
                        "scope was implicit or derived; do not claim the overall natural-"
                        "language scope is complete."
                    )
                )
            )
        ),
    }


def _public_backend_payload(value: Any) -> dict[str, Any]:
    """Normalize a research domain object without shortening any nested text."""

    for method_name in ("public_payload", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            value = method()
            break
    payload = to_jsonable(value)
    if not isinstance(payload, Mapping):
        raise RuntimeError("Research backend must return an object payload")
    return dict(payload)


def _research_id(payload: Mapping[str, Any]) -> str:
    research_id = str(payload.get("research_id") or payload.get("id") or "").strip()
    if not research_id:
        raise RuntimeError("Research backend did not return a research_id")
    _validate_identifier(research_id, "research_id")
    return research_id


def _validate_identifier(value: str, field_name: str, *, maximum: int = 200) -> None:
    if not value.strip() or len(value) > maximum:
        raise ValueError(f"{field_name} must contain between 1 and {maximum} characters")


def _optional_iso_date(value: str | None, field_name: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must use YYYY-MM-DD") from exc


def _next_action(
    tool: str | None,
    arguments: Mapping[str, Any],
    *,
    ko: str,
    en: str,
    retry_after_seconds: Any | None = None,
    optional: bool = False,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "tool": tool,
        "arguments": dict(arguments),
        "instruction_ko": ko,
        "instruction_en": en,
    }
    if retry_after_seconds is not None:
        action["retry_after_seconds"] = retry_after_seconds
    if optional:
        action["optional"] = True
    return action
