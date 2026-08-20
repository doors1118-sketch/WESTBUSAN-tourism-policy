"""Deterministic EPSG:5174 500 m grid materialization."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from uuid import UUID

from pyproj import Transformer
from shapely.geometry import box, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

from westbusan.config import SpatialConfig
from westbusan.db import Database
from westbusan.spatial.fencing import SpatialOperationLease
from westbusan.spatial.models import BoundaryApprovalError, GridBuildResult

_AREA_TIE_ABSOLUTE_TOLERANCE = 1e-5


def build_grid(
    db: Database,
    boundary_version_id: UUID,
    config: SpatialConfig,
) -> GridBuildResult:
    """Build or byte-verify one approved boundary's stable 500 m grid rows."""
    with SpatialOperationLease(db, "grid build") as lease:
        return _build_grid(db, boundary_version_id, config, lease)


def _build_grid(
    db: Database,
    boundary_version_id: UUID,
    config: SpatialConfig,
    lease: SpatialOperationLease,
) -> GridBuildResult:
    lease.refresh()
    boundary_rows = db.query(
        """select artifact.path, boundary.content_hash, artifact.content_hash
           from spatial_boundary_version as boundary
           join raw_artifact as artifact
             on artifact.artifact_id = boundary.raw_artifact_id
           where boundary.boundary_version_id = ?""",
        [boundary_version_id],
    )
    if not boundary_rows:
        raise BoundaryApprovalError("approved boundary version does not exist")
    artifact_path, boundary_hash, artifact_hash = boundary_rows[0]
    body = Path(artifact_path).read_bytes()
    observed_hash = hashlib.sha256(body).hexdigest()
    if observed_hash != boundary_hash or observed_hash != artifact_hash:
        raise BoundaryApprovalError("approved boundary artifact integrity mismatch")
    document = json.loads(body.decode("utf-8"))

    to_projected = Transformer.from_crs(
        config.crs_public, config.crs_projected, always_xy=True
    )
    to_public = Transformer.from_crs(
        config.crs_projected, config.crs_public, always_xy=True
    )
    dong_parts: dict[tuple[str, str, str], list[BaseGeometry]] = defaultdict(list)
    for feature in document["features"]:
        properties = feature["properties"]
        key = (
            properties["district"].strip(),
            properties["dong_code"].strip(),
            properties["dong_name"].strip(),
        )
        dong_parts[key].append(
            transform(to_projected.transform, shape(feature["geometry"]))
        )
    dongs = [
        (district, code, name, unary_union(parts))
        for (district, code, name), parts in sorted(dong_parts.items())
    ]
    busan = unary_union([dong[3] for dong in dongs])
    if busan.is_empty or busan.area <= 0:
        raise BoundaryApprovalError("approved boundary has no positive projected area")

    size = config.grid_size_m
    min_x = math.floor(busan.bounds[0] / size) * size
    min_y = math.floor(busan.bounds[1] / size) * size
    max_x = math.ceil(busan.bounds[2] / size) * size
    max_y = math.ceil(busan.bounds[3] / size) * size
    rows: list[tuple[object, ...]] = []
    for origin_x in range(min_x, max_x, size):
        lease.refresh()
        for origin_y in range(min_y, max_y, size):
            square = box(origin_x, origin_y, origin_x + size, origin_y + size)
            clipped = square.intersection(busan)
            if clipped.is_empty or clipped.area <= 0:
                continue
            rows.append(
                _build_row(
                    boundary_version_id,
                    origin_x,
                    origin_y,
                    size,
                    square,
                    clipped,
                    dongs,
                    to_public,
                )
            )
    rows.sort(key=lambda row: (int(row[2]), int(row[3])))
    if not rows:
        raise BoundaryApprovalError("approved boundary generated no positive-area cells")

    existing = db.query(
        """select boundary_version_id, grid_id, x_index, y_index, district_code,
                  district_name, primary_dong_code, primary_dong_name,
                  centroid_projected_x, centroid_projected_y,
                  centroid_wgs84_longitude, centroid_wgs84_latitude,
                  geometry_geojson, overlap_evidence_json, clipped_area_ratio
           from dim_spatial_grid_500m where boundary_version_id = ?
           order by x_index, y_index""",
        [boundary_version_id],
    )
    if existing:
        if existing != rows:
            raise BoundaryApprovalError(
                "existing grid rows differ from deterministic rebuild"
            )
    else:
        lease.commit(
            lambda: db.connection.executemany(
                """insert into dim_spatial_grid_500m (
                       boundary_version_id, grid_id, x_index, y_index, district_code,
                       district_name, primary_dong_code, primary_dong_name,
                       centroid_projected_x, centroid_projected_y,
                       centroid_wgs84_longitude, centroid_wgs84_latitude,
                       geometry_geojson, overlap_evidence_json, clipped_area_ratio
                   ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        )
    digest = hashlib.sha256(_canonical_json(rows).encode("utf-8")).hexdigest()
    return GridBuildResult(boundary_version_id, len(rows), digest)


def _build_row(
    boundary_version_id: UUID,
    origin_x: int,
    origin_y: int,
    size: int,
    square: BaseGeometry,
    clipped: BaseGeometry,
    dongs: list[tuple[str, str, str, BaseGeometry]],
    to_public: Transformer,
) -> tuple[object, ...]:
    overlaps: list[tuple[str, str, str, float]] = []
    for district, code, name, geometry in dongs:
        area = square.intersection(geometry).area
        if area > 0:
            overlaps.append((district, code, name, area))
    total_area = clipped.area
    ratio_sum = sum(overlap[3] / total_area for overlap in overlaps)
    if not math.isclose(ratio_sum, 1.0, rel_tol=1e-9, abs_tol=1e-9):
        raise BoundaryApprovalError(
            "dong overlap ratios do not sum approximately to one"
        )

    district_areas: dict[str, float] = defaultdict(float)
    for district, _code, _name, area in overlaps:
        district_areas[district] += area
    maximum_district_area = max(district_areas.values())
    tied_districts = {
        name
        for name, area in district_areas.items()
        if math.isclose(
            area,
            maximum_district_area,
            rel_tol=1e-12,
            abs_tol=_AREA_TIE_ABSOLUTE_TOLERANCE,
        )
    }
    district = min(
        tied_districts,
        key=lambda candidate: min(
            (code, name)
            for dong_district, code, name, _area in overlaps
            if dong_district == candidate
        ),
    )

    district_overlaps = [
        overlap for overlap in overlaps if overlap[0] == district
    ]
    maximum_dong_area = max(overlap[3] for overlap in district_overlaps)
    primary_candidates = [
        overlap
        for overlap in district_overlaps
        if math.isclose(
            overlap[3],
            maximum_dong_area,
            rel_tol=1e-12,
            abs_tol=_AREA_TIE_ABSOLUTE_TOLERANCE,
        )
    ]
    primary = min(primary_candidates, key=lambda overlap: (overlap[1], overlap[2]))

    dong_evidence = [
        {
            "district": item[0],
            "dong_code": item[1],
            "dong_name": item[2],
            "intersection_area_m2": round(item[3], 9),
            "overlap_ratio": round(item[3] / total_area, 12),
        }
        for item in sorted(overlaps, key=lambda item: (item[1], item[2]))
    ]
    overlap_evidence = _canonical_json(
        {
            "dong_overlaps": dong_evidence,
            "ratio_sum": round(sum(item["overlap_ratio"] for item in dong_evidence), 12),
        }
    )
    centroid_x = origin_x + size / 2
    centroid_y = origin_y + size / 2
    longitude, latitude = to_public.transform(centroid_x, centroid_y)
    public_geometry = transform(to_public.transform, clipped).normalize()
    geometry_geojson = _canonical_json(_round_coordinates(mapping(public_geometry)))
    x_index = origin_x // size
    y_index = origin_y // size
    return (
        boundary_version_id,
        f"g5174_500_{x_index}_{y_index}",
        x_index,
        y_index,
        None,
        district,
        primary[1],
        primary[2],
        centroid_x,
        centroid_y,
        longitude,
        latitude,
        geometry_geojson,
        overlap_evidence,
        round(total_area / square.area, 12),
    )


def _round_coordinates(value: object) -> object:
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, tuple):
        return [_round_coordinates(item) for item in value]
    if isinstance(value, list):
        return [_round_coordinates(item) for item in value]
    if isinstance(value, dict):
        return {key: _round_coordinates(item) for key, item in value.items()}
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
