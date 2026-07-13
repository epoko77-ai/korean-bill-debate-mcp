"""Connection lifecycle and schema management."""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from pathlib import Path


class Database:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        # Hosted MCP tools run in a serialized worker thread so blocking
        # network/PDF work cannot stall the ASGI event loop.  The connection is
        # shared with that worker; serialization is enforced by the MCP server.
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    @property
    def conn(self) -> sqlite3.Connection:
        return self.connection

    def initialize(self) -> None:
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        self.connection.executescript(schema)
        self.connection.commit()

    initialize_schema = initialize

    def close(self) -> None:
        self.connection.close()

    def __del__(self) -> None:
        # Long-lived MCP services own this connection for the process lifetime.
        # Close defensively at interpreter teardown to avoid leaking local CLI/test handles.
        with suppress(AttributeError, sqlite3.ProgrammingError):
            self.connection.close()

    def __enter__(self) -> Database:
        self.initialize()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
