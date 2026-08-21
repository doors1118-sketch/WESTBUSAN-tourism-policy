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
    """Render one deterministic map with visible district policy priorities."""
    priorities = _district_policy_priorities(bundle_data.metadata)
    metadata = dict(bundle_data.metadata)
    metadata["district_policy_priorities"] = priorities
    payload = {
        "evidence": list(bundle_data.evidence),
        "facilities": bundle_data.facility_geojson,
        "grids": bundle_data.grid_geojson,
        "metadata": metadata,
    }
    svg = _render_svg(
        bundle_data.grid_geojson, bundle_data.facility_geojson, priorities
    )
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
        "{{DISTRICT_PRIORITY}}": _render_priority_overlay(priorities),
        "{{BUSINESS_DATE}}": html.escape(str(bundle_data.metadata.get("business_date", "-"))),
        "{{BOUNDARY_VERSION}}": html.escape(
            str(bundle_data.metadata.get("boundary_version", "-"))
        ),
        "{{BOUNDARY_SOURCE}}": html.escape(
            str(bundle_data.metadata.get("boundary_source_organization", "공식 원천기관"))
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
    grids: Mapping[str, Any],
    facilities: Mapping[str, Any],
    priorities: Sequence[Mapping[str, Any]],
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
    ranks = {str(item["name"]): int(item["rank"]) for item in priorities}
    for feature in grid_features:
        properties = feature.get("properties", {})
        grade = str(properties.get("composite_grade", "insufficient_evidence"))
        rank = ranks.get(str(properties.get("district_name", "")))
        priority_class = f" district-priority-{rank}" if rank is not None else ""
        geometry = feature.get("geometry", {})
        path_data = _geometry_path(geometry, project)
        paths.append(
            '<path class="grid-feature grade-{grade}{priority_class}" d="{path}" tabindex="0" '
            'role="button" aria-label="{label}" data-kind="grid" '
            'data-key="{key}" data-grade="{grade}" data-district="{district}" '
            'data-policy-rank="{rank}" '
            'data-dong="{dong}" data-period="{period}" '
            'data-small-scale="{small}" data-aged="{aged}" '
            'data-context="{context}"/>'.format(
                grade=_attribute(grade),
                priority_class=priority_class,
                rank="" if rank is None else rank,
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
        rank = ranks.get(str(properties.get("district_name", "")))
        priority_class = f" district-priority-{rank}" if rank is not None else ""
        circles.append(
            '<circle class="facility-feature grade-{grade}{priority_class}" cx="{x:.3f}" '
            'cy="{y:.3f}" r="6" tabindex="0" role="button" '
            'aria-label="{label}" data-kind="facility" data-key="{key}" '
            'data-grade="{grade}" data-district="{district}" data-dong="{dong}" '
            'data-policy-rank="{rank}" '
            'data-period="{period}" data-small-scale="{small}" data-aged="{aged}" '
            'data-context="{context}"/>'.format(
                grade=_attribute(grade),
                priority_class=priority_class,
                rank="" if rank is None else rank,
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


def _district_policy_priorities(
    metadata: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw = metadata.get("district_policy_priorities")
    if raw is None:
        dashboard_data = json.loads(
            files("westbusan.tourism_dashboard")
            .joinpath("assets/data.json")
            .read_text(encoding="utf-8")
        )
        raw = dashboard_data.get("westDistricts", [])
    priorities: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        rank = item.get("rank")
        name = item.get("name")
        priority = item.get("priority")
        if rank not in {1, 2, 3, 4} or not isinstance(name, str):
            continue
        if not isinstance(priority, str) or not priority.strip():
            continue
        priorities.append({"rank": rank, "name": name, "priority": priority})
    return sorted(priorities, key=lambda item: int(item["rank"]))


def _render_priority_overlay(priorities: Sequence[Mapping[str, Any]]) -> str:
    return "".join(
        '<li class="priority-rank-{rank}"><b>{rank}순위</b>'
        '<span>{name}</span><small>{priority}</small></li>'.format(
            rank=int(item["rank"]),
            name=html.escape(str(item["name"])),
            priority=html.escape(str(item["priority"])),
        )
        for item in priorities
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
