from __future__ import annotations

import json

from westbusan.spatial.map import (
    PublicSpatialData,
    build_layer_candidate_rankings,
    build_policy_candidate_rankings,
    render_map,
)


def _map_data() -> PublicSpatialData:
    grids = {
        "type": "FeatureCollection",
        "crs": "EPSG:4326",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[128.9, 35.0], [129.0, 35.0], [129.0, 35.1], [128.9, 35.0]]
                    ],
                },
                "properties": {
                    "grid_id": "g-1",
                    "district_name": "사하구",
                    "primary_dong_name": "하단동",
                    "period": "2026-08",
                    "physical_facility_count": 3,
                    "mapped_facility_count": 3,
                    "room_sum": 30.0,
                    "room_coverage": 1.0,
                    "age_sample_size": 3,
                    "age_20y_facility_count": 2,
                    "coordinate_coverage": 0.8,
                    "facility_density": 12.0,
                    "room_density": 80.0,
                    "aged_facility_share": 0.3,
                    "tourism_supply_gap": 85.0,
                    "demand_context_score": 100.0,
                    "room_supply_score": 15.0,
                    "recommendation_kind": "remodel",
                    "small_scale_rating": "high",
                    "aged_building_rating": "medium",
                    "district_context_rating": "high",
                    "composite_score": 5.0,
                    "composite_grade": "priority_1",
                },
            }
        ],
    }
    facilities = {
        "type": "FeatureCollection",
        "crs": "EPSG:4326",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [128.95, 35.05]},
                "properties": {
                    "facility_key": "facility-000001",
                    "grid_id": "g-1",
                    "public_name": "공개 숙소",
                    "public_address": "부산 공개로 1",
                    "district_name": "사하구",
                    "primary_dong_name": "하단동",
                    "period": "2026-08",
                    "room_count": 10.0,
                    "use_approval_age_years": 31.0,
                    "small_scale_rating": "high",
                    "aged_building_rating": "high",
                    "district_context_rating": "medium",
                    "composite_score": 5.0,
                    "composite_grade": "priority_1",
                    "source_dates": ["2026-08-01"],
                },
            }
        ],
    }
    return PublicSpatialData(
        grid_geojson=grids,
        facility_geojson=facilities,
        evidence=(
            {
                "subject_type": "grid",
                "public_subject_key": "g-1",
                "period": "2026-08",
                "metric_name": "coordinate_coverage",
                "source_identity": "inventory.full_snapshot_membership",
                "source_period": "2026-08",
                "numerator": 8.0,
                "denominator": 10.0,
                "coverage": 0.8,
                "quality_band": "good",
                "evidence_json": '{"stock_observed":true}',
            },
        ),
        metadata={
            "business_date": "2026-08-17",
            "boundary_source_organization": "국토교통부",
            "boundary_version": "2026-08-official",
            "policy_version": "policy-v1",
            "district_policy_priorities": [
                {"rank": 1, "name": "강서구", "priority": "신규 공급·외국인 수용"},
                {"rank": 2, "name": "사하구", "priority": "노후시설 개선·체류상품"},
                {"rank": 3, "name": "북구", "priority": "교통수요 연계·품질개선"},
                {"rank": 4, "name": "사상구", "priority": "리모델링·관광상품화"},
            ],
        },
    )


def test_map_uses_vworld_basemap_and_policy_opportunity_layers() -> None:
    """Catches a blank schematic replacing the required real-world spatial context."""
    rendered = render_map(_map_data())

    assert 'id="filters-panel"' in rendered
    assert 'id="map-panel"' in rendered
    assert 'class="two-panel"' in rendered
    assert 'id="evidence-panel"' not in rendered
    assert 'id="selected-evidence"' not in rendered
    assert 'id="slippy-map"' in rendered
    assert 'id="spatial-map"' in rendered
    assert "부산 관광 숙박 투자기회 지도" in rendered
    assert 'id="vworld-tile-layer"' in rendered
    assert 'data-tile-template="/tourism/api/vworld/tiles/{z}/{x}/{y}.png"' in rendered
    assert 'data-max-zoom="19"' in rendered
    assert 'id="vworld-basemap"' not in rendered
    assert '/tourism/api/vworld/base.png' not in rendered
    assert "VWORLD_API_KEY" not in rendered
    assert 'data-layer="tourism_supply_gap"' in rendered
    assert 'data-layer="facility_density"' in rendered
    assert 'data-layer="aged_facilities"' in rendered
    assert 'data-layer="facility_locations"' in rendered
    assert "<link" not in rendered
    assert "<script src=" not in rendered
    assert "<path" in rendered
    assert "<circle" in rendered


def test_policy_overlays_share_the_geographic_vworld_tile_viewport() -> None:
    """Catches tile panning or zooming without moving the policy overlays."""
    rendered = render_map(_map_data())

    assert 'data-map-center="129.075,35.18"' in rendered
    assert 'data-map-zoom="10"' in rendered
    assert 'data-min-zoom="7"' in rendered
    assert 'data-max-zoom="19"' in rendered
    assert "function renderTiles" in rendered
    assert "function renderMap" in rendered
    assert "function fitGeographicBounds" in rendered
    assert 'd="M0.000,700.000' not in rendered


def test_map_has_filters_interactions_keyboard_labels_and_policy_decisions() -> None:
    """Catches an inaccessible colour-only map without decision-oriented layers."""
    rendered = render_map(_map_data())

    for marker in (
        'id="district-filter"',
        'id="dong-filter"',
        'id="period-filter"',
        'id="layer-controls"',
        'id="visible-grid-count"',
        'id="visible-facility-count"',
        'id="region-summary"',
        'id="region-facility-count"',
        'id="region-aged-count"',
        'id="region-room-count"',
        'id="region-gap-score"',
        'aria-label="지도 확대"',
        'aria-label="지도 축소"',
        'tabindex="0"',
        "wheel",
        "사하구",
        "관광수요 대비 숙박공급 부족",
        "숙박시설 밀집도",
        "노후 숙박시설 밀집도",
        "숙박시설 위치",
        "리모델링·관광숙박 전환 검토",
        "안전·위생·법적 적합성 평가가 아닙니다",
        "읍면동",
        "경계 출처 국토교통부",
        "지도 필터",
        "분석 레이어",
    ):
        assert marker in rendered


def test_dong_and_period_filters_have_matching_facility_attributes() -> None:
    """Catches facilities disappearing when their pinned dong or period is selected."""
    rendered = render_map(_map_data())
    circle = rendered.split("<circle", 1)[1].split("/>", 1)[0]

    assert 'data-dong="하단동"' in circle
    assert 'data-period="2026-08"' in circle
    assert 'id="period-filter"' in rendered


def test_supply_gap_is_applied_without_selecting_a_filter() -> None:
    """Catches policy direction being hidden behind filter interaction."""
    rendered = render_map(_map_data())

    assert 'data-district="사하구"' in rendered
    assert 'data-tourism-supply-gap="85.0"' in rendered
    assert 'data-mapped-facility-count="3"' in rendered
    assert 'data-aged-count="2"' in rendered
    assert 'data-age-known="3"' in rendered
    assert 'data-room-count="30.0"' in rendered
    assert 'data-demand-score="100.0"' in rendered
    assert 'data-supply-score="15.0"' in rendered
    assert 'data-recommendation="remodel"' in rendered
    assert 'class="layer-button is-active" data-layer="policy_priority"' in rendered
    assert "수요 대비 공급부족 85.0" in rendered
    assert "주소확인 시설 3.0개" in rendered
    assert "20년 이상 시설 2.0개 / 연수 확인 3.0개" in rendered
    assert "객실 10.0" in rendered
    assert 'r="3"' in rendered
    assert "숙박투자 v1" in rendered


def test_layer_controls_change_grid_encoding() -> None:
    """Catches visible layer buttons that do not update the mapped metric."""
    rendered = render_map(_map_data())

    assert 'data-layer="facility_density"' in rendered
    assert "activeLayer" in rendered
    assert "node.dataset.tourismSupplyGap" in rendered
    assert "node.dataset.mappedFacilityCount" in rendered
    assert "node.dataset.agedCount" in rendered
    assert 'raw === undefined || raw === ""' in rendered


def test_default_map_uses_policy_areas_without_marker_clutter() -> None:
    """Catches bubbles or facilities obscuring the policy and metric surfaces."""
    rendered = render_map(_map_data())

    assert "색상으로 지역 현황을 먼저 확인" in rendered
    assert 'data-layer="policy_priority"' in rendered
    assert 'class="facility-cluster"' in rendered
    assert "숙박시설 위치 레이어" in rendered
    assert "activeLayer = \"policy_priority\"" in rendered
    assert "detail-mode" in rendered
    assert ".grid-feature { stroke: none;" in rendered
    assert 'document.body.classList.toggle("facility-layer"' in rendered
    assert ".facility-layer .facility-cluster:not(.is-filtered)" in rendered
    assert ".facility-layer.detail-mode .facility-feature:not(.is-filtered)" in rendered


def test_clicking_a_region_populates_a_policy_readout_and_filters_are_dependent() -> None:
    """Catches filters and coloured areas that do not explain the selected region."""
    rendered = render_map(_map_data())

    assert 'id="region-summary-title"' in rendered
    assert "선택 지역 상세" in rendered
    assert "숙박시설 수" in rendered
    assert "20년 이상 시설" in rendered
    assert "공급부족도" in rendered
    assert "function selectRegion" in rendered
    assert 'node.addEventListener("click"' in rendered
    assert "function refreshDongOptions" in rendered
    assert "filters.district.addEventListener" in rendered
    assert "function focusSelection" in rendered
    assert "data-map-bounds" in rendered
    assert "data-geo-bounds" in rendered
    assert "MAX_ZOOM = 19" in rendered


def test_selected_region_offers_safe_regional_ai_interpretation() -> None:
    """Catches the empty map footer and unsafe free-form location AI requests."""
    rendered = render_map(_map_data())

    assert 'id="region-ai-button"' in rendered
    assert 'id="region-ai-result"' in rendered
    assert "AI 권역 해석" in rendered
    assert 'fetch("/tourism/data.json"' in rendered
    assert 'fetch("/tourism/api/insights"' in rendered
    assert 'period: "latest"' in rendered
    assert "selection: selectionContext" in rendered
    assert "function requestRegionInsight" in rendered
    assert "policy_options" in rendered
    assert "정책 아이디어" in rendered


def test_metric_layers_use_counts_and_explain_the_supply_gap_formula() -> None:
    """Catches sliver-area density and opaque demand/supply scores returning to the UI."""
    rendered = render_map(_map_data())

    assert "500m 안의 주소 확인 숙박시설 수" in rendered
    assert "20년 이상 숙박시설 수" in rendered
    assert "방문수요 점수 − 객실공급 점수" in rendered
    assert "facilityDensity" not in rendered


def test_policy_layer_ranks_dong_and_500m_candidates_not_whole_districts() -> None:
    """Catches district-wide colours being presented as investment locations."""
    rendered = render_map(_map_data())

    assert 'id="candidate-rank-list"' in rendered
    assert 'id="candidate-markers"' in rendered
    assert "읍면동·500m 후보지역" in rendered
    assert "policyColour(node.dataset.recommendation)" in rendered
    assert "buildCandidateRanking" in rendered
    assert "후보지역 선별 신호" in rendered
    assert "gridKey: node.dataset.key" in rendered
    assert "selectCandidate(item)" in rendered
    assert "renderGridSummary(item.node)" in rendered
    assert "requestRegionInsight(item.node)" in rendered
    assert "function updateVisibleCounts" in rendered
    assert "selectedGridNode && visible(selectedGridNode)" in rendered


def test_default_candidates_cover_west_busan_and_do_not_repeat_one_dong() -> None:
    """Catches adjacent cells from one dong occupying the whole regional ranking."""
    features: list[dict[str, object]] = []

    def add(
        grid_id: str,
        district: str,
        dong: str,
        kind: str | None,
        gap: float,
        aged: int,
        facilities: int,
    ) -> None:
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "grid_id": grid_id,
                    "district_name": district,
                    "primary_dong_name": dong,
                    "period": "2026-08",
                    "recommendation_kind": kind,
                    "tourism_supply_gap": gap,
                    "age_20y_facility_count": aged,
                    "mapped_facility_count": facilities,
                },
            }
        )

    add("saha-jangnim-1", "사하구", "장림동", "new_supply", 90, 3, 4)
    add("saha-jangnim-2", "사하구", "장림동", "new_supply", 89, 2, 3)
    add("saha-jangnim-3", "사하구", "장림동", "new_supply", 88, 1, 2)
    add("saha-hadan", "사하구", "하단동", "new_supply", 70, 1, 2)
    add("gangseo-myeongji", "강서구", "명지동", "remodel", 60, 4, 5)
    add("sasang-gwaebeop", "사상구", "괘법동", "investment_caution", 40, 7, 8)
    add("buk-gu-gupo", "북구", "구포동", None, 35, 5, 6)

    rankings = build_policy_candidate_rankings(features)
    default = rankings["default"]

    assert list(default.values()) == [1, 2, 3, 4, 5]
    selected = [
        next(
            feature["properties"]
            for feature in features
            if feature["properties"]["grid_id"] == grid_id  # type: ignore[index]
        )
        for grid_id in default
    ]
    assert {item["district_name"] for item in selected} == {
        "강서구",
        "사하구",
        "사상구",
        "북구",
    }
    assert len(
        {
            (item["district_name"], item["primary_dong_name"])
            for item in selected
        }
    ) == 5
    assert sum(item["primary_dong_name"] == "장림동" for item in selected) == 1
    assert set(rankings["district"]["사하구"]) == {
        "saha-jangnim-1",
        "saha-hadan",
    }


def test_each_decision_layer_selects_four_district_winners_plus_one_extra() -> None:
    """Catches every layer reusing the policy score or one district taking all ranks."""
    features: list[dict[str, object]] = []

    def add(
        grid_id: str,
        district: str,
        dong: str,
        *,
        gap: float,
        facilities: int,
        aged: int,
    ) -> None:
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "grid_id": grid_id,
                    "district_name": district,
                    "primary_dong_name": dong,
                    "period": "2026-08",
                    "recommendation_kind": "remodel",
                    "tourism_supply_gap": gap,
                    "demand_context_score": gap + 10,
                    "room_supply_score": 10,
                    "age_20y_facility_count": aged,
                    "age_sample_size": max(aged, 1),
                    "mapped_facility_count": facilities,
                },
            }
        )

    add("gangseo-a", "강서구", "명지동", gap=80, facilities=2, aged=1)
    add("gangseo-b", "강서구", "대저동", gap=60, facilities=9, aged=7)
    add("saha-a", "사하구", "하단동", gap=75, facilities=8, aged=5)
    add("saha-b", "사하구", "장림동", gap=70, facilities=10, aged=8)
    add("buk-a", "북구", "구포동", gap=65, facilities=11, aged=4)
    add("sasang-a", "사상구", "괘법동", gap=55, facilities=12, aged=6)

    rankings = {
        layer: build_layer_candidate_rankings(features, layer=layer)["default"]
        for layer in (
            "policy_priority",
            "tourism_supply_gap",
            "facility_density",
            "aged_facilities",
        )
    }

    for default in rankings.values():
        assert len(default) == 5
        selected_districts = {
            next(
                feature["properties"]["district_name"]  # type: ignore[index]
                for feature in features
                if feature["properties"]["grid_id"] == grid_id  # type: ignore[index]
            )
            for grid_id in default
        }
        assert selected_districts == {"강서구", "사하구", "북구", "사상구"}

    assert next(iter(rankings["tourism_supply_gap"])) == "gangseo-a"
    assert next(iter(rankings["facility_density"])) == "sasang-a"
    assert next(iter(rankings["aged_facilities"])) == "saha-b"


def test_facility_location_click_exposes_public_address_rooms_and_age() -> None:
    """Catches exact public facility details remaining inaccessible after zoom."""
    rendered = render_map(_map_data())

    assert 'data-public-name="공개 숙소"' in rendered
    assert 'data-public-address="부산 공개로 1"' in rendered
    assert 'data-room-count="10.0"' in rendered
    assert 'data-building-age="31.0"' in rendered
    assert "function renderFacilitySummary" in rendered
    assert 'id="region-metric-1-label"' in rendered
    assert 'id="region-metric-2-label"' in rendered
    assert "개별 시설 상세" in rendered


def test_map_has_layer_specific_candidates_and_lot_investment_ai() -> None:
    """Catches a generic top-five list or a free-form address prompt without evidence."""
    rendered = render_map(_map_data())

    assert "서부산 숙박시설 정책 우선방향" in rendered
    assert 'id="lot-investment-form"' in rendered
    assert 'id="lot-address"' in rendered
    assert 'fetch("/tourism/api/vworld/geocode"' in rendered
    assert "function findGridForPoint" in rendered
    assert "입력 지번 주변 500m" in rendered
    assert "buildCandidateRanking" in rendered
    assert "candidate_rankings" in rendered
    assert "20년 이상 ${formatNumber(agedCount)}개 /" not in rendered


def test_overview_clusters_facilities_by_dong_before_showing_exact_points() -> None:
    """Catches 500m bubbles recreating the same clutter as individual facilities."""
    data = _map_data()
    second = json.loads(json.dumps(data.facility_geojson["features"][0]))
    second["properties"]["facility_key"] = "facility-000002"
    second["properties"]["grid_id"] = "g-2"
    second["geometry"]["coordinates"] = [128.96, 35.06]
    data.facility_geojson["features"].append(second)

    rendered = render_map(data)

    assert rendered.count('class="facility-cluster"') == 1
    assert ">2</text>" in rendered


def test_embedded_json_matches_supplied_public_data_and_escapes_markup() -> None:
    """Catches map data drifting from exports or embedded text breaking script safety."""
    data = _map_data()
    data.facility_geojson["features"][0]["properties"]["public_name"] = "</script><x>"

    rendered = render_map(data)
    payload_text = rendered.split(
        '<script id="bundle-data" type="application/json">', 1
    )[1].split("</script>", 1)[0]
    payload = json.loads(payload_text)

    assert payload["grids"] == data.grid_geojson
    assert payload["facilities"] == data.facility_geojson
    assert payload["evidence"] == list(data.evidence)
    assert "</script><x>" not in payload_text


def test_default_priorities_do_not_mutate_manifest_bound_metadata() -> None:
    """Catches renderer-only priority context changing the exported payload identity."""
    data = _map_data()
    data.metadata.pop("district_policy_priorities")

    rendered = render_map(data)
    payload_text = rendered.split(
        '<script id="bundle-data" type="application/json">', 1
    )[1].split("</script>", 1)[0]
    payload = json.loads(payload_text)

    assert payload["metadata"] == data.metadata
    assert "관광수요 대비 숙박공급 부족" in rendered
