"""Conservative, explainable question-answer relation inference."""

from __future__ import annotations

from collections.abc import Sequence

from .models import Speech, SpeechRelation

_GOVERNMENT_ROLES = {
    "국무총리",
    "장관",
    "차관",
    "처장",
    "청장",
    "원장",
    "실장",
    "국장",
    "과장",
    "사무처장",
}
_QUESTION_SIGNALS = ("?", "습니까", "합니까", "인가요", "맞습니까", "어떻습니까")


def looks_like_question(text: str) -> bool:
    compact = text.rstrip()
    return any(signal in compact for signal in _QUESTION_SIGNALS)


def infer_question_answer_relations(speeches: Sequence[Speech]) -> list[SpeechRelation]:
    """Link explicit adjacent Q&A and conservative same-member follow-up questions."""

    relations: list[SpeechRelation] = []
    for question, answer in zip(speeches, speeches[1:], strict=False):
        if not looks_like_question(question.text):
            continue
        if answer.speaker_role not in _GOVERNMENT_ROLES:
            continue
        relations.append(SpeechRelation(question.id, answer.id, "QUESTION_TO", confidence=0.9))
        relations.append(SpeechRelation(answer.id, question.id, "ANSWER_TO", confidence=0.9))
    for first, answer, follow_up in zip(speeches, speeches[1:], speeches[2:], strict=False):
        if (
            looks_like_question(first.text)
            and answer.speaker_role in _GOVERNMENT_ROLES
            and looks_like_question(follow_up.text)
            and follow_up.speaker_name == first.speaker_name
        ):
            relations.append(SpeechRelation(follow_up.id, first.id, "FOLLOW_UP_TO", confidence=0.8))
    return relations
