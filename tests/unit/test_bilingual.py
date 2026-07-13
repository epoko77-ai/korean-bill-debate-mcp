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


def test_translates_official_english_committee_names() -> None:
    assert (
        korean_committee("Legislation and Judiciary Committee") == "법제사법위원회"
    )
    assert korean_committee("National Policy Committee") == "정무위원회"
    assert korean_committee("과학기술정보방송통신위원회") == "과학기술정보방송통신위원회"


def test_preserves_exact_bill_number_from_english_request() -> None:
    prepared = prepare_query(
        "For bill 2217784, show its current status and expert review report."
    )

    assert prepared.search_query == "2217784"
    assert prepared.language == "en"


def test_rejects_oversized_client_translation() -> None:
    with pytest.raises(ValueError, match="korean_query"):
        prepare_query("English request", "가" * 501)
