from __future__ import annotations

from datetime import date

import pytest

from kasm.research.assembly_terms import (
    ASSEMBLY_TERMS,
    DEFAULT_ASSEMBLY_TERM_BOUNDS,
    OFFICIAL_ASSEMBLY_TERM_SOURCE_URL,
    assembly_term,
    assembly_term_for_date,
    assembly_terms_intersecting,
)


def test_official_catalog_preserves_all_term_boundaries_from_first_to_current() -> None:
    expected = (
        (1, date(1948, 5, 31), date(1950, 5, 30)),
        (2, date(1950, 5, 31), date(1954, 5, 30)),
        (3, date(1954, 5, 31), date(1958, 5, 30)),
        (4, date(1958, 5, 31), date(1960, 7, 28)),
        (5, date(1960, 7, 29), date(1961, 5, 16)),
        (6, date(1963, 12, 17), date(1967, 6, 30)),
        (7, date(1967, 7, 1), date(1971, 6, 30)),
        (8, date(1971, 7, 1), date(1972, 10, 17)),
        (9, date(1973, 3, 12), date(1979, 3, 11)),
        (10, date(1979, 3, 12), date(1980, 10, 27)),
        (11, date(1981, 4, 11), date(1985, 4, 10)),
        (12, date(1985, 4, 11), date(1988, 5, 29)),
        (13, date(1988, 5, 30), date(1992, 5, 29)),
        (14, date(1992, 5, 30), date(1996, 5, 29)),
        (15, date(1996, 5, 30), date(2000, 5, 29)),
        (16, date(2000, 5, 30), date(2004, 5, 29)),
        (17, date(2004, 5, 30), date(2008, 5, 29)),
        (18, date(2008, 5, 30), date(2012, 5, 29)),
        (19, date(2012, 5, 30), date(2016, 5, 29)),
        (20, date(2016, 5, 30), date(2020, 5, 29)),
        (21, date(2020, 5, 30), date(2024, 5, 29)),
        (22, date(2024, 5, 30), date(2028, 5, 29)),
    )

    assert tuple((item.number, item.date_from, item.date_to) for item in ASSEMBLY_TERMS) == expected
    assert all(item.source_url == OFFICIAL_ASSEMBLY_TERM_SOURCE_URL for item in ASSEMBLY_TERMS)
    expected_bounds = {number: (date_from, date_to) for number, date_from, date_to in expected}
    assert dict(DEFAULT_ASSEMBLY_TERM_BOUNDS) == expected_bounds


@pytest.mark.parametrize(
    "value",
    (
        date(1961, 5, 17),
        date(1963, 12, 16),
        date(1972, 10, 18),
        date(1973, 3, 11),
        date(1980, 10, 28),
        date(1981, 4, 10),
    ),
)
def test_institutional_hiatus_is_not_misattributed_to_an_elected_term(value: date) -> None:
    assert assembly_term_for_date(value) is None


def test_date_range_intersection_skips_hiatus_without_losing_neighboring_terms() -> None:
    selected = assembly_terms_intersecting(date(1961, 5, 1), date(1964, 1, 1))

    assert tuple(item.number for item in selected) == (5, 6)
    assert assembly_term_for_date(date(1961, 5, 16)) == assembly_term(5)
    assert assembly_term_for_date(date(1963, 12, 17)) == assembly_term(6)


def test_unknown_term_and_reversed_date_range_fail_closed() -> None:
    with pytest.raises(ValueError, match="unsupported Assembly term"):
        assembly_term(23)
    with pytest.raises(ValueError, match="date_from"):
        assembly_terms_intersecting(date(2020, 1, 2), date(2020, 1, 1))
