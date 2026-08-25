"""Conservative development-screening rules for vacant-house candidates.

The result is a policy pre-screen, not a legal opinion or a substitute for an
official land-use, road, cadastral, or building-register review.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

DevelopmentReviewStatus = Literal["excluded", "conditional", "passed"]
DataReviewStatus = Literal["complete", "needs_review"]

_ADDITIONAL_LAND_USE_REVIEW_TERMS = (
    "자연녹지",
    "생산녹지",
    "보전녹지",
    "공업지역",
    "산업단지",
    "역사문화환경보존",
)


@dataclass(frozen=True, slots=True)
class DevelopmentReview:
    eligible: bool
    status: DevelopmentReviewStatus
    exclusion_reasons: tuple[str, ...]
    conditional_reasons: tuple[str, ...]
    data_status: DataReviewStatus
    data_gaps: tuple[str, ...]


def assess_development_review(
    *,
    road_sides: Sequence[str],
    land_use_zones: Sequence[str],
    has_cadastral_geometry: bool,
    building_register_linked: bool,
    construction_year_known: bool,
    building_structure_known: bool,
    explicit_lodging_use_restriction: bool = False,
) -> DevelopmentReview:
    """Classify a candidate using only explicit, published evidence.

    ``explicit_lodging_use_restriction`` must only be set from an authoritative
    reviewed source. The function intentionally does not infer a legal lodging
    prohibition from a broad zoning label alone.
    """
    roads = tuple(
        normalized
        for value in road_sides
        if (normalized := _normalized(value))
    )
    zones = tuple(
        normalized
        for value in land_use_zones
        if (normalized := _normalized(value))
    )
    exclusion_reasons: list[str] = []
    conditional_reasons: list[str] = []
    data_gaps: list[str] = []

    if not has_cadastral_geometry:
        exclusion_reasons.append("cadastral_geometry_unconfirmed")
    if not roads:
        exclusion_reasons.append("road_contact_unconfirmed")

    landlocked_count = sum("맹지" in value for value in roads)
    if roads and landlocked_count == len(roads):
        exclusion_reasons.append("landlocked_parcel")
    elif landlocked_count:
        conditional_reasons.append("partially_landlocked_parcels")

    if any("개발행위허가제한지역" in value for value in zones):
        exclusion_reasons.append("development_activity_restricted_area")
    if explicit_lodging_use_restriction:
        exclusion_reasons.append("lodging_use_explicitly_restricted")

    if any("(불)" in value or "（불）" in value for value in roads):
        conditional_reasons.append("weak_road_condition")
    if any("지정되지않음" in value for value in roads):
        conditional_reasons.append("road_contact_not_designated")
    if any(
        term in zone
        for zone in zones
        for term in _ADDITIONAL_LAND_USE_REVIEW_TERMS
    ):
        conditional_reasons.append("additional_land_use_review_required")

    if not building_register_linked:
        data_gaps.append("building_register_not_linked")
    else:
        if not construction_year_known:
            data_gaps.append("construction_year_unconfirmed")
        if not building_structure_known:
            data_gaps.append("building_structure_unconfirmed")

    exclusions = _ordered_unique(exclusion_reasons)
    conditions = _ordered_unique(conditional_reasons)
    gaps = _ordered_unique(data_gaps)
    data_status: DataReviewStatus = "needs_review" if gaps else "complete"
    if exclusions:
        return DevelopmentReview(
            False, "excluded", exclusions, conditions, data_status, gaps
        )
    if conditions:
        return DevelopmentReview(
            True, "conditional", (), conditions, data_status, gaps
        )
    return DevelopmentReview(True, "passed", (), (), data_status, gaps)


def _normalized(value: object) -> str:
    return "".join(str(value or "").split())


def _ordered_unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


__all__ = [
    "DevelopmentReview",
    "DevelopmentReviewStatus",
    "DataReviewStatus",
    "assess_development_review",
]
