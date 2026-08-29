from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
ASSET_ROOT = (
    ROOT
    / "src"
    / "westbusan"
    / "tourism_dashboard"
    / "assets"
    / "river-map"
)
RIVER_SOURCE = ASSET_ROOT / "river_layers.geojson"
BOUNDARY_TARGET = ASSET_ROOT / "park_boundaries.geojson"
METADATA_TARGET = ASSET_ROOT / "park_boundary_source_metadata.json"


PARKS = (
    {
        "park_id": "hwamyeong",
        "park_name": "화명생태공원",
        "color": "#2563EB",
        "source_feature_indices": [16, 17],
        "official_management_area_sq_km": 3.03,
        "management_length_km": 7.74,
        "official_info_url": "https://www.busan.go.kr/nakdong/hwamyungpark",
    },
    {
        "park_id": "daejeo",
        "park_name": "대저생태공원",
        "color": "#D97706",
        "source_feature_indices": [18],
        "official_management_area_sq_km": 3.43,
        "management_length_km": 7.62,
        "official_info_url": "https://www.busan.go.kr/nakdong/daejeopark01",
    },
    {
        "park_id": "samrak",
        "park_name": "삼락생태공원",
        "color": "#059669",
        "source_feature_indices": [14],
        "official_management_area_sq_km": 4.89,
        "management_length_km": 7.04,
        "official_info_url": "https://www.busan.go.kr/nakdong/samrakpark",
    },
    {
        "park_id": "maekdo",
        "park_name": "맥도생태공원",
        "color": "#7C3AED",
        "source_feature_indices": [15],
        "official_management_area_sq_km": 2.51,
        "management_length_km": 6.90,
        "official_info_url": "https://www.busan.go.kr/nakdong/macdopark",
    },
    {
        "park_id": "eulsukdo",
        "park_name": "을숙도생태공원",
        "color": "#DB2777",
        "source_feature_indices": [13],
        "official_management_area_sq_km": 3.20,
        "management_length_km": 4.50,
        "official_info_url": "https://www.busan.go.kr/nakdong/eulsukdo01",
    },
)


def _combined_geometry(source_features: list[dict]) -> dict:
    polygons: list[list] = []
    for feature in source_features:
        geometry = feature["geometry"]
        if geometry["type"] == "Polygon":
            polygons.append(geometry["coordinates"])
        elif geometry["type"] == "MultiPolygon":
            polygons.extend(geometry["coordinates"])
        else:
            raise ValueError(f"Unsupported geometry: {geometry['type']}")
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


def main() -> None:
    river_collection = json.loads(RIVER_SOURCE.read_text(encoding="utf-8"))
    river_features = river_collection["features"]
    park_features = []
    for park in PARKS:
        source_features = [river_features[index] for index in park["source_feature_indices"]]
        source_layers = sorted(
            {
                f"{item['properties']['source_layer']}:{item['properties']['zone_type']}"
                for item in source_features
            }
        )
        properties = dict(park)
        properties.update(
            {
                "boundary_status": "reference_interpretation",
                "geometry_basis": "RIMGIS 하천 관리지구 도형과 부산시 공원 안내도를 교차 확인한 관리범위 참고경계",
                "source_layers": source_layers,
                "legal_effect": False,
            }
        )
        park_features.append(
            {
                "type": "Feature",
                "properties": properties,
                "geometry": _combined_geometry(source_features),
            }
        )

    collection = {
        "type": "FeatureCollection",
        "name": "nakdong_five_ecological_parks_reference_boundaries",
        "features": park_features,
    }
    boundary_text = json.dumps(collection, ensure_ascii=False, separators=(",", ":")) + "\n"
    boundary_bytes = boundary_text.encode("utf-8")
    BOUNDARY_TARGET.write_bytes(boundary_bytes)
    digest = hashlib.sha256(boundary_bytes).hexdigest()

    metadata = {
        "title": "낙동강 5개 생태공원 관리범위 참고경계",
        "generated_at": "2026-08-29",
        "feature_count": 5,
        "geometry_source": {
            "system": "RIMGIS",
            "source_file": "river_layers.geojson",
            "retrieved_at": "2026-08-28T14:46:56+09:00",
            "method": "공원 중심점과 부산시 공식 안내지도에 대응하는 RIMGIS 하천 관리지구 도형을 공원별로 묶음",
        },
        "official_information": {
            "system": "부산광역시 낙동강관리본부",
            "overview_url": "https://www.busan.go.kr/nakdong/ndheadinfo01",
            "parks": [
                {
                    key: park[key]
                    for key in (
                        "park_id",
                        "park_name",
                        "official_management_area_sq_km",
                        "management_length_km",
                        "official_info_url",
                    )
                }
                for park in PARKS
            ],
        },
        "area_consistency": {
            "park_page_sum_sq_km": 17.06,
            "headquarters_total_sq_km": 14.38,
            "difference_sq_km": 2.68,
            "handling": "공식 페이지 간 집계 불일치로 면적값을 도형 생성·보정에 사용하지 않음",
        },
        "boundary_status": "reference_interpretation",
        "legal_effect": False,
        "display_label": "관리범위 참고경계",
        "sha256": digest,
        "limitations": [
            "공원별 법정·지적 경계 또는 도시계획시설 결정도형이 아니다.",
            "공원색은 위치 구분용이며 행위 허용·제한 등급을 의미하지 않는다.",
            "배경지도 시설명은 VWorld POI이므로 공원 관리범위 명칭과 다를 수 있다.",
            "인허가 검토에는 최신 고시도면과 낙동강관리본부·하천관리청 공식 의견이 필요하다.",
        ],
    }
    METADATA_TARGET.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
