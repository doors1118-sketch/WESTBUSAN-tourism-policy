"""Projected-distance accessibility evidence and guarded candidate ranking."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from pyproj import Transformer
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from westbusan.accessibility.transport import DongTransportMetric

_TO_5179 = Transformer.from_crs("EPSG:4326", "EPSG:5179", always_xy=True)


@dataclass(frozen=True, slots=True)
class AccessPoint:
    """One reviewed public point used only for distance and count evidence."""

    name: str
    longitude: float
    latitude: float
    kind: Literal["tourism_poi", "transport_hub"]


@dataclass(frozen=True, slots=True)
class AccessibilityEvidence:
    """Comparable evidence for one grid or vacant-house candidate geometry."""

    poi_count_1km: int
    nearest_poi_name: str | None
    nearest_poi_distance_m: float | None
    nearest_hub_name: str | None
    nearest_hub_distance_m: float | None
    transport_period: str | None
    transport_inbound_other_dong: float | None
    transport_inbound_other_district: float | None
    visitor_context: float | None
    visitor_context_scope: str
    coverage_status: str


@dataclass(frozen=True, slots=True)
class VacantCandidateEvidence:
    """Normalized components for one already-reviewed vacant candidate."""

    candidate_id: str
    existing_rank: int
    parcel_score: float
    transport_score: float | None
    tourism_score: float | None
    visitor_score: float | None


@dataclass(frozen=True, slots=True)
class RankedVacantCandidate:
    candidate_id: str
    weighted_score: float
    previous_rank: int


@dataclass(frozen=True, slots=True)
class CandidateRankingResult:
    status: Literal["ranked", "evidence_only", "empty"]
    ranked_candidates: tuple[RankedVacantCandidate, ...]
    original_candidate_ids: tuple[str, ...]


def measure_accessibility(
    subject_geometry: BaseGeometry,
    *,
    pois: tuple[AccessPoint, ...],
    hubs: tuple[AccessPoint, ...],
    transport: DongTransportMetric | None,
    visitor_context: float | None,
) -> AccessibilityEvidence:
    """Measure in metres using EPSG:5179; do not synthesize missing values."""
    if subject_geometry.is_empty:
        raise ValueError("subject geometry must not be empty")
    subject = transform(_TO_5179.transform, subject_geometry)
    poi_distances = _distances(subject, pois, expected_kind="tourism_poi")
    hub_distances = _distances(subject, hubs, expected_kind="transport_hub")
    nearest_poi = min(poi_distances, key=lambda item: item[1], default=None)
    nearest_hub = min(hub_distances, key=lambda item: item[1], default=None)
    missing_transport = transport is None
    missing_tourism = not pois
    if missing_transport and missing_tourism:
        coverage = "missing_transport_and_tourism"
    elif missing_transport:
        coverage = "missing_transport"
    elif missing_tourism:
        coverage = "missing_tourism"
    else:
        coverage = "complete"
    return AccessibilityEvidence(
        poi_count_1km=sum(distance <= 1000.0 for _, distance in poi_distances),
        nearest_poi_name=nearest_poi[0].name if nearest_poi else None,
        nearest_poi_distance_m=nearest_poi[1] if nearest_poi else None,
        nearest_hub_name=nearest_hub[0].name if nearest_hub else None,
        nearest_hub_distance_m=nearest_hub[1] if nearest_hub else None,
        transport_period=transport.period if transport else None,
        transport_inbound_other_dong=(
            transport.inbound_from_other_dong if transport else None
        ),
        transport_inbound_other_district=(
            transport.inbound_from_other_district if transport else None
        ),
        visitor_context=visitor_context,
        visitor_context_scope="district",
        coverage_status=coverage,
    )


def rank_vacant_candidates(
    candidates: tuple[VacantCandidateEvidence, ...],
) -> CandidateRankingResult:
    """Apply approved weights only when every compared candidate is complete."""
    original = tuple(
        item.candidate_id for item in sorted(candidates, key=lambda item: item.existing_rank)
    )
    if not candidates:
        return CandidateRankingResult("empty", (), ())
    for item in candidates:
        _validate_candidate(item)
    if any(
        component is None
        for item in candidates
        for component in (
            item.transport_score,
            item.tourism_score,
            item.visitor_score,
        )
    ):
        return CandidateRankingResult("evidence_only", (), original)
    ranked = tuple(
        sorted(
            (
                RankedVacantCandidate(
                    candidate_id=item.candidate_id,
                    weighted_score=(
                        item.parcel_score * 0.45
                        + float(item.transport_score) * 0.20
                        + float(item.tourism_score) * 0.20
                        + float(item.visitor_score) * 0.15
                    ),
                    previous_rank=item.existing_rank,
                )
                for item in candidates
            ),
            key=lambda item: (-item.weighted_score, item.previous_rank, item.candidate_id),
        )
    )
    return CandidateRankingResult("ranked", ranked, original)


def _distances(
    subject: BaseGeometry,
    points: tuple[AccessPoint, ...],
    *,
    expected_kind: str,
) -> tuple[tuple[AccessPoint, float], ...]:
    rows: list[tuple[AccessPoint, float]] = []
    for point in points:
        if point.kind != expected_kind:
            raise ValueError(f"expected {expected_kind} point")
        if not point.name.strip():
            raise ValueError("access point name must not be empty")
        if not all(math.isfinite(value) for value in (point.longitude, point.latitude)):
            raise ValueError("access point coordinate must be finite")
        projected = transform(
            _TO_5179.transform, Point(point.longitude, point.latitude)
        )
        rows.append((point, subject.distance(projected)))
    return tuple(rows)


def _validate_candidate(item: VacantCandidateEvidence) -> None:
    if not item.candidate_id.strip() or item.existing_rank < 1:
        raise ValueError("candidate identity and existing rank are required")
    values = (
        item.parcel_score,
        item.transport_score,
        item.tourism_score,
        item.visitor_score,
    )
    if any(
        value is not None and (not math.isfinite(value) or not 0.0 <= value <= 100.0)
        for value in values
    ):
        raise ValueError("candidate scores must be within 0..100")
