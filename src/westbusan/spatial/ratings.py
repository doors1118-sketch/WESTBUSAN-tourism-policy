"""Pure, explicit facility rating semantics for public policy support."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from westbusan.config import SpatialConfig

RatingBand = Literal["high", "medium", "low", "unavailable"]
CompositeGrade = Literal[
    "priority_1",
    "priority_2",
    "monitor",
    "general",
    "insufficient_evidence",
]
_CLASSIFIED_BANDS = frozenset({"high", "medium", "low"})
_POINTS: dict[RatingBand, int | None] = {
    "high": 2,
    "medium": 1,
    "low": 0,
    "unavailable": None,
}


@dataclass(frozen=True, slots=True)
class CompositeRating:
    score: int | None
    grade: CompositeGrade
    component_points: tuple[int | None, int | None, int | None]


@dataclass(frozen=True, slots=True)
class FacilityRatingInput:
    room_count: float | None
    room_count_quality: str
    building_age_years: float | None
    building_age_quality: str
    building_link_count: int
    demand_pressure_band: str | None
    room_supply_band: str | None


@dataclass(frozen=True, slots=True)
class FacilityRating:
    small_scale: RatingBand
    aged_building: RatingBand
    district_context: RatingBand
    composite: CompositeRating
    component_label: Literal["district_context"] = "district_context"
    public_interpretation: Literal["policy-support priority"] = (
        "policy-support priority"
    )
    not_assessments: tuple[str, ...] = (
        "safety",
        "hygiene",
        "legal compliance",
        "property condition",
        "occupancy",
    )


def rate_room_scale(
    room_count: float | None,
    config: SpatialConfig,
    *,
    quality: str = "reported",
) -> RatingBand:
    """Rate a positive, accepted room count at the exact configured breaks."""
    value = _finite_number(room_count)
    if quality not in {"good", "reported"} or value is None or value <= 0:
        return "unavailable"
    high_max, medium_max = config.room_scale_breaks
    if value <= high_max:
        return "high"
    if value <= medium_max:
        return "medium"
    return "low"


def rate_age(
    building_age_years: float | None,
    config: SpatialConfig,
    *,
    quality: str = "reported",
    building_link_count: int = 1,
) -> RatingBand:
    """Rate age only for one good run-scoped building link."""
    value = _finite_number(building_age_years)
    if (
        quality != "reported"
        or building_link_count != 1
        or value is None
        or value < 0
    ):
        return "unavailable"
    medium_min, high_min = config.age_year_breaks
    if value >= high_min:
        return "high"
    if value >= medium_min:
        return "medium"
    return "low"


def rate_district_context(
    demand_pressure_band: str | None,
    room_supply_band: str | None,
) -> RatingBand:
    """Rate exact-period district context without allocating it to a grid."""
    if (
        demand_pressure_band not in _CLASSIFIED_BANDS
        or room_supply_band not in _CLASSIFIED_BANDS
    ):
        return "unavailable"
    pressure_high = demand_pressure_band == "high"
    supply_low = room_supply_band == "low"
    if pressure_high and supply_low:
        return "high"
    if pressure_high != supply_low:
        return "medium"
    return "low"


def composite(
    small_scale: RatingBand | str,
    aged_building: RatingBand | str,
    district_context: RatingBand | str,
) -> CompositeRating:
    """Return a composite only when all component points are available."""
    bands = (small_scale, aged_building, district_context)
    points = tuple(_POINTS.get(band) for band in bands)
    if any(point is None for point in points):
        return CompositeRating(
            score=None,
            grade="insufficient_evidence",
            component_points=points,
        )
    score = sum(point for point in points if point is not None)
    if score >= 5:
        grade: CompositeGrade = "priority_1"
    elif score >= 3:
        grade = "priority_2"
    elif score >= 1:
        grade = "monitor"
    else:
        grade = "general"
    return CompositeRating(score=score, grade=grade, component_points=points)


def rate_facility(
    rating_input: FacilityRatingInput,
    config: SpatialConfig,
) -> FacilityRating:
    """Rate all components while retaining unavailable as an explicit band."""
    small_scale = rate_room_scale(
        rating_input.room_count,
        config,
        quality=rating_input.room_count_quality,
    )
    aged_building = rate_age(
        rating_input.building_age_years,
        config,
        quality=rating_input.building_age_quality,
        building_link_count=rating_input.building_link_count,
    )
    district_context = rate_district_context(
        rating_input.demand_pressure_band,
        rating_input.room_supply_band,
    )
    return FacilityRating(
        small_scale=small_scale,
        aged_building=aged_building,
        district_context=district_context,
        composite=composite(small_scale, aged_building, district_context),
    )


def _finite_number(value: float | None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
