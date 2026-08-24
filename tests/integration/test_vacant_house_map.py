from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from uuid import UUID

from shapely import to_wkb
from shapely.geometry import box

from westbusan.vacant_house.map_export import (
    export_vacant_house_map_current,
    validate_vacant_house_map_bundle,
)

HUB_RUN_ID = UUID("11111111-1111-1111-1111-111111111111")
INVENTORY_RUN_ID = UUID("22222222-2222-2222-2222-222222222222")


class _Result:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _PublishedVacantMapConnection:
    def __init__(self) -> None:
        parcels = [
            box(128.9900, 35.2000, 128.9902, 35.2002),
            box(128.9902, 35.2000, 128.9904, 35.2002),
            box(128.9904, 35.2000, 128.9906, 35.2002),
            box(128.9910, 35.2000, 128.9912, 35.2002),
            box(128.9912, 35.2000, 128.9914, 35.2002),
            box(128.9914, 35.2000, 128.9916, 35.2002),
            box(128.9930, 35.2000, 128.9934, 35.2004),
        ]
        self.parcels = parcels
        self.hubs = (
            parcels[0].union(parcels[1]).union(parcels[2]),
            parcels[3].union(parcels[4]).union(parcels[5]),
        )

    def execute(
        self, query: str, parameters: list[object] | None = None
    ) -> _Result:
        del parameters
        if "from vacant_house_hub_publication_current" in query:
            return _Result(
                [(HUB_RUN_ID, INVENTORY_RUN_ID, date(2025, 2, 28), date(2026, 8, 22))]
            )
        if "from vacant_house_hub where" in query:
            return _Result(
                [
                    (
                        "hub-west-01",
                        1,
                        3,
                        540.5,
                        to_wkb(self.hubs[0]),
                        '["26320"]',
                        '["05000"]',
                        '{"selection":"contiguous_parcels"}',
                        '["parcel_count","contiguous"]',
                    ),
                    (
                        "hub-west-02",
                        2,
                        3,
                        480.5,
                        to_wkb(self.hubs[1]),
                        '["26320"]',
                        '["05000"]',
                        '{"selection":"contiguous_parcels"}',
                        '["parcel_count","contiguous"]',
                    ),
                ]
            )
        if "from spatial_publication_current" in query:
            return _Result(
                [
                    (
                        UUID("33333333-3333-3333-3333-333333333333"),
                        date(2026, 8, 22),
                        date(2026, 8, 22),
                    )
                ]
            )
        if "from fact_tourism_demand" in query:
            return _Result([("북구", 1000.0), ("사하구", 500.0)])
        if "from mart_facility_priority_current" in query:
            return _Result([("북구", 100.0), ("사하구", 100.0)])
        if "from vacant_house_cadastral_evidence" in query:
            return _Result(
                [
                    (
                        f"26320050001000{index:04d}",
                        "26320",
                        "05000",
                        to_wkb(geometry),
                        date(2026, 8, 22),
                    )
                    for index, geometry in enumerate(self.parcels, start=1)
                ]
            )
        if "from vacant_house_hub_member" in query:
            return _Result(
                [
                    ("hub-west-01", f"26320050001000{index:04d}", index, 1)
                    for index in range(1, 4)
                ]
                + [
                    ("hub-west-02", f"26320050001000{index:04d}", index - 3, 1)
                    for index in range(4, 7)
                ]
            )
        if "join vacant_house_revision as revision" in query:
            return _Result(
                [
                    (
                        f"26320050001000{index:04d}",
                        str(UUID(int=index)),
                        "북구",
                        "구포동",
                        f"부산광역시 북구 구포동 {index}-1",
                        f"부산광역시 북구 시험로 {index}",
                        "단독주택",
                        1980 + index,
                        2,
                        45.0 + index,
                        70.0 + index,
                    )
                    for index in range(1, 8)
                ]
            )
        raise AssertionError(query)


def test_vacant_map_bundle_is_live_deterministic_and_exact_at_street_zoom(
    tmp_path: Path,
) -> None:
    connection = _PublishedVacantMapConnection()

    first = export_vacant_house_map_current(connection, tmp_path / "first")
    second = export_vacant_house_map_current(connection, tmp_path / "second")

    assert {path.name for path in first.paths} == {
        "index.html",
        "vacant-map.css",
        "vacant-map.js",
        "hubs.geojson",
        "standalone-candidates.geojson",
        "bukgu-supplemental-candidates.geojson",
        "parcels.geojson",
        "vacant-houses.geojson",
        "summary.json",
        "manifest.json",
    }
    assert validate_vacant_house_map_bundle(first)
    assert validate_vacant_house_map_bundle(second)
    assert {
        path.name: path.read_bytes() for path in first.paths
    } == {path.name: path.read_bytes() for path in second.paths}

    hubs = json.loads(first.hubs.read_text(encoding="utf-8"))
    standalone = json.loads(
        first.standalone_candidates.read_text(encoding="utf-8")
    )
    bukgu_supplemental = json.loads(
        first.bukgu_supplemental_candidates.read_text(encoding="utf-8")
    )
    parcels = json.loads(first.parcels.read_text(encoding="utf-8"))
    houses = json.loads(first.houses.read_text(encoding="utf-8"))
    summary = json.loads(first.summary.read_text(encoding="utf-8"))
    html = first.index_html.read_text(encoding="utf-8")
    script = first.script.read_text(encoding="utf-8")

    assert len(hubs["features"]) == 2
    assert hubs["features"][0]["properties"]["candidate_rank"] == 1
    assert hubs["features"][1]["properties"]["candidate_rank"] == 2
    assert len(standalone["features"]) == 1
    assert len(bukgu_supplemental["features"]) == 1
    assert bukgu_supplemental["features"][0]["properties"]["candidate_class"] == (
        "bukgu_supplemental_preliminary"
    )
    assert standalone["features"][0]["properties"]["candidate_class"] == (
        "standalone_preliminary"
    )
    assert standalone["features"][0]["properties"][
        "minimum_area_square_metres"
    ] == 300.0
    assert standalone["features"][0]["properties"]["district_demand_score"] == 100.0
    assert standalone["features"][0]["properties"]["missing_context"] == [
        "nearby_attractions",
        "transport_access",
    ]
    assert len(parcels["features"]) == 7
    assert len(houses["features"]) == 7
    assert houses["features"][0]["properties"]["exact_address"].startswith(
        "부산광역시 북구"
    )
    assert summary["candidate_count"] == 2
    assert summary["standalone_candidate_count"] == 1
    assert summary["distinct_parcel_count"] == 7
    assert summary["exact_location_count"] == 7
    assert summary["context_availability"] == {
        "district_visitor_demand": "available",
        "nearby_attractions": "reviewed_place_proximity_available",
        "station_proximity": "available",
        "transport_flow": "not_published",
    }
    assert summary["district_house_counts"] == {
        "강서구": 0,
        "북구": 7,
        "사상구": 0,
        "사하구": 0,
    }
    assert summary["district_parcel_counts"] == {
        "강서구": 0,
        "북구": 7,
        "사상구": 0,
        "사하구": 0,
    }
    assert summary["district_candidate_counts"]["북구"] == {
        "contiguous_hubs": 2,
        "standalone_candidates": 1,
        "supplemental_candidates": 1,
    }
    assert summary["district_candidate_counts"]["강서구"] == {
        "contiguous_hubs": 0,
        "standalone_candidates": 0,
        "supplemental_candidates": 0,
    }
    assert summary["standalone_candidate_policy"] == {
        "candidate_label": "단독개발·숙박전환 예비후보",
        "district_quota": False,
        "housing_type": "단독주택",
        "maximum_candidates": 6,
        "minimum_area_square_metres": 300.0,
        "scope": "서부산 4개 구",
    }
    assert summary["schema_version"] == "vacant-map-v3"
    assert "/tourism/api/vworld/tiles/{z}/{x}/{y}.png" in html
    assert "정적 지도" not in html
    assert "data-min-zoom=\"7\"" in html
    assert "data-max-zoom=\"19\"" in html
    assert "L.tileLayer" in script
    assert 'map.on("zoomend"' in script
    assert "fitBounds" in script
    assert "street-detail-mode" in script
    assert "vacant/address-analysis" in script
    assert "exact_address" in script
    assert "연속필지 거점개발 후보" in html
    assert "단독개발·숙박전환 예비후보" in html
    assert "standalone-candidates.geojson" in script
    assert "bukgu-supplemental-candidates.geojson" in script
    assert 'const WEST_DISTRICTS = ["강서구", "북구", "사상구", "사하구"]' in script
    assert "빈집 전수현황" in html
    assert "개발후보" in html
    assert "연속필지 개발후보 0개" in script
    assert "현재 게시된 단독개발 상위후보 0개" in script
    assert "C${Number(feature.properties.preliminary_rank)}" in script
    assert "nearby_attractions" in script
    assert "자료 미결합" in script
    assert "A${Number(feature.properties.candidate_rank)}" in script
    assert "B${Number(feature.properties.preliminary_rank)}" in script


def test_vacant_map_manifest_detects_modified_exact_location_bytes(
    tmp_path: Path,
) -> None:
    bundle = export_vacant_house_map_current(
        _PublishedVacantMapConnection(), tmp_path / "bundle"
    )
    document = json.loads(bundle.houses.read_text(encoding="utf-8"))
    document["features"][0]["properties"]["exact_address"] = "변조된 주소"
    bundle.houses.write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )

    assert not validate_vacant_house_map_bundle(bundle)
