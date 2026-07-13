"""SQLite FTS5 lexical candidate retrieval with shared structured filters."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .filters import SearchFilters

WORDS = re.compile(r"[\w가-힣]+", re.UNICODE)
_PARTICLES = (
    "으로",
    "에서",
    "에게",
    "까지",
    "부터",
    "처럼",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "에",
    "의",
    "와",
    "과",
    "로",
)


def query_terms(query: str) -> list[str]:
    """Tokenize a Korean query and add conservative particle-stripped forms."""
    terms: list[str] = []
    for raw in WORDS.findall(query):
        terms.append(raw)
        for particle in _PARTICLES:
            if len(raw) >= len(particle) + 2 and raw.endswith(particle):
                stripped = raw[: -len(particle)]
                if stripped not in terms:
                    terms.append(stripped)
                break
    return terms


class LexicalSearch:
    def __init__(self, connection: sqlite3.Connection | Any) -> None:
        self.connection = getattr(connection, "connection", connection)

    def search(
        self,
        query: str,
        filters: SearchFilters | None = None,
        *,
        candidate_limit: int = 50,
    ) -> list[dict[str, Any]]:
        terms = query_terms(query)
        if not terms:
            return []
        if candidate_limit < 1:
            raise ValueError("candidate_limit must be positive")
        filters = filters or SearchFilters()
        clauses = ["speeches_fts MATCH ?"]
        parameters: list[Any] = [" OR ".join(f'"{term}"' for term in terms)]
        mapping = {
            "assembly_term": "m.assembly_term = ?",
            "speaker_role": "s.speaker_role = ?",
            "organization": "s.organization = ?",
            "meeting_type": "m.meeting_type = ?",
            "date_from": "m.date >= ?",
            "date_to": "m.date <= ?",
        }
        values = filters.as_dict()
        for key, clause in mapping.items():
            if key in values:
                clauses.append(clause)
                parameters.append(values[key])
        if filters.committee is not None:
            clauses.append("(m.committee_id = ? OR m.committee_name_ko LIKE ?)")
            parameters.extend([filters.committee, f"%{filters.committee}%"])
        if filters.speaker is not None:
            clauses.append("s.speaker_name LIKE ?")
            parameters.append(f"%{filters.speaker}%")
        parameters.append(candidate_limit)
        rows = self.connection.execute(
            f"""SELECT s.*, m.title AS meeting, m.committee_name_ko AS committee,
                       m.date, m.source_url AS official_source,
                       -bm25(speeches_fts) AS lexical_score
                FROM speeches_fts
                JOIN speeches s ON s.rowid = speeches_fts.rowid
                JOIN meetings m ON m.id = s.meeting_id
                WHERE {" AND ".join(clauses)}
                ORDER BY bm25(speeches_fts), s.id LIMIT ?""",
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]
