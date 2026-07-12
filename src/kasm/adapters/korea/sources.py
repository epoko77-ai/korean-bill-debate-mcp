"""Official minutes sources and meeting-type normalization."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class MeetingSource(StrEnum):
    PLENARY = "plenary"
    COMMITTEE = "committee"
    SUBCOMMITTEE = "subcommittee"


@dataclass(frozen=True, slots=True)
class SourceCatalogEntry:
    kind: MeetingSource
    dataset: str
    catalog_url: str
    dataset_env: str


OFFICIAL_SOURCES = (
    SourceCatalogEntry(
        MeetingSource.PLENARY,
        "nzbyfwhwaoanttzje",
        "https://open.assembly.go.kr/portal/data/service/selectAPIServicePage.do/OO1X9P001017YF13038",
        "ASSEMBLY_PLENARY_DATASET",
    ),
    SourceCatalogEntry(
        MeetingSource.COMMITTEE,
        "ncwgseseafwbuheph",
        "https://open.assembly.go.kr/portal/data/service/selectAPIServicePage.do/OR137O001023MZ19321",
        "ASSEMBLY_COMMITTEE_DATASET",
    ),
    SourceCatalogEntry(
        MeetingSource.SUBCOMMITTEE,
        "VCONFSUBCCONFLIST",
        "https://open.assembly.go.kr/portal/data/service/selectAPIServicePage.do/OOWY4R001216HX11519",
        "ASSEMBLY_SUBCOMMITTEE_DATASET",
    ),
)

DATASET_BY_SOURCE = {entry.kind: entry.dataset for entry in OFFICIAL_SOURCES}


def classify_meeting(row: Mapping[str, Any]) -> MeetingSource:
    """Classify API rows without treating subcommittees as full committees.

    The official catalog exposes plenary and committee APIs. Subcommittee
    minutes are carried by the committee source and distinguished from fields
    such as meeting/class name rather than fetched from a third API.
    """

    combined = " ".join(
        str(row.get(key, ""))
        for key in (
            "CLASS_NAME",
            "CONF_NAME",
            "COMMITTEE_NAME",
            "COMM_NAME",
            "MEETING_NAME",
            "CMIT_NM",
            "SB_CMIT_NM",
            "TITLE",
        )
    ).casefold()
    if "소위원회" in combined or "소위" in combined:
        return MeetingSource.SUBCOMMITTEE
    if "본회의" in combined:
        return MeetingSource.PLENARY
    return MeetingSource.COMMITTEE
