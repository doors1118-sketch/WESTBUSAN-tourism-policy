"""Deterministic, standalone inline-SVG rendering for public spatial data."""

from __future__ import annotations

import html
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

_MAP_CENTER = (129.075, 35.18)
_MAP_ZOOM = 10
_MAP_WIDTH = 1000
_MAP_HEIGHT = 700
_WEST_DISTRICTS = ("강서구", "사하구", "사상구", "북구")
_CANDIDATE_LAYERS = (
    "policy_priority",
    "tourism_supply_gap",
    "facility_density",
    "aged_facilities",
)


@dataclass(frozen=True, slots=True)
class PublicSpatialData:
    """Already-redacted public records embedded in the offline map."""

    grid_geojson: Mapping[str, Any]
    facility_geojson: Mapping[str, Any]
    evidence: Sequence[Mapping[str, Any]]
    metadata: Mapping[str, Any]


def build_policy_candidate_rankings(
    features: Sequence[Mapping[str, Any]],
    *,
    limit: int = 5,
) -> dict[str, Any]:
    """Rank distinct west-Busan policy areas at region, district, and dong grain."""
    return build_layer_candidate_rankings(
        features,
        layer="policy_priority",
        limit=limit,
    )


def build_layer_candidate_rankings(
    features: Sequence[Mapping[str, Any]],
    *,
    layer: str,
    limit: int = 5,
) -> dict[str, Any]:
    """Rank one representative per west district plus one distinct extra area."""
    if layer not in _CANDIDATE_LAYERS:
        raise ValueError(f"unsupported candidate layer: {layer}")
    candidates: list[dict[str, Any]] = []
    for feature in features:
        properties = feature.get("properties", {})
        if not isinstance(properties, Mapping):
            continue
        district = str(properties.get("district_name") or "")
        dong = str(properties.get("primary_dong_name") or "")
        grid_id = str(properties.get("grid_id") or "")
        if district not in _WEST_DISTRICTS or not dong or not grid_id:
            continue
        kind = str(properties.get("recommendation_kind") or "")
        gap = _optional_number(properties.get("tourism_supply_gap"))
        aged = _optional_number(properties.get("age_20y_facility_count")) or 0.0
        age_known = _optional_number(properties.get("age_sample_size")) or 0.0
        facilities = _optional_number(properties.get("mapped_facility_count")) or 0.0
        score = _candidate_score(
            layer=layer,
            kind=kind,
            gap=gap,
            aged=aged,
            age_known=age_known,
            facilities=facilities,
        )
        if score is None:
            continue
        candidates.append(
            {
                "grid_id": grid_id,
                "district": district,
                "dong": dong,
                "score": score,
            }
        )

    def ranked(items: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        return sorted(
            items,
            key=lambda item: (-float(item["score"]), str(item["grid_id"])),
        )

    best_by_area: dict[tuple[str, str], Mapping[str, Any]] = {}
    for item in ranked(candidates):
        best_by_area.setdefault((str(item["district"]), str(item["dong"])), item)
    area_candidates = ranked(list(best_by_area.values()))

    representatives: list[Mapping[str, Any]] = []
    for district in _WEST_DISTRICTS:
        district_items = [
            item for item in area_candidates if item["district"] == district
        ]
        if district_items:
            representatives.append(district_items[0])
    selected_keys = {
        (str(item["district"]), str(item["dong"])) for item in representatives
    }
    extras = [
        item
        for item in area_candidates
        if (str(item["district"]), str(item["dong"])) not in selected_keys
    ]
    default_items = ranked(
        representatives + extras[: max(0, limit - len(representatives))]
    )[:limit]

    district_rankings: dict[str, dict[str, int]] = {}
    for district in _WEST_DISTRICTS:
        items = [item for item in area_candidates if item["district"] == district][
            :limit
        ]
        district_rankings[district] = {
            str(item["grid_id"]): rank for rank, item in enumerate(items, start=1)
        }

    dong_rankings: dict[str, dict[str, int]] = {}
    for district, dong in best_by_area:
        items = ranked(
            [
                item
                for item in candidates
                if item["district"] == district and item["dong"] == dong
            ]
        )[:limit]
        dong_rankings[f"{district}|{dong}"] = {
            str(item["grid_id"]): rank for rank, item in enumerate(items, start=1)
        }

    return {
        "default": {
            str(item["grid_id"]): rank
            for rank, item in enumerate(default_items, start=1)
        },
        "district": district_rankings,
        "dong": dong_rankings,
    }


def _candidate_score(
    *,
    layer: str,
    kind: str,
    gap: float | None,
    aged: float,
    age_known: float,
    facilities: float,
) -> float | None:
    if layer == "tourism_supply_gap":
        return gap
    if layer == "facility_density":
        return facilities if facilities > 0 else None
    if layer == "aged_facilities":
        return aged if age_known > 0 and aged > 0 else None
    if facilities <= 0 and not kind:
        return None
    signal = {
        "new_supply": 3.0,
        "remodel": 2.0,
        "quality_upgrade": 1.5,
        "content_first": 1.25,
        "investment_caution": 1.0,
    }.get(kind, 0.0)
    return signal * 1000 + (gap or 0.0) * 10 + aged + facilities * 0.1


def render_map(bundle_data: PublicSpatialData) -> str:
    """Render one deterministic, policy-oriented investment opportunity map."""
    payload = {
        "candidate_rankings": {
            layer: build_layer_candidate_rankings(
                list(bundle_data.grid_geojson.get("features", [])),
                layer=layer,
            )
            for layer in _CANDIDATE_LAYERS
        },
        "evidence": list(bundle_data.evidence),
        "facilities": bundle_data.facility_geojson,
        "grids": bundle_data.grid_geojson,
        "metadata": bundle_data.metadata,
    }
    priorities = _district_policy_priorities(bundle_data.metadata)
    svg = _render_svg(
        bundle_data.grid_geojson,
        bundle_data.facility_geojson,
        priorities,
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
        "{{BUSINESS_DATE}}": html.escape(str(bundle_data.metadata.get("business_date", "-"))),
        "{{BOUNDARY_VERSION}}": html.escape(
            str(bundle_data.metadata.get("boundary_version", "-"))
        ),
        "{{BOUNDARY_SOURCE}}": html.escape(
            str(bundle_data.metadata.get("boundary_source_organization", "공식 원천기관"))
        ),
        "{{POLICY_VERSION}}": "숙박투자 v1",
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
    candidate_rankings = build_policy_candidate_rankings(grid_features)
    policy_by_district = {
        str(item["name"]): (
            _policy_kind(int(item["rank"])),
            int(item["rank"]),
            _policy_short_label(int(item["rank"])),
        )
        for item in priorities
    }

    def project(point: Sequence[float]) -> tuple[float, float]:
        point_x, point_y = _web_mercator_pixel(
            float(point[0]), float(point[1]), _MAP_ZOOM
        )
        center_x, center_y = _web_mercator_pixel(
            _MAP_CENTER[0], _MAP_CENTER[1], _MAP_ZOOM
        )
        return (
            point_x - center_x + _MAP_WIDTH / 2,
            point_y - center_y + _MAP_HEIGHT / 2,
        )

    paths: list[str] = []
    for feature in grid_features:
        properties = feature.get("properties", {})
        grade = str(properties.get("composite_grade", "insufficient_evidence"))
        district = str(properties.get("district_name", ""))
        dong = str(properties.get("primary_dong_name", ""))
        grid_key = str(properties.get("grid_id", ""))
        policy_kind = policy_by_district.get(district, ("", 0, ""))[0]
        default_rank = candidate_rankings["default"].get(grid_key, "")
        district_rank = candidate_rankings["district"].get(district, {}).get(
            grid_key, ""
        )
        dong_rank = candidate_rankings["dong"].get(f"{district}|{dong}", {}).get(
            grid_key, ""
        )
        geometry = feature.get("geometry", {})
        path_data = _geometry_path(geometry, project)
        min_x, min_y, max_x, max_y = _projected_geometry_bounds(geometry, project)
        min_lon, min_lat, max_lon, max_lat = _geographic_geometry_bounds(geometry)
        paths.append(
            '<path class="grid-feature" d="{path}" tabindex="0" '
            'role="button" aria-label="{label}" data-kind="grid" '
            'data-key="{key}" data-grade="{grade}" data-district="{district}" '
            'data-dong="{dong}" data-period="{period}" '
            'data-small-scale="{small}" data-aged="{aged}" '
            'data-context="{context}" data-tourism-supply-gap="{gap}" '
            'data-mapped-facility-count="{mapped_count}" '
            'data-aged-count="{aged_count}" data-age-known="{age_known}" '
            'data-room-count="{room_count}" data-room-coverage="{room_coverage}" '
            'data-demand-score="{demand_score}" data-supply-score="{supply_score}" '
            'data-default-rank="{default_rank}" data-district-rank="{district_rank}" '
            'data-dong-rank="{dong_rank}" '
            'data-map-bounds="{min_x:.3f},{min_y:.3f},{max_x:.3f},{max_y:.3f}" '
            'data-geo-bounds="{min_lon:.7f},{min_lat:.7f},{max_lon:.7f},{max_lat:.7f}" '
            'data-recommendation="{recommendation}" data-policy-kind="{policy_kind}">'
            '<title>{title}</title></path>'.format(
                grade=_attribute(grade),
                path=_attribute(path_data),
                label=_attribute(
                    f"{properties.get('district_name', '')} "
                    f"{properties.get('primary_dong_name', '')} 숙박 투자 검토"
                ),
                key=_attribute(properties.get("grid_id")),
                district=_attribute(properties.get("district_name")),
                dong=_attribute(properties.get("primary_dong_name")),
                period=_attribute(properties.get("period")),
                small=_attribute(properties.get("small_scale_rating")),
                aged=_attribute(properties.get("aged_building_rating")),
                context=_attribute(properties.get("district_context_rating")),
                gap=_attribute(properties.get("tourism_supply_gap")),
                mapped_count=_attribute(properties.get("mapped_facility_count")),
                aged_count=_attribute(properties.get("age_20y_facility_count")),
                age_known=_attribute(properties.get("age_sample_size")),
                room_count=_attribute(properties.get("room_sum")),
                room_coverage=_attribute(properties.get("room_coverage")),
                demand_score=_attribute(properties.get("demand_context_score")),
                supply_score=_attribute(properties.get("room_supply_score")),
                default_rank=_attribute(default_rank),
                district_rank=_attribute(district_rank),
                dong_rank=_attribute(dong_rank),
                min_x=min_x,
                min_y=min_y,
                max_x=max_x,
                max_y=max_y,
                min_lon=min_lon,
                min_lat=min_lat,
                max_lon=max_lon,
                max_lat=max_lat,
                recommendation=_attribute(properties.get("recommendation_kind")),
                policy_kind=_attribute(policy_kind),
                title=html.escape(
                    " · ".join(
                        (
                            str(properties.get("district_name", "")),
                            str(properties.get("primary_dong_name", "")),
                            "수요 대비 공급부족 "
                            + _metric_label(properties.get("tourism_supply_gap")),
                            "주소확인 시설 "
                            + _metric_label(properties.get("mapped_facility_count"))
                            + "개",
                            "20년 이상 시설 "
                            + _metric_label(properties.get("age_20y_facility_count"))
                            + "개 / 연수 확인 "
                            + _metric_label(properties.get("age_sample_size"))
                            + "개",
                        )
                    )
                ),
            )
        )
    cluster_members: dict[
        tuple[str, str, str], list[tuple[float, float, Mapping[str, Any]]]
    ] = {}
    circles: list[str] = []
    for feature in facility_features:
        properties = feature.get("properties", {})
        coordinates = feature.get("geometry", {}).get("coordinates", [0, 0])
        x, y = project(coordinates)
        cluster_key = (
            str(properties.get("district_name", "")),
            str(properties.get("primary_dong_name", "")),
            str(properties.get("period", "")),
        )
        cluster_members.setdefault(cluster_key, []).append((x, y, properties))
        grade = str(properties.get("composite_grade", "insufficient_evidence"))
        circles.append(
            '<circle class="facility-feature" cx="{x:.3f}" '
            'cy="{y:.3f}" r="3" data-base-radius="3" tabindex="0" role="button" '
            'aria-label="{label}" data-kind="facility" data-key="{key}" '
            'data-grade="{grade}" data-district="{district}" data-dong="{dong}" '
            'data-period="{period}" data-small-scale="{small}" data-aged="{aged}" '
            'data-context="{context}" data-public-name="{public_name}" '
            'data-public-address="{public_address}" data-room-count="{room_count}" '
            'data-building-age="{building_age}"><title>{title}</title></circle>'.format(
                grade=_attribute(grade),
                x=x,
                y=y,
                label=_attribute(
                    f"{properties.get('public_name', '')} 숙박시설"
                ),
                key=_attribute(properties.get("facility_key")),
                district=_attribute(properties.get("district_name")),
                dong=_attribute(properties.get("primary_dong_name")),
                period=_attribute(properties.get("period")),
                small=_attribute(properties.get("small_scale_rating")),
                aged=_attribute(properties.get("aged_building_rating")),
                context=_attribute(properties.get("district_context_rating")),
                public_name=_attribute(properties.get("public_name")),
                public_address=_attribute(properties.get("public_address")),
                room_count=_attribute(properties.get("room_count")),
                building_age=_attribute(properties.get("use_approval_age_years")),
                title=html.escape(
                    " · ".join(
                        (
                            str(properties.get("public_name", "숙박시설")),
                            "객실 " + _metric_label(properties.get("room_count")),
                            "건물연수 "
                            + _metric_label(properties.get("use_approval_age_years")),
                        )
                    )
                ),
            )
        )
    clusters: list[str] = []
    for (district, dong, period), members in sorted(cluster_members.items()):
        count = len(members)
        x = sum(item[0] for item in members) / count
        y = sum(item[1] for item in members) / count
        known_rooms = [
            float(item[2]["room_count"])
            for item in members
            if item[2].get("room_count") is not None
        ]
        rooms = sum(known_rooms) if known_rooms else None
        radius = min(18.0, 5.5 + math.sqrt(count) * 1.7)
        clusters.append(
            '<g class="facility-cluster" transform="translate({x:.3f} {y:.3f})" '
            'data-x="{x:.3f}" data-y="{y:.3f}" '
            'tabindex="0" role="button" data-kind="cluster" '
            'data-district="{district}" data-dong="{dong}" data-period="{period}">'
            '<circle r="{radius:.2f}"><title>{title}</title></circle>'
            '<text y="3.5">{count}</text></g>'.format(
                x=x,
                y=y,
                radius=radius,
                count=count,
                district=_attribute(district),
                dong=_attribute(dong),
                period=_attribute(period),
                title=html.escape(
                    f"{district} {dong} · 숙박시설 {count}개 · "
                    f"확인 객실 {_metric_label(rooms)}실"
                ),
            )
        )
    return (
        '<svg id="spatial-map" viewBox="0 0 1000 700" '
        'data-map-center="129.075,35.18" data-map-zoom="10" '
        'aria-label="부산 관광 숙박 투자기회 지도" role="application">'
        '<g id="map-viewport">'
        + "".join(paths)
        + '<g id="candidate-markers"></g>'
        + "".join(clusters)
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


def _projected_geometry_bounds(
    geometry: Mapping[str, Any], project: Any
) -> tuple[float, float, float, float]:
    points = [
        project(point)
        for polygon in _polygons(geometry)
        for ring in polygon
        for point in ring
    ]
    if not points:
        return 0.0, 0.0, 0.0, 0.0
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _geographic_geometry_bounds(
    geometry: Mapping[str, Any],
) -> tuple[float, float, float, float]:
    points = [
        point
        for polygon in _polygons(geometry)
        for ring in polygon
        for point in ring
    ]
    if not points:
        return 0.0, 0.0, 0.0, 0.0
    longitudes = [float(point[0]) for point in points]
    latitudes = [float(point[1]) for point in points]
    return min(longitudes), min(latitudes), max(longitudes), max(latitudes)


def _metric_label(value: object, *, percent: bool = False) -> str:
    if value is None or value == "":
        return "자료 없음"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "자료 없음"
    if not math.isfinite(number):
        return "자료 없음"
    if percent:
        number *= 100
        return f"{number:.1f}%"
    return f"{number:.1f}"


def _optional_number(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _policy_kind(rank: int) -> str:
    return {
        1: "new_supply",
        2: "remodel",
        3: "transport_quality",
        4: "tourism_product",
    }.get(rank, "")


def _policy_short_label(rank: int) -> str:
    return {
        1: "신규 관광숙박 공급",
        2: "노후시설 개선·전환",
        3: "교통연계·품질개선",
        4: "리모델링·관광상품화",
    }.get(rank, "정책 검토")


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


def _web_mercator_pixel(longitude: float, latitude: float, zoom: int) -> tuple[float, float]:
    latitude = max(-85.05112878, min(85.05112878, latitude))
    world_size = 256 * 2**zoom
    x = (longitude + 180.0) / 360.0 * world_size
    latitude_radians = math.radians(latitude)
    y = (
        1.0 - math.asinh(math.tan(latitude_radians)) / math.pi
    ) / 2.0 * world_size
    return x, y


def _script_json(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _attribute(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)
