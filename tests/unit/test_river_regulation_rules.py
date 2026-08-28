from __future__ import annotations

import pytest

from westbusan.river_regulation.rules import assess_activity


@pytest.mark.parametrize(
    ("zone", "activity", "expected_grade"),
    [
        ("waterfront", "festival", "conditional"),
        ("waterfront", "lodging", "principally_restricted"),
        ("general_conservation", "walking", "conditional"),
        ("general_conservation", "lodging", "principally_restricted"),
        ("restoration", "ecology", "conditional"),
        ("restoration", "parking", "principally_restricted"),
        ("river_area_unclassified", "sports", "conditional"),
        ("outside_river_area", "lodging", "outside_scope"),
    ],
)
def test_activity_assessment_uses_conservative_river_zone_rules(
    zone: str,
    activity: str,
    expected_grade: str,
) -> None:
    result = assess_activity(zone, activity)

    assert result.grade == expected_grade
    assert result.label
    assert result.reason
    assert result.next_check
    assert result.legal_effect is False


def test_unknown_activity_or_zone_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown activity"):
        assess_activity("waterfront", "casino")
    with pytest.raises(ValueError, match="Unknown zone"):
        assess_activity("magic_zone", "walking")

