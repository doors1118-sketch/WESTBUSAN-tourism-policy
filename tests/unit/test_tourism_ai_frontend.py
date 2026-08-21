from __future__ import annotations

import json
import re
from pathlib import Path

ASSET_ROOT = (
    Path(__file__).parents[2]
    / "src"
    / "westbusan"
    / "tourism_dashboard"
    / "assets"
)


def _asset(name: str) -> str:
    return (ASSET_ROOT / name).read_text(encoding="utf-8")


def test_dashboard_exposes_three_decision_questions_and_required_tabs() -> None:
    html = _asset("index.html")

    assert "관광경제활력 도시 부산<br>" in html
    assert "동서 관광객 체류전환의 격차" in html
    assert "관광경제활력 도시 부산," not in html

    for label in (
        "종합현황",
        "관광 종합현황",
        "공급 격차",
        "민간투자 유도",
        "정책 인사이트 도출",
        "공간지도",
        "빈집",
    ):
        assert label in html

    assert 'data-room-donut' in html
    assert 'data-supply-donuts' in html


def test_dashboard_omits_unavailable_stay_duration_and_builds_region_donuts() -> None:
    html = _asset("index.html")
    script = _asset("app.js")

    assert "체류시간" not in html
    assert "체류시간" not in script
    assert "region-donut-card" in script
    assert "facilityShare" in script


def test_large_spatial_map_is_loaded_only_after_map_tab_is_selected() -> None:
    html = _asset("index.html")
    script = _asset("app.js")

    assert 'data-map-src="map/index.html?v=20260821-priority-v3"' in html
    assert '<iframe src="map/index.html"' not in html
    assert 'target === "map"' in script
    assert "방문·체류·소비·교통수요가 어디에서 얼마나 발생하는가" in html
    assert "숙박 객실·관광숙박 비중·시설 규모·노후도·신규 진입" in html
    assert "신규 공급·리모델링·빈집 전환·콘텐츠 투자" in html


def test_map_tab_can_request_ai_explanation_for_published_priority_map() -> None:
    """Catches the map losing its explicit, user-triggered AI explanation surface."""
    html = _asset("index.html")
    script = _asset("app.js")

    assert 'data-map-insight-button' in html
    assert 'data-map-insight-result' in html
    assert "AI로 지도 설명" in html
    assert 'requestInsight("west")' in script
    assert 'data-map-insight-button' in script


def test_page_load_does_not_generate_insights() -> None:
    script = _asset("app.js")

    assert 'addEventListener("click"' in script
    assert 'fetch("api/insights"' in script
    fetch_position = script.index('fetch("api/insights"')
    click_position = script.rindex('addEventListener("click"', 0, fetch_position)
    assert click_position < fetch_position


def test_model_output_is_rendered_as_text_not_html() -> None:
    script = _asset("app.js")

    assert ".textContent =" in script
    assert not re.search(r"innerHTML\s*=\s*.*(?:insight|finding|option|response)", script)
    assert "insertAdjacentHTML" not in script


def test_frontend_contains_no_api_key_or_secret_placeholder() -> None:
    combined = "\n".join(_asset(name) for name in ("index.html", "app.js", "data.json"))

    assert "OPENAI_API_KEY" not in combined
    assert not re.search(r"sk-(?:proj-)?[A-Za-z0-9_-]{16,}", combined)
    assert "exactVacantHouseAddress" not in combined


def test_metric_document_preserves_units_coverage_and_missing_state() -> None:
    document = json.loads(_asset("data.json"))
    regions = {row["id"]: row for row in document["regions"]}

    assert regions["west"]["roomCoverageShare"] == 90.5
    assert regions["east"]["roomCoverageShare"] == 51.8
    assert regions["west"]["visitorDailyAverage"] == 371553
    assert regions["west"]["licenseAgeAverageYears"] == 28.0
    assert document["availability"]["transport"] == "preparing"
    assert document["availability"]["stayDuration"] == "preparing"
    assert document["metricNotes"]["consumptionIndex"].startswith("원천지표")


def test_vacant_house_tab_does_not_embed_exact_locations() -> None:
    html = _asset("index.html")
    document = json.loads(_asset("data.json"))

    assert "정확주소는 권한이 있는 내부 상세화면에서만 제공" in html
    assert "vacantHouses" not in document
    assert "parcel" not in json.dumps(document).lower()
