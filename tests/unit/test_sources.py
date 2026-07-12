from kasm.adapters.korea.sources import OFFICIAL_SOURCES, MeetingSource, classify_meeting


def test_all_catalog_entries_are_open_assembly_only() -> None:
    assert OFFICIAL_SOURCES
    assert all(
        entry.catalog_url.startswith("https://open.assembly.go.kr/") for entry in OFFICIAL_SOURCES
    )
    assert {entry.dataset for entry in OFFICIAL_SOURCES} == {
        "nzbyfwhwaoanttzje",
        "ncwgseseafwbuheph",
        "VCONFSUBCCONFLIST",
    }


def test_classifies_all_required_meeting_sources() -> None:
    assert classify_meeting({"CONF_NAME": "국회본회의"}) is MeetingSource.PLENARY
    assert classify_meeting({"CONF_NAME": "과학기술정보방송통신위원회"}) is MeetingSource.COMMITTEE
    assert (
        classify_meeting({"MEETING_NAME": "법안심사소위원회 제1차 회의"})
        is MeetingSource.SUBCOMMITTEE
    )
