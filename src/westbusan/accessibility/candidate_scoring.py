"""Shared, type-aware access scoring for published map candidates."""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pyproj import Transformer
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

_TO_5179 = Transformer.from_crs("EPSG:4326", "EPSG:5179", always_xy=True)
_ANCHOR_TYPES = frozenset({"12", "14", "15", "25", "28"})
_SUPPORT_TYPES = frozenset({"38", "39"})
_EXCLUDED_TYPES = frozenset({"32"})


@dataclass(frozen=True, slots=True)
class AccessScoringCandidate:
    candidate_id: str
    geometry: BaseGeometry
    base_value: float
    district_names: tuple[str, ...]
    dong_names: tuple[str, ...]
    visitor_score: float | None = None


@dataclass(frozen=True, slots=True)
class CandidateScoreWeights:
    base: float
    transport: float
    tourism: float
    visitor: float

    def __post_init__(self) -> None:
        values = (self.base, self.transport, self.tourism, self.visitor)
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("candidate score weights must be finite and non-negative")
        if not math.isclose(sum(values), 1.0, abs_tol=1e-9):
            raise ValueError("candidate score weights must sum to one")


@dataclass(frozen=True, slots=True)
class CandidateAccessScore:
    candidate_id: str
    base_score: float
    transport_score: float | None
    tourism_score: float | None
    visitor_score: float | None
    weighted_score: float | None
    ranking_eligible: bool
    transport_period: str | None
    transport_inbound: float | None
    tourism_poi_count_1000m: int | None
    nearest_tourism_poi_name: str | None
    nearest_tourism_poi_distance_m: float | None


@dataclass(frozen=True, slots=True)
class _RawAccess:
    candidate: AccessScoringCandidate
    transport_period: str | None
    transport_inbound: float | None
    tourism_raw: float | None
    tourism_poi_count_1000m: int | None
    nearest_tourism_poi_name: str | None
    nearest_tourism_poi_distance_m: float | None


def score_access_candidates(
    candidates: Sequence[AccessScoringCandidate],
    access_features: Sequence[Mapping[str, object]],
    *,
    weights: CandidateScoreWeights,
) -> tuple[CandidateAccessScore, ...]:
    """Normalize comparable evidence and apply only complete weighted scores."""
    if not candidates:
        return ()
    identifiers = [item.candidate_id for item in candidates]
    if any(not value.strip() for value in identifiers) or len(set(identifiers)) != len(
        identifiers
    ):
        raise ValueError("candidate ids must be non-empty and unique")
    for item in candidates:
        if item.geometry.is_empty or not math.isfinite(float(item.base_value)):
            raise ValueError("candidate geometry and base value are required")
        if item.visitor_score is not None and not 0 <= item.visitor_score <= 100:
            raise ValueError("visitor score must be within 0..100")

    transports = _latest_transport(access_features)
    pois = _demand_pois(access_features)
    tourism_available = bool(pois)
    raw: list[_RawAccess] = []
    for candidate in candidates:
        transport = _candidate_transport(candidate, transports)
        tourism = _tourism_context(candidate.geometry, pois) if tourism_available else None
        raw.append(
            _RawAccess(
                candidate=candidate,
                transport_period=transport[0] if transport else None,
                transport_inbound=transport[1] if transport else None,
                tourism_raw=tourism[0] if tourism else None,
                tourism_poi_count_1000m=tourism[1] if tourism else None,
                nearest_tourism_poi_name=tourism[2] if tourism else None,
                nearest_tourism_poi_distance_m=tourism[3] if tourism else None,
            )
        )

    base_values = sorted(float(item.candidate.base_value) for item in raw)
    transport_values = sorted(
        float(item.transport_inbound)
        for item in raw
        if item.transport_inbound is not None
    )
    tourism_values = sorted(
        float(item.tourism_raw) for item in raw if item.tourism_raw is not None
    )
    scored: list[CandidateAccessScore] = []
    for item in raw:
        base_score = _percentile(float(item.candidate.base_value), base_values)
        transport_score = _percentile(item.transport_inbound, transport_values)
        tourism_score = _percentile(item.tourism_raw, tourism_values)
        visitor_score = item.candidate.visitor_score
        required = (
            (weights.transport, transport_score),
            (weights.tourism, tourism_score),
            (weights.visitor, visitor_score),
        )
        eligible = all(weight == 0 or value is not None for weight, value in required)
        weighted = None
        if eligible:
            weighted = (
                base_score * weights.base
                + float(transport_score or 0.0) * weights.transport
                + float(tourism_score or 0.0) * weights.tourism
                + float(visitor_score or 0.0) * weights.visitor
            )
        scored.append(
            CandidateAccessScore(
                candidate_id=item.candidate.candidate_id,
                base_score=round(base_score, 3),
                transport_score=(
                    round(transport_score, 3) if transport_score is not None else None
                ),
                tourism_score=(
                    round(tourism_score, 3) if tourism_score is not None else None
                ),
                visitor_score=visitor_score,
                weighted_score=round(weighted, 3) if weighted is not None else None,
                ranking_eligible=eligible,
                transport_period=item.transport_period,
                transport_inbound=item.transport_inbound,
                tourism_poi_count_1000m=item.tourism_poi_count_1000m,
                nearest_tourism_poi_name=item.nearest_tourism_poi_name,
                nearest_tourism_poi_distance_m=(
                    round(item.nearest_tourism_poi_distance_m, 1)
                    if item.nearest_tourism_poi_distance_m is not None
                    else None
                ),
            )
        )
    return tuple(scored)


def _latest_transport(
    features: Sequence[Mapping[str, object]],
) -> dict[tuple[str, str], tuple[str, float]]:
    result: dict[tuple[str, str], tuple[str, float]] = {}
    for feature in features:
        properties = feature.get("properties")
        if not isinstance(properties, Mapping) or properties.get("kind") != "transport_dong":
            continue
        district = _place(properties.get("district_name"))
        dong = _place(properties.get("dong_name"))
        period = str(properties.get("period") or "")
        raw_value = properties.get("inbound_other_district")
        if not district or not dong or not period or raw_value is None:
            continue
        value = float(raw_value)
        if not math.isfinite(value) or value < 0:
            continue
        key = (district, dong)
        if key not in result or period > result[key][0]:
            result[key] = (period, value)
    return result


def _candidate_transport(
    candidate: AccessScoringCandidate,
    transport: Mapping[tuple[str, str], tuple[str, float]],
) -> tuple[str, float] | None:
    matches = [
        transport[(district, dong)]
        for district in map(_place, candidate.district_names)
        for dong in map(_place, candidate.dong_names)
        if (district, dong) in transport
    ]
    if not matches:
        return None
    latest_period = max(item[0] for item in matches)
    latest = [item for item in matches if item[0] == latest_period]
    return latest_period, max(item[1] for item in latest)


def _demand_pois(
    features: Sequence[Mapping[str, object]],
) -> tuple[tuple[str, Point, float], ...]:
    result: list[tuple[str, Point, float]] = []
    for feature in features:
        properties = feature.get("properties")
        geometry = feature.get("geometry")
        if (
            not isinstance(properties, Mapping)
            or properties.get("kind") != "tourism_poi"
            or not isinstance(geometry, Mapping)
            or geometry.get("type") != "Point"
        ):
            continue
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, Sequence) or len(coordinates) < 2:
            continue
        content_type = str(
            properties.get("content_type_id")
            or properties.get("category_name")
            or properties.get("category_code")
            or ""
        )
        weight = _poi_weight(content_type)
        if weight <= 0:
            continue
        longitude, latitude = float(coordinates[0]), float(coordinates[1])
        if not all(math.isfinite(value) for value in (longitude, latitude)):
            continue
        name = str(properties.get("title") or "관광지").strip() or "관광지"
        projected = transform(_TO_5179.transform, Point(longitude, latitude))
        result.append((name, projected, weight))
    return tuple(result)


def _tourism_context(
    geometry: BaseGeometry,
    pois: tuple[tuple[str, Point, float], ...],
) -> tuple[float, int, str | None, float | None]:
    projected = transform(_TO_5179.transform, geometry)
    distances = [(name, projected.distance(point), weight) for name, point, weight in pois]
    within = [item for item in distances if item[1] <= 1000.0]
    nearest = min(distances, key=lambda item: item[1], default=None)
    raw = sum(weight * max(0.0, 1.0 - distance / 1000.0) for _, distance, weight in within)
    return (
        raw,
        len(within),
        nearest[0] if nearest else None,
        nearest[1] if nearest else None,
    )


def _poi_weight(content_type: str) -> float:
    if content_type in _ANCHOR_TYPES:
        return 1.0
    if content_type in _SUPPORT_TYPES:
        return 0.35
    if content_type in _EXCLUDED_TYPES:
        return 0.0
    return 0.0


def _percentile(value: float | None, comparable: Sequence[float]) -> float | None:
    if value is None or not comparable:
        return None
    if len(comparable) == 1:
        return 50.0
    left = bisect.bisect_left(comparable, float(value))
    right = bisect.bisect_right(comparable, float(value)) - 1
    return ((left + right) / 2) / (len(comparable) - 1) * 100.0


def _place(value: object) -> str:
    return "".join(str(value or "").split())


__all__ = [
    "AccessScoringCandidate",
    "CandidateAccessScore",
    "CandidateScoreWeights",
    "score_access_candidates",
]
