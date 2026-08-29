from __future__ import annotations

import hashlib
import json
from pathlib import Path

from shapely.geometry import Point, shape

ASSET_ROOT = (
    Path(__file__).parents[2]
    / "src"
    / "westbusan"
    / "tourism_dashboard"
    / "assets"
)
RIVER_ROOT = ASSET_ROOT / "river-map"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_dashboard_adds_lazy_river_review_before_ai_analysis() -> None:
    html = _read(ASSET_ROOT / "index.html")
    script = _read(ASSET_ROOT / "app.js")
    nav = html.split('<nav class="tabs"', 1)[1].split("</nav>", 1)[0]

    assert 'data-tab-target="river">낙동강 규제검토<' in nav
    assert nav.index('data-tab-target="vacant"') < nav.index('data-tab-target="river"')
    assert nav.index('data-tab-target="river"') < nav.index('data-tab-target="insights"')
    assert 'data-river-map-src="river-map/index.html?v=20260829-layer-focus-v10"' in html
    assert '<iframe src="river-map/index.html"' not in html
    assert 'target === "river"' in script
    assert "riverMapSrc" in script


def test_river_map_exposes_five_parks_layers_and_click_assessment() -> None:
    html = _read(RIVER_ROOT / "index.html")
    script = _read(RIVER_ROOT / "map.js")
    stylesheet = _read(RIVER_ROOT / "map.css")

    for park in ("화명생태공원", "대저생태공원", "삼락생태공원", "맥도생태공원", "을숙도생태공원"):
        assert park in html
    for label in (
        "하천구역",
        "일반보전지구",
        "근린친수지구",
        "복원지구",
        "습지보호구역",
        "국가유산",
        "도시공원",
        "용도지역",
    ):
        assert label in html
    for activity in ("산책·탐방", "축제·행사", "야영·캠핑", "숙박시설", "주차장·진입도로"):
        assert activity in html
    assert "pointInFeature" in script
    assert "assessSelection" in script
    assert "/tourism/api/regulations/point" in script
    assert "external-regulation-overlay" in html
    assert "regulation-results" in html
    assert "RIMGIS + VWorld + 국가유산 DB" in html
    assert 'id="structure-height"' in html
    assert 'id="roof-type"' in html
    assert "heritage_criteria" in script
    assert 'id="parcel-address"' in html
    assert 'id="parcel-search"' in html
    assert 'id="parcel-planning-results"' in html
    assert 'query.set("pnu"' in script
    assert 'fetch("/tourism/api/vworld/geocode"' in script
    assert "parcel_planning" in script
    assert 'id="policy-insight-button"' in html
    assert 'id="policy-insight-panel"' in html
    assert 'fetch("/tourism/api/regulations/insight"' in script
    assert "legal_evidence_source" in script
    assert "basis.law_name" in script
    assert "basis.articles" in script
    assert "parcel_resolution" in script
    assert 'query.set("height_m"' in script
    assert 'query.set("roof_type"' in script
    assert "법적 효력" in html
    assert "인허가 처분 또는 관리청 공식의견을 대체하지 않습니다" in html
    assert "@media (max-width: 760px)" in stylesheet


def test_river_map_distinguishes_five_reference_park_boundaries() -> None:
    html = _read(RIVER_ROOT / "index.html")
    script = _read(RIVER_ROOT / "map.js")
    stylesheet = _read(RIVER_ROOT / "map.css")
    boundaries = json.loads(_read(RIVER_ROOT / "park_boundaries.geojson"))
    metadata = json.loads(
        _read(RIVER_ROOT / "park_boundary_source_metadata.json")
    )

    assert boundaries["type"] == "FeatureCollection"
    assert len(boundaries["features"]) == 5
    properties = [feature["properties"] for feature in boundaries["features"]]
    assert {item["park_id"] for item in properties} == {
        "hwamyeong",
        "daejeo",
        "samrak",
        "maekdo",
        "eulsukdo",
    }
    assert len({item["color"] for item in properties}) == 5
    assert all(item["boundary_status"] == "reference_interpretation" for item in properties)
    assert all(item["legal_effect"] is False for item in properties)
    assert all(feature["geometry"]["type"] in {"Polygon", "MultiPolygon"} for feature in boundaries["features"])

    assert metadata["feature_count"] == 5
    assert metadata["legal_effect"] is False
    assert metadata["geometry_source"]["system"] == "RIMGIS"
    assert metadata["official_information"]["system"] == "부산광역시 낙동강관리본부"
    assert len(metadata["official_information"]["parks"]) == 5
    assert metadata["area_consistency"]["park_page_sum_sq_km"] == 17.06
    assert metadata["area_consistency"]["headquarters_total_sq_km"] == 14.38
    assert len(metadata["sha256"]) == 64

    assert 'id="park-boundary-overlay"' in html
    assert 'data-park-boundary-layer="park_boundary"' in html
    assert 'id="park-boundary-legend"' in html
    assert "관리범위 참고경계" in html
    assert 'fetch("park_boundaries.geojson"' in script
    assert 'fetch("park_boundary_source_metadata.json"' in script
    assert "parkAt" in script
    assert "setActiveParkBoundary" in script
    assert ".park-boundary" in stylesheet
    assert ".park-boundary.is-active" in stylesheet
    assert "공원색은 규제등급을 의미하지 않습니다" in html


def test_river_map_provides_focus_mode_and_text_overlap_summary() -> None:
    html = _read(RIVER_ROOT / "index.html")
    script = _read(RIVER_ROOT / "map.js")
    stylesheet = _read(RIVER_ROOT / "map.css")
    dashboard_html = _read(ASSET_ROOT / "index.html")

    assert 'id="layer-focus-status"' in html
    assert 'id="reset-layer-focus"' in html
    assert html.count('class="layer-focus-button"') == 8
    assert html.count('aria-pressed="false"') >= 8
    assert 'id="overlap-summary"' in html
    assert 'id="overlap-count"' in html
    assert 'id="overlap-badges"' in html
    assert 'id="overlap-summary-note"' in html

    assert "function setFocusedLayer" in script
    assert "function applyLayerReadability" in script
    assert "function renderOverlapSummary" in script
    assert 'classList.toggle("is-focus-layer"' in script
    assert 'classList.toggle("is-context-layer"' in script
    assert 'setAttribute("aria-pressed"' in script
    assert "provider_error" in script
    assert "invalid_response" in script

    assert ".layer-focus-button" in stylesheet
    assert ".is-focus-layer" in stylesheet
    assert ".is-context-layer" in stylesheet
    assert ".overlap-summary" in stylesheet
    assert ".overlap-badge" in stylesheet

    version = "20260829-layer-focus-v10"
    assert f"map.css?v={version}" in html
    assert f"map.js?v={version}" in html
    assert f'river-map/index.html?v={version}' in dashboard_html


def test_reference_park_boundaries_cover_each_published_map_label() -> None:
    boundary_path = RIVER_ROOT / "park_boundaries.geojson"
    boundary_bytes = boundary_path.read_bytes()
    boundaries = json.loads(boundary_bytes.decode("utf-8"))
    metadata = json.loads(
        _read(RIVER_ROOT / "park_boundary_source_metadata.json")
    )
    label_points = {
        "hwamyeong": (129.00547, 35.23847),
        "daejeo": (128.98894, 35.21074),
        "samrak": (128.9763, 35.1711),
        "maekdo": (128.95709, 35.15138),
        "eulsukdo": (128.9523, 35.1172),
    }
    by_id = {
        feature["properties"]["park_id"]: feature
        for feature in boundaries["features"]
    }

    for park_id, coordinates in label_points.items():
        assert shape(by_id[park_id]["geometry"]).covers(Point(*coordinates))
    assert hashlib.sha256(boundary_bytes).hexdigest() == metadata["sha256"]


def test_river_tab_remains_independent_from_investment_and_vacant_maps() -> None:
    html = _read(ASSET_ROOT / "index.html")

    assert 'data-tab-panel="investment"' in html
    assert 'data-map-src="map/index.html?v=20260827-poi-filters-v66"' in html
    assert 'data-tab-panel="vacant"' in html
    assert 'data-vacant-map-src="vacant-map/index.html?v=20260827-poi-filters-v66"' in html
    assert 'data-tab-panel="river"' in html
    assert "낙동강 친수공원 관광개발 규제 검토" in html
    assert (
        "하천·환경·국가유산·도시계획 규제를 중첩하여 "
        "관광개발 가능성을 사전 검토합니다."
    ) in html


def test_river_reference_bundle_has_traceable_precise_geometry() -> None:
    data = json.loads(_read(RIVER_ROOT / "river_layers.geojson"))
    metadata = json.loads(_read(RIVER_ROOT / "source_metadata.json"))
    regulation_metadata = json.loads(
        _read(RIVER_ROOT / "regulation_source_metadata.json")
    )

    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) >= 10
    assert {item["properties"]["zone_type"] for item in data["features"]} >= {
        "river_area",
        "general_conservation",
        "waterfront",
        "restoration",
    }
    assert metadata["source_system"] == "RIMGIS"
    assert metadata["retrieved_at"].startswith("2026-08-28")
    assert metadata["legal_effect"] is False
    assert len(metadata["sha256"]) == 64
    assert regulation_metadata["legal_effect"] is False
    assert {source["category"] for source in regulation_metadata["sources"]} == {
        "wetland",
        "heritage",
        "urban_park",
        "land_use",
    }
