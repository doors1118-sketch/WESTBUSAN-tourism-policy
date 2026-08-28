from __future__ import annotations

import json
from pathlib import Path

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

    assert 'data-tab-target="river">낙동강 공원 규제·행위 검토<' in nav
    assert nav.index('data-tab-target="vacant"') < nav.index('data-tab-target="river"')
    assert nav.index('data-tab-target="river"') < nav.index('data-tab-target="insights"')
    assert 'data-river-map-src="river-map/index.html?v=20260828-river-review-v1"' in html
    assert '<iframe src="river-map/index.html"' not in html
    assert 'target === "river"' in script
    assert "riverMapSrc" in script


def test_river_map_exposes_five_parks_layers_and_click_assessment() -> None:
    html = _read(RIVER_ROOT / "index.html")
    script = _read(RIVER_ROOT / "map.js")
    stylesheet = _read(RIVER_ROOT / "map.css")

    for park in ("화명생태공원", "대저생태공원", "삼락생태공원", "맥도생태공원", "을숙도생태공원"):
        assert park in html
    for label in ("하천구역", "일반보전지구", "근린친수지구", "복원지구"):
        assert label in html
    for activity in ("산책·탐방", "축제·행사", "야영·캠핑", "숙박시설", "주차장·진입도로"):
        assert activity in html
    assert "pointInFeature" in script
    assert "assessSelection" in script
    assert "RIMGIS" in html
    assert "법적 효력" in html
    assert "@media (max-width: 760px)" in stylesheet


def test_river_reference_bundle_has_traceable_precise_geometry() -> None:
    data = json.loads(_read(RIVER_ROOT / "river_layers.geojson"))
    metadata = json.loads(_read(RIVER_ROOT / "source_metadata.json"))

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
