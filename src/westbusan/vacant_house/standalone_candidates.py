"""Deterministic screening of large non-contiguous vacant parcels."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace

from pyproj import Transformer
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from westbusan.vacant_house.hub_models import (
    CadastralParcel,
    StandaloneCandidate,
    VacantParcel,
)

_WEST_BUSAN_DISTRICTS = frozenset({"26320", "26380", "26440", "26530"})
_SINGLE_FAMILY_TYPES = frozenset({"단독", "단독주택"})
_TO_METRES = Transformer.from_crs("EPSG:4326", "EPSG:5179", always_xy=True)


def build_standalone_candidates(
    cadastral_parcels: Sequence[CadastralParcel],
    inventory_by_pnu: Mapping[str, VacantParcel],
    *,
    excluded_pnus: Collection[str],
    district_demand_scores: Mapping[str, float],
    minimum_area: float = 300.0,
    limit: int = 6,
) -> tuple[StandaloneCandidate, ...]:
    """Return preliminary standalone candidates from reviewed non-hub parcels."""
    if not math.isfinite(minimum_area) or minimum_area <= 0:
        raise ValueError("invalid_standalone_minimum_area")
    if not 1 <= limit <= 6:
        raise ValueError("invalid_standalone_candidate_limit")

    excluded = frozenset(excluded_pnus)
    eligible: list[StandaloneCandidate] = []
    for cadastral in cadastral_parcels:
        inventory = inventory_by_pnu.get(cadastral.pnu)
        if (
            cadastral.district_code not in _WEST_BUSAN_DISTRICTS
            or cadastral.pnu in excluded
            or inventory is None
            or not _single_family_only(inventory)
            or not _valid_polygon(cadastral.geometry)
        ):
            continue
        parcel_area = _projected_area(cadastral.geometry)
        if parcel_area < minimum_area:
            continue
        demand_score = _optional_score(
            district_demand_scores.get(cadastral.district_code)
        )
        coverage = (
            ("district_visitor_demand",) if demand_score is not None else ()
        )
        missing = tuple(
            item
            for item in (
                "district_visitor_demand",
                "nearby_attractions",
                "transport_access",
            )
            if item not in coverage
        )
        candidate_id = "vh_standalone_" + hashlib.sha256(
            cadastral.pnu.encode("utf-8")
        ).hexdigest()[:16]
        eligible.append(
            StandaloneCandidate(
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
                context_coverage=coverage,
                missing_context=missing,
            )
        )
    ordered = sorted(eligible, key=_rank_key)[:limit]
    return tuple(
        replace(candidate, preliminary_rank=rank)
        for rank, candidate in enumerate(ordered, start=1)
    )


def _single_family_only(parcel: VacantParcel) -> bool:
    normalized = {
        value.replace(" ", "").strip() for value in parcel.housing_types if value.strip()
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
        raise ValueError("invalid_standalone_projected_area")
    return area


def _optional_score(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 100:
        raise ValueError("invalid_district_demand_score")
    return score


def _rank_key(candidate: StandaloneCandidate) -> tuple[object, ...]:
    score = candidate.district_demand_score
    return (
        score is None,
        -(score or 0.0),
        -round(candidate.parcel_area, 6),
        candidate.pnu,
    )


__all__ = ["build_standalone_candidates"]
