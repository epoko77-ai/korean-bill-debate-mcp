from kasm.adapters.korea.normalizer import normalize_text
from kasm.adapters.korea.parser import parse_transcript, split_speaker_label


def test_realistic_markers_roles_agenda_and_locators():
    source = """제22대 국회
1. 인공지능 정책 현안
○위원장 홍길동  회의를 시작합니다.
○과학기술정보통신부 장관 이영희: 자체 모델이 필요합니다.
○김철수 위원  해외 모델 의존이 우려됩니다.
"""
    result = parse_transcript(source, locator_prefix="page-3")
    assert [speech.speaker_name for speech in result.speeches] == ["홍길동", "이영희", "김철수"]
    assert result.speeches[1].organization == "과학기술정보통신부"
    assert result.speeches[1].speaker_role == "장관"
    assert result.speeches[2].agenda == "1. 인공지능 정책 현안"
    assert result.speeches[0].source_locator.startswith("page-3:")
    assert result.failures == []


def test_failure_is_not_silently_discarded():
    result = parse_transcript("마커가 없는 원문 발언입니다")
    assert not result.speeches
    assert result.failures[0].reason == "no speaker markers found"
    assert result.failures[0].excerpt == "마커가 없는 원문 발언입니다"


def test_normalizer_is_conservative():
    assert normalize_text("  안녕\t 하세요.\r\n\r\n\r\n 다음  문단 ") == "안녕 하세요.\n\n다음 문단"
    assert split_speaker_label("위원장홍길동") == ("홍길동", "위원장", None)
    assert split_speaker_label("소위원장이원택") == ("이원택", "소위원장", None)
    assert split_speaker_label("농림축산식품부차관박범수") == (
        "박범수",
        "차관",
        "농림축산식품부",
    )
    assert split_speaker_label("진술인원승연") == ("원승연", "진술인", None)
    assert split_speaker_label("김용민위원갑") == ("김용민", "위원", None)
    assert split_speaker_label("박은정위원녕십까?") == ("박은정", "위원", None)
    assert split_speaker_label("위원장김정호맙") == ("김정호", "위원장", None)
    assert split_speaker_label("한국수출입은행장황기연") == (
        "황기연",
        "은행장",
        "한국수출입",
    )
    assert split_speaker_label("법원행정처장권한대행기우종") == (
        "기우종",
        "처장권한대행",
        "법원행정",
    )
    assert split_speaker_label("이해민 위원 녕십까?") == ("이해민", "위원", None)
    assert split_speaker_label("보건복지위원장대리 이수진") == (
        "이수진",
        "위원장대리",
        "보건복지",
    )


def test_structural_and_role_only_labels_are_never_emitted_as_people():
    for label in ("소위", "소위원회", "의안", "안건", "보고", "심사경과", "심사경과보고"):
        assert split_speaker_label(label) == ("", None, None)
    assert split_speaker_label("위원장") == ("", None, None)


def test_structural_markers_are_quarantined_but_roleless_personal_name_is_preserved():
    source = """○소위  법안심사 결과입니다.
○의안: 약사법 일부개정법률안
○안건  의사일정 제1항
○보고  심사 결과를 보고합니다.
○심사경과  위원회 심사를 마쳤습니다.
○위원장  다음 순서로 넘어가겠습니다.
○조정식  법안의 취지를 말씀드리겠습니다.
"""

    result = parse_transcript(source)

    assert [speech.speaker_name for speech in result.speeches] == ["조정식"]
    assert result.speeches[0].speaker_role is None
    assert len(result.failures) == 6
    assert {failure.reason for failure in result.failures} == {
        "non-speaker or ambiguous marker label"
    }


def test_next_bill_agenda_heading_is_not_appended_to_previous_speech():
    source = """1. 약사법 일부개정법률안 (의안번호 2205513)
○김윤 위원  첫 의안에 대한 질문입니다.
○보건복지부장관 정은경  첫 의안에 대한 정부 답변입니다.
2. 다른 약사법 일부개정법률안 (의안번호 2299999)
○무관 위원  다음 의안에 대한 발언입니다.
"""

    result = parse_transcript(source)

    assert [speech.speaker_name for speech in result.speeches] == [
        "김윤",
        "정은경",
        "무관",
    ]
    assert result.speeches[1].text == "첫 의안에 대한 정부 답변입니다."
    assert "2299999" not in result.speeches[1].text
    assert result.speeches[2].agenda and "2299999" in result.speeches[2].agenda


def test_multiple_agendas_are_not_misattributed_to_the_last_bill():
    source = """48. 공수처법 일부개정법률안(의안번호 1)
64. 형사소송법 일부개정법률안(의안번호 2)
65. 형사소송법 일부개정법률안(의안번호 3)
○위원장 홍길동  여러 법안을 일괄 상정합니다.
○김용민 위원  보완수사권 폐지에 관하여 질의하겠습니다.
○법무부 장관 이영희  정부 입장을 말씀드리겠습니다.
"""
    speeches = parse_transcript(source).speeches
    assert {speech.agenda for speech in speeches} == {"복수 의사일정 제48항~제65항 일괄 심사"}
    assert all("의안번호 3" not in (speech.agenda or "") for speech in speeches)


def test_prose_reference_does_not_change_agenda_for_next_speaker():
    source = """64. 형사소송법 일부개정법률안
○김용민 위원  의사일정 제101항은 별도 검토가 필요합니다.
○법무부 장관 이영희  답변드리겠습니다.
"""
    speeches = parse_transcript(source).speeches
    assert speeches[1].agenda == "64. 형사소송법 일부개정법률안"
