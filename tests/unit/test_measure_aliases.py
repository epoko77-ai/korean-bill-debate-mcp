from kasm.search.measure_aliases import REGISTRY_VERSION, resolve_measure_alias


def test_doctor_now_nickname_resolves_to_source_and_primary_vehicle_hints() -> None:
    hint = resolve_measure_alias("최근 통과한 닥터나우 금지법의 주요 논의를 정리해줘")

    assert hint is not None
    assert hint.bill_numbers == ("2205513", "2214609")
    assert hint.primary_vehicle_bill_no == "2214609"
    assert hint.committee == "보건복지위원회"
    assert hint.milestone_months == ("2025-11", "2026-08")
    assert "의약품 도매상" in hint.evidence_query
    public = hint.public_payload()
    assert public["registry_version"] == REGISTRY_VERSION
    assert public["not_evidence"] is True
    assert public["confidence"] == "retrieval_hint_pending_live_verification"


def test_contextual_variant_resolves_but_bare_company_name_does_not() -> None:
    assert resolve_measure_alias(
        "닥터나우 비대면진료 플랫폼의 의약품 도매상 운영 규제"
    ) is not None
    assert resolve_measure_alias("닥터나우 최근 소식") is None
    assert resolve_measure_alias("일반 약사법 개정안") is None
