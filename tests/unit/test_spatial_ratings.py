import math

import pytest

from westbusan.config import SpatialConfig
from westbusan.spatial.ratings import (
    FacilityRatingInput,
    composite,
    rate_age,
    rate_district_context,
    rate_facility,
    rate_room_scale,
)


def _config() -> SpatialConfig:
    return SpatialConfig.default()


@pytest.mark.parametrize(
    ("rooms", "expected"),
    [(10, "high"), (11, "medium"), (20, "medium"), (21, "low")],
)
def test_room_rating_boundaries(rooms: int, expected: str) -> None:
    assert rate_room_scale(rooms, _config()) == expected


def test_room_rating_accepts_core_analytics_reported_quality() -> None:
    assert rate_room_scale(10, _config(), quality="reported") == "high"


@pytest.mark.parametrize(
    ("rooms", "quality"),
    [
        (None, "reported"),
        (0, "reported"),
        (-1, "reported"),
        (math.nan, "reported"),
        (10, "rejected"),
    ],
)
def test_room_unavailable_is_not_encoded_as_low_or_zero(
    rooms: float | None, quality: str
) -> None:
    assert rate_room_scale(rooms, _config(), quality=quality) == "unavailable"


@pytest.mark.parametrize(
    ("years", "expected"),
    [(19.99, "low"), (20, "medium"), (29.99, "medium"), (30, "high")],
)
def test_age_rating_boundaries(years: float, expected: str) -> None:
    assert rate_age(years, _config()) == expected


def test_age_rating_accepts_core_analytics_reported_quality() -> None:
    assert rate_age(30, _config(), quality="reported", building_link_count=1) == "high"


@pytest.mark.parametrize(
    ("years", "quality", "link_count"),
    [
        (None, "reported", 1),
        (-0.1, "reported", 1),
        (math.inf, "reported", 1),
        (30, "ambiguous", 1),
        (30, "reported", 0),
        (30, "reported", 2),
        (30, "good", 1),
    ],
)
def test_age_requires_good_exactly_one_building_link(
    years: float | None, quality: str, link_count: int
) -> None:
    assert (
        rate_age(years, _config(), quality=quality, building_link_count=link_count)
        == "unavailable"
    )


@pytest.mark.parametrize(
    ("pressure", "supply", "expected"),
    [
        ("high", "low", "high"),
        ("high", "medium", "medium"),
        ("high", "high", "medium"),
        ("medium", "low", "medium"),
        ("low", "low", "medium"),
        ("medium", "medium", "low"),
        ("medium", "high", "low"),
        ("low", "medium", "low"),
        ("low", "high", "low"),
        ("unclassified", "low", "unavailable"),
        ("high", "unclassified", "unavailable"),
        (None, "low", "unavailable"),
        ("high", None, "unavailable"),
    ],
)
def test_district_context_truth_table(
    pressure: str | None, supply: str | None, expected: str
) -> None:
    assert rate_district_context(pressure, supply) == expected


@pytest.mark.parametrize(
    ("bands", "score", "grade"),
    [
        (("high", "high", "high"), 6, "priority_1"),
        (("high", "high", "medium"), 5, "priority_1"),
        (("high", "medium", "low"), 3, "priority_2"),
        (("medium", "medium", "medium"), 3, "priority_2"),
        (("medium", "low", "low"), 1, "monitor"),
        (("high", "low", "low"), 2, "monitor"),
        (("low", "low", "low"), 0, "general"),
        (("high", "high", "unavailable"), None, "insufficient_evidence"),
    ],
)
def test_composite_thresholds_and_null_unavailable_score(
    bands: tuple[str, str, str], score: int | None, grade: str
) -> None:
    result = composite(*bands)

    assert result.score == score
    assert result.grade == grade
    if score is None:
        assert result.component_points[2] is None


def test_facility_rating_labels_context_and_interpretation_limits() -> None:
    result = rate_facility(
        FacilityRatingInput(
            room_count=10,
            room_count_quality="reported",
            building_age_years=30,
            building_age_quality="reported",
            building_link_count=1,
            demand_pressure_band="high",
            room_supply_band="low",
        ),
        _config(),
    )

    assert result.component_label == "district_context"
    assert result.composite.grade == "priority_1"
    assert result.public_interpretation == "policy-support priority"
    assert result.not_assessments == (
        "safety",
        "hygiene",
        "legal compliance",
        "property condition",
        "occupancy",
    )
