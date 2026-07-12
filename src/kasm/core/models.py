"""Dependency-free domain models and JSON-friendly serialization."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import Any, ClassVar, TypeVar, cast

from .exceptions import ValidationError

T = TypeVar("T", bound="Serializable")


class Serializable:
    _date_fields: ClassVar[set[str]] = set()
    _datetime_fields: ClassVar[set[str]] = set()

    def to_dict(self) -> dict[str, Any]:
        data = cast(dict[str, Any], asdict(self))  # type: ignore[call-overload]
        for key, value in data.items():
            if isinstance(value, (date, datetime)):
                data[key] = value.isoformat()
        return data

    @classmethod
    def from_dict(cls: type[T], data: dict[str, Any]) -> T:
        values = dict(data)
        for key in cls._date_fields:
            if isinstance(values.get(key), str):
                values[key] = date.fromisoformat(values[key])
        for key in cls._datetime_fields:
            if isinstance(values.get(key), str):
                values[key] = datetime.fromisoformat(values[key].replace("Z", "+00:00"))
        return cls(**values)


@dataclass(frozen=True, slots=True)
class Meeting(Serializable):
    id: str
    assembly_term: int
    committee_id: str | None
    committee_name_ko: str | None
    committee_name_en: str | None
    title: str
    meeting_type: str
    meeting_number: str | None
    date: date
    source_url: str
    source_hash: str
    retrieved_at: datetime

    _date_fields = {"date"}
    _datetime_fields = {"retrieved_at"}

    def __post_init__(self) -> None:
        if self.assembly_term <= 0 or not self.id or not self.title or not self.meeting_type:
            raise ValidationError("meeting requires a positive term, id, title, and type")
        if not self.source_url or not self.source_hash:
            raise ValidationError("meeting provenance is required")


@dataclass(frozen=True, slots=True)
class Person(Serializable):
    id: str
    name_ko: str
    name_en: str | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.name_ko.strip():
            raise ValidationError("person id and Korean name are required")


@dataclass(frozen=True, slots=True)
class Speech(Serializable):
    id: str
    meeting_id: str
    sequence: int
    speaker_id: str | None
    speaker_name: str
    speaker_role: str | None
    organization: str | None
    text: str
    agenda: str | None
    previous_speech_id: str | None
    next_speech_id: str | None
    source_locator: str | None
    source_hash: str
    parser_version: str

    def __post_init__(self) -> None:
        if not self.id or not self.meeting_id or self.sequence < 0:
            raise ValidationError("speech requires ids and a non-negative sequence")
        if not self.speaker_name.strip() or not self.text.strip():
            raise ValidationError("speaker name and speech text are required")
        if not self.source_hash or not self.parser_version:
            raise ValidationError("speech provenance is required")


@dataclass(frozen=True, slots=True)
class Bill(Serializable):
    id: str
    bill_no: str
    name: str
    assembly_term: int
    proposer: str | None
    committee: str | None
    proposed_at: date | None
    process_result: str | None
    processed_at: date | None
    official_url: str
    source_hash: str
    retrieved_at: datetime

    _date_fields = {"proposed_at", "processed_at"}
    _datetime_fields = {"retrieved_at"}

    def __post_init__(self) -> None:
        if not self.id or not self.bill_no or not self.name.strip() or self.assembly_term <= 0:
            raise ValidationError("bill requires ids, a name, and a positive term")
        if not self.official_url or not self.source_hash:
            raise ValidationError("bill provenance is required")

    @property
    def status(self) -> str:
        return self.process_result.strip() if self.process_result else "계류"


@dataclass(frozen=True, slots=True)
class SpeechRelation(Serializable):
    source_speech_id: str
    target_speech_id: str
    relation_type: str
    confidence: float

    TYPES: ClassVar[frozenset[str]] = frozenset(
        {"QUESTION_TO", "ANSWER_TO", "FOLLOW_UP_TO", "CONTINUES"}
    )

    def __post_init__(self) -> None:
        if self.relation_type not in self.TYPES:
            raise ValidationError(f"unsupported relation type: {self.relation_type}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValidationError("confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class EmbeddingRecord(Serializable):
    speech_id: str
    model_name: str
    dimensions: int
    vector_location: str
    created_at: datetime

    _datetime_fields = {"created_at"}

    def __post_init__(self) -> None:
        if not self.speech_id or not self.model_name or not self.vector_location:
            raise ValidationError("embedding identifiers are required")
        if self.dimensions <= 0:
            raise ValidationError("dimensions must be positive")


def utc_now() -> datetime:
    return datetime.now(UTC)
