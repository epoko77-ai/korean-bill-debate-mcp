from kasm.core.models import Speech
from kasm.core.relations import infer_question_answer_relations


def speech(
    identifier: str, sequence: int, role: str, text: str, *, name: str | None = None
) -> Speech:
    return Speech(
        identifier,
        "kna:22:committee:2025-01-23:1",
        sequence,
        None,
        name or identifier,
        role,
        None,
        text,
        None,
        None,
        None,
        f"page:{sequence}",
        "hash",
        "v1",
    )


def test_links_explicit_question_to_adjacent_government_answer() -> None:
    speeches = [
        speech("q1", 1, "위원", "추가 예산을 확보할 계획입니까?"),
        speech("a1", 2, "차관", "필요한 만큼 확보하겠습니다."),
        speech("c1", 3, "위원", "잘 알겠습니다."),
    ]
    relations = infer_question_answer_relations(speeches)
    actual = [
        (item.relation_type, item.source_speech_id, item.target_speech_id) for item in relations
    ]
    assert actual == [
        ("QUESTION_TO", "q1", "a1"),
        ("ANSWER_TO", "a1", "q1"),
    ]


def test_does_not_invent_relation_without_signal_or_government_role() -> None:
    speeches = [
        speech("s1", 1, "위원", "의견을 말씀드립니다."),
        speech("s2", 2, "위원", "동의하십니까?"),
        speech("s3", 3, "위원", "동의합니다."),
    ]
    assert infer_question_answer_relations(speeches) == []


def test_links_same_member_follow_up_and_second_answer() -> None:
    speeches = [
        speech("q1", 1, "위원", "예산을 확보할 계획입니까?", name="질의위원"),
        speech("a1", 2, "차관", "확보하겠습니다."),
        speech("q2", 3, "위원", "확보 시점은 언제입니까?", name="질의위원"),
        speech("a2", 4, "장관", "내년입니다."),
    ]
    relations = infer_question_answer_relations(speeches)
    assert any(
        item.relation_type == "FOLLOW_UP_TO"
        and item.source_speech_id == "q2"
        and item.target_speech_id == "q1"
        for item in relations
    )
    assert any(
        item.relation_type == "ANSWER_TO" and item.source_speech_id == "a2" for item in relations
    )
