"""Transparent accommodation investment metrics and policy recommendations."""

from __future__ import annotations

import bisect
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class GridCell:
    grid_id: str
    district: str
    area_km2: float


@dataclass(frozen=True, slots=True)
class FacilityPoint:
    facility_id: str
    grid_id: str
    room_count: float | None
    building_age_years: float | None
    license_date: date | None
    tourism_registration: bool


@dataclass(frozen=True, slots=True)
class OpportunityMetrics:
    demand_score: float | None
    room_supply_score: float | None
    accessibility_score: float | None
    facility_density: float
    room_density: float | None
    aged_share: float | None
    small_scale_share: float | None
    tourism_registration_share: float | None
    recent_entry_share: float | None
    demand_per_100_rooms: float | None
    room_coverage: float
    age_coverage: float


@dataclass(frozen=True, slots=True)
class Recommendation:
    kind: str
    evidence_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GridOpportunity:
    grid_id: str
    district: str
    facility_count: int
    known_room_count: float
    facility_density: float
    room_density: float | None
    small_scale_share: float | None
    aged_share: float | None
    recent_entry_share: float | None
    tourism_registration_share: float | None
    room_coverage: float
    age_coverage: float
    demand_value: float | None
    demand_per_100_rooms: float | None
    demand_score: float | None
    room_supply_score: float | None
    accessibility_score: float | None
    recommendation: Recommendation | None


def recommend_investment(metrics: OpportunityMetrics) -> Recommendation | None:
    """Return one traceable policy conclusion only when core evidence is usable."""
    if (
        metrics.demand_score is None
        or metrics.room_supply_score is None
        or metrics.room_coverage < 0.5
    ):
        return None
    high_demand = metrics.demand_score >= 70
    low_demand = metrics.demand_score < 30
    low_supply = metrics.room_supply_score <= 30
    high_supply = metrics.room_supply_score >= 70
    aged_cluster = (
        metrics.aged_share is not None
        and metrics.aged_share >= 0.5
        and metrics.facility_density >= 5
    )
    if high_demand and aged_cluster:
        return Recommendation("remodel", ("high_demand", "aged_facility_cluster"))
    if high_demand and low_supply:
        return Recommendation("new_supply", ("high_demand", "low_room_supply"))
    if (
        high_demand
        and metrics.small_scale_share is not None
        and metrics.small_scale_share >= 0.6
        and metrics.tourism_registration_share is not None
        and metrics.tourism_registration_share <= 0.1
    ):
        return Recommendation(
            "quality_upgrade",
            ("high_demand", "small_scale_cluster", "low_tourism_registration"),
        )
    if (
        low_demand
        and metrics.accessibility_score is not None
        and metrics.accessibility_score >= 70
    ):
        return Recommendation("content_first", ("high_accessibility", "low_demand"))
    if low_demand and high_supply:
        return Recommendation("investment_caution", ("low_demand", "high_supply"))
    return None


def build_grid_opportunities(
    grids: Sequence[GridCell],
    facilities: Sequence[FacilityPoint],
    *,
    demand_by_grid: Mapping[str, float],
    accessibility_by_grid: Mapping[str, float],
    as_of: date,
) -> tuple[GridOpportunity, ...]:
    """Aggregate facilities once into comparable, denominator-bound grid metrics."""
    by_grid: dict[str, list[FacilityPoint]] = defaultdict(list)
    for facility in facilities:
        by_grid[facility.grid_id].append(facility)

    raw: list[dict[str, object]] = []
    for grid in sorted(grids, key=lambda item: item.grid_id):
        if grid.area_km2 <= 0:
            raise ValueError("grid area_km2 must be positive")
        members = by_grid.get(grid.grid_id, [])
        rooms = [float(item.room_count) for item in members if item.room_count is not None]
        ages = [
            float(item.building_age_years)
            for item in members
            if item.building_age_years is not None
        ]
        known_rooms = sum(rooms)
        facility_count = len(members)
        room_coverage = len(rooms) / facility_count if facility_count else 0.0
        age_coverage = len(ages) / facility_count if facility_count else 0.0
        demand = demand_by_grid.get(grid.grid_id)
        room_density = known_rooms / grid.area_km2 if rooms else None
        demand_per_rooms = (
            float(demand) / known_rooms * 100
            if demand is not None and known_rooms > 0
            else None
        )
        recent_cutoff = date(as_of.year - 5, as_of.month, as_of.day)
        license_dates = [item.license_date for item in members if item.license_date]
        raw.append(
            {
                "grid": grid,
                "facility_count": facility_count,
                "known_rooms": known_rooms,
                "facility_density": facility_count / grid.area_km2,
                "room_density": room_density,
                "small_scale_share": _share(rooms, lambda value: value <= 20),
                "aged_share": _share(ages, lambda value: value >= 20),
                "recent_entry_share": _share(
                    license_dates,
                    lambda value, cutoff=recent_cutoff: value >= cutoff,
                ),
                "tourism_share": (
                    sum(item.tourism_registration for item in members) / facility_count
                    if facility_count
                    else None
                ),
                "room_coverage": room_coverage,
                "age_coverage": age_coverage,
                "demand": float(demand) if demand is not None else None,
                "demand_per_rooms": demand_per_rooms,
                "accessibility": accessibility_by_grid.get(grid.grid_id),
            }
        )

    demand_scores = _scores({str(item["grid"].grid_id): item["demand"] for item in raw})  # type: ignore[union-attr]
    supply_scores = _scores(
        {str(item["grid"].grid_id): item["room_density"] for item in raw}  # type: ignore[union-attr]
    )
    output: list[GridOpportunity] = []
    for item in raw:
        grid = item["grid"]
        assert isinstance(grid, GridCell)
        metrics = OpportunityMetrics(
            demand_score=demand_scores[grid.grid_id],
            room_supply_score=supply_scores[grid.grid_id],
            accessibility_score=_optional_float(item["accessibility"]),
            facility_density=float(item["facility_density"]),
            room_density=_optional_float(item["room_density"]),
            aged_share=_optional_float(item["aged_share"]),
            small_scale_share=_optional_float(item["small_scale_share"]),
            tourism_registration_share=_optional_float(item["tourism_share"]),
            recent_entry_share=_optional_float(item["recent_entry_share"]),
            demand_per_100_rooms=_optional_float(item["demand_per_rooms"]),
            room_coverage=float(item["room_coverage"]),
            age_coverage=float(item["age_coverage"]),
        )
        output.append(
            GridOpportunity(
                grid_id=grid.grid_id,
                district=grid.district,
                facility_count=int(item["facility_count"]),
                known_room_count=float(item["known_rooms"]),
                facility_density=metrics.facility_density,
                room_density=metrics.room_density,
                small_scale_share=metrics.small_scale_share,
                aged_share=metrics.aged_share,
                recent_entry_share=metrics.recent_entry_share,
                tourism_registration_share=metrics.tourism_registration_share,
                room_coverage=metrics.room_coverage,
                age_coverage=metrics.age_coverage,
                demand_value=_optional_float(item["demand"]),
                demand_per_100_rooms=metrics.demand_per_100_rooms,
                demand_score=metrics.demand_score,
                room_supply_score=metrics.room_supply_score,
                accessibility_score=metrics.accessibility_score,
                recommendation=recommend_investment(metrics),
            )
        )
    return tuple(output)


def _share(values: Sequence[object], predicate: object) -> float | None:
    if not values:
        return None
    matcher = predicate
    assert callable(matcher)
    return sum(bool(matcher(value)) for value in values) / len(values)


def _scores(values: Mapping[str, object]) -> dict[str, float | None]:
    numeric = sorted(
        float(value) for value in values.values() if value is not None
    )
    result: dict[str, float | None] = {}
    for key, value in values.items():
        if value is None:
            result[key] = None
            continue
        number = float(value)
        if len(numeric) == 1:
            result[key] = 50.0
            continue
        left = bisect.bisect_left(numeric, number)
        right = bisect.bisect_right(numeric, number) - 1
        rank = statistics.mean((left, right))
        result[key] = rank / (len(numeric) - 1) * 100
    return result


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)
