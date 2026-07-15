import pytest

from kasm.search.bilingual import english_search_query, korean_committee, prepare_query


def test_translates_high_signal_english_legislative_concepts() -> None:
    query = (
        "In July 2026, compare arguments for and against abolishing prosecutors' "
        "supplementary investigation authority in bills and subcommittee minutes."
    )

    prepared = prepare_query(query)

    assert prepared.language == "en"
    assert prepared.translation_mode == "built_in_glossary"
    assert prepared.search_query == (
        "2026년 7월 보완수사권 검찰 폐지 소위원회 회의록 찬반 의견 법안"
    )


def test_preserves_a_historical_nineteen_nineties_year_and_month() -> None:
    prepared = prepare_query("In March 1999, show climate crisis bills")

    assert prepared.search_query == "1999년 3월 기후위기 법안"


def test_explicit_korean_query_handles_unmapped_english_proper_nouns() -> None:
    prepared = prepare_query(
        "What did lawmakers say about the Acme framework?",
        "에크미 프레임워크 의원 발언",
    )

    assert prepared.search_query == "에크미 프레임워크 의원 발언"
    assert prepared.translation_mode == "client_supplied"
    assert prepared.metadata()["source_language"] == "ko"


def test_unmapped_english_query_is_not_silently_mistranslated() -> None:
    assert english_search_query("What happened to Project Aster?") == ""
    prepared = prepare_query("What happened to Project Aster?")
    assert prepared.search_query == "What happened to Project Aster?"
    assert prepared.translation_mode == "untranslated"


def test_mixed_query_preserves_korean_scope_and_expands_ai() -> None:
    query = "2026년 1월 1일부터 현재까지 최근 AI 입법"

    prepared = prepare_query(query)

    assert prepared.original == query
    assert prepared.language == "ko"
    assert prepared.translation_mode == "built_in_glossary"
    assert prepared.search_query == f"{query} 인공지능"
    assert prepared.expansion_reasons == ("equivalent_alias:AI→인공지능",)
    assert prepared.metadata()["terminology_version"]


def test_mixed_query_deduplicates_existing_korean_concept() -> None:
    query = "최근 인공지능(AI) 입법"

    prepared = prepare_query(query)

    assert prepared.search_query == query
    assert prepared.translation_mode == "none"
    assert prepared.search_query.count("인공지능") == 1


def test_mixed_query_preserves_an_existing_bilingual_term() -> None:
    prepared = prepare_query("소버린 AI")

    assert prepared.search_query == "소버린 AI"
    assert prepared.translation_mode == "none"


def test_mixed_query_expansion_is_stable_and_deduplicated() -> None:
    query = "최근 AI AI bills와 subcommittee minutes 확인"

    first = prepare_query(query)
    second = prepare_query(query)

    assert first == second
    assert first.search_query == f"{query} 인공지능 소위원회 회의록 법안"
    assert first.search_query.count("인공지능") == 1
    assert first.search_query.count("소위원회 회의록") == 1


def test_rejects_oversized_original_query() -> None:
    with pytest.raises(ValueError, match="query"):
        prepare_query("가" * 501 + " AI")


def test_translates_official_english_committee_names() -> None:
    assert (
        korean_committee("Legislation and Judiciary Committee") == "법제사법위원회"
    )
    assert korean_committee("National Policy Committee") == "정무위원회"
    assert korean_committee("과학기술정보방송통신위원회") == "과학기술정보방송통신위원회"
    assert korean_committee("법사위") == "법사위"


def test_ai_basic_act_uses_the_reviewed_specific_concept() -> None:
    prepared = prepare_query("Show recent AI Basic Act bills")

    assert prepared.search_query == "인공지능 기본법 법안"
    assert prepared.expansion_reasons[0] == (
        "equivalent_alias:AI Basic Act→인공지능 기본법"
    )


def test_preserves_exact_bill_number_from_english_request() -> None:
    prepared = prepare_query(
        "For bill 2217784, show its current status and expert review report."
    )

    assert prepared.search_query == "2217784"
    assert prepared.language == "en"


def test_rejects_oversized_client_translation() -> None:
    with pytest.raises(ValueError, match="korean_query"):
        prepare_query("English request", "가" * 501)
