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

    assert 'data-map-src="map/index.html?v=20260821-policy-map-v6"' in html
    assert '<iframe src="map/index.html"' not in html
    assert 'target === "map"' in script
    assert "방문·체류·소비·교통수요가 어디에서 얼마나 발생하는가" in html
    assert "숙박 객실·관광숙박 비중·시설 규모·노후도·신규 진입" in html
    assert "신규 공급·리모델링·빈집 전환·콘텐츠 투자" in html


def test_dashboard_versions_static_assets_to_prevent_stale_ui() -> None:
    """Catches a new HTML release reusing cached CSS or JavaScript bytes."""
    html = _asset("index.html")

    assert 'href="app.css?v=20260822-fixed-sidebar-v20"' in html
    assert 'src="app.js?v=20260822-fixed-sidebar-v20"' in html


def test_map_tab_can_request_ai_explanation_for_published_priority_map() -> None:
    """Catches the map losing its explicit, user-triggered AI explanation surface."""
    html = _asset("index.html")
    script = _asset("app.js")

    assert 'data-map-insight-button' in html
    assert 'data-map-insight-result' in html
    assert "AI로 지도 설명" in html
    assert 'requestInsight("west")' in script
    assert 'data-map-insight-button' in script


def test_map_tab_explains_the_policy_decisions_before_interaction() -> None:
    """Catches the spatial tab reverting to an unexplained technical grid."""
    html = _asset("index.html")

    for label in (
        "관광수요 대비 숙박공급 부족",
        "숙박시설 밀집도",
        "노후 숙박시설 밀집도",
        "신규 숙박업 진입",
        "신규 공급",
        "리모델링",
        "관광숙박 전환",
    ):
        assert label in html
    assert "시설 좌표 확인률이 0%" not in html
    assert "관광 숙박 투자기회 AI 해석" in html


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

    assert regions["west"]["roomCoverageShare"] == 90.3
    assert regions["east"]["roomCoverageShare"] == 51.8
    assert regions["west"]["visitorDailyAverage"] == 371553
    assert regions["west"]["licenseAgeAverageYears"] == 28.0
    assert document["availability"]["transport"] == "preparing"
    assert document["availability"]["stayDuration"] == "preparing"
    assert document["metricNotes"]["consumptionIndex"].startswith("원천지표")


def test_overview_leads_with_exclusive_facility_mix_not_room_coverage() -> None:
    document = json.loads(_asset("data.json"))
    script = _asset("app.js")
    west = next(row for row in document["regions"] if row["id"] == "west")

    assert west["facilities"] == 431
    assert west["rooms"] == 10442
    assert west["facilityMix"] == [
        {"id": "tourism", "name": "관광숙박", "facilities": 3},
        {"id": "general", "name": "일반숙박", "facilities": 389},
        {"id": "other", "name": "기타", "facilities": 39},
    ]
    assert sum(item["facilities"] for item in west["facilityMix"]) == west["facilities"]
    assert "서부산 숙박업체" in script
    assert 'value(west.facilities, "개소")' in script
    assert 'value(west.rooms, "실")' in script
    assert "객실 확인률 ${west.roomCoverageShare}%" not in script


def test_overview_separates_absolute_demand_from_supply_pressure() -> None:
    script = _asset("app.js")

    assert 'kpi("서부산 일평균 방문수요", value(west.visitorDailyAverage)' in script
    assert 'kpi("방문수요 대비 객실공급 압력", value(west.demandPer100Rooms)' in script
    assert "숙박객·객실점유율이 아닌 정책 검토용 파생지표" in script
    assert 'kpi("객실 100실당 방문수요"' not in script


def test_facility_card_renders_rooms_as_a_smaller_secondary_value() -> None:
    script = _asset("app.js")
    stylesheet = _asset("app.css")

    assert "facilitySupplyKpi(west, east)" in script
    assert 'node("span", "secondary-value"' in script
    assert ".secondary-value" in stylesheet
    assert "font-size:.68em" in stylesheet


def test_overview_has_eight_distinct_policy_cards() -> None:
    script = _asset("app.js")

    for label in (
        "서부산 숙박업체",
        "서부산 일평균 방문수요",
        "관광숙박 등록",
        "2021년 이후 신규",
        "관광소비 지표",
        "20년 이상 노후시설",
        "인허가 평균업력",
        "외국인 대상 관광등록",
    ):
        assert label in script

    overview_block = script.split('const overview = document.querySelector("[data-overview-kpis]");', 1)[1]
    overview_block = overview_block.split('const summary = document.querySelector("[data-region-summary]");', 1)[0]
    assert overview_block.count("kpi(") == 7
    assert overview_block.count("facilitySupplyKpi(west, east)") == 1
    assert "통화금액이 아닌 원천지표" in overview_block
    assert "관광숙박·외국인관광 도시민박" in overview_block


def test_facility_mix_wraps_only_between_complete_category_chips() -> None:
    script = _asset("app.js")
    stylesheet = _asset("app.css")

    assert 'node("small", "facility-mix")' in script
    assert 'node("span", "facility-mix-item"' in script
    assert ".facility-mix-item" in stylesheet
    assert "white-space:nowrap" in stylesheet


def test_overview_cards_use_equal_height_grid_tracks() -> None:
    stylesheet = _asset("app.css")

    assert "[data-overview-kpis]{grid-auto-rows:1fr}" in stylesheet
    assert "[data-overview-kpis] .kpi{height:100%}" in stylesheet


def test_overview_comparison_badges_are_all_percent_of_east_busan() -> None:
    script = _asset("app.js")
    overview_block = script.split('const overview = document.querySelector("[data-overview-kpis]");', 1)[1]
    overview_block = overview_block.split('const summary = document.querySelector("[data-region-summary]");', 1)[0]

    assert "function relativeToEast" in script
    assert 'relativeToEast(west.facilities, east.facilities)' in script
    assert overview_block.count("relativeToEast(") == 7
    assert "`동부산 ${" not in overview_block


def test_visitor_demand_uses_plain_language_in_the_ui() -> None:
    script = _asset("app.js")

    assert "외지인+외국인 · 일별 방문인원 평균" in script
    assert '"방문자-인일/일' not in script


def test_hero_copy_omits_internal_publication_and_grid_details() -> None:
    html = _asset("index.html")

    assert "민간투자 검토지역을 데이터로 비교합니다" in html
    assert "하나의 발행" not in html
    assert "500m 공간격자" not in html
    assert "방문수요 355일" not in html


def test_desktop_navigation_is_grouped_vertically_and_mobile_stays_horizontal() -> None:
    html = _asset("index.html")
    script = _asset("app.js")
    stylesheet = _asset("app.css")

    assert 'class="dashboard-shell" data-dashboard-shell' in html
    assert 'class="sidebar"' in html
    assert 'class="sidebar-brand"' in html
    assert 'class="dashboard-content"' in html
    for group in ("현황", "정책분석", "공간분석"):
        assert f'<span class="tabs-group-title">{group}</span>' in html
    assert ".sidebar{position:fixed;inset:0 auto 0 0;width:250px" in stylesheet
    assert "body{padding-left:250px}" in stylesheet
    assert ".topbar .brand{display:none}" in stylesheet
    assert "flex-direction:column" in stylesheet
    assert "@media(max-width:1180px){body{padding-left:0}" in stylesheet
    assert ".sidebar{position:static;width:auto;height:auto" in stylesheet
    assert ".tabs-group{display:contents}" in stylesheet
    assert 'shell.classList.toggle("map-mode", target === "map")' not in script
    assert 'item.dataset.tabTarget === initialTarget' in script


def test_overview_comparison_badges_are_pinned_to_each_card_bottom() -> None:
    stylesheet = _asset("app.css")

    assert "[data-overview-kpis] .kpi{display:flex;flex-direction:column}" in stylesheet
    assert "[data-overview-kpis] .kpi .delta{align-self:flex-start;margin-top:auto}" in stylesheet


def test_vacant_house_tab_does_not_embed_exact_locations() -> None:
    html = _asset("index.html")
    document = json.loads(_asset("data.json"))

    assert "정확주소는 권한이 있는 내부 상세화면에서만 제공" in html
    assert "vacantHouses" not in document
    assert "parcel" not in json.dumps(document).lower()
