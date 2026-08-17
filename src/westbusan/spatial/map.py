"""Deterministic, standalone inline-SVG rendering for public spatial data."""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Any


@dataclass(frozen=True, slots=True)
class PublicSpatialData:
    """Already-redacted public records embedded in the offline map."""

    grid_geojson: Mapping[str, Any]
    facility_geojson: Mapping[str, Any]
    evidence: Sequence[Mapping[str, Any]]
    metadata: Mapping[str, Any]


def render_map(bundle_data: PublicSpatialData) -> str:
    """Render one deterministic three-panel map with no external dependency."""
    payload = {
        "evidence": list(bundle_data.evidence),
        "facilities": bundle_data.facility_geojson,
        "grids": bundle_data.grid_geojson,
        "metadata": bundle_data.metadata,
    }
    svg = _render_svg(bundle_data.grid_geojson, bundle_data.facility_geojson)
    default_evidence = _default_evidence(bundle_data)
    package = files("westbusan.spatial")
    template = (
        package.joinpath("templates/map.html").read_text(encoding="utf-8")
    )
    style = package.joinpath("assets/map.css").read_text(encoding="utf-8")
    script = package.joinpath("assets/map.js").read_text(encoding="utf-8")
    replacements = {
        "{{STYLE}}": style,
        "{{SCRIPT}}": script,
        "{{SVG}}": svg,
        "{{BUNDLE_DATA}}": _script_json(payload),
        "{{DEFAULT_EVIDENCE}}": default_evidence,
        "{{BUSINESS_DATE}}": html.escape(str(bundle_data.metadata.get("business_date", "-"))),
        "{{BOUNDARY_VERSION}}": html.escape(
            str(bundle_data.metadata.get("boundary_version", "-"))
        ),
        "{{POLICY_VERSION}}": html.escape(
            str(bundle_data.metadata.get("policy_version", "-"))
        ),
        "{{GRID_COUNT}}": str(len(bundle_data.grid_geojson.get("features", []))),
        "{{FACILITY_COUNT}}": str(
            len(bundle_data.facility_geojson.get("features", []))
        ),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


def _render_svg(
    grids: Mapping[str, Any], facilities: Mapping[str, Any]
) -> str:
    grid_features = list(grids.get("features", []))
    facility_features = list(facilities.get("features", []))
    points = list(_all_points(grid_features, facility_features))
    min_x, min_y, max_x, max_y = _bounds(points)

    def project(point: Sequence[float]) -> tuple[float, float]:
        x = (float(point[0]) - min_x) / (max_x - min_x) * 1000
        y = (max_y - float(point[1])) / (max_y - min_y) * 700
        return x, y

    paths: list[str] = []
    for feature in grid_features:
        properties = feature.get("properties", {})
        grade = str(properties.get("composite_grade", "insufficient_evidence"))
        geometry = feature.get("geometry", {})
        path_data = _geometry_path(geometry, project)
        paths.append(
            '<path class="grid-feature grade-{grade}" d="{path}" tabindex="0" '
            'role="button" aria-label="{label}" data-kind="grid" '
            'data-key="{key}" data-grade="{grade}" data-district="{district}" '
            'data-dong="{dong}" data-period="{period}" '
            'data-small-scale="{small}" data-aged="{aged}" '
            'data-context="{context}"/>'.format(
                grade=_attribute(grade),
                path=_attribute(path_data),
                label=_attribute(
                    f"{properties.get('grid_id', '')} {grade} 정책지원 우선도"
                ),
                key=_attribute(properties.get("grid_id")),
                district=_attribute(properties.get("district_name")),
                dong=_attribute(properties.get("primary_dong_name")),
                period=_attribute(properties.get("period")),
                small=_attribute(properties.get("small_scale_rating")),
                aged=_attribute(properties.get("aged_building_rating")),
                context=_attribute(properties.get("district_context_rating")),
            )
        )
    circles: list[str] = []
    for feature in facility_features:
        properties = feature.get("properties", {})
        coordinates = feature.get("geometry", {}).get("coordinates", [0, 0])
        x, y = project(coordinates)
        grade = str(properties.get("composite_grade", "insufficient_evidence"))
        circles.append(
            '<circle class="facility-feature grade-{grade}" cx="{x:.3f}" '
            'cy="{y:.3f}" r="6" tabindex="0" role="button" '
            'aria-label="{label}" data-kind="facility" data-key="{key}" '
            'data-grade="{grade}" data-district="{district}" data-dong="{dong}" '
            'data-period="{period}" data-small-scale="{small}" data-aged="{aged}" '
            'data-context="{context}"/>'.format(
                grade=_attribute(grade),
                x=x,
                y=y,
                label=_attribute(
                    f"{properties.get('public_name', '')} {grade} 정책지원 우선도"
                ),
                key=_attribute(properties.get("facility_key")),
                district=_attribute(properties.get("district_name")),
                dong=_attribute(properties.get("primary_dong_name")),
                period=_attribute(properties.get("period")),
                small=_attribute(properties.get("small_scale_rating")),
                aged=_attribute(properties.get("aged_building_rating")),
                context=_attribute(properties.get("district_context_rating")),
            )
        )
    return (
        '<svg id="spatial-map" viewBox="0 0 1000 700" '
        'aria-label="부산 숙박업 정책지원 우선도 지도" role="img">'
        '<g id="map-viewport">'
        + "".join(paths)
        + "".join(circles)
        + "</g></svg>"
    )


def _all_points(
    grids: Sequence[Mapping[str, Any]], facilities: Sequence[Mapping[str, Any]]
):
    for feature in grids:
        geometry = feature.get("geometry", {})
        for polygon in _polygons(geometry):
            for ring in polygon:
                yield from ring
    for feature in facilities:
        coordinates = feature.get("geometry", {}).get("coordinates")
        if isinstance(coordinates, list) and len(coordinates) >= 2:
            yield coordinates


def _bounds(points: Sequence[Sequence[float]]) -> tuple[float, float, float, float]:
    if not points:
        return 0.0, 0.0, 1.0, 1.0
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    if min_x == max_x:
        max_x += 1.0
    if min_y == max_y:
        max_y += 1.0
    return min_x, min_y, max_x, max_y


def _polygons(geometry: Mapping[str, Any]) -> list[Any]:
    coordinates = geometry.get("coordinates", [])
    if geometry.get("type") == "Polygon":
        return [coordinates]
    if geometry.get("type") == "MultiPolygon":
        return list(coordinates)
    return []


def _geometry_path(geometry: Mapping[str, Any], project: Any) -> str:
    parts: list[str] = []
    for polygon in _polygons(geometry):
        for ring in polygon:
            for index, point in enumerate(ring):
                x, y = project(point)
                parts.append(f"{'M' if index == 0 else 'L'}{x:.3f},{y:.3f}")
            parts.append("Z")
    return "".join(parts)


def _default_evidence(data: PublicSpatialData) -> str:
    facilities = data.facility_geojson.get("features", [])
    grids = data.grid_geojson.get("features", [])
    properties = (facilities or grids or [{"properties": {}}])[0].get(
        "properties", {}
    )
    rows = [
        ("대상", properties.get("public_name") or properties.get("grid_id") or "-"),
        ("등급", properties.get("composite_grade", "insufficient_evidence")),
        ("소규모", properties.get("small_scale_rating", "unavailable")),
        ("노후도", properties.get("aged_building_rating", "unavailable")),
        ("district context", properties.get("district_context_rating", "unavailable")),
        ("numerator / denominator / coverage", "선택한 근거 지표에서 확인"),
    ]
    return "".join(
        f"<dt>{html.escape(str(label))}</dt><dd>{html.escape(str(value))}</dd>"
        for label, value in rows
    )


def _script_json(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _attribute(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)
