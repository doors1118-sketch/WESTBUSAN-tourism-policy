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

    assert 'data-tab-target="river">낙동강 관광자원화 투자 정보<' in nav
    assert nav.index('data-tab-target="vacant"') < nav.index('data-tab-target="river"')
    assert nav.index('data-tab-target="river"') < nav.index('data-tab-target="insights"')
    assert 'data-river-map-src="river-map/index.html?v=20260830-focus-preserve-v22"' in html
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
    assert "법령기반 AI 정책인사이트" in html
    assert 'id="assessment-basis-list"' in html
    assert 'id="assessment-check-list"' in html
    assert 'id="policy-evidence-summary"' in html
    assert 'id="policy-evidence-counts"' in html
    assert 'id="policy-parcel-facts"' in html
    assert 'id="reviewable-actions"' in html
    assert 'id="restricted-actions"' in html
    assert 'id="pending-actions"' in html
    assert 'id="policy-insight-meta"' in html
    assert 'fetch("/tourism/api/regulations/insight"' in script
    assert "legal_evidence_source" in script
    assert "basis.law_name" in script
    assert "basis.articles" in script
    assert "link.href = basis.official_url" in script
    assert "insight.legal_source_urls || []" in script
    assert 'link.target = "_blank"' in script
    assert "renderedLegalUrls" in script
    assert "if (renderedLegalUrls.has(url)) return" in script
    assert "function renderPolicyParcelFacts" in script
    assert "function renderActionScreenings" in script
    assert "basis.review_effect" in script
    assert "basis.rationale" in script
    assert "현재 계획대로 추진이 어려운 행위" in html
    assert "허가·협의를 전제로 검토 가능한 행위" in html
    assert "하천·공간관리" in script
    assert "필지 도시계획 지정" in script
    assert "외부 규제범주" not in script
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
    assert "function parkBoundaryLayerEnabled" in script
    assert "function setParkBoundaryLayerVisible" in script
    assert "if (parkBoundaryLayerEnabled())" in script
    assert 'input.addEventListener("change", () => setParkBoundaryLayerVisible(input.checked))' in script
    assert 'setParkBoundaryLayerVisible(true)' in script
    assert ".park-boundary" in stylesheet
    assert ".park-boundary.is-active" in stylesheet
    assert "공원색은 규제등급을 의미하지 않습니다" in html


def test_river_map_provides_focus_mode_and_text_overlap_summary() -> None:
    html = _read(RIVER_ROOT / "index.html")
    script = _read(RIVER_ROOT / "map.js")
    stylesheet = _read(RIVER_ROOT / "map.css")
    dashboard_html = _read(ASSET_ROOT / "index.html")

    assert 'id="layer-focus-status"' in html
    assert 'id="clear-all-layers"' in html
    assert "전체 레이어 해제" in html
    assert html.count('class="layer-focus-button"') == 8
    assert html.count('aria-pressed="false"') >= 8
    assert 'id="overlap-summary"' in html
    assert 'id="overlap-count"' in html
    assert 'id="overlap-badges"' in html
    assert 'id="overlap-summary-note"' in html

    assert "function setFocusedLayer" in script
    assert "function clearAllLayers" in script
    assert "function applyLayerReadability" in script
    assert "function renderOverlapSummary" in script
    assert "function renderDecisionBasis" in script
    assert "input.checked = true" in script
    assert 'node.classList.remove("is-hidden")' in script
    assert "clearAllLayersButton.disabled = false" in script
    assert '[data-park-boundary-layer], [data-layer], [data-regulation-layer]' in script
    assert "input.checked = showAll" in script
    assert 'parkBoundaryPathNodes.forEach(({ node }) => node.classList.toggle("is-hidden", !showAll))' in script
    assert "setView(INITIAL.lon, INITIAL.lat, INITIAL.zoom)" in script
    assert 'classList.toggle("is-focus-layer"' in script
    assert 'classList.toggle("is-context-layer"' in script
    assert 'setAttribute("aria-pressed"' in script
    assert "provider_error" in script
    assert "invalid_response" in script
    assert "function fitFocusedFeatures" not in script
    assert "fitFocusedFeatures(focusedLayer)" not in script
    assert "현재 지도 중심과 배율을 유지한 채" in script
    assert "function updateFocusButtons" in script
    assert "GeometryCollection" in script
    assert 'className = "focus-feature-label"' in script
    assert "공원 경계가 아닌 RIMGIS 하천공간관리 구간" in html
    assert "잠정 변경정보" in html
    assert "도형 미확보" in html
    assert 'fetch("wetland_boundary.geojson"' in script
    assert 'fetch("wetland_boundary_source_metadata.json"' in script
    assert "full_extent_snapshot" in script
    assert "전수경계 지도 표시" in script
    assert 'cache: "no-store"' in script

    assert ".layer-focus-button" in stylesheet
    assert ".is-focus-layer" in stylesheet
    assert ".is-context-layer" in stylesheet
    assert ".overlap-summary" in stylesheet
    assert ".overlap-badge" in stylesheet
    assert ".assessment-support-list" in stylesheet
    assert ".policy-evidence-summary" in stylesheet
    assert ".policy-evidence-counts" in stylesheet
    assert ".policy-parcel-facts" in stylesheet
    assert ".policy-insight-meta" in stylesheet
    assert ".policy-action-matrix" in stylesheet
    assert ".action-screening-card" in stylesheet
    assert ".legal-basis-card" in stylesheet
    assert ".focus-feature-label" in stylesheet
    assert "#river-map.has-focused-layer #tile-layer" in stylesheet
    assert "stroke-opacity:.72" in stylesheet
    assert "#river-map.has-focused-layer .park-label{opacity:.86}" in stylesheet

    assert html.count(">강조</button>") == 4

    version = "20260830-focus-preserve-v22"
    assert f"map.css?v={version}" in html
    assert f"map.js?v={version}" in html
    assert f'river-map/index.html?v={version}' in dashboard_html


def test_clear_all_layers_button_restores_every_layer_on_second_click() -> None:
    script = _read(RIVER_ROOT / "map.js")
    clear_all_layers = script.split("  function clearAllLayers() {", 1)[1].split(
        "\n  }\n\n  function riverOverlapItems", 1
    )[0]

    assert "const showAll = layerInputs.every((input) => !input.checked);" in clear_all_layers
    assert "input.checked = showAll" in clear_all_layers
    assert clear_all_layers.count('classList.toggle("is-hidden", !showAll)') == 3
    assert 'showAll ? "전체 레이어를 다시 표시합니다."' in clear_all_layers
    assert (
        'clearAllLayersButton.textContent = allLayersCleared()\n'
        '      ? "전체 레이어 켜기"\n'
        '      : "전체 레이어 해제";'
    ) in script


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
    assert 'data-map-src="map/index.html?v=20260830-map-layout-v67"' in html
    assert 'data-tab-panel="vacant"' in html
    assert 'data-vacant-map-src="vacant-map/index.html?v=20260830-map-layout-v67"' in html
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
    assert all(
        item["geometry"]["type"] in {"Polygon", "MultiPolygon"}
        and shape(item["geometry"]).is_valid
        and not shape(item["geometry"]).is_empty
        and shape(item["geometry"]).area > 0
        for item in data["features"]
    )
    layer_counts = {
        layer: sum(
            item["properties"]["zone_type"] == layer for item in data["features"]
        )
        for layer in ("river_area", "general_conservation", "waterfront", "restoration")
    }
    assert layer_counts == {
        "river_area": 18,
        "general_conservation": 2,
        "waterfront": 5,
        "restoration": 1,
    }
    assert metadata["source_system"] == "RIMGIS"
    assert metadata["retrieved_at"].startswith("2026-08-29")
    assert metadata["legal_effect"] is False
    assert metadata["geometry_interpretation"]["waterfront_is_park_boundary"] is False
    assert metadata["preliminary_change_context"][0]["geometry_available"] is False
    assert len(metadata["sha256"]) == 64
    assert regulation_metadata["legal_effect"] is False
    assert regulation_metadata["published_at"] == "2026-08-29"
    assert {source["category"] for source in regulation_metadata["sources"]} == {
        "wetland",
        "heritage",
        "urban_park",
        "land_use",
    }
    wetland_source = next(
        source for source in regulation_metadata["sources"]
        if source["category"] == "wetland"
    )
    assert wetland_source["full_extent_published"] is True
    assert wetland_source["deduplicated_feature_count"] == 1
    assert wetland_source["notice"].startswith("환경부 고시 제2009-34호")


def test_wetland_full_extent_snapshot_is_valid_deduplicated_and_traceable() -> None:
    boundary_path = RIVER_ROOT / "wetland_boundary.geojson"
    boundary_bytes = boundary_path.read_bytes()
    collection = json.loads(boundary_bytes.decode("utf-8"))
    metadata = json.loads(
        _read(RIVER_ROOT / "wetland_boundary_source_metadata.json")
    )

    assert collection["type"] == "FeatureCollection"
    assert len(collection["features"]) == 1
    feature = collection["features"][0]
    geometry = shape(feature["geometry"])
    assert geometry.geom_type == "MultiPolygon"
    assert geometry.is_valid and not geometry.is_empty
    assert geometry.bounds[0] < 128.89
    assert geometry.bounds[1] < 35.04
    assert geometry.bounds[2] > 128.96
    assert geometry.bounds[3] > 35.11
    assert feature["properties"]["category"] == "wetland"
    assert feature["properties"]["label"] == "낙동강하구 습지보호지역"
    assert feature["properties"]["delivery"] == "full_extent_snapshot"
    assert feature["properties"]["corroborating_datasets"] == [
        "LT_C_UM901",
        "LT_C_WGISARWET",
    ]

    assert metadata["feature_count"] == 1
    assert metadata["dataset_feature_counts"] == {
        "LT_C_UM901": 2,
        "LT_C_WGISARWET": 1,
    }
    assert metadata["notice"]["number"] == "환경부 고시 제2009-34호"
    assert hashlib.sha256(boundary_bytes).hexdigest() == metadata["sha256"]
