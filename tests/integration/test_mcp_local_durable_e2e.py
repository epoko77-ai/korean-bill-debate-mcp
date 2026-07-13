from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import threading
import urllib.parse
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from kasm.adapters.korea.bills import BILL_DATASET, BILL_STATUS_DATASET
from kasm.adapters.korea.client import ApiPage
from kasm.mcp.server import create_server, run
from kasm.mcp.tools import ServiceContext
from kasm.research.artifact_job_storage import ArtifactResearchJobStore
from kasm.research.artifact_run_storage import ArtifactResearchRunStore
from kasm.research.artifacts import FilesystemResearchArtifactStore
from kasm.research.backend import DurableResearchBackend
from kasm.research.contracts import EvidenceType
from kasm.research.credentials import ResearchCredential
from kasm.research.document_worker import DocumentWorkResult
from kasm.research.documents import (
    FilesystemOfficialDocumentStore,
    OfficialDocumentKind,
    ParsedOfficialDocument,
    TextSegment,
)
from kasm.research.engine import (
    BillDocumentDiscovery,
    DocumentWorkItem,
    ResearchEngine,
)
from kasm.research.finalizer import ConnectedResearchFinalizer
from kasm.research.partitioning import ResearchPartitionPlan, ResearchPartitionPlanner
from kasm.research.planner import ResearchContractPlanner, ResearchPlan
from kasm.research.queue import LeasedResearchTask, ResearchTask, ResearchTaskStage
from kasm.research.resolver import MetadataCandidateResolver

ROOT = Path(__file__).parents[2]
LEGACY_TOOL_NAMES = {
    "search_speeches",
    "get_speech",
    "get_speech_context",
    "list_committees",
    "list_meetings",
    "search_bills",
    "get_bill_status",
    "explore_issue",
}
DURABLE_TOOL_NAMES = {
    *LEGACY_TOOL_NAMES,
    "start_research",
    "get_research_status",
    "get_research_overview",
    "get_research_page",
    "get_evidence_document",
}
_BILL_NO = "2219564"
_QUERY = (
    "2026-01-01부터 2026-07-14까지 의안번호 2219564 보완수사권 관련 "
    "법안·상태·회의록·검토보고서·발언을 조사해줘"
)
_PARSED_AT = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)


class _LegacyServices:
    """Small deterministic legacy surface; durable work never calls it."""

    def search(self, query: str, **filters: Any) -> list[Any]:
        del query, filters
        return []

    def get(self, speech_id: str) -> Any:
        del speech_id
        return None

    def list_meetings(self, **_filters: Any) -> list[Any]:
        return []


class _LocalQueue:
    """Idempotent in-memory delivery queue used only by this transport smoke."""

    def __init__(self) -> None:
        self._tasks: list[ResearchTask] = []
        self._keys: set[str] = set()
        self._position = 0
        self._lock = threading.Lock()

    def publish(
        self,
        task: ResearchTask,
        *,
        retention_seconds: int = 86_400,
        delay_seconds: int = 0,
    ) -> str:
        assert retention_seconds >= 60
        assert delay_seconds >= 0
        with self._lock:
            if task.idempotency_key not in self._keys:
                self._keys.add(task.idempotency_key)
                self._tasks.append(task)
        return f"local-{task.idempotency_key[:16]}"

    def pop(self) -> ResearchTask | None:
        with self._lock:
            if self._position >= len(self._tasks):
                return None
            task = self._tasks[self._position]
            self._position += 1
            return task

    def receive(
        self,
        *,
        max_messages: int = 1,
        visibility_timeout_seconds: int = 300,
    ) -> tuple[LeasedResearchTask, ...]:
        del max_messages, visibility_timeout_seconds
        return ()

    def acknowledge(self, receipt_handle: str) -> None:
        del receipt_handle

    def extend(self, receipt_handle: str, visibility_timeout_seconds: int) -> None:
        del receipt_handle, visibility_timeout_seconds


class _Credentials:
    capability = "g" * 120

    def issue(
        self,
        *,
        research_id: str,
        query_fingerprint: str,
        assembly_api_key: str,
        ttl_seconds: int = 3600,
    ) -> str:
        assert research_id
        assert len(query_fingerprint) == 64
        assert assembly_api_key == "local-user-key"
        assert ttl_seconds >= 60
        return self.capability

    def reveal(
        self,
        token: str,
        *,
        research_id: str,
        query_fingerprint: str,
    ) -> ResearchCredential:
        assert token == self.capability
        return ResearchCredential(
            research_id=research_id,
            query_fingerprint=query_fingerprint,
            assembly_api_key="local-user-key",
            expires_at=2_000_000_000.0,
        )


class _BillsOnlyPartitionPlanner(ResearchPartitionPlanner):
    """Keep this smoke bounded while exercising every deferred bill stage."""

    def plan(self, research_plan: ResearchPlan) -> ResearchPartitionPlan:
        original = super().plan(research_plan)
        selected = next(
            item
            for item in original.planned_partitions
            if item.source.value == "bill_metadata"
        )
        return replace(original, planned_partitions=(selected,))


class _PageClient:
    def fetch_page(
        self,
        dataset: str,
        *,
        page: int = 1,
        page_size: int = 100,
        parameters: Any = None,
        refresh: bool = False,
    ) -> ApiPage:
        assert refresh is False
        assert page == 1
        values = dict(parameters or {})
        assert str(values["BILL_NO"]) == _BILL_NO
        if dataset == BILL_DATASET:
            rows = [
                {
                    "BILL_NO": _BILL_NO,
                    "AGE": "22",
                    "BILL_NAME": "형사소송법 일부개정법률안",
                    "summary": "보완수사권의 범위와 절차를 정비한다.",
                    "PROPOSE_DT": "2026-06-01",
                    "PROPOSER": "국회의원 테스트 외 9인",
                    "COMMITTEE": "법제사법위원회",
                    "DETAIL_LINK": (
                        "https://likms.assembly.go.kr/bill/"
                        "billDetail.do?billId=PRC_SMOKE_22"
                    ),
                }
            ]
        elif dataset == BILL_STATUS_DATASET:
            rows = [
                {
                    "BILL_NO": _BILL_NO,
                    "AGE": "22",
                    "PROC_RESULT": "위원회 심사",
                    "PROC_DT": "2026-07-10",
                }
            ]
        else:  # pragma: no cover - a new partition would intentionally fail this smoke
            raise AssertionError(f"unexpected official dataset: {dataset}")
        source_hash = hashlib.sha256(repr((dataset, rows)).encode()).hexdigest()
        return ApiPage(
            dataset,
            page,
            page_size,
            len(rows),
            tuple(rows),
            (
                f"https://open.assembly.go.kr/portal/openapi/{dataset}"
                f"?KEY=%2A%2A%2A&pIndex={page}&pSize={page_size}"
            ),
            source_hash,
        )


class _BillDocuments:
    def discover_one(
        self, plan: ResearchPlan, bill: Any
    ) -> BillDocumentDiscovery:
        assert plan.contract.bill_numbers == (_BILL_NO,)
        number = str(bill.candidate["BILL_NO"])
        return BillDocumentDiscovery(
            number,
            (
                DocumentWorkItem.create(
                    OfficialDocumentKind.BILL_TEXT,
                    (
                        "https://likms.assembly.go.kr/bill/bi/bill/detail/"
                        "downloadDtlZip.do?billId=PRC_SMOKE_22&billNo=2219564"
                        "&billKindCd=law&dwFileGbn=B"
                    ),
                    evidence_types=(EvidenceType.BILL_TEXT,),
                    related_bill_numbers=(number,),
                ),
                DocumentWorkItem.create(
                    OfficialDocumentKind.REVIEW_REPORT,
                    "https://likms.assembly.go.kr/filegate/review.pdf?id=2219564-smoke",
                    evidence_types=(EvidenceType.REVIEW_REPORTS,),
                    related_bill_numbers=(number,),
                ),
            ),
        )


class _Worker:
    def process(
        self,
        kind: OfficialDocumentKind,
        official_url: str,
        *,
        refresh: bool = False,
    ) -> DocumentWorkResult:
        assert refresh is False
        if kind is OfficialDocumentKind.BILL_TEXT:
            segments = (
                TextSegment(
                    "p.1",
                    "제1조(목적) 보완수사권의 행사 범위와 통제 절차를 정한다.\n" * 120,
                ),
                TextSegment(
                    "p.2",
                    "제2조(절차) 수사 요구와 이행 결과를 기록하고 국회에 보고한다.\n" * 130,
                ),
            )
        elif kind is OfficialDocumentKind.REVIEW_REPORT:
            segments = (
                TextSegment(
                    "p.1",
                    (
                        "전문위원은 보완수사 범위의 명확성과 기본권 보호 장치를 "
                        "검토해야 한다고 지적했다.\n"
                    )
                    * 135,
                ),
                TextSegment(
                    "p.2",
                    "정부는 제도 공백을 막되 통제 기준과 사후 보고 의무를 함께 두겠다고 답변했다.\n"
                    * 145,
                ),
            )
        else:  # pragma: no cover - the fixture discovers no minutes document
            raise AssertionError(f"unexpected document kind: {kind}")
        full_text = "\n\n".join(item.text for item in segments)
        source_hash = hashlib.sha256(f"{official_url}\0{full_text}".encode()).hexdigest()
        document = ParsedOfficialDocument(
            kind=kind,
            official_url=official_url,
            source_hash=source_hash,
            parser_version="local-durable-smoke-v1",
            parsed_at=_PARSED_AT,
            segments=segments,
        )
        return DocumentWorkResult(
            kind=kind,
            official_url=official_url,
            parser_version=document.parser_version,
            byte_count=len(full_text.encode()),
            page_count=len(segments),
            character_count=len(full_text),
            source_hash=source_hash,
            text_hash=document.text_hash,
            cache_hit=False,
            raw_object_key=f"official/raw/{source_hash}",
            parsed_object_key=document.object_key,
            document=document,
        )


class _DrainingBackend(DurableResearchBackend):
    """Model a worker fleet after the first observable status poll."""

    def __init__(
        self,
        engine: ResearchEngine,
        document_store: FilesystemOfficialDocumentStore,
        queue: _LocalQueue,
    ) -> None:
        super().__init__(
            engine,
            document_store,
            assembly_api_key_provider=lambda: "local-user-key",
        )
        self._queue = queue
        self._seen_status: set[str] = set()
        self._status_lock = threading.Lock()

    def get_research_status(self, research_id: str) -> dict[str, Any]:
        with self._status_lock:
            first_poll = research_id not in self._seen_status
            self._seen_status.add(research_id)
        if first_poll:
            self._drain(max_tasks=1)
        else:
            self._drain()
        return super().get_research_status(research_id)

    def _drain(self, *, max_tasks: int | None = None) -> None:
        processed = 0
        while (task := self._queue.pop()) is not None:
            if task.stage is ResearchTaskStage.COLLECT_METADATA:
                self.engine.process_metadata_task(task)
            elif task.stage is ResearchTaskStage.HYDRATE_DOCUMENT:
                self.engine.process_document_task(task)
            elif task.stage is ResearchTaskStage.FINALIZE:
                self.engine.process_finalize_task(task)
            else:  # pragma: no cover - enum exhaustiveness guard
                raise AssertionError(f"unexpected task stage: {task.stage}")
            processed += 1
            if max_tasks is not None and processed >= max_tasks:
                return


def _durable_services(root: Path) -> ServiceContext:
    artifacts = FilesystemResearchArtifactStore(root / "artifacts")
    jobs = ArtifactResearchJobStore(artifacts)
    runs = ArtifactResearchRunStore(artifacts)
    queue = _LocalQueue()
    page_client = _PageClient()

    def page_client_factory(key: str) -> _PageClient:
        assert key == "local-user-key"
        return page_client

    engine = ResearchEngine(
        index_revision="local-mcp-durable-e2e-v1",
        planner=ResearchContractPlanner(),
        partition_planner=_BillsOnlyPartitionPlanner(),
        jobs=jobs,
        queue=queue,
        credentials=_Credentials(),
        page_client_factory=page_client_factory,
        resolver=MetadataCandidateResolver(),
        bill_documents=_BillDocuments(),
        document_worker=_Worker(),
        finalizer=ConnectedResearchFinalizer(build_sha="local-durable-smoke-build"),
        runs=runs,
        direct_fanout_limit=16,
        fanout_chunk_size=32,
    )
    document_store = FilesystemOfficialDocumentStore(root / "documents")
    legacy = _LegacyServices()
    return ServiceContext(
        search=legacy,
        repository=legacy,
        catalog=legacy,
        research=_DrainingBackend(engine, document_store, queue),
    )


def _payload(result: Any) -> dict[str, Any]:
    assert not result.isError, result.content
    assert isinstance(result.structuredContent, dict)
    return dict(result.structuredContent)


async def _provisional_overview(
    session: ClientSession, research_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    offset = 0
    entries: list[dict[str, Any]] = []
    first: dict[str, Any] | None = None
    page_count = 0
    while True:
        payload = _payload(
            await session.call_tool(
                "get_research_overview",
                {"research_id": research_id, "offset": offset, "page_size": 1},
            )
        )
        page_count += 1
        first = first or payload
        assert payload["phase"] == "metadata"
        assert payload["provisional"] is True
        assert payload["substantive_conclusion_available"] is False
        selection = payload["priority_candidates_selection"]
        assert selection["policy"] == "deterministic_core_first_preview"
        assert selection["returned"] == len(payload["priority_candidates"])
        assert selection["accepted_total"] == payload["accepted_total"]
        assert selection["full_inventory"] == "catalog"
        catalog = payload["catalog"]
        assert catalog["offset"] == offset
        assert catalog["returned_count"] == len(catalog["entries"])
        entries.extend(catalog["entries"])
        next_offset = catalog["next_offset"]
        assert catalog["complete"] is (next_offset is None)
        if next_offset is None:
            assert payload["next_action"]["tool"] == "get_research_status"
            assert payload["accepted_total"] == len(entries)
            break
        assert payload["next_action"]["tool"] == "get_research_overview"
        offset = int(next_offset)
    assert first is not None
    assert first["source"]["source_complete"] is True
    assert len({str(item["candidate_id"]) for item in entries}) == len(entries)
    return first, entries, page_count


async def _final_overview(
    session: ClientSession, research_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    offset = 0
    groups: list[dict[str, Any]] = []
    first: dict[str, Any] | None = None
    page_count = 0
    while True:
        payload = _payload(
            await session.call_tool(
                "get_research_overview",
                {"research_id": research_id, "offset": offset, "page_size": 1},
            )
        )
        page_count += 1
        if first is None:
            first = payload
        else:
            assert payload["query_fingerprint"] == first["query_fingerprint"]
            assert payload["core"] == first["core"]
            assert payload["core_full_text_required_ids"] == first[
                "core_full_text_required_ids"
            ]
        assert payload["phase"] == "final"
        assert payload["substantive_conclusion_available"] is True
        catalog = payload["catalog"]
        page = catalog["page"]
        assert page["returned_count"] == len(catalog["groups"])
        groups.extend(catalog["groups"])
        assert page["returned_through"] == len(groups)
        next_offset = page["next_offset"]
        assert page["complete"] is (next_offset is None)
        if next_offset is None:
            assert page["total"] == len(groups)
            break
        assert payload["next_action"]["tool"] == "get_research_overview"
        offset = int(next_offset)
    assert first is not None
    assert first["entity_totals"]["catalog_total"] == len(groups)
    identities = [
        (str(item["entity_type"]), str(item["entity_id"])) for item in groups
    ]
    assert len(identities) == len(set(identities))
    return first, groups, page_count


async def _result_inventory(
    session: ClientSession,
    research_id: str,
    *,
    exhaustive: bool,
) -> tuple[list[dict[str, Any]], list[str], int, dict[str, Any]]:
    cursor: str | None = None
    evidence: list[dict[str, Any]] = []
    full_text_ids: list[str] = []
    matched_total: int | None = None
    page_count = 0
    final_payload: dict[str, Any] | None = None
    while True:
        arguments: dict[str, Any] = {
            "research_id": research_id,
            "page_size": 2,
            "exhaustive": exhaustive,
        }
        if cursor is not None:
            arguments["cursor"] = cursor
        payload = _payload(await session.call_tool("get_research_page", arguments))
        final_payload = payload
        page_count += 1
        page = payload["page"]
        if matched_total is None:
            matched_total = int(page["matched_total"])
        assert page["matched_total"] == matched_total
        assert page["returned_count"] == len(payload["evidence"])
        evidence.extend(payload["evidence"])
        full_text_ids.extend(payload["full_text_required_ids"])
        assert page["returned_through"] == len(evidence)
        cursor = page["next_cursor"]
        assert page["complete"] is (cursor is None)
        if cursor is None:
            assert payload["full_text_required_total"] == len(full_text_ids)
            break
        assert payload["next_action"]["tool"] == "get_research_page"
        assert payload["next_action"]["arguments"]["exhaustive"] is exhaustive
    assert final_payload is not None
    assert matched_total == len(evidence)
    evidence_ids = [str(item["id"]) for item in evidence]
    assert len(evidence_ids) == len(set(evidence_ids))
    assert full_text_ids == [
        str(item["id"])
        for item in evidence
        if item["text_inline_complete"] is False
    ]
    return evidence, full_text_ids, page_count, final_payload


async def _read_document(
    session: ClientSession,
    research_id: str,
    item: dict[str, Any],
    *,
    scope: str,
) -> tuple[str, dict[str, Any], int]:
    evidence_id = str(item["id"])
    cursor: str | None = None
    chunks: list[str] = []
    expected_start = 0
    calls = 0
    final_payload: dict[str, Any] | None = None
    while True:
        arguments: dict[str, Any] = {
            "research_id": research_id,
            "evidence_id": evidence_id,
            "max_characters": 1_300,
            "scope": scope,
        }
        if cursor is not None:
            arguments["cursor"] = cursor
        payload = _payload(
            await session.call_tool("get_evidence_document", arguments)
        )
        final_payload = payload
        calls += 1
        assert payload["scope"] == scope
        returned = payload["returned_range"]
        assert returned["character_start"] == expected_start
        assert returned["characters"] == len(payload["text"])
        assert returned["character_end"] == expected_start + len(payload["text"])
        assert payload["text_hash"] == item["text_hash"]
        assert payload["text_characters"] == item["text_characters"]
        assert payload["official_url"] == item["citation"]["official_url"]
        assert urllib.parse.urlsplit(payload["official_url"]).hostname in {
            "likms.assembly.go.kr",
            "open.assembly.go.kr",
            "record.assembly.go.kr",
        }
        for segment in payload["segments"]:
            segment_range = segment["returned_range"]
            assert segment_range["chunk_character_start"] >= 0
            assert segment_range["chunk_character_end"] <= len(payload["text"])
        chunks.append(str(payload["text"]))
        expected_start = int(returned["character_end"])
        cursor = payload["next_cursor"]
        assert payload["complete"] is (cursor is None)
        if cursor is None:
            break
        assert payload["next_action"]["tool"] == "get_evidence_document"
        assert payload["next_action"]["arguments"]["scope"] == scope
    assert final_payload is not None
    text = "".join(chunks)
    assert len(text) == item["text_characters"]
    assert hashlib.sha256(text.encode()).hexdigest() == item["text_hash"]
    if item["text_inline_complete"]:
        assert text == item["text"]
    return text, final_payload, calls


async def _exercise_durable_session(session: ClientSession) -> dict[str, Any]:
    initialized = await session.initialize()
    assert initialized.serverInfo.name == "Korean Bill & Debate MCP"
    tools = await session.list_tools()
    tool_names = {item.name for item in tools.tools}
    assert tool_names == DURABLE_TOOL_NAMES

    receipt = _payload(await session.call_tool("start_research", {"query": _QUERY}))
    research_id = str(receipt["research_id"])
    assert receipt["status"] == "queued"
    assert receipt["next_action"]["tool"] == "get_research_status"
    assert receipt["comprehensive_answer_allowed"] is False

    first_status = _payload(
        await session.call_tool("get_research_status", {"research_id": research_id})
    )
    assert first_status["status"] == "running"
    assert first_status["work"]["snapshot_ready"] is False
    assert first_status["overview_available"] is True
    assert first_status["overview_phase"] == "metadata"
    assert first_status["next_action"]["tool"] == "get_research_overview"

    provisional, provisional_entries, provisional_pages = await _provisional_overview(
        session, research_id
    )
    assert provisional["accepted_total"] == 1
    assert len(provisional_entries) == 1
    assert provisional_entries[0]["exact_identifiers"]["bill_no"] == _BILL_NO

    terminal = _payload(
        await session.call_tool("get_research_status", {"research_id": research_id})
    )
    assert terminal["status"] in {"complete", "partial"}
    assert terminal["work"]["snapshot_ready"] is True
    assert terminal["progress"] == 1.0
    assert terminal["overview_available"] is True
    assert terminal["overview_phase"] == "final"
    assert terminal["next_action"]["tool"] == "get_research_overview"

    overview, groups, overview_pages = await _final_overview(session, research_id)
    assert overview["evidence_count"] >= 6
    assert len(groups) >= 3
    core_ids = [
        str(item["evidence_id"])
        for item in overview["core"]
        if item["text_inline_complete"] is False
    ]
    assert overview["core_full_text_required_ids"] == core_ids

    evidence, full_text_ids, page_count, selected_inventory_end = (
        await _result_inventory(session, research_id, exhaustive=False)
    )
    exhaustive_evidence, exhaustive_full_text_ids, exhaustive_pages, all_inventory_end = (
        await _result_inventory(session, research_id, exhaustive=True)
    )
    assert exhaustive_evidence == evidence
    assert exhaustive_full_text_ids == full_text_ids
    assert selected_inventory_end["coverage"]["complete"] is (
        terminal["status"] == "complete"
    )
    assert all_inventory_end["next_action"]["tool"] == "get_evidence_document"
    assert all_inventory_end["next_action"]["arguments"]["scope"] == "all"

    assert len(evidence) >= 6
    assert page_count >= 3
    assert exhaustive_pages == page_count
    assert full_text_ids
    item_by_id = {str(item["id"]): item for item in evidence}

    selected_text, selected_end, selected_calls = await _read_document(
        session,
        research_id,
        item_by_id[full_text_ids[0]],
        scope="selected",
    )
    assert selected_text
    assert selected_calls > 1
    assert selected_end["selected_evidence_complete"] is True
    assert not selected_end["next_evidence_id"]

    observed_core: list[str] = []
    next_core_id = core_ids[0] if core_ids else None
    core_texts: dict[str, str] = {}
    core_end: dict[str, Any] | None = None
    while next_core_id is not None:
        observed_core.append(next_core_id)
        core_text, core_end, core_calls = await _read_document(
            session,
            research_id,
            item_by_id[next_core_id],
            scope="core",
        )
        assert core_calls > 1
        core_texts[next_core_id] = core_text
        raw_next = str(core_end.get("next_evidence_id") or "")
        next_core_id = raw_next or None
    assert observed_core == core_ids
    if core_ids:
        assert core_end is not None and core_end["core_evidence_complete"] is True

    observed_all: list[str] = []
    next_all_id: str | None = full_text_ids[0]
    all_texts: dict[str, str] = {}
    all_end: dict[str, Any] | None = None
    while next_all_id is not None:
        observed_all.append(next_all_id)
        all_text, all_end, all_calls = await _read_document(
            session,
            research_id,
            item_by_id[next_all_id],
            scope="all",
        )
        assert all_calls > 1
        all_texts[next_all_id] = all_text
        raw_next = str(all_end.get("next_evidence_id") or "")
        next_all_id = raw_next or None
    assert observed_all == full_text_ids
    assert set(all_texts) == set(full_text_ids)
    assert all_end is not None and all_end["research_evidence_complete"] is True
    assert all_texts[full_text_ids[0]] == selected_text
    for evidence_id, text in core_texts.items():
        assert all_texts[evidence_id] == text
    if terminal["status"] == "complete":
        assert all_end["research_coverage_complete"] is True
        assert all_end["comprehensive_answer_allowed"] is True

    return {
        "research_id": research_id,
        "terminal_status": terminal["status"],
        "provisional_overview_pages": provisional_pages,
        "final_overview_pages": overview_pages,
        "result_pages": page_count,
        "evidence_total": len(evidence),
        "full_text_total": len(full_text_ids),
    }


async def _exercise_http(root: Path) -> dict[str, Any]:
    server = create_server(_durable_services(root), stateless_http=True)
    app = server.streamable_http_app()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8000"
        ) as client,
        streamable_http_client(
            "http://127.0.0.1:8000/mcp",
            http_client=client,
            terminate_on_close=False,
        ) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        return await _exercise_durable_session(session)


async def _exercise_stdio(root: Path) -> dict[str, Any]:
    existing_pythonpath = os.environ.get("PYTHONPATH")
    pythonpath = str(ROOT / "src")
    if existing_pythonpath:
        pythonpath = f"{pythonpath}{os.pathsep}{existing_pythonpath}"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve()), "--stdio-server", str(root)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": pythonpath},
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        return await _exercise_durable_session(session)


def test_legacy_local_surface_remains_exactly_eight_tools() -> None:
    legacy = _LegacyServices()
    services = ServiceContext(search=legacy, repository=legacy, catalog=legacy)

    async def exercise() -> None:
        server = create_server(services, stateless_http=True)
        app = server.streamable_http_app()
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8000"
            ) as client,
            streamable_http_client(
                "http://127.0.0.1:8000/mcp",
                http_client=client,
                terminate_on_close=False,
            ) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
            assert {item.name for item in listed.tools} == LEGACY_TOOL_NAMES
            result = await session.call_tool("list_meetings", {})
            assert _payload(result) == {"result": []}

    asyncio.run(exercise())


def test_real_durable_engine_over_in_process_streamable_http(tmp_path: Path) -> None:
    report = asyncio.run(_exercise_http(tmp_path / "http"))
    assert report["terminal_status"] in {"complete", "partial"}
    assert report["provisional_overview_pages"] >= 1
    assert report["final_overview_pages"] >= 3
    assert report["result_pages"] >= 3
    assert report["full_text_total"] >= 1


def test_real_durable_engine_over_stdio_subprocess(tmp_path: Path) -> None:
    report = asyncio.run(_exercise_stdio(tmp_path / "stdio"))
    assert report["terminal_status"] in {"complete", "partial"}
    assert report["provisional_overview_pages"] >= 1
    assert report["final_overview_pages"] >= 3
    assert report["result_pages"] >= 3
    assert report["full_text_total"] >= 1


if __name__ == "__main__":  # pragma: no cover - stdio subprocess entry point
    if len(sys.argv) != 3 or sys.argv[1] != "--stdio-server":
        raise SystemExit("usage: test_mcp_local_durable_e2e.py --stdio-server ROOT")
    run(_durable_services(Path(sys.argv[2])), transport="stdio")
