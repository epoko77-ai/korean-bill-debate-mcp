"""Versioned legislative nickname hints that must be verified against official records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

REGISTRY_VERSION = "2026-08-23"


@dataclass(frozen=True, slots=True)
class MeasureIdentityHint:
    bill_no: str
    role: str
    name: str
    proposer: str
    proposed_at: str
    official_url: str

    def to_dict(self) -> dict[str, str]:
        return {
            "bill_no": self.bill_no,
            "role": self.role,
            "name": self.name,
            "proposer": self.proposer,
            "proposed_at": self.proposed_at,
            "official_url": self.official_url,
        }


@dataclass(frozen=True, slots=True)
class MeasureAliasHint:
    key: str
    matched_alias: str
    assembly_term: int
    committee: str
    identities: tuple[MeasureIdentityHint, ...]
    primary_vehicle_bill_no: str
    evidence_terms: tuple[str, ...]
    milestone_months: tuple[str, ...]

    @property
    def bill_numbers(self) -> tuple[str, ...]:
        return tuple(identity.bill_no for identity in self.identities)

    @property
    def evidence_query(self) -> str:
        return " ".join((*self.evidence_terms, *self.bill_numbers))

    def identity(self, bill_no: str) -> MeasureIdentityHint | None:
        return next(
            (identity for identity in self.identities if identity.bill_no == bill_no),
            None,
        )

    def public_payload(self) -> dict[str, Any]:
        return {
            "registry_version": REGISTRY_VERSION,
            "alias_key": self.key,
            "matched_alias": self.matched_alias,
            "assembly_term": self.assembly_term,
            "committee": self.committee,
            "measure_family": [identity.to_dict() for identity in self.identities],
            "primary_vehicle_bill_no": self.primary_vehicle_bill_no,
            "retrieval_terms": list(self.evidence_terms),
            "milestone_months": list(self.milestone_months),
            "confidence": "retrieval_hint_pending_live_verification",
            "not_evidence": True,
            "instruction": (
                "이 별칭 레지스트리는 후보를 좁히는 검색 힌트입니다. 공식 의안번호·안건·"
                "처리상태가 일치한 자료만 답변 근거로 사용하세요."
            ),
        }


_DOCTOR_NOW_IDENTITIES = (
    MeasureIdentityHint(
        bill_no="2205513",
        role="source_member_bill",
        name="약사법 일부개정법률안",
        proposer="김윤의원 등 11인",
        proposed_at="2024-11-13",
        official_url=(
            "https://opinion.lawmaking.go.kr/gcom/nsmLmSts/out/2205513/detailRP"
        ),
    ),
    MeasureIdentityHint(
        bill_no="2214609",
        role="committee_alternative_primary_vehicle",
        name="약사법 일부개정법률안(대안)",
        proposer="보건복지위원장",
        proposed_at="2025-11-26",
        official_url=(
            "https://opinion.lawmaking.go.kr/gcom/nsmLmSts/out/2214609/detailRP"
        ),
    ),
)


def resolve_measure_alias(query: str) -> MeasureAliasHint | None:
    """Resolve a narrow nickname to retrieval anchors, never to proof by itself."""

    normalized = " ".join(query.casefold().split())
    named = next(
        (
            alias
            for alias in ("닥터나우 방지법", "닥터나우 금지법")
            if alias in normalized
        ),
        None,
    )
    contextual = (
        "닥터나우" in normalized
        and any(term in normalized for term in ("비대면진료", "비대면 진료", "플랫폼"))
        and any(term in normalized for term in ("의약품 도매", "도매상", "리베이트"))
    )
    if named is None and not contextual:
        return None
    return MeasureAliasHint(
        key="doctor_now_pharmaceutical_wholesale_restriction",
        matched_alias=named or "닥터나우 플랫폼 의약품 도매 규제",
        assembly_term=22,
        committee="보건복지위원회",
        identities=_DOCTOR_NOW_IDENTITIES,
        primary_vehicle_bill_no="2214609",
        evidence_terms=(
            "약사법",
            "비대면진료 중개업자",
            "의약품 도매상",
            "리베이트",
            "닥터나우",
        ),
        milestone_months=("2025-11", "2026-08"),
    )


__all__ = [
    "REGISTRY_VERSION",
    "MeasureAliasHint",
    "MeasureIdentityHint",
    "resolve_measure_alias",
]
