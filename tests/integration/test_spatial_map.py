from __future__ import annotations

import json

from westbusan.spatial.map import PublicSpatialData, render_map


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
                    "coordinate_coverage": 0.8,
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
            "boundary_version": "2026-08-official",
            "policy_version": "policy-v1",
        },
    )


def test_map_is_standalone_three_panel_and_network_free() -> None:
    """Catches a sidecar/network dependency or loss of the required map layout."""
    rendered = render_map(_map_data())

    assert 'id="filters-panel"' in rendered
    assert 'id="map-panel"' in rendered
    assert 'id="evidence-panel"' in rendered
    assert 'id="spatial-map"' in rendered
    assert "Priority 1" in rendered
    assert "district context" in rendered
    assert "정책지원 우선도" in rendered
    assert "https://" not in rendered
    assert "http://" not in rendered
    assert "fetch(" not in rendered
    assert "<link" not in rendered
    assert "<script src=" not in rendered
    assert "<path" in rendered
    assert "<circle" in rendered


def test_map_has_filters_interactions_keyboard_labels_and_caveats() -> None:
    """Catches an inaccessible colour-only static map without evidence context."""
    rendered = render_map(_map_data())

    for marker in (
        'id="district-filter"',
        'id="dong-filter"',
        'id="component-filter"',
        'id="grade-filter"',
        'id="visible-grid-count"',
        'id="visible-facility-count"',
        'aria-label="지도 확대"',
        'aria-label="지도 축소"',
        'tabindex="0"',
        "keydown",
        "wheel",
        "small_sample",
        "insufficient_evidence",
        "numerator",
        "denominator",
        "coverage",
        "district context",
        "점유율이 아닙니다",
        "안전·위생·법적 적합성 평가가 아닙니다",
        "원천기관에 정정 요청",
    ):
        assert marker in rendered


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
