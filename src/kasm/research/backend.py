"""Public MCP facade over the durable research engine."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Protocol, runtime_checkable

from .documents import OfficialDocumentStore
from .engine import DerivedResearchStatus, ResearchEngine
from .jobs import JobStatus
from .overview import build_provisional_research_overview
from .results import EvidenceIndexEntry, EvidenceRecord
from .status_storage import BoundedResearchStatusView


@runtime_checkable
class _BoundedStatusRunStore(Protocol):
    def get_status_view(self, research_id: str) -> BoundedResearchStatusView | None: ...


class DurableResearchBackend:
    """Bind request-scoped user credentials to credential-free durable results.

    The Assembly key is read only while starting a job and is handed to the
    engine's encrypted capability codec.  Status, result pages, and documents
    are subsequently served by research ID without copying that key into any
    public payload or durable artifact.
    """

    def __init__(
        self,
        engine: ResearchEngine,
        document_store: OfficialDocumentStore,
        *,
        assembly_api_key_provider: Callable[[], str | None],
    ) -> None:
        self.engine = engine
        self.document_store = document_store
        self._assembly_api_key_provider = assembly_api_key_provider

    def start_research(
        self,
        query: str,
        *,
        korean_query: str | None = None,
        assembly_term: int | None = None,
        committees: tuple[str, ...] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        key = str(self._assembly_api_key_provider() or "").strip()
        if not key:
            raise RuntimeError("이 MCP 연결에 열린국회정보 API 인증키가 없습니다.")
        receipt = self.engine.gateway(
            query,
            assembly_api_key=key,
            korean_query=korean_query,
            assembly_term=assembly_term,
            committees=committees,
            date_from=_optional_date(date_from, "date_from"),
            date_to=_optional_date(date_to, "date_to"),
        )
        return {**receipt.to_dict(), "retry_after_seconds": 1}

    def get_research_status(self, research_id: str) -> dict[str, Any]:
        if isinstance(self.engine.runs, _BoundedStatusRunStore):
            bounded = self.engine.runs.get_status_view(research_id)
        else:
            bounded = None
        if bounded is None:
            derived = self.engine.derive_status(research_id)
            summary = self.engine.runs.get_snapshot_summary(research_id)
        else:
            derived = bounded.derived
            summary = bounded.summary
        job = self.engine.jobs.get(research_id)
        if summary is not None:
            status = JobStatus.COMPLETE if summary.coverage.complete else JobStatus.PARTIAL
        elif job is not None:
            status = job.status
        else:
            status = JobStatus.RUNNING
        payload: dict[str, Any] = {
            "research_id": research_id,
            "status": status.value,
            "stage": derived.stage,
            "progress": _progress(derived),
            "work": _derived_payload(derived),
            # Status reads are bounded to a handful of checkpoint objects.
            # A one-second hint lets web MCP clients surface an observed, explicitly
            # incomplete first-page map promptly without restarting or duplicating work.
            "retry_after_seconds": 1,
            "overview_available": derived.overview_available,
            "overview_phase": (
                "final"
                if derived.snapshot_ready
                else "metadata_first_page_preview"
                if derived.overview_available and derived.stage == "metadata_discovery"
                else "metadata"
                if derived.overview_available
                else None
            ),
        }
        if job is not None:
            payload.update(
                {
                    "created_at": job.created_at.isoformat(),
                    "updated_at": job.updated_at.isoformat(),
                    "expires_at": job.expires_at.isoformat(),
                    "error": (
                        {"code": job.error_code, "message": job.error_message}
                        if job.error_code
                        else None
                    ),
                }
            )
        if summary is not None:
            payload.update(summary.to_dict())
            if job is not None:
                payload["contract"] = job.contract.canonical_payload()
        terminal = status in {
            JobStatus.COMPLETE,
            JobStatus.PARTIAL,
            JobStatus.FAILED,
            JobStatus.EXPIRED,
        }
        coverage_complete = bool(summary is not None and summary.coverage.complete)
        if summary is not None:
            warning_codes = [] if coverage_complete else ["coverage_incomplete"]
            coverage_value = payload.get("coverage")
            coverage = dict(coverage_value) if isinstance(coverage_value, dict) else {}
            coverage.update(
                {
                    "state": "complete" if coverage_complete else "partial",
                    "complete": coverage_complete,
                    "warning_codes": warning_codes,
                }
            )
            payload.update(
                {
                    "terminal": True,
                    "provisional": not coverage_complete,
                    "source_complete": coverage_complete,
                    "pending_total": 0,
                    "pending_total_known": True,
                    "warning_codes": warning_codes,
                    "coverage": coverage,
                }
            )
        elif terminal:
            warning_codes = [f"research_{status.value}", "coverage_unavailable"]
            payload.update(
                {
                    "terminal": True,
                    "provisional": True,
                    "source_complete": False,
                    "pending_total": None,
                    "pending_total_known": False,
                    "warning_codes": warning_codes,
                    "coverage": {
                        "state": status.value,
                        "complete": False,
                        "warning_codes": warning_codes,
                    },
                }
            )
        else:
            warning_codes = ["official_source_verification_pending"]
            if derived.overview_available and derived.stage == "metadata_discovery":
                warning_codes.insert(0, "metadata_source_prefix_incomplete")
            payload.update(
                {
                    "terminal": False,
                    "provisional": True,
                    "source_complete": False,
                    "pending_total": None,
                    "pending_total_known": False,
                    "warning_codes": warning_codes,
                    "coverage": {
                        "state": "pending",
                        "complete": False,
                        "warning_codes": warning_codes,
                    },
                }
            )
        return payload

    def get_research_overview(
        self,
        research_id: str,
        *,
        offset: int = 0,
        page_size: int = 20,
        view_source_hash: str | None = None,
    ) -> dict[str, Any]:
        """Return a core-first map while exhaustive source work continues."""

        if offset < 0:
            raise ValueError("offset must not be negative")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        if view_source_hash is not None and (
            len(view_source_hash) != 64
            or any(character not in "0123456789abcdef" for character in view_source_hash)
        ):
            raise ValueError("view_source_hash must be a lowercase SHA-256 digest")

        def metadata_overview() -> Any:
            compact_getter = getattr(self.engine.runs, "get_provisional_overview", None)
            if callable(compact_getter):
                return compact_getter(research_id)
            # Compatibility for third-party/legacy run stores. Hosted and local
            # built-in stores always serve the compact accepted-only artifact.
            discovery = self.engine.runs.get_discovery(research_id)
            return build_provisional_research_overview(discovery) if discovery is not None else None

        overview = metadata_overview() if view_source_hash is not None else None
        if view_source_hash is not None and (
            overview is None or overview.source_hash != view_source_hash
        ):
            raise RuntimeError(
                "요청한 후보 지도 버전을 찾을 수 없습니다. 같은 research_id의 상태를 "
                "다시 확인해 최신 next_action을 사용하세요."
            )

        final = (
            None
            if view_source_hash is not None
            else self.engine.runs.get_overview_page(
                research_id,
                offset=offset,
                page_size=page_size,
            )
        )
        if final is not None:
            coverage_complete = bool(final.get("complete"))
            warning_codes = [] if coverage_complete else ["coverage_incomplete"]
            coverage_value = final.get("coverage")
            coverage = dict(coverage_value) if isinstance(coverage_value, dict) else {}
            coverage.update(
                {
                    "state": "complete" if coverage_complete else "partial",
                    "complete": coverage_complete,
                    "warning_codes": warning_codes,
                }
            )
            return {
                **final,
                "research_id": research_id,
                "phase": "final",
                "terminal": True,
                "provisional": not coverage_complete,
                "source_complete": coverage_complete,
                "pending_total": 0,
                "pending_total_known": True,
                "warning_codes": warning_codes,
                "coverage": coverage,
                "substantive_conclusion_available": True,
                "full_evidence_inventory_delivery": "get_research_page",
            }

        if overview is None:
            overview = metadata_overview()
        if overview is None:
            status = self._derived_status(research_id)
            raise RuntimeError(
                f"조사 자료 지도가 아직 준비되지 않았습니다. 현재 단계: {status.stage}"
            )
        priority_candidate_limit = 12
        priority_candidates = tuple(
            item.to_dict() for item in overview.entries[:priority_candidate_limit]
        )
        metadata_inventory_complete = overview.source.source_complete is True
        if not metadata_inventory_complete and offset != 0:
            raise RuntimeError(
                "첫 페이지 후보 미리보기는 추가 페이지를 제공하지 않습니다. "
                "같은 research_id의 상태를 확인해 전체 후보 지도를 기다리세요."
            )
        page = overview.page(offset=offset, page_size=page_size)
        source = {
            **overview.source.to_dict(),
            "scope": "metadata_discovery_partitions",
            "scope_complete": metadata_inventory_complete,
        }
        if metadata_inventory_complete:
            catalog = {
                **page.to_dict(),
                "inventory_complete": True,
                "truncated": False,
            }
        else:
            # A preview is one stable, relevance-ranked orientation page only. Never
            # issue an offset into it: full discovery may replace the preferred map
            # between calls, which would otherwise mix two immutable inventories.
            entries = [item.to_dict() for item in page.entries]
            catalog = {
                "offset": 0,
                "page_size": page_size,
                "total": len(entries),
                "observed_accepted_total": overview.accepted_total,
                "returned_count": len(entries),
                "next_offset": None,
                "complete": True,
                "inventory_complete": False,
                "truncated": len(entries) < overview.accepted_total,
                "selection": "deterministic_relevance_ranked_orientation",
                "entries": entries,
            }
        warning_codes = ["official_source_verification_pending"]
        if not metadata_inventory_complete:
            warning_codes.insert(0, "metadata_source_prefix_incomplete")
        return {
            "research_id": research_id,
            "phase": "metadata",
            "metadata_stage": (
                "complete_discovery" if metadata_inventory_complete else "first_page_preview"
            ),
            "query": overview.query,
            "source_hash": overview.source_hash,
            "provisional": True,
            "complete": False,
            "terminal": False,
            # Top-level source completeness covers the requested research, not merely
            # metadata pagination. It remains false until a complete final snapshot.
            "source_complete": False,
            "metadata_inventory_complete": metadata_inventory_complete,
            "substantive_conclusion_available": False,
            "warning": (
                (
                    "공식 API 첫 페이지들에서 먼저 확인된 후보 지도입니다. 전체 후보 수집과 "
                    if not metadata_inventory_complete
                    else "전체 메타데이터 후보 지도입니다. "
                )
                + "원문·회의록·검토보고서 확인 전이므로 "
                "실질적 결론으로 사용하지 마세요."
            ),
            "warning_codes": warning_codes,
            "coverage": {
                "state": "pending",
                "complete": False,
                "warning_codes": warning_codes,
            },
            "accepted_total": overview.accepted_total,
            "accepted_total_scope": (
                "complete_metadata_discovery"
                if metadata_inventory_complete
                else "observed_first_pages"
            ),
            "rejected_total": sum(item.rejected_count for item in overview.families),
            "family_accounting_scope": (
                "complete_metadata_discovery"
                if metadata_inventory_complete
                else "observed_first_pages"
            ),
            # Full deferred work is not planned until discovery closes, so zero would
            # be a false completeness signal here.
            "pending_total": None,
            "pending_total_known": False,
            "families": [item.to_dict() for item in overview.families],
            "source": source,
            "priority_candidates": list(priority_candidates),
            "priority_candidates_selection": {
                "policy": "deterministic_core_first_preview",
                "limit": priority_candidate_limit,
                "returned": len(priority_candidates),
                "accepted_total": overview.accepted_total,
                "complete": overview.accepted_total <= priority_candidate_limit,
                "inventory_complete": metadata_inventory_complete,
                "full_inventory": (
                    "catalog"
                    if metadata_inventory_complete
                    else "pending_complete_metadata_catalog"
                ),
            },
            "catalog": catalog,
            "catalog_scope": (
                "accepted_metadata_inventory_page"
                if metadata_inventory_complete
                else "observed_first_pages_core_orientation"
            ),
            "catalog_completion_meaning": (
                "catalog.complete는 현재 반환한 orientation 페이지의 끝만 뜻합니다. "
                "catalog.inventory_complete가 false이면 이 목록은 페이지 순회 대상이 아니며 "
                "전체 메타데이터 수집도 계속됩니다. "
                "같은 research_id의 status를 확인한 뒤 final overview와 evidence tools를 "
                "사용하세요."
            ),
        }

    def get_research_page(
        self,
        research_id: str,
        *,
        cursor: str | None = None,
        page_size: int = 20,
    ) -> dict[str, Any]:
        page = self.engine.runs.get_result_page(
            research_id,
            cursor=cursor,
            page_size=page_size,
        )
        if page is not None:
            return page
        # A result page is published only after every compact index shard is
        # durable.  Keep the error stage-specific instead of loading the giant
        # source-text snapshot as a hidden slow-path.
        status = self._derived_status(research_id)
        raise RuntimeError(
            f"조사 결과 인덱스가 아직 준비되지 않았습니다. 현재 단계: {status.stage}"
        )

    def get_evidence_document(
        self,
        research_id: str,
        evidence_id: str,
        *,
        cursor: str | None = None,
        max_characters: int = 20_000,
        scope: str = "selected",
    ) -> dict[str, Any]:
        if not 1 <= max_characters <= 50_000:
            raise ValueError("max_characters must be between 1 and 50000")
        if scope not in {"selected", "core", "all"}:
            raise ValueError("scope must be selected, core, or all")
        evidence = self.engine.runs.get_evidence_index_entry(research_id, evidence_id)
        if evidence is None:
            status = self._derived_status(research_id)
            raise RuntimeError(
                f"조사 결과 인덱스가 아직 준비되지 않았습니다. 현재 단계: {status.stage}"
            )
        if evidence.inline_text is not None:
            return self._with_evidence_progress(
                research_id,
                evidence_id,
                _evidence_index_page(
                    research_id,
                    evidence,
                    cursor=cursor,
                    max_characters=max_characters,
                ),
                scope=scope,
            )
        # Every non-inline evidence unit lives in a bounded immutable text
        # shard.  Serving the exact finalized unit avoids re-reading and
        # reparsing an entire PDF for every requested page while preserving
        # the source hash and locator carried by the public index.
        return self._with_evidence_progress(
            research_id,
            evidence_id,
            _evidence_record_page(
                research_id,
                self._required_overflow_evidence(research_id, evidence_id),
                cursor=cursor,
                max_characters=max_characters,
            ),
            scope=scope,
        )

    def _derived_status(self, research_id: str) -> DerivedResearchStatus:
        """Prefer constant-read hosted checkpoints on every status/error path."""

        if isinstance(self.engine.runs, _BoundedStatusRunStore):
            bounded = self.engine.runs.get_status_view(research_id)
            if bounded is not None:
                return bounded.derived
        return self.engine.derive_status(research_id)

    def _required_overflow_evidence(
        self,
        research_id: str,
        evidence_id: str,
    ) -> EvidenceRecord:
        evidence = self.engine.runs.get_overflow_evidence_record(research_id, evidence_id)
        if evidence is None:
            status = self._derived_status(research_id)
            raise RuntimeError(f"조사가 아직 완료되지 않았습니다. 현재 단계: {status.stage}")
        return evidence

    def _with_evidence_progress(
        self,
        research_id: str,
        evidence_id: str,
        payload: dict[str, Any],
        *,
        scope: str,
    ) -> dict[str, Any]:
        complete = bool(payload.get("complete"))
        next_evidence_id = None
        if complete and scope == "core":
            next_evidence_id = self.engine.runs.get_next_core_evidence_id(research_id, evidence_id)
        elif complete and scope == "all":
            next_evidence_id = self.engine.runs.get_next_full_text_evidence_id(
                research_id, evidence_id
            )
        summary = self.engine.runs.get_snapshot_summary(research_id)
        sequence_complete = complete and next_evidence_id is None
        payload.update(
            {
                "scope": scope,
                "next_evidence_id": next_evidence_id,
                "selected_evidence_complete": complete,
                "core_evidence_complete": scope == "core" and sequence_complete,
                "research_evidence_complete": scope == "all" and sequence_complete,
                "research_coverage_complete": bool(
                    summary is not None and summary.coverage.complete
                ),
            }
        )
        return payload


def _evidence_index_page(
    research_id: str,
    evidence: EvidenceIndexEntry,
    *,
    cursor: str | None,
    max_characters: int,
) -> dict[str, Any]:
    if evidence.inline_text is None:
        raise RuntimeError("인덱스에 인라인 공식 원문이 없습니다.")
    return _document_page(
        research_id=research_id,
        evidence_id=evidence.id,
        full_text=evidence.inline_text,
        text_hash=evidence.text_hash,
        segments=((evidence.citation.source_locator, evidence.inline_text),),
        cursor=cursor,
        max_characters=max_characters,
        metadata={
            "research_id": research_id,
            "evidence_id": evidence.id,
            "kind": "evidence_record",
            "title": evidence.title,
            "official_url": evidence.citation.official_url,
            "source_hash": evidence.citation.source_hash,
            "retrieved_at": evidence.citation.retrieved_at.isoformat(),
            "requested_locator": evidence.citation.source_locator,
            "citation": evidence.citation.to_dict(),
            "metadata": dict(evidence.metadata),
        },
    )


def _evidence_record_page(
    research_id: str,
    evidence: EvidenceRecord,
    *,
    cursor: str | None,
    max_characters: int,
) -> dict[str, Any]:
    return _document_page(
        research_id=research_id,
        evidence_id=evidence.id,
        full_text=evidence.text,
        text_hash=evidence.text_hash,
        segments=((evidence.citation.source_locator, evidence.text),),
        cursor=cursor,
        max_characters=max_characters,
        metadata={
            "research_id": research_id,
            "evidence_id": evidence.id,
            "kind": "evidence_record",
            "title": evidence.title,
            "official_url": evidence.citation.official_url,
            "source_hash": evidence.citation.source_hash,
            "retrieved_at": evidence.citation.retrieved_at.isoformat(),
            "requested_locator": evidence.citation.source_locator,
            "citation": evidence.citation.to_dict(),
            "metadata": dict(evidence.metadata),
        },
    )


def _optional_date(value: str | None, name: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD)") from None


def _derived_payload(status: DerivedResearchStatus) -> dict[str, int | bool]:
    return {
        "metadata_partitions_expected": status.metadata_partitions_expected,
        "metadata_partitions_complete": status.metadata_partitions_complete,
        "metadata_pages_expected": status.metadata_pages_expected,
        "metadata_pages_complete": status.metadata_pages_complete,
        "bill_document_checks_expected": status.bill_document_checks_expected,
        "bill_document_checks_complete": status.bill_document_checks_complete,
        "documents_expected": status.documents_expected,
        "documents_complete": status.documents_complete,
        "documents_failed": status.documents_failed,
        "snapshot_ready": status.snapshot_ready,
        "overview_available": status.overview_available,
        "complete": status.complete,
    }


def _progress(status: DerivedResearchStatus) -> float:
    expected = (
        status.metadata_pages_expected
        + status.bill_document_checks_expected
        + status.documents_expected
    )
    complete = (
        status.metadata_pages_complete
        + status.bill_document_checks_complete
        + status.documents_complete
    )
    if status.snapshot_ready:
        return 1.0
    if expected == 0:
        return 0.0
    return round(min(0.99, complete / expected), 4)


@dataclass(frozen=True, slots=True)
class _DocumentCursor:
    """Opaque cursor bound to one immutable evidence document and chunk size."""

    version: int
    research_id: str
    evidence_id: str
    text_hash: str
    character_offset: int
    byte_offset: int
    max_characters: int

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ValueError("unsupported document cursor version")
        if not self.research_id or not self.evidence_id:
            raise ValueError("document cursor scope is required")
        if len(self.text_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.text_hash
        ):
            raise ValueError("document cursor text_hash must be a SHA-256 digest")
        if self.character_offset < 0 or self.byte_offset < 0:
            raise ValueError("document cursor offsets must be non-negative")
        if not 1 <= self.max_characters <= 50_000:
            raise ValueError("document cursor max_characters is invalid")

    def encode(self) -> str:
        payload = json.dumps(
            asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        checksum = hashlib.sha256(payload).digest()[:16]
        return base64.urlsafe_b64encode(checksum + payload).rstrip(b"=").decode()

    @classmethod
    def decode(cls, value: str) -> _DocumentCursor:
        try:
            if not 1 <= len(value) <= 8192 or any(
                character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                for character in value
            ):
                raise ValueError("invalid document cursor encoding")
            padded = value + "=" * (-len(value) % 4)
            raw = base64.b64decode(padded, altchars=b"-_", validate=True)
            checksum, payload = raw[:16], raw[16:]
            if len(checksum) != 16 or not hmac.compare_digest(
                checksum, hashlib.sha256(payload).digest()[:16]
            ):
                raise ValueError("document cursor checksum does not match")
            decoded = json.loads(payload)
            if not isinstance(decoded, dict):
                raise ValueError("document cursor payload must be an object")
            result = cls(**decoded)
            if result.encode() != value:
                raise ValueError("document cursor encoding is not canonical")
            return result
        except (
            binascii.Error,
            UnicodeError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ) as exc:
            raise ValueError("invalid evidence document cursor") from exc


def _document_page(
    *,
    research_id: str,
    evidence_id: str,
    full_text: str,
    text_hash: str,
    segments: tuple[tuple[str, str], ...],
    cursor: str | None,
    max_characters: int,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Return one exact Unicode slice plus non-duplicating locator spans."""

    expected_hash = hashlib.sha256(full_text.encode()).hexdigest()
    if text_hash != expected_hash:
        raise RuntimeError("보존된 공식 원문의 텍스트 해시가 일치하지 않습니다.")
    spans, total_bytes = _segment_spans(segments)
    if "\n\n".join(text for _locator, text in segments) != full_text:
        raise RuntimeError("보존된 공식 원문의 구간과 전체 텍스트가 일치하지 않습니다.")

    character_start = 0
    byte_start = 0
    if cursor is not None:
        decoded = _DocumentCursor.decode(cursor)
        if decoded.research_id != research_id or decoded.evidence_id != evidence_id:
            raise ValueError("cursor belongs to another evidence document")
        if decoded.text_hash != text_hash:
            raise ValueError("cursor belongs to another document revision")
        if decoded.max_characters != max_characters:
            raise ValueError("max_characters must match the cursor")
        character_start = decoded.character_offset
        byte_start = decoded.byte_offset
        if character_start > len(full_text):
            raise ValueError("document cursor is beyond the end of the document")
        actual_byte_start = len(full_text[:character_start].encode())
        if byte_start != actual_byte_start:
            raise ValueError("document cursor byte and character offsets do not match")

    character_end = min(len(full_text), character_start + max_characters)
    text = full_text[character_start:character_end]
    byte_end = byte_start + len(text.encode())
    complete = character_end == len(full_text)
    next_cursor = None
    if not complete:
        next_cursor = _DocumentCursor(
            version=1,
            research_id=research_id,
            evidence_id=evidence_id,
            text_hash=text_hash,
            character_offset=character_end,
            byte_offset=byte_end,
            max_characters=max_characters,
        ).encode()

    returned_segments: list[dict[str, Any]] = []
    for span in spans:
        document_range = span["document_range"]
        segment_start = int(document_range["character_start"])
        segment_end = int(document_range["character_end"])
        intersection_start = max(segment_start, character_start)
        intersection_end = min(segment_end, character_end)
        if intersection_start >= intersection_end:
            continue
        segment_text = segments[int(span["index"])][1]
        local_start = intersection_start - segment_start
        local_end = intersection_end - segment_start
        intersection_byte_start = int(document_range["byte_start"]) + len(
            segment_text[:local_start].encode()
        )
        intersection_byte_end = int(document_range["byte_start"]) + len(
            segment_text[:local_end].encode()
        )
        returned_segments.append(
            {
                **span,
                "returned_range": {
                    "document_character_start": intersection_start,
                    "document_character_end": intersection_end,
                    "document_byte_start": intersection_byte_start,
                    "document_byte_end": intersection_byte_end,
                    "segment_character_start": local_start,
                    "segment_character_end": local_end,
                    "chunk_character_start": intersection_start - character_start,
                    "chunk_character_end": intersection_end - character_start,
                },
            }
        )
    payload = {
        **metadata,
        # Compatibility: ``text`` remains the source text field and
        # ``text_characters`` remains the full document size. Callers must use
        # complete/next_cursor to distinguish a whole document from one chunk.
        "text": text,
        "text_hash": text_hash,
        "text_characters": len(full_text),
        "total_characters": len(full_text),
        "total_bytes": total_bytes,
        "total_segments": len(segments),
        "character_unit": "unicode_code_points",
        "byte_encoding": "utf-8",
        "returned_range": {
            "character_start": character_start,
            "character_end": character_end,
            "characters": character_end - character_start,
            "byte_start": byte_start,
            "byte_end": byte_end,
            "bytes": byte_end - byte_start,
        },
        # Segment entries intentionally contain only locators and ranges. The
        # exact source text appears once, in ``text``, so large pages are not
        # doubled on the wire.
        "segments": returned_segments,
        "returned_segments": len(returned_segments),
        "next_cursor": next_cursor,
        "complete": complete,
    }
    return payload


def _segment_spans(
    segments: tuple[tuple[str, str], ...],
) -> tuple[list[dict[str, Any]], int]:
    spans: list[dict[str, Any]] = []
    character_offset = 0
    byte_offset = 0
    for index, (locator, text) in enumerate(segments):
        if index:
            character_offset += 2
            byte_offset += 2
        character_end = character_offset + len(text)
        byte_end = byte_offset + len(text.encode())
        spans.append(
            {
                "index": index,
                "locator": locator,
                "document_range": {
                    "character_start": character_offset,
                    "character_end": character_end,
                    "byte_start": byte_offset,
                    "byte_end": byte_end,
                },
            }
        )
        character_offset = character_end
        byte_offset = byte_end
    return spans, byte_offset


__all__ = ["DurableResearchBackend"]
