"""Deterministic contiguous cadastral parcel hub construction."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence

from pyproj import Transformer
from shapely import STRtree
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

from westbusan.vacant_house.hub_models import CadastralParcel, VacantHub

_WEST_BUSAN_DISTRICTS = frozenset({"26320", "26380", "26440", "26530"})
_TO_METRES = Transformer.from_crs("EPSG:4326", "EPSG:5179", always_xy=True)
_PRECISION_TOLERANCE_METRES = 0.05


def build_contiguous_hubs(
    parcels: Sequence[CadastralParcel],
    context: Mapping[str, Mapping[str, object]],
    minimum_parcels: int = 3,
    limit: int = 10,
) -> tuple[VacantHub, ...]:
    """Return globally ranked West Busan components of touching parcel polygons."""
    if minimum_parcels < 3:
        raise ValueError("minimum_parcels_below_policy_floor")
    if not 1 <= limit <= 10:
        raise ValueError("invalid_hub_limit")
    _require_distinct_pnus(parcels)
    reviewed = tuple(
        sorted(
            (
                parcel
                for parcel in parcels
                if parcel.district_code in _WEST_BUSAN_DISTRICTS
            ),
            key=lambda parcel: parcel.pnu,
        )
    )
    for parcel in reviewed:
        _validate_geometry(parcel.geometry)
    if not reviewed:
        return ()

    projected = tuple(
        transform(_TO_METRES.transform, parcel.geometry) for parcel in reviewed
    )
    graph = _adjacency_graph(projected, _PRECISION_TOLERANCE_METRES)
    components = _connected_components(graph)
    eligible = tuple(
        _build_hub(tuple(reviewed[index] for index in component), context)
        for component in components
        if len(component) >= minimum_parcels
    )
    return tuple(sorted(eligible, key=_stable_rank_key)[:limit])


def _require_distinct_pnus(parcels: Sequence[CadastralParcel]) -> None:
    seen: set[str] = set()
    for parcel in parcels:
        if parcel.pnu in seen:
            raise ValueError("duplicate_pnu")
        seen.add(parcel.pnu)


def _validate_geometry(geometry: BaseGeometry) -> None:
    if (
        geometry.geom_type not in {"Polygon", "MultiPolygon"}
        or geometry.is_empty
        or not geometry.is_valid
        or geometry.area <= 0
        or not all(math.isfinite(value) for value in geometry.bounds)
    ):
        raise ValueError("invalid_cadastral_geometry")


def _adjacency_graph(
    geometries: Sequence[BaseGeometry],
    tolerance: float,
) -> tuple[tuple[int, ...], ...]:
    tree = STRtree(geometries)
    neighbours: list[set[int]] = [set() for _ in geometries]
    for left_index, left in enumerate(geometries):
        query_area = left.buffer(tolerance).envelope
        for raw_right_index in tree.query(query_area):
            right_index = int(raw_right_index)
            if right_index <= left_index:
                continue
            right = geometries[right_index]
            if _connected(left, right, tolerance):
                neighbours[left_index].add(right_index)
                neighbours[right_index].add(left_index)
    return tuple(tuple(sorted(values)) for values in neighbours)


def _connected(
    left: BaseGeometry,
    right: BaseGeometry,
    tolerance: float,
) -> bool:
    return left.intersects(right) or left.boundary.distance(right.boundary) <= tolerance


def _connected_components(
    graph: Sequence[Sequence[int]],
) -> tuple[tuple[int, ...], ...]:
    remaining = set(range(len(graph)))
    components: list[tuple[int, ...]] = []
    while remaining:
        root = min(remaining)
        remaining.remove(root)
        stack = [root]
        component: list[int] = []
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbour in reversed(graph[current]):
                if neighbour in remaining:
                    remaining.remove(neighbour)
                    stack.append(neighbour)
        components.append(tuple(sorted(component)))
    return tuple(components)


def _build_hub(
    parcels: Sequence[CadastralParcel],
    context: Mapping[str, Mapping[str, object]],
) -> VacantHub:
    pnus = tuple(sorted(parcel.pnu for parcel in parcels))
    wgs84_union = unary_union([parcel.geometry for parcel in parcels])
    projected_union = transform(_TO_METRES.transform, wgs84_union)
    hub_id = "vh_" + hashlib.sha256("|".join(pnus).encode("utf-8")).hexdigest()[:20]
    return VacantHub(
        hub_id=hub_id,
        pnus=pnus,
        district_codes=tuple(sorted({parcel.district_code for parcel in parcels})),
        legal_dong_codes=tuple(
            sorted({parcel.legal_dong_code for parcel in parcels})
        ),
        geometry=wgs84_union,
        union_area=float(projected_union.area),
        context=_aggregate_context(pnus, context),
    )


def _aggregate_context(
    pnus: Sequence[str],
    context: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    rows = tuple(context[pnu] for pnu in pnus if pnu in context)
    numeric: dict[str, list[float]] = {}
    for row in rows:
        for key, value in row.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            number = float(value)
            if math.isfinite(number):
                numeric.setdefault(str(key), []).append(number)
    aggregate: dict[str, object] = {
        "context_covered_parcels": len(rows),
        "context_score": sum(
            sum(values)
            for key, values in numeric.items()
            if key.endswith("_score")
        ),
    }
    for key in sorted(numeric):
        values = numeric[key]
        aggregate[f"{key}_mean"] = sum(values) / len(values)
    return aggregate


def _stable_rank_key(hub: VacantHub) -> tuple[object, ...]:
    score = hub.context.get("context_score", 0.0)
    numeric_score = float(score) if isinstance(score, (int, float)) else 0.0
    return (
        -hub.parcel_count,
        -round(hub.union_area, 6),
        -numeric_score,
        hub.hub_id,
    )


__all__ = ["build_contiguous_hubs"]
