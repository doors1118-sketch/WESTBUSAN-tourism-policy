"""Pure, fail-closed facility coordinate resolution."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pyproj import Transformer
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

_PUBLIC_CRS = "EPSG:4326"
_PROJECTED_CRS = "EPSG:5174"
_GRID_SIZE_M = 500
_SOUTH_KOREA_BOUNDS = (124.0, 33.0, 132.0, 39.0)
_TO_PUBLIC = Transformer.from_crs(_PROJECTED_CRS, _PUBLIC_CRS, always_xy=True)
_TO_PROJECTED = Transformer.from_crs(_PUBLIC_CRS, _PROJECTED_CRS, always_xy=True)


@dataclass(frozen=True, slots=True)
class ResolvedPoint:
    """One accepted public point and its deterministic projected grid identity."""

    longitude: float
    latitude: float
    projected_x: float
    projected_y: float
    grid_id: str
    source_crs: str


@dataclass(frozen=True, slots=True)
class SpatialException:
    """A public-safe reason why a facility point was not accepted."""

    code: str
    evidence: tuple[tuple[str, str], ...] = ()


def resolve_facility_point(
    record: Mapping[str, object] | object,
    boundary: BaseGeometry,
) -> ResolvedPoint | SpatialException:
    """Resolve explicit WGS84 or EPSG:5174 coordinates without guessing.

    Grid IDs use ``floor(projected_coordinate / 500)``. Consequently, a point
    exactly on a cell's west or south edge belongs to the cell beginning at
    that edge; this is the deterministic half-open ``[origin, origin + 500)``
    convention used for point assignment. Boundary eligibility is independent
    and uses ``covers``, so points on the reviewed Busan outline remain eligible.
    """
    crs_value = _field(record, "coordinate_crs")
    if crs_value is None or not str(crs_value).strip():
        return SpatialException("UNKNOWN_CRS")
    crs = str(crs_value).strip()
    if crs == _PUBLIC_CRS:
        longitude = _number(_field(record, "longitude"))
        latitude = _number(_field(record, "latitude"))
        if longitude is None and latitude is None:
            if _has_value(record, "projected_x") or _has_value(record, "projected_y"):
                return SpatialException("CRS_MISMATCH")
            return SpatialException("MISSING_COORDINATES")
        if longitude is None or latitude is None:
            return SpatialException("INVALID_COORDINATES")
        projected_x, projected_y = _TO_PROJECTED.transform(longitude, latitude)
    elif crs == _PROJECTED_CRS:
        projected_x = _number(_field(record, "projected_x"))
        projected_y = _number(_field(record, "projected_y"))
        if projected_x is None and projected_y is None:
            if _has_value(record, "longitude") or _has_value(record, "latitude"):
                return SpatialException("CRS_MISMATCH")
            return SpatialException("MISSING_COORDINATES")
        if projected_x is None or projected_y is None:
            return SpatialException("INVALID_COORDINATES")
        longitude, latitude = _TO_PUBLIC.transform(projected_x, projected_y)
    else:
        return SpatialException("UNKNOWN_CRS", (("coordinate_crs", crs),))

    values = (longitude, latitude, projected_x, projected_y)
    if not all(math.isfinite(value) for value in values):
        return SpatialException("INVALID_COORDINATES")
    west, south, east, north = _SOUTH_KOREA_BOUNDS
    if not (west <= longitude <= east and south <= latitude <= north):
        return SpatialException("OUTSIDE_SOUTH_KOREA")
    if boundary.is_empty or not boundary.covers(Point(longitude, latitude)):
        return SpatialException("OUTSIDE_BUSAN")

    x_index = math.floor(projected_x / _GRID_SIZE_M)
    y_index = math.floor(projected_y / _GRID_SIZE_M)
    return ResolvedPoint(
        longitude=longitude,
        latitude=latitude,
        projected_x=projected_x,
        projected_y=projected_y,
        grid_id=f"g5174_500_{x_index}_{y_index}",
        source_crs=crs,
    )


def _field(record: Mapping[str, object] | object, name: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def _has_value(record: Mapping[str, object] | object, name: str) -> bool:
    return _field(record, name) is not None


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number
