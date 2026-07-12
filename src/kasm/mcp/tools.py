"""Transport-independent implementations of the public MCP tools.

The functions in this module deliberately do not depend on the MCP SDK.  This
makes the product API usable from the CLI and straightforward to unit test.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast


class SearchService(Protocol):
    def search(self, query: str, **filters: Any) -> Any: ...


class SpeechRepository(Protocol):
    def get(self, speech_id: str) -> Any: ...


@dataclass(slots=True)
class ServiceContext:
    """Dependencies required by the MCP tools.

    ``catalog`` may be the same object as ``repository``.  Keeping it separate
    allows a search index and metadata store to evolve independently.
    """

    search: SearchService
    repository: SpeechRepository
    catalog: Any | None = None


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
    ) -> dict[str, Any]:
        """Discover relevant official meetings live, ingest bounded minutes, and search speeches."""
        if not query.strip():
            raise ValueError("query must not be empty")
        if len(query) > 500:
            raise ValueError("query must not exceed 500 characters")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        filters = {
            "assembly_term": assembly_term,
            "committee": committee,
            "speaker": speaker,
            "speaker_role": speaker_role,
            "organization": organization,
            "meeting_type": meeting_type,
            "date_from": date_from,
            "date_to": date_to,
            "limit": limit,
            "include_context": include_context,
        }
        result = self.services.search.search(
            query, **{key: value for key, value in filters.items() if value is not None}
        )
        payload = to_jsonable(result)
        if isinstance(payload, Mapping) and "results" in payload:
            return {"query": query, **dict(payload)}
        return {"query": query, "results": payload or []}

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
        if before > 20 or after > 20:
            raise ValueError("before and after must not exceed 20")
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
        filters = {"assembly_term": assembly_term, "query": query}
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
            "committee": committee,
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
    ) -> dict[str, Any]:
        """Search live bills and attach on-demand official committee expert review reports."""
        if not query.strip():
            raise ValueError("query must not be empty")
        if status not in {None, "pending", "processed"}:
            raise ValueError("status must be pending or processed")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        catalog = self.services.catalog or self.services.repository
        filters = {
            "assembly_term": assembly_term,
            "committee": committee,
            "status": status,
            "limit": limit,
        }
        result = _invoke(
            catalog, ("search_bills",), query, **{k: v for k, v in filters.items() if v is not None}
        )
        return {"query": query, "results": to_jsonable(result) or []}

    def get_bill_status(self, bill_id_or_no: str) -> dict[str, Any]:
        """Refresh one bill's status and attach its official expert review report when available."""
        if not bill_id_or_no.strip():
            raise ValueError("bill_id_or_no must not be empty")
        catalog = self.services.catalog or self.services.repository
        result = _invoke(catalog, ("get_bill_status",), bill_id_or_no)
        if result is None:
            raise LookupError(f"Bill not found: {bill_id_or_no}")
        return cast(dict[str, Any], to_jsonable(result))

    def explore_issue(self, query: str, limit: int = 20) -> dict[str, Any]:
        """Research an issue across bills, status, committees and full discussion context.

        Use this as the primary tool for questions asking what happened, who argued what,
        or how a policy and bill evolved. Results include evidence-ranked speeches, ordered
        multi-turn discussion threads, bill and review-report links, official provenance, and
        live-check metadata.
        It queries official Open Assembly APIs before searching the private local cache and reports
        bounded-refresh diagnostics. Synthesize the answer from actual turns; do not infer a stance
        that is not supported by a quoted speech. Put each quote's citation.official_url next
        to the claim so the user can open and verify the original minutes immediately.
        """
        if not query.strip():
            raise ValueError("query must not be empty")
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        catalog = self.services.catalog or self.services.repository
        return cast(
            dict[str, Any], to_jsonable(_invoke(catalog, ("explore_issue",), query, limit=limit))
        )
