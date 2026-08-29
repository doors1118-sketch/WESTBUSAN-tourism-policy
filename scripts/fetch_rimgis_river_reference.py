"""Build a reproducible Lower Nakdong RIMGIS GeoJSON snapshot for the dashboard."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from shapely import make_valid
from shapely.geometry import (
    GeometryCollection,
    MultiPolygon,
    Polygon,
    box,
    mapping,
    shape,
)
from shapely.ops import unary_union

ENDPOINT = "https://www.river.go.kr/geoserver/rimgis/wfs"
RIVER_PLAN_CODE = "2000010200912"
MAP_BOUNDS = (128.90, 35.08, 129.03, 35.30)
LAYERS = {
    "rc100": ("river_area", "하천구역", 1),
    "rc161_dst": ("general_conservation", "일반보전지구", 2),
    "rc164_dst": ("waterfront", "근린친수지구", 3),
    "rc165_dst": ("restoration", "복원지구", 4),
}
OUTPUT = (
    Path(__file__).parents[1]
    / "src"
    / "westbusan"
    / "tourism_dashboard"
    / "assets"
    / "river-map"
)


def polygonal_part(geometry: object) -> Polygon | MultiPolygon | None:
    """Discard line/point remnants introduced when a polygon is clipped."""

    if isinstance(geometry, (Polygon, MultiPolygon)):
        return geometry
    if not isinstance(geometry, GeometryCollection):
        return None
    polygon_parts: list[Polygon] = []
    for member in geometry.geoms:
        candidate = polygonal_part(member)
        if isinstance(candidate, Polygon):
            polygon_parts.append(candidate)
        elif isinstance(candidate, MultiPolygon):
            polygon_parts.extend(candidate.geoms)
    if not polygon_parts:
        return None
    merged = unary_union(polygon_parts)
    return merged if isinstance(merged, (Polygon, MultiPolygon)) else None


def fetch_layer(client: httpx.Client, layer: str) -> dict[str, object]:
    response = client.get(
        ENDPOINT,
        params={
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": f"rimgis:{layer}",
            "outputFormat": "application/json",
            "srsName": "EPSG:4326",
            "bbox": "128.75,34.85,129.45,35.45,EPSG:4326",
        },
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    clip = box(*MAP_BOUNDS)
    output_features: list[dict[str, object]] = []
    source_counts: dict[str, dict[str, int]] = {}

    with httpx.Client(
        timeout=60,
        headers={
            "User-Agent": "westbusan-policy-map/1.0",
            "Referer": "https://www.river.go.kr/map/rimMap.do",
        },
    ) as client:
        for layer, (zone_type, zone_label, priority) in LAYERS.items():
            payload = fetch_layer(client, layer)
            matched = 0
            published = 0
            for feature in payload.get("features", []):
                properties = feature.get("properties") or {}
                if properties.get("rmp_code") != RIVER_PLAN_CODE:
                    continue
                matched += 1
                geometry = make_valid(shape(feature["geometry"]))
                if not geometry.intersects(clip):
                    continue
                clipped = polygonal_part(geometry.intersection(clip))
                if clipped is None or clipped.is_empty:
                    continue
                published += 1
                output_features.append(
                    {
                        "type": "Feature",
                        "id": f"{layer}-{published}",
                        "geometry": mapping(clipped),
                        "properties": {
                            "zone_type": zone_type,
                            "zone_label": zone_label,
                            "priority": priority,
                            "source_layer": layer,
                            "source_feature_id": feature.get("id"),
                            "rmp_code": RIVER_PLAN_CODE,
                            "geo_key": properties.get("geo_key"),
                        },
                    }
                )
            source_counts[layer] = {"matched": matched, "published": published}

    collection = {
        "type": "FeatureCollection",
        "name": "lower_nakdong_river_review_reference",
        "bbox": list(MAP_BOUNDS),
        "features": output_features,
    }
    body = (
        json.dumps(collection, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")
    data_path = OUTPUT / "river_layers.geojson"
    data_path.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    retrieved_at = datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")
    metadata = {
        "source_system": "RIMGIS",
        "source_map": "https://www.river.go.kr/map/rimMap.do",
        "source_service": ENDPOINT,
        "retrieved_at": retrieved_at,
        "scope": "낙동강 화명·대저·삼락·맥도·을숙도 생태공원 일원",
        "bbox": list(MAP_BOUNDS),
        "river_plan_code": RIVER_PLAN_CODE,
        "geometry_version": "RIMGIS WFS 응답에 기준일·고시번호 필드 없음",
        "geometry_interpretation": {
            "waterfront_is_park_boundary": False,
            "waterfront_meaning": (
                "근린친수지구 도형은 공원 시설경계가 아니라 RIMGIS 하천공간관리 "
                "구간이므로 개별 생태공원보다 넓게 보일 수 있음"
            ),
            "park_boundaries": (
                "별도 5개 생태공원 참고경계로 표시하며 법정·지적 경계가 아님"
            ),
        },
        "official_notice_context": [
            {
                "notice": "환경부 낙동강유역환경청 고시 제2026-11호",
                "date": "2026-02-24",
                "subject": "낙동강(국가하천) 하천구역 결정(변경) 및 지형도면",
                "url": "https://www.eum.go.kr/web/gs/gv/gvGosiDet.jsp?seq=632222",
                "geometry_matched": False,
            },
            {
                "notice": "환경부 낙동강유역환경청 고시 제2026-12호",
                "date": "2026-03-04",
                "subject": "낙동강(국가하천) 홍수관리구역 지정 및 지형도면",
                "url": "https://www.eum.go.kr/web/gs/gv/gvGosiDet.jsp?seq=633773",
                "geometry_matched": False,
            },
        ],
        "preliminary_change_context": [
            {
                "source": "낙동강유역환경청 공고 제2024-84호",
                "date": "2024-06-05",
                "subject": "낙동강 하류권역 하천기본계획 전략환경영향평가 주민의견 반영결과",
                "url": (
                    "https://mcee.go.kr/ndg/web/board/read.do?boardId=1679830"
                    "&boardMasterId=156&menuId=3284"
                ),
                "summary": (
                    "대저·맥도 일부 일반보전·근린친수 구간의 친수거점 전환과 "
                    "대저 일부 근린친수 구간의 일반보전 전환이 검토·반영되었다는 "
                    "문서상 기록"
                ),
                "geometry_available": False,
                "application": "문구형 잠정 변경정보만 제공하며 지도 도형에는 미반영",
            },
            {
                "source": "2025-03 언론보도(2024-12 계획 변경 인용)",
                "date": "2025-03-09",
                "subject": "화명생태공원 상류·화명2지구 약 730,400㎡ 친수지구 변경 보도",
                "url": "https://v.daum.net/v/20250309191307134",
                "geometry_available": False,
                "application": "내부 참고용 잠정정보이며 최종 고시도형 확보 후 교체",
            },
        ],
        "feature_count": len(output_features),
        "source_counts": source_counts,
        "sha256": digest,
        "legal_effect": False,
        "limitation": (
            "RIMGIS 정보는 공간 검토용 참고자료이며 법적 효력을 갖는 허가·규제 "
            "판정이 아닙니다. WFS 도형을 2026-11·12호 고시도면과 형상·필지 "
            "단위로 대조하지 못했으며, 최종 고시도면과 관리청 공식 의견으로 "
            "재확인해야 합니다."
        ),
    }
    (OUTPUT / "source_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
