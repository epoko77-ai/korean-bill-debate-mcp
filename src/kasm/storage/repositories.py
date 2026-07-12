"""Typed repositories over SQLite."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any, Generic, TypeVar

from kasm.core.exceptions import NotFoundError
from kasm.core.models import Bill, EmbeddingRecord, Meeting, Person, Speech, SpeechRelation

T = TypeVar("T")


class Repository(Generic[T]):
    table: str
    model: type[T]

    def __init__(self, connection: sqlite3.Connection | Any) -> None:
        self.connection = getattr(connection, "connection", connection)

    def get(self, record_id: str) -> T | None:
        row = self.connection.execute(
            f"SELECT * FROM {self.table} WHERE id = ?", (record_id,)
        ).fetchone()
        return self.model.from_dict(dict(row)) if row else None  # type: ignore[attr-defined]

    def require(self, record_id: str) -> T:
        result = self.get(record_id)
        if result is None:
            raise NotFoundError(f"{self.table} record not found: {record_id}")
        return result

    def list(self) -> list[T]:
        rows = self.connection.execute(f"SELECT * FROM {self.table} ORDER BY id").fetchall()
        return [self.model.from_dict(dict(row)) for row in rows]  # type: ignore[attr-defined]

    def delete(self, record_id: str) -> bool:
        cursor = self.connection.execute(f"DELETE FROM {self.table} WHERE id = ?", (record_id,))
        self.connection.commit()
        return cursor.rowcount > 0


class MeetingRepository(Repository[Meeting]):
    table, model = "meetings", Meeting

    def save(self, meeting: Meeting) -> None:
        _upsert(self.connection, self.table, meeting.to_dict(), "id")


class PersonRepository(Repository[Person]):
    table, model = "persons", Person

    def save(self, person: Person) -> None:
        _upsert(self.connection, self.table, person.to_dict(), "id")


class SpeechRepository(Repository[Speech]):
    table, model = "speeches", Speech

    def save(self, speech: Speech) -> None:
        _upsert(self.connection, self.table, speech.to_dict(), "id")

    def save_many(self, speeches: Iterable[Speech]) -> None:
        with self.connection:
            for speech in speeches:
                _upsert(self.connection, self.table, speech.to_dict(), "id", commit=False)

    def for_meeting(self, meeting_id: str) -> list[Speech]:
        rows = self.connection.execute(
            "SELECT * FROM speeches WHERE meeting_id = ? ORDER BY sequence", (meeting_id,)
        ).fetchall()
        return [Speech.from_dict(dict(row)) for row in rows]

    def context(self, speech_id: str, before: int = 1, after: int = 1) -> list[Speech]:
        speech = self.require(speech_id)
        rows = self.connection.execute(
            """SELECT * FROM speeches WHERE meeting_id = ?
               AND sequence BETWEEN ? AND ? ORDER BY sequence""",
            (speech.meeting_id, max(0, speech.sequence - before), speech.sequence + after),
        ).fetchall()
        return [Speech.from_dict(dict(row)) for row in rows]


class BillRepository(Repository[Bill]):
    table, model = "bills", Bill

    def save(self, bill: Bill) -> None:
        _upsert(self.connection, self.table, bill.to_dict(), "id")

    def save_many(self, bills: Iterable[Bill]) -> None:
        with self.connection:
            for bill in bills:
                _upsert(self.connection, self.table, bill.to_dict(), "id", commit=False)


class SpeechRelationRepository:
    def __init__(self, connection: sqlite3.Connection | Any) -> None:
        self.connection = getattr(connection, "connection", connection)

    def save(self, relation: SpeechRelation) -> None:
        _upsert(
            self.connection,
            "speech_relations",
            relation.to_dict(),
            "source_speech_id,target_speech_id,relation_type",
        )


class EmbeddingRepository:
    def __init__(self, connection: sqlite3.Connection | Any) -> None:
        self.connection = getattr(connection, "connection", connection)

    def save(self, embedding: EmbeddingRecord) -> None:
        _upsert(self.connection, "embeddings", embedding.to_dict(), "speech_id,model_name")


def _upsert(
    connection: sqlite3.Connection,
    table: str,
    data: dict[str, Any],
    conflict: str,
    *,
    commit: bool = True,
) -> None:
    columns = list(data)
    updates = [column for column in columns if column not in conflict.split(",")]
    sql = (
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
        f"ON CONFLICT ({conflict}) DO UPDATE SET "
        + ",".join(f"{column}=excluded.{column}" for column in updates)
    )
    connection.execute(sql, [data[column] for column in columns])
    if commit:
        connection.commit()
