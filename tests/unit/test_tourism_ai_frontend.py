from __future__ import annotations

import json
import re
from html.parser import HTMLParser
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


class _WorkationCandidateTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._inside_table = False
        self._inside_body = False
        self._inside_cell = False
        self._cell_parts: list[str] = []
        self._row: list[str] | None = None
        self.rows: list[list[str]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if tag == "table" and "data-workation-candidate-table" in attributes:
            self._inside_table = True
        elif self._inside_table and tag == "tbody":
            self._inside_body = True
        elif self._inside_body and tag == "tr":
            self._row = []
        elif self._row is not None and tag == "td":
            self._inside_cell = True
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        if self._inside_cell:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._inside_cell and tag == "td":
            assert self._row is not None
            self._row.append(" ".join("".join(self._cell_parts).split()))
            self._inside_cell = False
        elif self._inside_body and tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif self._inside_table and tag == "tbody":
            self._inside_body = False
        elif self._inside_table and tag == "table":
            self._inside_table = False


def test_dashboard_exposes_three_decision_questions_and_required_tabs() -> None:
    html = _asset("index.html")

    assert "관광경제활력 도시 부산<br>" in html
    assert "동서 관광객 체류전환의 격차" in html
    assert "관광경제활력 도시 부산," not in html

    for label in (
        "종합현황",
        "서부산 자치구 현황",
        "동서 숙박공급 격차",
        "서부산 숙박시설 투자정보",
        "서부산 빈집 관광숙박 투자 정보",
        "낙동강 관광자원화 투자 정보",
        "AI 종합 분석",
    ):
        assert label in html

    assert 'data-room-donut' in html
    assert 'data-supply-donuts' in html


def test_mobile_dashboard_gives_investment_map_enough_embedded_height() -> None:
    """Keeps the reordered investment map substantially visible inside its iframe."""
    stylesheet = _asset("app.css")

    mobile_block = stylesheet.split("@media(max-width:600px)")[-1]
    assert ".investment-map-card iframe{height:940px}" in mobile_block


def test_dashboard_resizes_same_origin_map_frames_to_their_document_height() -> None:
    """Prevents nested iframe scrollbars from hiding map results after the redesign."""
    script = _asset("app.js")

    assert 'event.origin !== window.location.origin' in script
    assert 'event.data.type !== "westbusan:map-height"' in script
    assert "frame.contentWindow === event.source" in script
    assert "Math.min(3600" in script


def test_supply_gap_compares_east_west_demand_and_reception_capacity() -> None:
    """Catches the supply tab losing the demand evidence that explains the gap."""
    html = _asset("index.html")
    script = _asset("app.js")
    stylesheet = _asset("app.css")
    document = json.loads(_asset("data.json"))

    assert 'data-tab-target="supply">동서 숙박공급 격차<' in html
    assert "동·서부산 숙박공급 격차" in html
    assert 'data-supply-gap-summary' in html
    assert 'data-visitor-demand-bars' in html
    assert "외지인·외국인 방문수요" in html
    assert "관광객 순인원·숙박객이 아닌 일별 추정 방문인원" in html
    assert "객실자료 확보율" in html
    assert "전체 숙박시설 중 객실 수가 확인된 시설의 비율" in html
    assert "객실 확인률" not in html

    regions = {region["id"]: region for region in document["regions"]}
    assert regions["west"]["nonlocalVisitorDailyAverage"] == 346768.6
    assert regions["west"]["foreignVisitorDailyAverage"] == 24784.7
    assert regions["east"]["nonlocalVisitorDailyAverage"] == 435173.1
    assert regions["east"]["foreignVisitorDailyAverage"] == 27476.9
    for region in regions.values():
        split_total = (
            region["nonlocalVisitorDailyAverage"]
            + region["foreignVisitorDailyAverage"]
        )
        assert round(split_total) == region["visitorDailyAverage"]

    assert "function renderSupplyGapSummary" in script
    assert "function renderVisitorDemandComparison" in script
    assert '"외지인 방문수요"' in script
    assert '"외국인 방문수요"' in script
    assert "외국인 방문수요는 동부산의" in script
    assert "외국인 숙박 대응시설은 동부산의" in script
    assert 'label: "외국인 숙박 대응시설"' in script
    assert "관광숙박업·외국인관광 도시민박업 등록시설 비율" in script
    assert 'label: "2021년 이후 숙박업 등록"' in script
    assert "현재 영업시설 중 최초 인허가일 2021.1.1 이후 비율" in script
    assert 'label: "전체 숙박업체"' in script
    assert "현재 영업 중인 전체 숙박업체 수" in script
    assert 'west: value(west.facilities, "개소")' in script
    assert 'east: value(east.facilities, "개소")' in script
    assert "서부산은 동부산의" in script
    assert "괄호는 일반숙박 비중" not in script
    assert "서부산의 일반숙박 비중이 동부산보다" not in script
    for selector in (
        ".supply-gap-summary",
        ".supply-gap-stat",
        ".visitor-demand-card",
        ".visitor-demand-row",
        ".visitor-demand-bar",
        ".visitor-demand-insight",
    ):
        assert selector in stylesheet
    assert "grid-template-columns:repeat(5,minmax(0,1fr))" in stylesheet
    assert "featured: true" not in script


def test_supply_tab_orders_evidence_and_adds_cached_east_west_ai_analysis() -> None:
    """Keeps the supply decision flow and AI analysis tied to one publication."""
    html = _asset("index.html")
    script = _asset("app.js")
    stylesheet = _asset("app.css")

    supply = html.split('data-tab-panel="supply"', 1)[1].split(
        'data-tab-panel="investment"', 1
    )[0]
    visitor = supply.index('data-visitor-demand-bars')
    donuts = supply.index('data-supply-donuts')
    registrations = supply.index('data-registration-type-bars')
    ai_analysis = supply.index('data-supply-insight-button')
    assert visitor < donuts < registrations < ai_analysis

    assert "서부산·동부산 공급구조 AI 비교" in html
    assert 'data-supply-insight-result' in html
    assert 'requestInsight("all")' in script
    assert "supplyInsightPromises" in script
    assert "dashboardData.publishedRun" in script
    assert 'data-supply-insight-cache' in script
    for selector in (
        ".supply-ai-control",
        ".supply-ai-result",
        ".supply-ai-findings",
    ):
        assert selector in stylesheet


def test_supply_donuts_are_graph_focused_without_redundant_detail_text() -> None:
    script = _asset("app.js")
    block = script.split('const supplyDonuts', 1)[1].split(
        'const supplyBody', 1
    )[0]

    assert 'node("h3", "", region.name)' in block
    assert "부산 시설 비중" in block
    assert "숙박시설 ${value(region.facilities" not in block
    assert "확인 객실" not in block
    assert "2021년 이후 숙박업 등록" not in block


def test_supply_gap_compares_registration_type_facilities_by_region() -> None:
    """Keeps the separate registration-type chart facility based and source backed."""
    html = _asset("index.html")
    script = _asset("app.js")
    stylesheet = _asset("app.css")
    document = json.loads(_asset("data.json"))

    assert "등록유형별 숙박업체 비교" in html
    assert 'data-registration-type-bars' in html
    assert "업체 수 기준" in html
    assert "등록유형 기준·복수 등록 중복 가능" in html
    assert "function renderRegistrationTypeComparison" in script

    expected = {
        "lodgings": ("일반숙박", {"west": 389, "east": 713, "other": 1132}),
        "tourist_accommodations": (
            "관광숙박",
            {"west": 3, "east": 155, "other": 48},
        ),
        "foreigner_city_homestays": (
            "외국인도시민박",
            {"west": 8, "east": 372, "other": 115},
        ),
        "rural_homestays": (
            "농어촌민박",
            {"west": 31, "east": 132, "other": 0},
        ),
        "hanok_experience": (
            "한옥체험",
            {"west": 0, "east": 5, "other": 0},
        ),
    }
    actual = {
        item["id"]: (item["name"], item["regions"])
        for item in document["registrationTypes"]
    }
    assert actual == expected

    for selector in (
        ".registration-type-card",
        ".registration-type-chart",
        ".registration-type-row",
        ".registration-type-bar",
        ".registration-type-legend",
    ):
        assert selector in stylesheet


def test_dashboard_omits_unavailable_stay_duration_and_builds_region_donuts() -> None:
    html = _asset("index.html")
    script = _asset("app.js")

    assert "체류시간" not in html
    assert "체류시간" not in script
    assert "region-donut-card" in script
    assert "facilityShare" in script


def test_large_spatial_map_is_loaded_only_after_investment_tab_is_selected() -> None:
    html = _asset("index.html")
    script = _asset("app.js")

    assert 'data-map-src="map/index.html?v=20260830-map-layout-v67"' in html
    assert '<iframe src="map/index.html"' not in html
    assert 'target === "investment"' in script
    assert "방문·체류·소비·교통수요가 어디에서 얼마나 발생하는가" in html
    assert "숙박 객실·관광숙박 비중·시설 규모·노후도·신규 진입" in html
    assert "신규 공급·리모델링·빈집 전환·콘텐츠 투자" in html


def test_dashboard_versions_static_assets_to_prevent_stale_ui() -> None:
    """Catches a new HTML release reusing cached CSS or JavaScript bytes."""
    html = _asset("index.html")

    assert 'href="app.css?v=20260830-map-layout-v25"' in html
    assert 'src="app.js?v=20260830-map-layout-v25"' in html


def test_comprehensive_tab_has_all_report_sections_and_print_action() -> None:
    html = _asset("index.html")
    script = _asset("app.js")
    stylesheet = _asset("app.css")

    assert 'data-report-button' in html
    assert 'data-report-print' in html
    assert html.count('data-report-section=') == 8
    for section_id in (
        "executive_summary",
        "tourism_supply",
        "east_west_gap",
        "west_districts",
        "accommodation_investment",
        "vacant_hubs",
        "policy_programmes",
        "limitations",
    ):
        assert f'data-report-section="{section_id}"' in html
    assert 'fetch("api/report"' in script
    assert 'body: JSON.stringify({ scope: "west" })' in script
    assert 'window.print()' in script
    assert "@media print" in stylesheet


def test_investment_information_can_request_ai_explanation_for_priority_map() -> None:
    """Catches the map losing its explicit, user-triggered AI explanation surface."""
    html = _asset("index.html")
    script = _asset("app.js")

    assert 'data-map-insight-button' in html
    assert 'data-map-insight-result' in html
    assert "AI 투자정보 해석" in html
    assert 'requestInsight("west")' in script
    assert 'data-map-insight-button' in script


def test_investment_information_explains_policy_decisions_before_interaction() -> None:
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


def test_investment_information_combines_actions_map_ai_and_district_summary() -> None:
    """Catches investment actions and the practical map drifting into separate tabs."""
    html = _asset("index.html")

    assert 'data-tab-target="investment">서부산 숙박시설 투자정보<' in html
    assert 'data-tab-target="map"' not in html
    assert 'data-tab-panel="map"' not in html

    investment = html.split('data-tab-panel="investment"', 1)[1].split(
        'data-tab-panel="insights"', 1
    )[0]
    actions = investment.index('class="action-grid"')
    workflow = investment.index('class="investment-use-flow"')
    map_frame = investment.index('data-map-src=')
    ai = investment.index('data-map-insight-button')
    district_summary = investment.index('data-district-table')
    assert actions < workflow < map_frame < ai < district_summary
    assert "지역·번호 선택" in investment
    assert "수요·공급·노후도 비교" in investment
    assert "AI 투자해석 확인" in investment
    assert "구별 검토순위 요약" in investment


def test_navigation_orders_decision_tabs_and_places_ai_analysis_last() -> None:
    """Catches the task flow becoming scattered across the top navigation."""
    html = _asset("index.html")
    nav = html.split('<nav class="tabs"', 1)[1].split("</nav>", 1)[0]

    expected = (
        'data-tab-target="overview">종합현황<',
        'data-tab-target="tourism">서부산 자치구 현황<',
        'data-tab-target="supply">동서 숙박공급 격차<',
        'data-tab-target="investment">서부산 숙박시설 투자정보<',
        'data-tab-target="vacant">서부산 빈집 관광숙박 투자 정보<',
        'data-tab-target="river">낙동강 관광자원화 투자 정보<',
        'data-tab-target="insights">AI 종합 분석<',
    )
    positions = [nav.index(item) for item in expected]
    assert positions == sorted(positions)


def test_legacy_map_hash_opens_investment_information() -> None:
    """Catches old bookmarked map URLs becoming dead links after tab consolidation."""
    script = _asset("app.js")

    assert 'function resolveTabTarget(target)' in script
    assert 'return target === "map" ? "investment" : target;' in script
    assert 'const initialTarget = resolveTabTarget(location.hash.slice(1));' in script


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
    assert '{ label: "객실 100실당 방문 압력", key: "demandPer100Rooms"' in script
    assert "숙박객·점유율이 아닌 공급 검토용 지표" in script
    assert 'kpi("객실 100실당 방문수요", value(west.demandPer100Rooms)' in script
    assert "일평균 방문수요 ÷ 확인 객실 × 100" in script
    assert "실제 숙박객 수·객실 가동률이 아닙니다" in script
    assert "방문량 대비 관광소비 상대지수" not in script


def test_overview_places_demand_first_and_preserves_eight_card_order() -> None:
    script = _asset("app.js")
    overview_block = script.split(
        'const overview = document.querySelector("[data-overview-kpis]");', 1
    )[1]
    overview_block = overview_block.split(
        'const summary = document.querySelector("[data-region-summary]");', 1
    )[0]
    expected_markers = (
        'kpi("서부산 일평균 방문수요"',
        "facilitySupplyKpi(west, east)",
        'kpi("관광숙박업 등록시설 비율"',
        'kpi("2021년 이후 숙박업 등록"',
        'kpi("객실 100실당 방문수요"',
        'kpi("건축연령 20년 이상 시설"',
        'kpi("평균 인허가 경과연수"',
        'kpi("외국인 숙박 대응시설"',
    )

    positions = [overview_block.index(marker) for marker in expected_markers]
    assert positions == sorted(positions)


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
        "서부산 일평균 방문수요",
        "서부산 숙박업체",
        "관광숙박업 등록시설 비율",
        "2021년 이후 숙박업 등록",
        "객실 100실당 방문수요",
        "건축연령 20년 이상 시설",
        "평균 인허가 경과연수",
        "외국인 숙박 대응시설",
    ):
        assert label in script

    overview_block = script.split('const overview = document.querySelector("[data-overview-kpis]");', 1)[1]
    overview_block = overview_block.split('const summary = document.querySelector("[data-region-summary]");', 1)[0]
    assert overview_block.count("kpi(") == 7
    assert overview_block.count("facilitySupplyKpi(west, east)") == 1
    assert "객실 비중이 아닙니다" in overview_block
    assert "연결 인허가의 가장 이른 인허가일" in overview_block
    assert "일평균 방문수요 ÷ 확인 객실 × 100" in overview_block
    assert "실제 숙박객 수·객실 가동률이 아닙니다" in overview_block
    assert "내부 리모델링 상태를 뜻하지 않습니다" in overview_block
    assert "동일 사업자의 영업기간과 다릅니다" in overview_block
    for note in (
        "전체 숙박시설 대비 · 시설 수 기준",
        "현재 영업시설 · 최초 인허가일 기준",
        "일평균 방문수요 ÷ 확인 객실 × 100",
        "건축물대장 사용승인일부터 산정",
        "최초 인허가일~기준일 평균",
    ):
        assert note in overview_block
    assert "자료 확인률" not in overview_block
    assert 'noteElement.classList.add("metric-definition")' in script
    assert "metric-definition" in _asset("app.css")
    assert "white-space:nowrap" in _asset("app.css")
    assert "text-overflow:ellipsis" in _asset("app.css")
    assert "관광숙박업·외국인관광 도시민박업" in overview_block


def test_tourism_tab_is_west_district_detail_with_haeundae_benchmark() -> None:
    html = _asset("index.html")
    script = _asset("app.js")
    document = json.loads(_asset("data.json"))

    assert '>서부산 자치구 현황</button>' in html
    assert "관광 종합현황</button>" not in html
    tourism_panel = html.split('data-tab-panel="tourism"', 1)[1].split('data-tab-panel="supply"', 1)[0]
    for marker in (
        "data-west-district-summary",
        "data-district-chart-metrics",
        "data-west-district-chart",
        "data-west-district-tabs",
        "data-district-profile",
        "data-district-comparison",
        "data-district-policy",
    ):
        assert marker in tourism_panel
    assert "data-tourism-kpis" not in tourism_panel
    assert "data-demand-bars" not in tourism_panel
    assert "data-source-indices" not in tourism_panel
    assert "해운대구를 동일 기준 비교값으로 사용" in tourism_panel

    assert "function renderDistrictDetail" in script
    assert "function renderWestDistrictSummary" in script
    assert "district-summary-rank" not in script
    assert "우선순위 ${district.rank}" not in script
    assert "function renderWestDistrictChart" in script
    assert "district-chart-bar" in script
    assert "district-comparison-row" in script
    assert "benchmarkDistrict" in script
    assert len(document["westDistricts"]) == 4
    assert document["benchmarkDistrict"]["name"] == "해운대구"
    for district in [*document["westDistricts"], document["benchmarkDistrict"]]:
        for field in (
            "facilities",
            "rooms",
            "roomCoverageShare",
            "roomMedian",
            "visitorDailyAverage",
            "demandPer100Rooms",
            "tourismFacilityShare",
            "foreignCapableShare",
            "old20Share",
            "recentLicenseShare",
            "consumptionIndex",
            "stay3Index",
        ):
            assert field in district


def test_district_chart_marks_city_shares_without_publishing_a_priority_rank() -> None:
    html = _asset("index.html")
    script = _asset("app.js")
    document = json.loads(_asset("data.json"))

    city_facilities = sum(region["facilities"] for region in document["regions"])
    city_rooms = sum(region["rooms"] for region in document["regions"])
    city_visitors = sum(region["visitorDailyAverage"] for region in document["regions"])
    assert city_facilities == 3103
    assert city_rooms == 67949
    assert city_visitors == 1814315
    assert [round(district["facilities"] / city_facilities * 100, 1) for district in document["westDistricts"]] == [2.4, 2.8, 3.2, 5.5]
    assert [district["name"] for district in sorted(document["westDistricts"], key=lambda item: item["demandPer100Rooms"], reverse=True)] == ["강서구", "사하구", "북구", "사상구"]

    assert "data-district-priority-note" in html
    assert "우선순위 산정 기준" not in html
    assert "자치구 비교 지표" in html
    assert "괄호는 부산 전체 비중" in html
    assert "객실 100실당 일평균 방문수요" in html
    assert "최종 투자순위를 의미하지 않습니다" in html
    assert "function districtCityShare" in script
    assert script.count("cityShare: true") == 3
    assert "function loadDistrictInsight" in script
    assert "districtInsightPromises" in script
    assert 'requestInsight("west", district.id)' in script
    assert "AI 정책검토 포인트" in script
    assert "저장 분석" in script


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
    document = json.loads(_asset("data.json"))
    overview_block = script.split('const overview = document.querySelector("[data-overview-kpis]");', 1)[1]
    overview_block = overview_block.split('const summary = document.querySelector("[data-region-summary]");', 1)[0]
    regions = {region["id"]: region for region in document["regions"]}

    assert "function relativeToEast" in script
    assert 'relativeToEast(west.facilities, east.facilities)' in script
    assert 'relativeToEast(west.demandPer100Rooms, east.demandPer100Rooms)' in overview_block
    assert regions["west"]["demandPer100Rooms"] == 3558.3
    assert regions["east"]["demandPer100Rooms"] == 1997.5
    assert round(3558.3 / 1997.5 * 100, 1) == 178.1
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


def test_navigation_is_horizontal_and_placed_before_the_hero() -> None:
    html = _asset("index.html")
    script = _asset("app.js")
    stylesheet = _asset("app.css")

    assert 'class="dashboard-content"' in html
    assert '<main>\n    <nav class="tabs"' in html
    assert html.index('<nav class="tabs"') < html.index('<section class="hero">')
    assert 'class="sidebar"' not in html
    assert 'class="tab-index"' not in html
    assert "body{padding-left:0!important}" in stylesheet
    assert ".topbar .brand{display:flex!important}" in stylesheet
    assert "main>.tabs{display:flex;flex-direction:row" in stylesheet
    assert 'shell.classList.toggle("map-mode", target === "map")' not in script
    assert 'item.dataset.tabTarget === initialTarget' in script


def test_overview_comparison_badges_are_pinned_to_each_card_bottom() -> None:
    stylesheet = _asset("app.css")

    assert "[data-overview-kpis] .kpi{display:flex;flex-direction:column}" in stylesheet
    assert "[data-overview-kpis] .kpi .delta{align-self:flex-start;margin-top:auto}" in stylesheet


def test_monthly_trend_data_has_12_complete_months_and_explicit_semantics() -> None:
    document = json.loads(_asset("data.json"))
    trends = document["monthlyTrends"]

    assert len(trends) == 12
    assert trends[0]["period"] == "2025-07"
    assert trends[-1]["period"] == "2026-06"
    assert trends[0]["west"] == {
        "visitorDailyAverage": 367179.6,
        "newActiveFacilities": 1,
    }
    assert trends[-1]["west"] == {
        "visitorDailyAverage": 361358.7,
        "newActiveFacilities": 0,
    }
    assert trends[-1]["east"]["newActiveFacilities"] == 42
    for month in trends:
        assert set(month) == {"period", "west", "east", "other"}
        for region in ("west", "east", "other"):
            assert month[region]["visitorDailyAverage"] > 0
            assert month[region]["newActiveFacilities"] >= 0
    assert "현재 영업 중인 시설" in document["metricNotes"]["monthlyNewActiveFacilities"]
    assert "최초 인허가월" in document["metricNotes"]["monthlyNewActiveFacilities"]


def test_overview_contains_readable_demand_and_entry_combo_chart() -> None:
    html = _asset("index.html")
    script = _asset("app.js")
    stylesheet = _asset("app.css")

    for marker in (
        "data-monthly-trend",
        "data-trend-summary",
        "data-trend-chart",
        "data-trend-tooltip",
    ):
        assert marker in html
    assert "data-trend-region" not in html
    assert "월별 방문수요·신규 숙박업체 진입 추이" in html
    assert "최근 12개 완결월" not in html
    assert "현재 영업시설의 최초 인허가 월" in html
    assert "서부산·동부산을 같은 축에서 직접 비교" not in html
    assert "방문수요(천 명)" in html
    assert "신규 객실공급이 아님" in html
    assert "trend-footnote" not in html
    assert "trend-definition" in html

    assert "function renderMonthlyTrend" in script
    assert "function renderMonthlyTrend(data)" in script
    assert 'createElementNS("http://www.w3.org/2000/svg", "svg")' in script
    assert '"trend-line west"' in script
    assert '"trend-line east"' in script
    assert '"trend-bar west"' in script
    assert '"trend-bar east"' in script
    assert '"trend-demand-panel"' in script
    assert '"trend-entry-panel"' in script
    assert '"trend-west-entry-label"' in script
    assert '"trend-east-entry-label"' in script
    assert "const height = 390;" in script
    assert "const xInset = 32;" in script
    assert 'data-trend-region' not in script
    assert "월별 방문수요·신규 숙박업체 진입 추이" in _asset("index.html")
    assert '"신규 숙박업체 진입(개소)"' in script
    assert "12개월 신규 진입" in script
    assert "최초 인허가 시설(개소)" not in script
    assert "visitorDailyAverage" in script
    assert "newActiveFacilities" in script
    assert "Math.round(amount / 1000)" in script
    assert "현재 영업시설의 최초 인허가 월 기준" in script

    for selector in (
        ".trend-card",
        ".trend-summary",
        ".trend-chart",
        ".trend-line",
        ".trend-bar",
        ".trend-panel-label",
        ".trend-panel-divider",
        ".trend-west-entry-label",
        ".trend-east-entry-label",
        ".trend-definition",
        ".trend-tooltip",
    ):
        assert selector in stylesheet
    assert ".trend-line.east" in stylesheet
    assert ".trend-bar.east" in stylesheet
    assert "@media(max-width:760px)" in stylesheet


def test_vacant_house_tab_lazy_loads_exact_internal_map_without_embedding_source() -> None:
    html = _asset("index.html")
    script = _asset("app.js")
    document = json.loads(_asset("data.json"))

    assert (
        'data-vacant-map-src="vacant-map/index.html?v=20260830-map-layout-v67"'
        in html
    )
    assert '<iframe src="vacant-map/index.html"' not in html
    assert 'target === "vacant"' in script
    assert "연속 필지군" in html
    assert "A형 연속필지 거점개발 후보 4곳" in html
    assert "B형 자치구별 단독개발·숙박전환 예비후보 19곳" in html
    assert "총 23곳" in html
    assert "주소·지번 분석" in html
    assert "vacantHouses" not in document
    assert "parcel" not in json.dumps(document).lower()


def test_vacant_house_tab_exposes_workation_candidate_programme_table() -> None:
    html = _asset("index.html")
    parser = _WorkationCandidateTableParser()

    parser.feed(html)

    assert parser.rows == [
        [
            "1",
            "사상구 모라동",
            "대규모 연속 필지",
            "숙박·업무·교류 기능을 결합한 복합형 워케이션 거점 우선 검토",
        ],
        [
            "2",
            "강서구 명지동·동선동·죽동동",
            "일정 규모 이상의 단독필지",
            "필지별 독립형 워케이션 숙박·업무시설 개발 검토",
        ],
        [
            "3",
            "사하구 괴정동·사상구 감전동",
            "다수의 연속 필지",
            "연속 필지를 활용한 통합개발 또는 소규모 워케이션 거점 조성 검토",
        ],
        [
            "4",
            "강서구 죽림동·녹산동",
            "기존 건축물이 있는 중소규모 필지",
            "기존 건축물의 리모델링·전환·재생을 통한 워케이션 시설 조성 검토",
        ],
        [
            "5",
            "사하구 장림동",
            "개발면적이 협소한 연속 필지",
            "인접 빈집·유휴부지 추가 확보를 전제로 조건부 추진",
        ],
    ]
    assert "빈집 활용 워케이션 개발사업 후보지역 및 검토방향" in html
    assert "투자검토 우선지역 선별 결과이며 사업대상지 확정이 아닙니다" in html


def test_vacant_house_tab_distinguishes_contiguous_and_standalone_candidates() -> None:
    html = _asset("index.html")

    assert "A형 연속필지 거점개발 후보 4곳" in html
    assert "B형 자치구별 단독개발·숙박전환 예비후보 19곳" in html
    assert "총 23곳" in html
    assert "빈집 개발 후보지 10개" not in html
