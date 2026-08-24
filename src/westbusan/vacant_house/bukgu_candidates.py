"""Reviewed North District supplements using published demand and place proximity."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from importlib import resources

from pyproj import Transformer
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from westbusan.vacant_house.hub_models import CadastralParcel, VacantParcel

_BUKGU_CODE = "26320"
_SINGLE_FAMILY_TYPES = frozenset({"단독", "단독주택"})
_TO_METRES = Transformer.from_crs("EPSG:4326", "EPSG:5179", always_xy=True)
_EARTH_RADIUS_METRES = 6_371_008.8


@dataclass(frozen=True, slots=True)
class PlaceAnchor:
    """One reviewed WGS84 station or attraction point."""

    name: str
    longitude: float
    latitude: float

    def __post_init__(self) -> None:
        if (
            not self.name.strip()
            or not math.isfinite(self.longitude)
            or not math.isfinite(self.latitude)
            or not 128.8 <= self.longitude <= 129.1
            or not 35.15 <= self.latitude <= 35.35
        ):
            raise ValueError("invalid_bukgu_place_anchor")


@dataclass(frozen=True, slots=True)
class BukguSupplementalCandidate:
    """Preliminary non-hub candidate supported by demand and place proximity."""

    preliminary_rank: int
    candidate_id: str
    pnu: str
    district_code: str
    legal_dong_code: str
    geometry: BaseGeometry
    parcel_area: float
    source_record_count: int
    housing_types: tuple[str, ...]
    district_demand_score: float
    nearest_station: str
    station_distance_metres: float
    nearest_attraction: str
    attraction_distance_metres: float
    composite_score: float
    context_coverage: tuple[str, ...]
    missing_context: tuple[str, ...]

    @property
    def candidate_class(self) -> str:
        return "bukgu_supplemental_preliminary"


def load_bukgu_context_anchors() -> tuple[
    tuple[PlaceAnchor, ...], tuple[PlaceAnchor, ...], dict[str, object]
]:
    """Load reviewed VWorld points packaged with explicit provenance."""
    document = json.loads(
        resources.files("westbusan.vacant_house")
        .joinpath("reference/bukgu_candidate_anchors.json")
        .read_text(encoding="utf-8")
    )
    stations = tuple(
        PlaceAnchor(str(row["name"]), float(row["longitude"]), float(row["latitude"]))
        for row in document["stations"]
    )
    attractions = tuple(
        PlaceAnchor(str(row["name"]), float(row["longitude"]), float(row["latitude"]))
        for row in document["attractions"]
    )
    provenance = {
        "provider": str(document["provider"]),
        "tourism_name_source": str(document["tourism_name_source"]),
        "transport_flow_status": str(document["transport_flow_status"]),
        "verified_date": str(document["verified_date"]),
        "crs": str(document["crs"]),
        "distance_method": str(document["distance_method"]),
    }
    return stations, attractions, provenance


def build_bukgu_supplemental_candidates(
    cadastral_parcels: Sequence[CadastralParcel],
    inventory_by_pnu: Mapping[str, VacantParcel],
    *,
    excluded_pnus: Collection[str],
    district_demand_scores: Mapping[str, float],
    station_anchors: Sequence[PlaceAnchor],
    attraction_anchors: Sequence[PlaceAnchor],
    minimum_area: float = 300.0,
    limit: int = 5,
) -> tuple[BukguSupplementalCandidate, ...]:
    """Return North supplements without treating proximity as transit ridership."""
    if not math.isfinite(minimum_area) or minimum_area <= 0:
        raise ValueError("invalid_bukgu_minimum_area")
    if not 1 <= limit <= 5:
        raise ValueError("invalid_bukgu_candidate_limit")
    if not station_anchors or not attraction_anchors:
        raise ValueError("bukgu_context_anchors_required")
    demand_score = _optional_score(district_demand_scores.get(_BUKGU_CODE))
    if demand_score is None:
        return ()

    excluded = frozenset(excluded_pnus)
    eligible: list[BukguSupplementalCandidate] = []
    for cadastral in cadastral_parcels:
        inventory = inventory_by_pnu.get(cadastral.pnu)
        if (
            cadastral.district_code != _BUKGU_CODE
            or cadastral.pnu in excluded
            or inventory is None
            or not _single_family_only(inventory)
            or not _valid_polygon(cadastral.geometry)
        ):
            continue
        parcel_area = _projected_area(cadastral.geometry)
        if parcel_area < minimum_area:
            continue
        point = cadastral.geometry.representative_point()
        station, station_distance = _nearest_anchor(
            point.x, point.y, station_anchors
        )
        attraction, attraction_distance = _nearest_anchor(
            point.x, point.y, attraction_anchors
        )
        score = _composite_score(
            parcel_area=parcel_area,
            station_distance=station_distance,
            attraction_distance=attraction_distance,
            district_demand_score=demand_score,
        )
        candidate_id = "vh_bukgu_" + hashlib.sha256(
            cadastral.pnu.encode("utf-8")
        ).hexdigest()[:16]
        eligible.append(
            BukguSupplementalCandidate(
                preliminary_rank=0,
                candidate_id=candidate_id,
                pnu=cadastral.pnu,
                district_code=cadastral.district_code,
                legal_dong_code=cadastral.legal_dong_code,
                geometry=cadastral.geometry,
                parcel_area=parcel_area,
                source_record_count=inventory.source_record_count,
                housing_types=inventory.housing_types,
                district_demand_score=demand_score,
                nearest_station=station.name,
                station_distance_metres=station_distance,
                nearest_attraction=attraction.name,
                attraction_distance_metres=attraction_distance,
                composite_score=score,
                context_coverage=(
                    "district_visitor_demand",
                    "nearby_attractions",
                    "station_proximity",
                ),
                missing_context=("transport_flow",),
            )
        )
    ordered = sorted(
        eligible,
        key=lambda candidate: (
            -round(candidate.composite_score, 6),
            -round(candidate.parcel_area, 6),
            candidate.pnu,
        ),
    )[:limit]
    return tuple(
        replace(candidate, preliminary_rank=rank)
        for rank, candidate in enumerate(ordered, start=1)
    )


def _single_family_only(parcel: VacantParcel) -> bool:
    normalized = {
        value.replace(" ", "").strip()
        for value in parcel.housing_types
        if value.strip()
    }
    return bool(normalized) and normalized <= _SINGLE_FAMILY_TYPES


def _valid_polygon(geometry: BaseGeometry) -> bool:
    return (
        geometry.geom_type in {"Polygon", "MultiPolygon"}
        and not geometry.is_empty
        and geometry.is_valid
        and geometry.area > 0
        and all(math.isfinite(value) for value in geometry.bounds)
    )


def _projected_area(geometry: BaseGeometry) -> float:
    area = float(transform(_TO_METRES.transform, geometry).area)
    if not math.isfinite(area) or area <= 0:
        raise ValueError("invalid_bukgu_projected_area")
    return area


def _optional_score(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 100:
        raise ValueError("invalid_district_demand_score")
    return score


def _nearest_anchor(
    longitude: float,
    latitude: float,
    anchors: Sequence[PlaceAnchor],
) -> tuple[PlaceAnchor, float]:
    return min(
        (
            (anchor, _haversine(longitude, latitude, anchor.longitude, anchor.latitude))
            for anchor in anchors
        ),
        key=lambda item: (item[1], item[0].name),
    )


def _haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    lon1r, lat1r, lon2r, lat2r = map(math.radians, (lon1, lat1, lon2, lat2))
    delta_lon = lon2r - lon1r
    delta_lat = lat2r - lat1r
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1r) * math.cos(lat2r) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_METRES * math.asin(math.sqrt(value))


def _composite_score(
    *,
    parcel_area: float,
    station_distance: float,
    attraction_distance: float,
    district_demand_score: float,
) -> float:
    area_score = min(parcel_area / 1_000.0 * 100.0, 100.0)
    station_score = max(0.0, 100.0 * (1.0 - station_distance / 3_000.0))
    attraction_score = max(0.0, 100.0 * (1.0 - attraction_distance / 5_000.0))
    return round(
        area_score * 0.35
        + station_score * 0.25
        + attraction_score * 0.25
        + district_demand_score * 0.15,
        1,
    )


__all__ = [
    "BukguSupplementalCandidate",
    "PlaceAnchor",
    "build_bukgu_supplemental_candidates",
    "load_bukgu_context_anchors",
]
