"""Default local application wiring and dependency-free sample dataset."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from kasm.core.models import Bill, Meeting, Speech
from kasm.core.quality import issue_quality
from kasm.core.relations import infer_question_answer_relations
from kasm.indexing.embeddings import HashEmbeddingProvider, SentenceTransformersProvider
from kasm.indexing.vector import ExactVectorIndex, FaissVectorIndex
from kasm.mcp.tools import ServiceContext
from kasm.research.relevance import RelevanceCriteria, RelevanceResult, evaluate_candidate
from kasm.search.filters import SearchFilters
from kasm.search.hybrid import HybridSearch
from kasm.search.lexical import LexicalSearch, query_terms
from kasm.search.semantic import SemanticSearch
from kasm.storage.database import Database
from kasm.storage.repositories import (
    BillDocumentRepository,
    BillRepository,
    MeetingRepository,
    SpeechRelationRepository,
    SpeechRepository,
)

_COMMITTEE_TOPIC_HINTS = (
    (
        "법제사법위원회",
        (
            "검찰",
            "수사권",
            "보완수사",
            "중수청",
            "공소청",
            "형사소송법",
            "법왜곡죄",
            "대법원장",
            "압수수색",
            "국선변호",
            "공소심의",
            "구속기간",
            "조건부 석방",
            "임금체불",
            "이상동기범죄",
            "불법 도박",
            "방화범",
            "명예훼손죄",
            "간이공판",
            "수용자",
            "주택임대차보호법",
        ),
    ),
    (
        "문화체육관광위원회",
        ("문체위", "대한축구협회", "축구협회", "문화체육", "문화예술", "K-컬처", "관광"),
    ),
    (
        "과학기술정보방송통신위원회",
        (
            "과방위",
            "인공지능 산업",
            "AI 산업",
            "AI 생태계",
            "AI 대전환",
            "AI 기본법",
            "디지털 포용법",
            "통신 정책",
            "방송 정책",
            "방송 개혁",
            "미디어 환경",
        ),
    ),
    (
        "재정경제기획위원회",
        (
            "재정정책",
            "세제",
            "국세청",
            "세무조사",
            "관세",
            "통상 환경",
            "수출입은행",
            "정책금융",
            "국가데이터처",
            "물가",
            "민생경제",
            "예산 집행",
            "재정 건전성",
        ),
    ),
    (
        "기후에너지환경노동위원회",
        ("기후위기", "에너지전환", "산업 전환", "비정규직", "노동자", "고용노동"),
    ),
    (
        "농림축산식품해양수산위원회",
        ("농업", "농림", "축산", "수산", "한우", "식량"),
    ),
    (
        "정무위원회",
        ("정무위", "홈플러스", "MBK", "금융 공공성", "공정거래", "국민권익", "보훈"),
    ),
)

_COMMITTEE_NAMES = tuple(committee for committee, _hints in _COMMITTEE_TOPIC_HINTS)

_BILL_TITLE_HINTS = (
    (("보완수사", "수사권", "중수청", "공소청", "검사 직접수사"), "형사소송법"),
    (("주택임대차", "임대차보호"), "주택임대차보호법"),
)


def infer_issue_committee(query: str) -> str | None:
    """Infer a committee only for high-signal institutional topic phrases."""
    folded = query.casefold()
    for committee in _COMMITTEE_NAMES:
        if committee.casefold() in folded:
            return committee
    for committee, hints in _COMMITTEE_TOPIC_HINTS:
        if any(hint.casefold() in folded for hint in hints):
            return committee
    return None


def infer_bill_title_query(query: str) -> str | None:
    """Map policy language to a statute title only when the relationship is unambiguous."""
    folded = query.casefold()
    for hints, title in _BILL_TITLE_HINTS:
        if any(hint.casefold() in folded for hint in hints):
            return title
    return None


class LocalServices:
    def __init__(self, database: Database, hybrid: HybridSearch | None = None) -> None:
        self.database = database
        self.meetings = MeetingRepository(database)
        self.speeches = SpeechRepository(database)
        self.bills = BillRepository(database)
        self.bill_documents = BillDocumentRepository(database)
        self.lexical = LexicalSearch(database)
        self.hybrid = hybrid

    def search(self, query: str, **filters: Any) -> list[dict[str, Any]]:
        terms = query_terms(query)
        # Public tools validate their own small response limit.  Internal
        # inventory builders deliberately pass the complete cache row count so
        # the core selection can be accompanied by a lossless candidate map.
        limit = max(1, int(filters.get("limit", 10)))
        search_filters = SearchFilters(
            **{
                key: filters[key]
                for key in (
                    "assembly_term",
                    "committee",
                    "speaker",
                    "speaker_role",
                    "organization",
                    "meeting_type",
                    "date_from",
                    "date_to",
                )
                if filters.get(key) is not None
            }
        )
        rows = (
            self.hybrid.search(
                query,
                search_filters,
                limit=limit,
                candidate_limit=limit,
            )
            if self.hybrid is not None
            else self.lexical.search(query, search_filters, candidate_limit=limit)
        )
        include_context = filters.get("include_context", True)
        results: list[dict[str, Any]] = []
        for rank, row in enumerate(rows, 1):
            item = dict(row)
            item["speech_id"] = item.pop("id")
            item["speaker"] = item.pop("speaker_name")
            item.setdefault("lexical_rank", rank)
            item.setdefault("semantic_rank", None)
            item.setdefault("hybrid_score", 1.0 / (60 + rank))
            item["matched_terms"] = [
                term for term in terms if term.casefold() in row["text"].casefold()
            ]
            if include_context:
                context = self.speeches.context(item["speech_id"], before=1, after=1)
                before = [speech.text for speech in context if speech.sequence < row["sequence"]]
                after = [speech.text for speech in context if speech.sequence > row["sequence"]]
                item["context_before"] = before[-1] if before else None
                item["context_after"] = after[0] if after else None
            item["citation"] = {
                "official_url": item.get("official_source"),
                "source_locator": item.get("source_locator"),
                "meeting": item.get("meeting"),
                "date": item.get("date"),
                "speaker": item.get("speaker"),
            }
            results.append(item)
        return results

    def get(self, speech_id: str) -> dict[str, Any] | None:
        row = self.database.connection.execute(
            """SELECT s.*, m.title AS meeting, m.committee_name_ko AS committee,
                      m.date, m.source_url AS official_source,
                      m.source_hash AS meeting_source_hash, m.retrieved_at
               FROM speeches s JOIN meetings m ON m.id = s.meeting_id
               WHERE s.id = ?""",
            (speech_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def context(self, speech_id: str, before: int = 2, after: int = 2) -> dict[str, Any]:
        speeches = self.speeches.context(speech_id, before, after)
        relations = self.database.connection.execute(
            """SELECT * FROM speech_relations
               WHERE source_speech_id = ? OR target_speech_id = ?
               ORDER BY source_speech_id, target_speech_id, relation_type""",
            (speech_id, speech_id),
        ).fetchall()
        return {
            "speech_id": speech_id,
            "speeches": speeches,
            "relations": [dict(row) for row in relations],
        }

    def list_committees(
        self, assembly_term: int | None = None, query: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if assembly_term is not None:
            clauses.append("assembly_term = ?")
            parameters.append(assembly_term)
        if query:
            clauses.append("(committee_name_ko LIKE ? OR committee_name_en LIKE ?)")
            parameters.extend([f"%{query}%", f"%{query}%"])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.database.connection.execute(
            f"""SELECT committee_id, committee_name_ko, committee_name_en,
                       MIN(date) AS date_from, MAX(date) AS date_to
                FROM meetings {where}
                GROUP BY committee_id, committee_name_ko, committee_name_en
                ORDER BY committee_name_ko""",
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    def list_meetings(self, **filters: Any) -> list[dict[str, Any]]:
        clauses, parameters = [], []
        for key, column in (
            ("committee", "committee_name_ko"),
            ("date_from", "date"),
            ("date_to", "date"),
            ("meeting_type", "meeting_type"),
        ):
            if filters.get(key) is not None:
                operator = ">=" if key == "date_from" else "<=" if key == "date_to" else "="
                clauses.append(f"{column} {operator} ?")
                parameters.append(filters[key])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.database.connection.execute(
            f"SELECT * FROM meetings {where} ORDER BY date DESC, id", parameters
        ).fetchall()
        return [dict(row) for row in rows]

    def search_bills(self, query: str, **filters: Any) -> list[dict[str, Any]]:
        include_documents = bool(filters.get("include_documents", True))
        normalized_query = query.strip()
        if normalized_query.isdigit():
            row = self.database.connection.execute(
                "SELECT * FROM bills WHERE bill_no = ?", (normalized_query,)
            ).fetchone()
            return [
                self._bill_payload(
                    dict(row),
                    query=query,
                    include_documents=include_documents,
                )
            ] if row else []
        terms = query_terms(query)
        clauses = ["bills_fts MATCH ?"]
        parameters: list[Any] = [" OR ".join(f'"{term}"' for term in terms)]
        if filters.get("assembly_term") is not None:
            clauses.append("b.assembly_term = ?")
            parameters.append(filters["assembly_term"])
        if filters.get("committee"):
            clauses.append("b.committee LIKE ?")
            parameters.append(f"%{filters['committee']}%")
        status = filters.get("status")
        if status == "pending":
            clauses.append("COALESCE(TRIM(b.process_result), '') = ''")
        elif status == "processed":
            clauses.append("COALESCE(TRIM(b.process_result), '') <> ''")
        parameters.append(max(1, int(filters.get("limit", 10))))
        rows = self.database.connection.execute(
            f"""SELECT b.* FROM bills_fts JOIN bills b ON b.rowid = bills_fts.rowid
                WHERE {" AND ".join(clauses)} ORDER BY bm25(bills_fts), b.proposed_at DESC
                LIMIT ?""",
            parameters,
        ).fetchall()
        results = [dict(row) for row in rows]
        document_clauses = ["bill_documents_fts MATCH ?"]
        document_parameters: list[Any] = [" OR ".join(f'"{term}"' for term in terms)]
        if filters.get("assembly_term") is not None:
            document_clauses.append("b.assembly_term = ?")
            document_parameters.append(filters["assembly_term"])
        if filters.get("committee"):
            document_clauses.append("b.committee LIKE ?")
            document_parameters.append(f"%{filters['committee']}%")
        if status == "pending":
            document_clauses.append("COALESCE(TRIM(b.process_result), '') = ''")
        elif status == "processed":
            document_clauses.append("COALESCE(TRIM(b.process_result), '') <> ''")
        document_parameters.append(max(1, int(filters.get("limit", 10))))
        document_rows = self.database.connection.execute(
            f"""SELECT DISTINCT b.* FROM bill_documents_fts
                JOIN bill_documents d ON d.rowid = bill_documents_fts.rowid
                JOIN bills b ON b.id = d.bill_id
                WHERE {" AND ".join(document_clauses)}
                ORDER BY b.proposed_at DESC LIMIT ?""",
            document_parameters,
        ).fetchall()
        seen = {row["id"] for row in results}
        for row in document_rows:
            if row["id"] not in seen:
                results.append(dict(row))
                seen.add(row["id"])
        limit = max(1, int(filters.get("limit", 10)))
        return [
            self._bill_payload(
                row,
                query=query,
                include_documents=include_documents,
            )
            for row in results[:limit]
        ]

    def get_bill_status(self, bill_id_or_no: str) -> dict[str, Any] | None:
        row = self.database.connection.execute(
            "SELECT * FROM bills WHERE id = ? OR bill_no = ?", (bill_id_or_no, bill_id_or_no)
        ).fetchone()
        if not row:
            return None
        result = self._bill_payload(dict(row))
        related = self.database.connection.execute(
            """SELECT s.id AS speech_id, s.speaker_name AS speaker, s.text,
                      m.title AS meeting, m.committee_name_ko AS committee, m.meeting_type,
                      l.relation_type, l.confidence, l.evidence
               FROM speech_bill_links l JOIN speeches s ON s.id = l.speech_id
               JOIN meetings m ON m.id = s.meeting_id WHERE l.bill_id = ?
               ORDER BY l.confidence DESC, m.date DESC, s.id""",
            (result["id"],),
        ).fetchall()
        result["related_speeches"] = [dict(item) for item in related]
        result["related_speeches_count"] = len(related)
        result["related_speeches_complete"] = True
        return result

    def explore_issue(
        self,
        query: str,
        limit: int = 20,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        assembly_term: int | None = None,
    ) -> dict[str, Any]:
        limit = max(1, int(limit))
        inferred_committee = infer_issue_committee(query)
        speech_cache_total = self._table_count("speeches")
        all_speeches = self.search(
            query,
            limit=max(1, speech_cache_total),
            include_context=False,
            committee=inferred_committee,
            **({"date_from": date_from} if date_from else {}),
            **({"date_to": date_to} if date_to else {}),
            **({"assembly_term": assembly_term} if assembly_term is not None else {}),
        )
        ranked_speeches = list(all_speeches)
        if self.hybrid is None:
            stopwords = {
                "대한",
                "관련",
                "논의",
                "의견",
                "의원",
                "정부",
                "답변",
                "입장",
                "문제",
                "대책",
                "검토",
                "역할",
                "법안",
                "제도",
                "실제",
                "이후",
                "하는",
                "해야",
            }
            content_terms = {term for term in query_terms(query) if term not in stopwords}
            required = min(2, len(content_terms))
            focused = [
                item
                for item in ranked_speeches
                if len(content_terms.intersection(item["matched_terms"])) >= required
            ]
            if focused:
                ranked_speeches = focused
        speeches = [
            self._with_adjacent_context(item) for item in ranked_speeches[:limit]
        ]
        speech_inventory = [self._speech_inventory_item(item) for item in all_speeches]

        bill_cache_total = self._table_count("bills")
        all_bills = self.search_bills(
            query,
            limit=max(1, bill_cache_total),
            committee=inferred_committee,
            include_documents=False,
            **({"assembly_term": assembly_term} if assembly_term is not None else {}),
        )
        inferred_bill_query = infer_bill_title_query(query)
        if inferred_bill_query:
            inferred_bills = self.search_bills(
                inferred_bill_query,
                limit=max(1, bill_cache_total),
                committee=inferred_committee,
                include_documents=False,
                **({"assembly_term": assembly_term} if assembly_term is not None else {}),
            )
            seen = {str(bill["id"]) for bill in all_bills}
            all_bills.extend(
                bill for bill in inferred_bills if str(bill["id"]) not in seen
            )

        linked_bill_rows = self._linked_bills_for_speeches(
            [item["speech_id"] for item in all_speeches]
        )
        seen_bill_ids = {str(bill["id"]) for bill in all_bills}
        for row in linked_bill_rows:
            if str(row["id"]) in seen_bill_ids:
                continue
            all_bills.append(
                self._bill_payload(dict(row), include_documents=False)
            )
            seen_bill_ids.add(str(row["id"]))
        links = self._links_for_bills([str(bill["id"]) for bill in all_bills])
        bills = self._select_relevant_bills(
            query,
            all_bills,
            all_speeches,
            links,
            limit=limit,
        )
        eligible_bill_count = sum(
            bool(
                (bill.get("selection_relevance") or {}).get(
                    "eligible_for_synthesis"
                )
            )
            for bill in all_bills
        )
        threads = self._discussion_threads(speeches)
        payload = {
            "query": query,
            "inferred_committee": inferred_committee,
            "inferred_bill_query": inferred_bill_query,
            "bills": bills,
            "speeches": speeches,
            "discussion_threads": threads,
            "timeline": self._issue_timeline(bills, threads),
            "links": links,
            "scope_inventory": {
                "cache_scope": {
                    "complete": True,
                    "official_source_complete": False,
                    "note": (
                        "현재 로컬 캐시에 저장된 자료에 대한 전건 지도입니다. "
                        "열린국회 전체 범위의 완전성을 뜻하지 않습니다."
                    ),
                },
                "bill_candidates": {
                    "complete": True,
                    "total": len(all_bills),
                    "items": [self._bill_inventory_item(item) for item in all_bills],
                },
                "speech_candidates": {
                    "complete": True,
                    "total": len(speech_inventory),
                    "items": speech_inventory,
                },
                "links": {
                    "complete": True,
                    "total": len(links),
                    "items": links,
                },
                "selected_for_synthesis": {
                    "selection_limit": limit,
                    "bill_count": len(bills),
                    "eligible_bill_count": eligible_bill_count,
                    "speech_count": len(speeches),
                    "discussion_thread_count": len(threads),
                    "bill_selection_complete": len(bills) == eligible_bill_count,
                    "speech_selection_complete": len(speeches) == len(speech_inventory),
                    "note": (
                        "bills·speeches·discussion_threads는 빠른 핵심 확인용입니다. "
                        "전체 후보와 관계는 bill_candidates·speech_candidates·links에 "
                        "있으며, 선택한 원문은 get_bill_status·get_speech로 여세요."
                    ),
                },
            },
            "graph": {
                "node_types": [
                    "bill",
                    "bill_document",
                    "committee",
                    "meeting",
                    "person",
                    "speech",
                ],
                "edge_types": [
                    "HAS_REVIEW_REPORT",
                    "REFERRED_TO",
                    "HELD",
                    "SPOKE_IN",
                    "MENTIONS",
                    "REPLIES_TO",
                ],
            },
        }
        payload["quality"] = issue_quality(payload)
        return payload

    def _table_count(self, table: str) -> int:
        if table not in {"bills", "speeches"}:
            raise ValueError("unsupported inventory table")
        return int(
            self.database.connection.execute(
                f"SELECT count(*) FROM {table}"
            ).fetchone()[0]
        )

    def _linked_bills_for_speeches(
        self, speech_ids: list[str]
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for chunk in _chunks(speech_ids):
            placeholders = ",".join("?" for _ in chunk)
            fetched = self.database.connection.execute(
                f"""SELECT b.*, l.relation_type AS linked_by,
                           l.confidence AS link_confidence, l.evidence AS link_evidence
                    FROM speech_bill_links l JOIN bills b ON b.id = l.bill_id
                    WHERE l.speech_id IN ({placeholders})
                    ORDER BY l.confidence DESC, b.proposed_at DESC, b.id""",
                chunk,
            ).fetchall()
            rows.extend(dict(row) for row in fetched)
        unique: dict[str, dict[str, Any]] = {}
        for row in sorted(
            rows,
            key=lambda item: (
                -float(item.get("link_confidence") or 0.0),
                str(item.get("proposed_at") or ""),
                str(item["id"]),
            ),
        ):
            unique.setdefault(str(row["id"]), row)
        return list(unique.values())

    def _links_for_bills(self, bill_ids: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for chunk in _chunks(bill_ids):
            placeholders = ",".join("?" for _ in chunk)
            fetched = self.database.connection.execute(
                f"""SELECT * FROM speech_bill_links WHERE bill_id IN ({placeholders})
                    ORDER BY confidence DESC, bill_id, speech_id, relation_type""",
                chunk,
            ).fetchall()
            rows.extend(dict(row) for row in fetched)
        return sorted(
            rows,
            key=lambda item: (
                -float(item.get("confidence") or 0.0),
                str(item.get("bill_id") or ""),
                str(item.get("speech_id") or ""),
                str(item.get("relation_type") or ""),
            ),
        )

    def _select_relevant_bills(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        speeches: list[dict[str, Any]],
        links: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Choose the strict core while retaining every candidate in the map."""

        criteria = RelevanceCriteria.from_query(query)
        speech_text = {
            str(speech.get("speech_id") or ""): str(speech.get("text") or "")
            for speech in speeches
        }
        links_by_bill: dict[str, list[dict[str, Any]]] = {}
        for link in links:
            links_by_bill.setdefault(str(link.get("bill_id") or ""), []).append(link)
        document_texts = self._bill_document_texts(
            [str(candidate.get("id") or "") for candidate in candidates]
        )

        evaluated: list[tuple[dict[str, Any], RelevanceResult]] = []
        for candidate in candidates:
            bill_id = str(candidate.get("id") or "")
            supporting_text = [document_texts.get(bill_id, "")]
            for link in links_by_bill.get(bill_id, []):
                supporting_text.extend(
                    (
                        str(link.get("evidence") or ""),
                        speech_text.get(str(link.get("speech_id") or ""), ""),
                    )
                )
            scored_candidate = {
                **candidate,
                "description": "\n".join(
                    text for text in supporting_text if text.strip()
                ),
            }
            result = evaluate_candidate(scored_candidate, criteria)
            evaluated.append((candidate, result))

        accepted = [(candidate, result) for candidate, result in evaluated if result.relevant]
        canonical_terms = {
            expansion.term
            for expansion in criteria.terminology_expansions
            if not expansion.reason.startswith("related_concept:")
        }
        if accepted and canonical_terms:
            best_tier = min(
                _bill_relevance_tier(result, canonical_terms, criteria)
                for _candidate, result in accepted
            )
            accepted = [
                (candidate, result)
                for candidate, result in accepted
                if _bill_relevance_tier(result, canonical_terms, criteria) == best_tier
            ]

        accepted.sort(
            key=lambda item: (
                _bill_relevance_tier(item[1], canonical_terms, criteria),
                -item[1].score,
                -_bill_proposed_ordinal(item[0].get("proposed_at")),
                str(item[0].get("bill_no") or ""),
                str(item[0].get("id") or ""),
            )
        )
        selected = accepted[:limit]
        eligible_ids = {
            str(candidate.get("id") or "") for candidate, _result in accepted
        }
        selected_ids = {
            str(candidate.get("id") or "") for candidate, _result in selected
        }
        for candidate, result in evaluated:
            candidate["selection_relevance"] = {
                "accepted": result.relevant,
                "eligible_for_synthesis": (
                    str(candidate.get("id") or "") in eligible_ids
                ),
                "selected_for_synthesis": (
                    str(candidate.get("id") or "") in selected_ids
                ),
                "score": result.score,
                "match_reasons": list(result.match_reasons),
                "rejection_reasons": list(result.rejection_reasons),
                "selection_tier": (
                    _bill_relevance_tier(result, canonical_terms, criteria)
                    if result.relevant and canonical_terms
                    else None
                ),
            }
        return [candidate for candidate, _result in selected]

    def _bill_document_texts(self, bill_ids: list[str]) -> dict[str, str]:
        grouped: dict[str, list[str]] = {}
        for chunk in _chunks([bill_id for bill_id in bill_ids if bill_id]):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.database.connection.execute(
                f"""SELECT bill_id, text FROM bill_documents
                    WHERE bill_id IN ({placeholders}) ORDER BY bill_id, id""",
                chunk,
            ).fetchall()
            for row in rows:
                grouped.setdefault(str(row["bill_id"]), []).append(str(row["text"]))
        return {bill_id: "\n".join(texts) for bill_id, texts in grouped.items()}

    def _with_adjacent_context(self, item: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(item)
        context = self.speeches.context(str(item["speech_id"]), before=1, after=1)
        sequence = int(item["sequence"])
        before = [speech.text for speech in context if speech.sequence < sequence]
        after = [speech.text for speech in context if speech.sequence > sequence]
        enriched["context_before"] = before[-1] if before else None
        enriched["context_after"] = after[0] if after else None
        return enriched

    @staticmethod
    def _speech_inventory_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "speech_id": item.get("speech_id"),
            "speaker": item.get("speaker"),
            "meeting_id": item.get("meeting_id"),
            "meeting": item.get("meeting"),
            "committee": item.get("committee"),
            "date": item.get("date"),
            "source_locator": item.get("source_locator"),
            "official_url": item.get("official_source"),
            "text_length": len(str(item.get("text") or "")),
            "matched_terms": item.get("matched_terms") or [],
        }

    @staticmethod
    def _bill_inventory_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "bill_id": item.get("id"),
            "bill_no": item.get("bill_no"),
            "name": item.get("name"),
            "committee": item.get("committee"),
            "proposed_at": item.get("proposed_at"),
            "processed_at": item.get("processed_at"),
            "process_result": item.get("process_result"),
            "status": item.get("status"),
            "official_url": item.get("official_url"),
            "document_count": item.get("document_count", 0),
            "linked_by": item.get("linked_by"),
            "link_confidence": item.get("link_confidence"),
            "link_evidence": item.get("link_evidence"),
            "selection_relevance": item.get("selection_relevance"),
        }

    @staticmethod
    def _issue_timeline(
        bills: list[dict[str, Any]], threads: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Merge proposal, processing, and debate events into one dated evidence trail."""
        events: list[dict[str, Any]] = []
        for bill in bills:
            if bill.get("proposed_at"):
                events.append(
                    {
                        "date": bill["proposed_at"],
                        "event_type": "bill_proposed",
                        "bill_no": bill["bill_no"],
                        "title": bill["name"],
                        "detail": f"{bill.get('proposer') or '제안자 미상'} 발의",
                        "official_url": bill["official_url"],
                    }
                )
            if bill.get("processed_at"):
                events.append(
                    {
                        "date": bill["processed_at"],
                        "event_type": "bill_processed",
                        "bill_no": bill["bill_no"],
                        "title": bill["name"],
                        "detail": bill.get("process_result") or "처리",
                        "official_url": bill["official_url"],
                    }
                )
        for thread in threads:
            events.append(
                {
                    "date": thread["date"],
                    "event_type": "debate",
                    "meeting_id": thread["meeting_id"],
                    "title": thread["meeting"],
                    "detail": f"{thread['committee']} · {len(thread['turns'])}개 발언",
                    "participants": thread["participants"],
                    "official_url": thread["turns"][0]["official_source"],
                }
            )
        return sorted(events, key=lambda event: (event["date"], event["event_type"]))

    def _discussion_threads(self, matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Expand nearby matches into ordered, source-verifiable conversational threads."""
        groups: list[list[dict[str, Any]]] = []
        for match in sorted(matches, key=lambda item: (item["meeting_id"], item["sequence"])):
            if (
                not groups
                or groups[-1][-1]["meeting_id"] != match["meeting_id"]
                or match["sequence"] - groups[-1][-1]["sequence"] > 8
            ):
                groups.append([match])
            else:
                groups[-1].append(match)
        threads: list[dict[str, Any]] = []
        for group in groups:
            first, last = group[0], group[-1]
            rows = self.database.connection.execute(
                """SELECT s.id AS speech_id, s.sequence, s.speaker_name AS speaker,
                          s.speaker_role, s.organization, s.text, s.agenda, s.source_locator,
                          m.title AS meeting, m.committee_name_ko AS committee, m.date,
                          m.meeting_type, m.source_url AS official_source
                   FROM speeches s JOIN meetings m ON m.id = s.meeting_id
                   WHERE s.meeting_id = ? AND s.sequence BETWEEN ? AND ?
                   ORDER BY s.sequence""",
                (first["meeting_id"], max(0, first["sequence"] - 2), last["sequence"] + 2),
            ).fetchall()
            turns = [dict(row) for row in rows]
            for turn in turns:
                turn["citation"] = {
                    "official_url": turn["official_source"],
                    "source_locator": turn["source_locator"],
                    "meeting": turn["meeting"],
                    "date": turn["date"],
                    "speaker": turn["speaker"],
                }
            threads.append(
                {
                    "meeting_id": first["meeting_id"],
                    "meeting": first["meeting"],
                    "committee": first["committee"],
                    "date": first["date"],
                    "matched_speech_ids": [item["speech_id"] for item in group],
                    "participants": list(dict.fromkeys(turn["speaker"] for turn in turns)),
                    "turns": turns,
                }
            )
        return threads

    def _bill_payload(
        self,
        row: dict[str, Any],
        *,
        query: str | None = None,
        include_documents: bool = True,
    ) -> dict[str, Any]:
        row["status"] = row.get("process_result") or "계류"
        row["is_pending"] = not bool(row.get("process_result"))
        documents = self.database.connection.execute(
            """SELECT id, document_type, title, file_format, official_url, text,
                      source_hash, retrieved_at
               FROM bill_documents WHERE bill_id = ? ORDER BY title, official_url""",
            (row["id"],),
        ).fetchall()
        row["document_count"] = len(documents)
        row["documents_included"] = include_documents
        row["documents_complete"] = include_documents
        row["documents"] = [
            {
                "document_id": document["id"],
                "document_type": document["document_type"],
                "title": document["title"],
                "file_format": document["file_format"],
                "official_url": document["official_url"],
                "text": document["text"],
                "text_length": len(document["text"]),
                "text_sha256": hashlib.sha256(
                    document["text"].encode("utf-8")
                ).hexdigest(),
                "text_inline_complete": True,
                "source_hash": document["source_hash"],
                "retrieved_at": document["retrieved_at"],
                "citation": {
                    "official_url": document["official_url"],
                    "source_locator": "전문위원 검토보고서 PDF",
                },
            }
            for document in documents
        ] if include_documents else []
        return row


def _bill_relevance_tier(
    result: RelevanceResult,
    canonical_terms: set[str],
    criteria: RelevanceCriteria,
) -> int:
    """Prefer explicit bill identity before supporting-body keyword spillover."""

    reasons = set(result.match_reasons)
    related_terms = {
        *criteria.related_statute_terms,
        *criteria.related_issue_terms,
    }

    def matches(terms: set[str], sources: tuple[str, ...]) -> bool:
        return any(
            reason.endswith(f"@{source}")
            and any(
                reason.startswith(f"{kind}:{term}@")
                for kind in ("statute", "issue", "related_statute", "related_issue")
            )
            for reason in reasons
            for source in sources
            for term in terms
        )

    if matches(canonical_terms, ("title", "agenda")):
        return 0
    if matches(related_terms, ("title", "agenda")):
        return 1
    if matches(canonical_terms, ("body",)):
        return 2
    if matches(related_terms, ("body",)):
        return 3
    if any(reason.endswith(("@title", "@agenda")) for reason in reasons):
        return 4
    if any(reason.endswith("@body") for reason in reasons):
        return 5
    return 6


def _bill_proposed_ordinal(value: Any) -> int:
    if isinstance(value, datetime):
        return value.date().toordinal()
    if isinstance(value, date):
        return value.toordinal()
    try:
        return date.fromisoformat(str(value or "")[:10]).toordinal()
    except ValueError:
        return 0


def _chunks(values: list[str], size: int = 400) -> list[list[str]]:
    return [values[start : start + size] for start in range(0, len(values), size)]


def create_services() -> ServiceContext:
    """Create an isolated, immediately searchable sample application."""
    database = Database()
    database.initialize()
    local = LocalServices(database)
    _load_sample(local)
    return ServiceContext(search=local, repository=local, catalog=local)


def create_deployed_services(
    database_path: str | None = None, vector_path: str | None = None
) -> ServiceContext:
    """Open an explicit existing cache/index for offline or private deployments."""
    database_path = database_path or os.getenv("KASM_DATABASE")
    if not database_path:
        raise RuntimeError("KASM_DATABASE is required for deployed services")
    if not Path(database_path).is_file():
        raise RuntimeError(f"prepared database does not exist: {database_path}")
    database = Database(database_path)
    database.initialize()
    counts = {
        table: database.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("meetings", "speeches", "bills")
    }
    empty = [table for table, count in counts.items() if count == 0]
    if empty and os.getenv("KASM_ALLOW_EMPTY_DATABASE") != "1":
        database.close()
        raise RuntimeError("prepared database is incomplete; empty tables: " + ", ".join(empty))
    local = LocalServices(database)
    vector_path = vector_path or os.getenv("KASM_VECTOR_INDEX")
    if vector_path:
        path = Path(vector_path)
        if not path.is_file():
            raise RuntimeError(f"prepared vector index does not exist: {path}")
        index = (
            FaissVectorIndex.load(path) if path.suffix == ".faiss" else ExactVectorIndex.load(path)
        )
        provider = (
            HashEmbeddingProvider(index.metadata.dimensions)
            if index.metadata.model_name == HashEmbeddingProvider.model_name
            else SentenceTransformersProvider(index.metadata.model_name)
        )

        def hydrate(speech_id: str) -> dict[str, Any] | None:
            row = database.connection.execute(
                """SELECT s.*, m.title AS meeting, m.committee_name_ko AS committee,
                          m.assembly_term, m.meeting_type, m.date,
                          m.source_url AS official_source
                   FROM speeches s JOIN meetings m ON m.id = s.meeting_id
                   WHERE s.id = ?""",
                (speech_id,),
            ).fetchone()
            return dict(row) if row is not None else None

        local.hybrid = HybridSearch(local.lexical, SemanticSearch(provider, index, hydrate))
    return ServiceContext(search=local, repository=local, catalog=local)


def create_auto_services() -> ServiceContext:
    """Use the caller's Open Assembly key and a private local cache by default."""
    database = os.getenv("KBD_DATABASE") or os.getenv("KASM_DATABASE")
    vector = os.getenv("KBD_VECTOR_INDEX") or os.getenv("KASM_VECTOR_INDEX")
    if database:
        return create_deployed_services(database, vector)
    if os.getenv("KBD_OFFLINE_DEMO") == "1":
        return create_services()
    from kasm.live import create_live_services

    return create_live_services()


def _load_sample(local: LocalServices) -> None:
    retrieved = datetime(2026, 7, 11, tzinfo=UTC)
    meeting = Meeting(
        id="kna:22:committee:2025-03-18:sample-001",
        assembly_term=22,
        committee_id="science-ict",
        committee_name_ko="과학기술정보방송통신위원회",
        committee_name_en="Science, ICT, Broadcasting and Communications Committee",
        title="KASM 합성 데모 회의록",
        meeting_type="committee",
        meeting_number="sample-001",
        date=date(2025, 3, 18),
        source_url="https://example.invalid/kasm/synthetic-sample",
        source_hash="synthetic-demo-v1",
        retrieved_at=retrieved,
    )
    local.meetings.save(meeting)
    texts = [
        (
            "김미래",
            "국회의원",
            "해외 기반 인공지능 모델에만 의존하면 국가의 AI 협상력이 약해지지 않겠습니까?",
        ),
        (
            "박정책",
            "장관",
            "국산 인공지능(AI) 모델 역량을 확보해 전략적 자율성과 선택권을 높이겠습니다.",
        ),
        (
            "김미래",
            "국회의원",
            "공공 데이터와 연산 자원을 국내 인공지능 생태계에 연결하는 방안도 필요합니다.",
        ),
    ]
    speeches = []
    for sequence, (speaker, role, text) in enumerate(texts, 1):
        speech_id = f"{meeting.id}:speech-{sequence:04d}"
        speeches.append(
            Speech(
                id=speech_id,
                meeting_id=meeting.id,
                sequence=sequence,
                speaker_id=None,
                speaker_name=speaker,
                speaker_role=role,
                organization=None,
                text=text,
                agenda="소버린 AI / sovereign AI / domestic foundation models",
                previous_speech_id=f"{meeting.id}:speech-{sequence - 1:04d}"
                if sequence > 1
                else None,
                next_speech_id=f"{meeting.id}:speech-{sequence + 1:04d}"
                if sequence < len(texts)
                else None,
                source_locator=f"synthetic:line-{sequence}",
                source_hash="synthetic-demo-v1",
                parser_version="demo-1",
            )
        )
    local.speeches.save_many(speeches)
    relation_repository = SpeechRelationRepository(local.database)
    for relation in infer_question_answer_relations(speeches):
        relation_repository.save(relation)
    local.bills.save(
        Bill(
            id="synthetic-bill-ai-001",
            bill_no="2200001",
            name="인공지능 생태계 경쟁력 강화에 관한 법률안",
            assembly_term=22,
            proposer="김미래의원 등 10인",
            committee="과학기술정보방송통신위원회",
            proposed_at=date(2025, 3, 4),
            process_result=None,
            processed_at=None,
            official_url="https://example.invalid/kasm/synthetic-bill",
            source_hash="synthetic-demo-v1",
            retrieved_at=retrieved,
        )
    )
    local.database.connection.execute(
        """INSERT INTO speech_bill_links
           (speech_id, bill_id, relation_type, confidence, evidence)
           VALUES (?, ?, 'AGENDA_MATCH', 0.95, ?)""",
        (speeches[0].id, "synthetic-bill-ai-001", "인공지능 생태계 정책 의제"),
    )
    local.database.connection.commit()
