from __future__ import annotations

from datetime import date

import pytest

from kasm.research.relevance import (
    BillDateBasis,
    RelevanceCriteria,
    evaluate_candidate,
    rank_candidates,
)


def test_exact_bill_number_is_a_hard_match_including_aggregated_agendas() -> None:
    criteria = RelevanceCriteria.from_query("2219564번 의안 원문과 회의록")
    exact_bill = {
        "id": "exact-bill",
        "bill_no": "2219564",
        "name": "형사소송법 일부개정법률안",
    }
    exact_agenda = {
        "id": "exact-agenda",
        "agenda_items": [{"bill_no": "2219564", "title": "형사소송법 일부개정법률안"}],
    }
    tempting_wrong_bill = {
        "id": "wrong",
        "bill_no": "2219565",
        "name": "2219564번 의안과 유사한 형사소송법 개정안",
    }

    ranked = rank_candidates([tempting_wrong_bill, exact_agenda, exact_bill], criteria)

    assert {result.candidate_id for result in ranked} == {"exact-bill", "exact-agenda"}
    assert all(result.score >= 100 for result in ranked)
    assert all("bill_no_exact:2219564" in result.match_reasons for result in ranked)
    assert evaluate_candidate(tempting_wrong_bill, criteria).rejection_reasons == (
        "bill_no_mismatch",
    )


def test_supplementary_investigation_excludes_maritime_and_civil_false_positives() -> None:
    criteria = RelevanceCriteria.from_query(
        "2026년 1월 1일부터 현재까지 보완수사권 관련 법안·회의록",
        statute_terms=("형사소송법",),
        committees=("법제사법위원회",),
        date_from=date(2026, 1, 1),
        date_to=date(2026, 7, 13),
    )
    connected_agenda = {
        "id": "criminal-agenda",
        "committee": "법사위",
        "date": "2026-06-18",
        "agenda_items": [
            {
                "bill_no": "2219564",
                "title": "형사소송법 일부개정법률안(보완수사요구권 정비)",
            }
        ],
        "agenda_text": "검사의 보완수사권과 정부 답변 심사",
    }
    criminal_bill = {
        "id": "criminal-bill",
        "bill_no": "2219564",
        "name": "형사소송법 일부개정법률안",
        "summary": "검사의 보완수사 범위를 정비한다.",
        "committee_name_ko": "법제사법위원회",
        "proposed_date": "2026-05-10",
    }
    maritime = {
        "id": "maritime",
        "name": "해양사고의 조사 및 심판에 관한 법률 일부개정법률안",
        "summary": "해양사고 조사 권한을 보완한다.",
        "committee": "농림축산식품해양수산위원회",
        "date": "2026-04-01",
    }
    civil = {
        "id": "civil",
        "name": "민법 일부개정법률안",
        "summary": "권리 보호를 보완하고 사실 조사를 실시한다.",
        "committee": "법제사법위원회",
        "date": "2026-05-01",
    }
    old_exact_text = {
        "id": "old",
        "name": "형사소송법 일부개정법률안",
        "summary": "보완수사권 정비",
        "committee": "법제사법위원회",
        "date": "2025-12-31",
    }

    ranked = rank_candidates(
        [maritime, civil, criminal_bill, old_exact_text, connected_agenda], criteria
    )

    assert [result.candidate_id for result in ranked] == [
        "criminal-agenda",
        "criminal-bill",
    ]
    assert ranked[0].match_reasons == (
        "committee_exact:법제사법위원회",
        "date_in_range:2026-06-18",
        "statute:형사소송법@agenda",
        "issue:보완수사권@agenda",
        "related_issue:보완수사요구권@agenda",
    )
    assert evaluate_candidate(maritime, criteria).rejection_reasons == ("committee_mismatch",)
    assert evaluate_candidate(civil, criteria).rejection_reasons == ("below_minimum_score",)
    assert evaluate_candidate(old_exact_text, criteria).rejection_reasons == ("date_out_of_range",)


@pytest.mark.parametrize(
    "query",
    (
        "최근 AI 입법",
        "recent artificial intelligence legislation",
        "최근 인공지능 법안",
    ),
)
def test_ai_inputs_normalize_to_the_same_specific_issue(query: str) -> None:
    criteria = RelevanceCriteria.from_query(query)
    candidates = [
        {"id": "maritime", "name": "해양사고 조사법 일부개정법률안"},
        {"id": "ai", "name": "인공지능 기본법 일부개정법률안"},
    ]

    ranked = rank_candidates(candidates, criteria)

    assert criteria.issue_terms == ("인공지능",)
    assert [result.candidate_id for result in ranked] == ["ai"]
    assert ranked[0].match_reasons == ("issue:인공지능@title",)


def test_research_instruction_words_do_not_become_policy_subjects() -> None:
    criteria = RelevanceCriteria.from_query(
        "2026년 7월 인공지능 관련 법안과 위원회 논의를 공식 원문 기준으로 조사해줘"
    )

    assert criteria.issue_terms == ("인공지능",)
    assert (
        evaluate_candidate(
            {
                "id": "procedural",
                "name": "위원회 공식 조사 기준 개선 법률안",
            },
            criteria,
        ).relevant
        is False
    )
    assert (
        evaluate_candidate(
            {
                "id": "ai",
                "name": "인공지능 산업 진흥에 관한 법률안",
            },
            criteria,
        ).relevant
        is True
    )


def test_proposal_year_uses_only_official_proposal_date_for_bill_filtering() -> None:
    criteria = RelevanceCriteria.from_query(
        "2026년 발의된 인공지능 관련 법안",
        date_from=date(2026, 1, 1),
        date_to=date(2026, 12, 31),
    )
    proposed_in_2025_processed_in_2026 = {
        "BILL_NO": "2210001",
        "BILL_NAME": "인공지능 활용 촉진법안",
        "PROPOSE_DT": "2025-11-01",
        "RGS_PROC_DT": "2026-03-09",
    }
    proposed_in_2026_processed_in_2025 = {
        "BILL_NO": "2210002",
        "BILL_NAME": "인공지능 안전법안",
        "PROPOSE_DT": "2026-01-08",
        "RGS_PROC_DT": "2025-12-31",
    }
    processed_in_2026_without_proposal_date = {
        "BILL_NO": "2210003",
        "BILL_NAME": "인공지능 책임법안",
        "RGS_PROC_DT": "2026-04-01",
    }

    rejected = evaluate_candidate(proposed_in_2025_processed_in_2026, criteria)
    accepted = evaluate_candidate(proposed_in_2026_processed_in_2025, criteria)
    missing = evaluate_candidate(processed_in_2026_without_proposal_date, criteria)

    assert criteria.bill_date_basis is BillDateBasis.PROPOSAL
    assert rejected.relevant is False
    assert rejected.rejection_reasons == ("proposal_date_out_of_range",)
    assert accepted.relevant is True
    assert "proposal_date_in_range:2026-01-08" in accepted.match_reasons
    assert missing.relevant is False
    assert missing.rejection_reasons == ("proposal_date_missing",)


def test_non_proposal_date_query_preserves_general_bill_date_matching() -> None:
    criteria = RelevanceCriteria.from_query(
        "2026년 심사된 인공지능 관련 법안",
        date_from=date(2026, 1, 1),
        date_to=date(2026, 12, 31),
    )
    candidate = {
        "BILL_NO": "2210001",
        "BILL_NAME": "인공지능 활용 촉진법안",
        "PROPOSE_DT": "2025-11-01",
        "RGS_PROC_DT": "2026-03-09",
    }

    result = evaluate_candidate(candidate, criteria)

    assert criteria.bill_date_basis is BillDateBasis.ANY
    assert result.relevant is True
    assert "date_in_range:2026-03-09" in result.match_reasons


def test_related_issue_is_discoverable_but_scored_below_an_equivalent() -> None:
    criteria = RelevanceCriteria.from_query("보완수사권 관련 법안")
    exact = {"id": "exact", "name": "보완수사권 정비 법안"}
    related = {"id": "related", "name": "보완수사요구권 정비 법안"}

    ranked = rank_candidates([related, exact], criteria)

    assert criteria.issue_terms == ("보완수사권",)
    assert criteria.related_issue_terms == ("보완수사요구권",)
    assert [result.candidate_id for result in ranked] == ["exact", "related"]
    assert ranked[0].match_reasons == ("issue:보완수사권@title",)
    assert ranked[1].match_reasons == ("related_issue:보완수사요구권@title",)
    assert ranked[0].score > ranked[1].score
    assert criteria.expansion_reasons == (
        "canonical_match:supplementary_investigation_authority",
        "related_concept:보완수사권→보완수사요구권",
        "related_concept:보완수사권→형사소송법",
    )


def test_generic_research_words_alone_fail_closed() -> None:
    criteria = RelevanceCriteria.from_query(
        "최근 국회 법안 회의록 관련 내용을 확인해줘",
        issue_terms=("법안", "회의록", "최근"),
    )
    candidate = {
        "id": "generic",
        "title": "최근 국회 법안 회의록",
        "summary": "관련 내용을 확인한 조사 결과",
    }

    result = evaluate_candidate(candidate, criteria)

    assert result.score == 0
    assert result.relevant is False
    assert result.match_reasons == ()
    assert result.rejection_reasons == ("no_meaningful_terms",)


@pytest.mark.parametrize(
    ("query", "expected_terms", "matching_title", "unrelated_title"),
    (
        (
            "딥페이크 관련 법안과 회의록",
            {"딥페이크"},
            "딥페이크 성범죄 방지법 일부개정법률안",
            "해양사고 조사법 일부개정법률안",
        ),
        (
            "플랫폼 노동 종사자 보호 입법",
            {"플랫폼노동종사자", "플랫폼", "노동", "종사자"},
            "플랫폼 노동 종사자 보호법안",
            "원양어선 선원 보호법안",
        ),
        (
            "장애인 이동권 보장 법안",
            {"장애인이동권", "장애인", "이동권"},
            "장애인 이동권 보장을 위한 교통약자법 개정안",
            "산업단지 교통 개선법안",
        ),
        (
            "기후 적응과 에너지 전환 관련 국회 논의",
            {"기후적응에너지", "기후", "적응", "에너지", "전환"},
            "기후 적응 및 에너지 전환 지원법안",
            "학교급식 지원법안",
        ),
        (
            "감염병 병상 확보 대책 법안",
            {"감염병병상", "감염병", "병상"},
            "감염병 병상 확보 지원법안",
            "중소기업 수출 지원법안",
        ),
    ),
)
def test_unfamiliar_korean_policy_topics_do_not_depend_on_curated_registry(
    query: str,
    expected_terms: set[str],
    matching_title: str,
    unrelated_title: str,
) -> None:
    criteria = RelevanceCriteria.from_query(query)

    matching = evaluate_candidate({"id": "matching", "name": matching_title}, criteria)
    unrelated = evaluate_candidate({"id": "unrelated", "name": unrelated_title}, criteria)

    assert expected_terms.issubset(set(criteria.issue_terms))
    assert matching.relevant is True
    assert unrelated.relevant is False
    assert unrelated.rejection_reasons == ("below_minimum_score",)
    assert criteria.query == query


def test_topic_free_structured_scope_is_exhaustive_inside_hard_filters() -> None:
    criteria = RelevanceCriteria.from_query(
        "올해 법제사법위원회 회의록 전체",
        committees=("법제사법위원회",),
        date_from=date(2026, 1, 1),
        date_to=date(2026, 7, 13),
    )

    result = evaluate_candidate(
        {
            "id": "meeting",
            "committee": "법사위",
            "date": "2026-06-18",
            "title": "제4차 법안심사제1소위원회",
        },
        criteria,
    )

    assert criteria.issue_terms == ()
    assert result.relevant is True
    assert result.score == criteria.minimum_score
    assert result.match_reasons == (
        "committee_exact:법제사법위원회",
        "date_in_range:2026-06-18",
        "structured_scope_only",
    )


def test_rank_order_is_deterministic_for_equal_scores() -> None:
    criteria = RelevanceCriteria.from_query("AI 법안")
    older = {
        "id": "older",
        "name": "인공지능 산업법",
        "date": "2026-01-01",
    }
    newer_b = {
        "id": "newer-b",
        "name": "인공지능 안전법",
        "date": "2026-06-01",
    }
    newer_a = {
        "id": "newer-a",
        "name": "인공지능 책임법",
        "date": "2026-06-01",
    }

    forward = rank_candidates([newer_b, older, newer_a], criteria)
    reverse = rank_candidates([newer_a, older, newer_b], criteria)

    assert [result.candidate_id for result in forward] == [
        "newer-a",
        "newer-b",
        "older",
    ]
    assert [result.candidate_id for result in reverse] == [
        "newer-a",
        "newer-b",
        "older",
    ]


def test_literal_query_concepts_are_never_silently_cut_after_thirty_two() -> None:
    terms = [f"특수의제{chr(0xAC00 + index)}영역" for index in range(40)]

    criteria = RelevanceCriteria.from_query(" ".join(terms))

    assert set(terms).issubset(set(criteria.issue_terms))
    assert len(criteria.issue_terms) > 32


def _proposer_bill(
    *,
    bill_no: str = "2219951",
    title: str = "인공지능 안전법안",
    representative: str = "김남근",
    co_proposers: str = "송재봉,민병덕,박정,김윤",
) -> dict[str, str]:
    return {
        "id": f"bill:{bill_no}",
        "BILL_NO": bill_no,
        "BILL_NAME": title,
        "RST_PROPOSER": representative,
        "PUBL_PROPOSER": co_proposers,
        "PROPOSER": f"{representative}의원 등 4인",
        "MEMBER_LIST": "https://likms.assembly.go.kr/bill/coactorListPopup.do?billId=test",
    }


def test_representative_and_co_proposer_roles_use_distinct_official_fields() -> None:
    representative = evaluate_candidate(
        _proposer_bill(),
        RelevanceCriteria.from_query("김남근 의원이 대표발의한 법안"),
    )
    wrong_role = evaluate_candidate(
        _proposer_bill(),
        RelevanceCriteria.from_query("김윤 의원이 대표발의한 법안"),
    )
    co_proposer = evaluate_candidate(
        _proposer_bill(),
        RelevanceCriteria.from_query("김윤 의원이 공동발의한 법안"),
    )

    assert representative.relevant is True
    assert representative.match_reasons == ("proposer_exact:representative:김남근@RST_PROPOSER",)
    assert wrong_role.relevant is False
    assert wrong_role.rejection_reasons == ("representative_proposer_mismatch:김윤",)
    assert co_proposer.relevant is True
    assert co_proposer.match_reasons == ("proposer_exact:co_proposer:김윤@PUBL_PROPOSER",)


@pytest.mark.parametrize("name", ("김남근", "김윤"))
def test_generic_proposer_scope_matches_either_official_role(name: str) -> None:
    result = evaluate_candidate(
        _proposer_bill(),
        RelevanceCriteria.from_query(f"{name} 의원이 발의한 법안"),
    )

    assert result.relevant is True
    assert any(reason.startswith("proposer_exact:") for reason in result.match_reasons)


@pytest.mark.parametrize(
    ("criteria", "expected_reason"),
    (
        (
            RelevanceCriteria.from_query("김남근 대표발의 법안과 박정 대표발의 법안"),
            "proposer_exact:representative:김남근@RST_PROPOSER",
        ),
        (
            RelevanceCriteria.from_query("김윤 공동발의 법안과 이정민 공동발의 법안"),
            "proposer_exact:co_proposer:김윤@PUBL_PROPOSER",
        ),
        (
            RelevanceCriteria.from_query("김남근 발의 법안과 박정 발의 법안"),
            "proposer_exact:representative:김남근@RST_PROPOSER",
        ),
    ),
)
def test_multiple_names_within_one_proposer_role_are_union_scopes(
    criteria: RelevanceCriteria,
    expected_reason: str,
) -> None:
    result = evaluate_candidate(_proposer_bill(), criteria)

    assert result.relevant is True
    assert expected_reason in result.match_reasons


@pytest.mark.parametrize(
    "query",
    (
        "김남근 대표발의 법안",
        "김남근이 대표 발의한 법안",
        "김윤 공동발의 법안",
        "박정이 발의한 법안",
    ),
)
def test_explicit_proposer_role_without_member_title_is_not_mistaken_for_topic(
    query: str,
) -> None:
    criteria = RelevanceCriteria.from_query(query)
    result = evaluate_candidate(_proposer_bill(), criteria)

    assert criteria.issue_terms == ()
    assert result.relevant is True


def test_proposer_name_and_topic_are_independent_hard_gates() -> None:
    criteria = RelevanceCriteria.from_query("김남근 의원이 대표발의한 인공지능 법안")

    exact = evaluate_candidate(_proposer_bill(), criteria)
    wrong_topic = evaluate_candidate(
        _proposer_bill(title="해양사고 조사법 일부개정법률안"),
        criteria,
    )
    wrong_person = evaluate_candidate(
        _proposer_bill(representative="김민석"),
        criteria,
    )

    assert exact.relevant is True
    assert "인공지능" in criteria.issue_terms
    assert not {"김남근", "대표발의한", "의원"}.intersection(criteria.issue_terms)
    assert wrong_topic.relevant is False
    assert wrong_topic.rejection_reasons == ("proposer_topic_mismatch",)
    assert wrong_person.relevant is False
    assert wrong_person.rejection_reasons == ("representative_proposer_mismatch:김남근",)


def test_proposer_matching_uses_full_name_boundaries_and_ignores_member_list_url() -> None:
    criteria = RelevanceCriteria.from_query("김민 의원이 공동발의한 법안")
    candidate = _proposer_bill(
        representative="김민석",
        co_proposers="박김민수,이정민",
    )
    candidate["MEMBER_LIST"] = "https://official.example/member/김민"

    result = evaluate_candidate(candidate, criteria)

    assert result.relevant is False
    assert result.rejection_reasons == ("co_proposer_mismatch:김민",)


def test_compact_proposer_label_is_only_a_representative_fallback() -> None:
    candidate = _proposer_bill()
    candidate.pop("RST_PROPOSER")
    candidate.pop("PUBL_PROPOSER")

    representative = evaluate_candidate(
        candidate,
        RelevanceCriteria.from_query("김남근 의원이 대표발의한 법안"),
    )
    unproven_co_proposer = evaluate_candidate(
        candidate,
        RelevanceCriteria.from_query("김윤 의원이 공동발의한 법안"),
    )

    assert representative.match_reasons == ("proposer_exact:representative:김남근@PROPOSER",)
    assert unproven_co_proposer.relevant is False
